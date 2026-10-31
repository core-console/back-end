"""Correction isolation, exact Money, linked protection and legacy compatibility."""

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_advance_corrections import correct, correction, money
from integration.test_finance_api import finance_client
from integration.test_finance_credit_applications import retain_credit
from integration.test_finance_settlements import advance, advance_client

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


async def test_corrections_are_owner_and_ledger_isolated(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = correction(source)
    detail = await correct(client, actor, base, body)
    other = User(
        identity_issuer="https://identity.example.test/correction",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(other)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=other) as foreign:
        for path in [f"advance-corrections/{detail['id']}", f"advances/{source['id']}/corrections"]:
            assert (await foreign.get(f"{base}/{path}")).status_code == 404
        assert (
            await foreign.post(f"{base}/advance-corrections/preview", json=body)
        ).status_code == 404
        rejected = await foreign.post(
            f"{base}/advance-corrections",
            json={**body, "previewToken": detail["previewToken"]},
            headers=submission_headers(other.id),
        )
        assert rejected.status_code == 404
        assert "submissionReceipt" not in rejected.json()
        assert (
            await foreign.get(
                f"/api/finance/submissions/{uuid4()}", headers=submission_headers(other.id)
            )
        ).status_code == 404
    ledger = await client.post(
        "/api/finance/ledgers", json={"name": "Other ledger"}, headers=submission_headers(actor.id)
    )
    other_base = f"/api/finance/ledgers/{ledger.json()['outcome']['resource']['id']}"
    assert (
        await client.post(f"{other_base}/advance-corrections/preview", json=body)
    ).status_code == 404
    assert (await client.get(f"{other_base}/advance-corrections/{detail['id']}")).status_code == 404
    other_draft = {**draft, "fundingAccountId": draft["fundingAccountId"]}
    # A real managed Account in another Ledger must look exactly like a missing Account.
    other_account = await create_finance_resource(
        client,
        f"{other_base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Other cash",
            "nature": "asset",
            "currency": "CNY",
            "openingBalance": money("100.00"),
            "trackingStartDate": "2026-10-01",
        },
    )
    other_participant = (
        await client.post(f"{other_base}/participants", json={"name": "Other debtor"})
    ).json()
    other_draft.update(
        fundingAccountId=other_account.json()["id"], participantId=other_participant["id"]
    )
    other_source = await advance(client, actor, other_base, other_draft, "10.00", "2026-10-20")
    for funding_id in [str(uuid4()), other_source["obligation"]["receivableAccountId"]]:
        hidden = await client.post(
            f"{base}/advance-corrections/preview", json={**body, "fundingAccountId": funding_id}
        )
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["code"] == "finance_account_not_found"


async def test_corrected_managed_positions_and_effects_reject_manual_bypasses(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    detail = await correct(client, actor, base, correction(source))
    for tx in detail["financialEffectsAfter"]:
        deleted = await client.delete(f"{base}/transactions/{tx['transactionId']}")
        assert deleted.status_code == 409, deleted.text
        assert deleted.json()["code"] == "finance_linked_record_protected"
        replaced = await client.put(
            f"{base}/transactions/{tx['transactionId']}",
            json={
                "kind": "expense",
                "accountId": draft["fundingAccountId"],
                "transactionDate": "2026-10-20",
                "economicAmount": money("1.00"),
                "categoryAllocations": [{"amount": money("1.00"), "categoryId": None}],
            },
        )
        assert replaced.status_code == 409, replaced.text
    account = detail["after"]["obligations"][0]["receivableAccountId"]
    for body in [{"name": "Manual receivable edit"}, {"openingBalance": money("1.00")}]:
        edited = await client.patch(f"{base}/accounts/{account}", json=body)
        assert edited.status_code == 409, edited.text
    manual = await client.post(
        f"{base}/transactions",
        json={
            "kind": "income",
            "accountId": account,
            "transactionDate": "2026-10-20",
            "economicAmount": money("1.00"),
            "categoryAllocations": [{"amount": money("1.00"), "categoryId": None}],
        },
        headers=submission_headers(actor.id),
    )
    assert manual.status_code == 409, manual.text
    adjusted = await client.post(
        f"{base}/balance-adjustments",
        json={
            "accountId": account,
            "transactionDate": "2026-10-20",
            "expectedDerivedBalance": money("80.00"),
            "expectedAccountNature": "asset",
            "targetBalance": money("0.00"),
        },
        headers=submission_headers(actor.id),
    )
    assert adjusted.status_code == 409, adjusted.text
    assert (await client.get(f"{base}/advances/{source['id']}")).json() == detail["after"]
    # Unrelated manual funding-account bookkeeping retains its Finance v1 behavior.
    ordinary = await client.post(
        f"{base}/transactions",
        json={
            "kind": "income",
            "accountId": draft["fundingAccountId"],
            "transactionDate": "2026-10-20",
            "economicAmount": money("1.00"),
            "categoryAllocations": [{"amount": money("1.00"), "categoryId": None}],
        },
        headers=submission_headers(actor.id),
    )
    assert ordinary.status_code == 201, ordinary.text
    assert (
        await client.delete(f"{base}/transactions/{ordinary.json()['outcome']['resource']['id']}")
    ).status_code == 204


@pytest.mark.parametrize(
    "currency,amount,owner,share",
    [
        ("JPY", "120", "20", "100"),
        ("USD", "120.01", "20.00", "100.01"),
        ("CNY", "1000000000000000000000000000000.01", "20.00", "999999999999999999999999999980.01"),
    ],
)
async def test_exact_money_currency_and_non_bmp_text_survive_confirmation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    currency: str,
    amount: str,
    owner: str,
    share: str,
) -> None:
    client, actor, base, draft = advance_client
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": currency,
            "nature": "liability",
            "currency": currency,
            "openingBalance": {
                "amount": "0" if currency == "JPY" else "0.00",
                "currency": currency,
            },
            "trackingStartDate": "2026-10-01",
        },
    )
    original = {
        "fundingAccountId": account.json()["id"],
        "transactionDate": "2026-10-20",
        "amount": {"amount": amount, "currency": currency},
        "ownerShare": {"amount": owner, "currency": currency},
        "participantShares": [
            {
                "participantId": draft["participantId"],
                "amount": {"amount": share, "currency": currency},
            }
        ],
    }
    plan = (await client.post(f"{base}/advances/preview", json=original)).json()
    receipt = await client.post(
        f"{base}/advances",
        json={**original, "previewToken": plan["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
    )
    assert receipt.status_code == 201, receipt.text
    body = {
        **original,
        "advanceId": receipt.json()["outcome"]["resource"]["id"],
        "reason": "  " + "😀" * 500 + "  ",
        "note": "😀" * 500,
        "transactionDate": "2026-11-20",
    }
    detail = await correct(client, actor, base, body)
    assert detail["reason"] == "😀" * 500
    assert detail["after"]["amount"] == {"amount": amount, "currency": currency}
    assert detail["after"]["obligations"][0]["remaining"] == {"amount": share, "currency": currency}
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[account.json()["id"]]["currentBalance"] == {
        "amount": amount,
        "currency": currency,
    }
    invalid = await client.post(
        f"{base}/advance-corrections/preview", json={**body, "reason": "😀" * 501}
    )
    assert invalid.status_code == 422


async def test_correction_never_consumes_unrelated_available_credit_or_replays_settlements(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "50.00")
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    before = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    result = await correct(client, actor, base, correction(source, amount="120.00", share="100.00"))
    assert result["after"]["obligations"][0]["remaining"] == money("100.00")
    assert result["after"]["obligations"][0]["creditApplied"] == money("0.00")
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/credits")
    ).json() == before
    assert (await client.get(f"{base}/settlements/{origin['id']}")).json() == origin
