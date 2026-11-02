"""Receipt Correction isolation, exact Money, stale references and bypass protection."""

from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_credit_applications import application, apply_credit, retain_credit
from integration.test_finance_credit_returns import credit_return, return_credit
from integration.test_finance_settlement_corrections import correct, correction, money
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


async def test_receipt_corrections_hide_foreign_owners_and_ledgers(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    body = correction(source)
    detail = await correct(client, actor, base, body)
    other = User(
        identity_issuer="https://identity.example.test/t13",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(other)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=other) as foreign:
        for path in [
            f"settlement-corrections/{detail['id']}",
            f"settlements/{source['id']}/corrections",
        ]:
            assert (await foreign.get(f"{base}/{path}")).status_code == 404
        assert (
            await foreign.post(f"{base}/settlement-corrections/preview", json=body)
        ).status_code == 404
        rejected = await foreign.post(
            f"{base}/settlement-corrections",
            json={**body, "previewToken": detail["previewToken"]},
            headers=submission_headers(other.id),
        )
        assert rejected.status_code == 404
        assert "submissionReceipt" not in rejected.json()
    ledger = await client.post(
        "/api/finance/ledgers", json={"name": "Other"}, headers=submission_headers(actor.id)
    )
    other_base = f"/api/finance/ledgers/{ledger.json()['outcome']['resource']['id']}"
    assert (
        await client.post(f"{other_base}/settlement-corrections/preview", json=body)
    ).status_code == 404
    assert (
        await client.get(f"{other_base}/settlement-corrections/{detail['id']}")
    ).status_code == 404
    rejected = await client.post(
        f"{other_base}/settlement-corrections",
        json={**body, "previewToken": detail["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert rejected.status_code == 404
    assert rejected.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"


@pytest.mark.parametrize("activity", ["applied", "returned", "combined", "forgiveness"])
async def test_later_lifecycle_workflows_are_explicitly_ineligible_without_mutation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    activity: str,
) -> None:
    client, actor, base, draft = advance_client
    if activity in ("applied", "returned"):
        source = await retain_credit(client, actor, base, draft, "50.00")
        if activity == "applied":
            debt = await advance(client, actor, base, draft, "10.00", "2026-10-20")
            await apply_credit(
                client, actor, base, application(draft, [debt["obligation"]["id"]], "10.00")
            )
        else:
            await return_credit(client, actor, base, credit_return(draft, "10.00"))
        body = correction(source, "40.00", "retainCredit")
    else:
        debt = await advance(client, actor, base, draft, "100.00", "2026-10-20")
        action: dict[str, Any] = {
            "kind": activity,
            "participantId": draft["participantId"],
            "transactionDate": "2026-10-22",
            "amount": money("20.00"),
            "selectedObligationIds": [debt["obligation"]["id"]],
            "reason": "Forgive original debt",
        }
        if activity == "combined":
            action.update(
                receivingAccountId=draft["fundingAccountId"], forgivenessAmount=money("10.00")
            )
        preview = await client.post(f"{base}/settlements/preview", json=action)
        assert preview.status_code == 200, preview.text
        receipt = await client.post(
            f"{base}/settlements",
            json={**action, "previewToken": preview.json()["previewToken"]},
            headers={
                **submission_headers(actor.id),
                "Finance-Command-Version": "6" if activity == "combined" else "5",
            },
        )
        assert receipt.status_code == 201, receipt.text
        source = (
            await client.get(f"{base}/settlements/{receipt.json()['outcome']['resource']['id']}")
        ).json()
        body = correction(source, "10.00")
    before = [
        (await client.get(f"{base}/{path}")).json()
        for path in ["accounts", "transactions", f"settlements/{source['id']}"]
    ]
    response = await client.post(f"{base}/settlement-corrections/preview", json=body)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "finance_settlement_correction_ineligible"
    response = await client.post(
        f"{base}/settlement-corrections",
        json={**body, "previewToken": "0" * 64},
        headers=submission_headers(actor.id),
    )
    assert response.status_code == 409, response.text
    assert response.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    assert [
        (await client.get(f"{base}/{path}")).json()
        for path in ["accounts", "transactions", f"settlements/{source['id']}"]
    ] == before


async def test_stale_preview_retains_rejection_and_changed_intent_cannot_reuse_key(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    body = correction(source)
    plan = (await client.post(f"{base}/settlement-corrections/preview", json=body)).json()
    await confirm(client, actor, base, repayment(draft, "10.00"))
    headers = submission_headers(actor.id)
    command = {**body, "previewToken": plan["previewToken"]}
    rejected = await client.post(f"{base}/settlement-corrections", json=command, headers=headers)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "finance_settlement_correction_preview_stale"
    assert (
        await client.post(f"{base}/settlement-corrections", json=command, headers=headers)
    ).json() == rejected.json()
    fresh = (await client.post(f"{base}/settlement-corrections/preview", json=body)).json()
    changed = await client.post(
        f"{base}/settlement-corrections",
        json={**body, "previewToken": fresh["previewToken"]},
        headers=headers,
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "finance_submission_content_conflict"
    detail = await correct(client, actor, base, body)
    assert detail["obligationsAfter"][0]["remaining"] == money("20.00")


async def test_corrected_effects_and_managed_positions_reject_manual_bypasses(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    debt = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    detail = await correct(client, actor, base, correction(source, "110.00", "retainCredit"))
    for effect in detail["financialEffectsAfter"]:
        id = effect["transactionId"]
        response = await client.delete(f"{base}/transactions/{id}")
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "finance_linked_record_protected"
        response = await client.put(
            f"{base}/transactions/{id}",
            json={
                "kind": "internalTransfer",
                "sourceAccountId": draft["fundingAccountId"],
                "destinationAccountId": debt["obligation"]["receivableAccountId"],
                "transactionDate": "2026-10-23",
                "amount": money("1.00"),
            },
        )
        assert response.status_code == 409, response.text
    credits = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    for id, nature, balance in [
        (debt["obligation"]["receivableAccountId"], "asset", "0.00"),
        (credits[0]["liabilityAccountId"], "liability", "10.00"),
    ]:
        assert (
            await client.patch(f"{base}/accounts/{id}", json={"name": "Manual edit"})
        ).status_code == 409
        response = await client.post(
            f"{base}/transactions",
            json={
                "kind": "income",
                "accountId": id,
                "transactionDate": "2026-10-22",
                "economicAmount": money("1.00"),
                "categoryAllocations": [{"amount": money("1.00"), "categoryId": None}],
            },
            headers=submission_headers(actor.id),
        )
        assert response.status_code == 409, response.text
        response = await client.post(
            f"{base}/balance-adjustments",
            json={
                "accountId": id,
                "transactionDate": "2026-10-22",
                "expectedAccountNature": nature,
                "expectedDerivedBalance": money(balance),
                "targetBalance": money("1.00"),
            },
            headers=submission_headers(actor.id),
        )
        assert response.status_code == 409, response.text


@pytest.mark.parametrize("amount", ["0.00", "-1.00", "70.000", "NaN", "1e2", 70])
async def test_invalid_cash_amounts_are_definitively_not_admitted(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    amount: Any,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    body = {
        **correction(source),
        "amount": {"amount": amount, "currency": "CNY"},
        "previewToken": "0" * 64,
    }
    headers = submission_headers(actor.id)
    response = await client.post(f"{base}/settlement-corrections", json=body, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["commandValidationRejection"]["kind"] == "definitivelyNotAdmitted"
    lookup = await client.get(
        f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
    )
    assert lookup.status_code == 404


@pytest.mark.parametrize(
    "currency, amount, corrected, remainder",
    [
        ("JPY", "80", "70", "30"),
        ("USD", "80.01", "70.01", "29.99"),
        ("CNY", "123456789012345678901234567890.98", "123456789012345678901234567890.97", "0.03"),
    ],
)
async def test_currency_scale_and_large_exact_receipt_reconciliation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    currency: str,
    amount: str,
    corrected: str,
    remainder: str,
) -> None:
    client, actor, base, draft = advance_client
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Exact cash",
            "nature": "asset",
            "currency": currency,
            "openingBalance": money("0" if currency == "JPY" else "0.00", currency),
            "trackingStartDate": "2026-10-01",
        },
    )
    principal = (
        "123456789012345678901234567891.00"
        if len(amount) > 20
        else ("100" if currency == "JPY" else "100.00")
    )
    body = {
        **draft,
        "fundingAccountId": account.json()["id"],
        "amount": money(principal, currency),
        "participantShare": money(principal, currency),
        "ownerShare": money("0" if currency == "JPY" else "0.00", currency),
    }
    plan = (await client.post(f"{base}/advances/preview", json=body)).json()
    created = await client.post(
        f"{base}/advances",
        json={**body, "previewToken": plan["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert created.status_code == 201, created.text
    receipt = await confirm(
        client,
        actor,
        base,
        {
            **repayment(draft),
            "receivingAccountId": account.json()["id"],
            "amount": money(amount, currency),
        },
    )
    detail = await correct(
        client, actor, base, {**correction(receipt), "amount": money(corrected, currency)}
    )
    assert detail["obligationsAfter"][0]["remaining"] == money(remainder, currency)


async def test_category_ownership_eligibility_and_stale_classification_are_authoritative(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "100.00"))
    body = correction(source, "110.00", "acceptIncome")
    ledger = await client.post(
        "/api/finance/ledgers",
        json={"name": "Other category ledger"},
        headers=submission_headers(actor.id),
    )
    other_base = f"/api/finance/ledgers/{ledger.json()['outcome']['resource']['id']}"
    other = await create_finance_resource(
        client, f"{other_base}/categories", owner_id=actor.id, json={"name": "Foreign"}
    )
    for category_id in [str(uuid4()), other.json()["id"]]:
        rejected = await client.post(
            f"{base}/settlement-corrections/preview", json={**body, "categoryId": category_id}
        )
        assert rejected.status_code == 404, rejected.text
        assert rejected.json()["code"] == "finance_category_not_found"
    category = await create_finance_resource(
        client, f"{base}/categories", owner_id=actor.id, json={"name": "Surplus"}
    )
    body["categoryId"] = category.json()["id"]
    preview = (await client.post(f"{base}/settlement-corrections/preview", json=body)).json()
    archived = await client.post(f"{base}/categories/{category.json()['id']}/archive")
    assert archived.status_code == 200, archived.text
    rejected = await client.post(
        f"{base}/settlement-corrections",
        json={**body, "previewToken": preview["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "finance_settlement_correction_preview_stale"
    rejected = await client.post(f"{base}/settlement-corrections/preview", json=body)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "finance_category_archived"


@pytest.mark.parametrize(
    "change",
    [
        {"transactionDate": "2026-10-23"},
        {"receivingAccountId": str(uuid4())},
        {"participantId": str(uuid4())},
        {"selectedObligationIds": [str(uuid4())]},
        {"reason": "😀" * 501},
        {"amount": {"amount": "70.00", "currency": "USD"}},
    ],
)
async def test_correction_cannot_change_other_facts_or_currency(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    change: dict[str, Any],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    response = await client.post(
        f"{base}/settlement-corrections/preview", json={**correction(source), **change}
    )
    assert response.status_code == 422, response.text
    assert (await client.get(f"{base}/settlements/{source['id']}")).json() == source
