"""Ownership, eligibility, explicit confirmation and linked consistency for T10."""

from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_credit_applications import application, apply_credit, retain_credit
from integration.test_finance_credits import confirm_credit
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "currency,credit,principal,remainder",
    [
        ("JPY", "2", "10", "8"),
        ("USD", "2.00", "10.00", "8.00"),
        ("CNY", "123456789012345678901234567890.01", "123456789012345678901234567890.02", "0.01"),
    ],
)
async def test_exact_currency_and_future_application_preserve_cash_and_financial_position(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    currency: str,
    credit: str,
    principal: str,
    remainder: str,
) -> None:
    client, actor, base, draft = advance_client
    zero = "0" if currency == "JPY" else "0.00"
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Card",
            "nature": "liability",
            "currency": currency,
            "openingBalance": {"amount": zero, "currency": currency},
            "trackingStartDate": "2026-10-01",
        },
    )
    account_id = account.json()["id"]
    await confirm_credit(
        client,
        actor,
        base,
        {
            **repayment(draft, credit),
            "receivingAccountId": account_id,
            "amount": {"amount": credit, "currency": currency},
            "excessDisposition": "retainCredit",
        },
    )
    debt_body = {
        "fundingAccountId": account_id,
        "transactionDate": "2026-11-01",
        "amount": {"amount": principal, "currency": currency},
        "participantShares": [
            {
                "participantId": draft["participantId"],
                "amount": {"amount": principal, "currency": currency},
            }
        ],
        "ownerShare": {"amount": zero, "currency": currency},
    }
    preview = await client.post(f"{base}/advances/preview", json=debt_body)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    receipt = await client.post(
        f"{base}/advances",
        json={**debt_body, "previewToken": plan["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
    )
    assert receipt.status_code == 201, receipt.text
    debt = (
        await client.get(f"{base}/advances/{receipt.json()['outcome']['resource']['id']}")
    ).json()
    before = next(a for a in (await client.get(f"{base}/accounts")).json() if a["id"] == account_id)
    body = {
        **application(draft, [debt["obligations"][0]["id"]], credit),
        "transactionDate": "2026-11-02",
        "amount": {"amount": credit, "currency": currency},
    }
    await apply_credit(client, actor, base, body)
    after = (await client.get(f"{base}/advances/{debt['id']}")).json()["obligations"][0]
    assert after["remaining"] == {"amount": remainder, "currency": currency}
    assert after["creditApplied"] == {"amount": credit, "currency": currency}
    assert (
        next(a for a in (await client.get(f"{base}/accounts")).json() if a["id"] == account_id)
        == before
    )
    overview = (await client.get(f"{base}/overview", params={"month": "2026-11"})).json()
    position = next(p for p in overview["financialPositionByCurrency"] if p["currency"] == currency)
    assert position["assetTotal"]["amount"] == ("100.01" if currency == "CNY" else remainder)
    assert position["liabilityTotal"]["amount"] == remainder
    summary = next(s for s in overview["monthSummaryByCurrency"] if s["currency"] == currency)
    assert summary["income"]["amount"] == summary["expense"]["amount"] == zero


@pytest.mark.parametrize(
    "invalid",
    [
        "zero",
        "negative",
        "precision",
        "number",
        "credit_excess",
        "debt_excess",
        "currency",
        "empty",
        "duplicate",
        "unknown",
        "other_participant",
        "future_debt",
        "before_origin",
        "cash_account",
        "income",
        "forgiveness",
        "return",
        "v1",
        "v2",
        "v3",
        "v4",
        "v5",
        "v6",
    ],
)
async def test_invalid_application_has_no_effect_and_retains_correlated_evidence(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00", "2026-10-25")
    debt = await advance(client, actor, base, draft, "100.00", "2026-10-21")
    body = application(draft, [debt["obligation"]["id"]], "25.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    if invalid in ("zero", "negative", "precision", "number", "credit_excess", "debt_excess"):
        body["amount"]["amount"] = {
            "zero": "0.00",
            "negative": "-1.00",
            "precision": "1.001",
            "number": 1,
            "credit_excess": "50.01",
            "debt_excess": "100.01",
        }[invalid]
    elif invalid == "currency":
        body["amount"]["currency"] = "USD"
    elif invalid in ("empty", "duplicate", "unknown"):
        body["selectedObligationIds"] = {
            "empty": [],
            "duplicate": [debt["obligation"]["id"]] * 2,
            "unknown": [str(uuid4())],
        }[invalid]
    elif invalid == "other_participant":
        other = (await client.post(f"{base}/participants", json={"name": "Other"})).json()
        foreign = await advance(
            client, actor, base, {**draft, "participantId": other["id"]}, "10.00", "2026-10-20"
        )
        body["selectedObligationIds"] = [foreign["obligation"]["id"]]
    elif invalid in ("future_debt", "before_origin"):
        body["transactionDate"] = "2026-10-20" if invalid == "future_debt" else "2026-10-24"
    elif invalid == "cash_account":
        body["receivingAccountId"] = draft["fundingAccountId"]
    elif invalid == "income":
        body["excessDisposition"] = "acceptIncome"
    elif invalid == "forgiveness":
        body["reason"] = "not an application field"
    elif invalid == "return":
        body["kind"] = "creditReturn"
    paths = [
        "accounts",
        "transactions",
        f"participants/{draft['participantId']}/obligations",
        f"participants/{draft['participantId']}/credits",
        f"participants/{draft['participantId']}/settlements",
    ]
    before = [(await client.get(f"{base}/{p}")).json() for p in paths]
    if not invalid.startswith("v"):
        assert (await client.post(f"{base}/settlements/preview", json=body)).status_code == 422
    headers = {
        **submission_headers(actor.id),
        "Finance-Command-Version": invalid[1:] if invalid.startswith("v") else "7",
    }
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers=headers,
    )
    assert result.status_code in (409, 422), result.text
    if result.status_code == 422 and "commandValidationRejection" in result.json():
        assert (
            result.json()["commandValidationRejection"]["commandVersion"]
            == headers["Finance-Command-Version"]
        )
    assert [(await client.get(f"{base}/{p}")).json() for p in paths] == before


@pytest.mark.parametrize(
    "change", ["credit", "cash", "amount", "selection", "date", "note", "archive"]
)
async def test_changed_intent_or_financial_state_requires_new_preview_and_new_identity(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    change: str,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00")
    debt = await advance(client, actor, base, draft, "100.00", "2026-10-21")
    other = await advance(client, actor, base, draft, "100.00", "2026-10-22")
    body = application(draft, [debt["obligation"]["id"]], "25.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    if change == "credit":
        await apply_credit(
            client, actor, base, application(draft, [other["obligation"]["id"]], "5.00")
        )
    elif change == "cash":
        await confirm(client, actor, base, repayment(draft, "10.00"))
    elif change == "amount":
        body["amount"]["amount"] = "20.00"
    elif change == "selection":
        body["selectedObligationIds"] = [other["obligation"]["id"]]
    elif change == "date":
        body["transactionDate"] = "2026-10-28"
    elif change == "note":
        body["note"] = "changed intent"
    else:
        await client.post(f"{base}/participants/{draft['participantId']}/archive")
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "7"}
    command = {**body, "previewToken": preview["previewToken"]}
    rejected = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert (
        rejected.status_code == 409
        and rejected.json()["code"] == "finance_settlement_preview_stale"
    )
    assert rejected.json()["submissionReceipt"]["commandVersion"] == "7"
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == rejected.json()
    fresh = (await client.post(f"{base}/settlements/preview", json=body)).json()
    conflict = await client.post(
        f"{base}/settlements", json={**body, "previewToken": fresh["previewToken"]}, headers=headers
    )
    assert (
        conflict.status_code == 409
        and conflict.json()["code"] == "finance_submission_content_conflict"
    )
    await apply_credit(client, actor, base, body)


async def test_applications_and_recovery_are_private_to_owner_and_ledger(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00")
    debt = await advance(client, actor, base, draft, "100.00", "2026-10-21")
    body = application(draft, [debt["obligation"]["id"]], "25.00")
    detail = await apply_credit(client, actor, base, body)
    ledger = await client.post(
        "/api/finance/ledgers", headers=submission_headers(actor.id), json={"name": "Other"}
    )
    other_base = f"/api/finance/ledgers/{ledger.json()['outcome']['resource']['id']}"
    participant = (await client.post(f"{other_base}/participants", json={"name": "Other"})).json()
    invalid = await client.post(
        f"{other_base}/settlements/preview", json={**body, "participantId": participant["id"]}
    )
    assert invalid.status_code == 422
    assert (await client.get(f"{other_base}/settlements/{detail['id']}")).status_code == 404
    outsider = User(
        identity_issuer="https://identity.example.test/t10",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(outsider)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=outsider) as private:
        assert (await private.post(f"{base}/settlements/preview", json=body)).status_code == 404
        assert (await private.get(f"{base}/settlements/{detail['id']}")).status_code == 404


async def test_consumed_credit_receivable_and_application_transfers_stay_protected(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "50.00")
    debt = await advance(client, actor, base, draft, "100.00", "2026-10-21")
    detail = await apply_credit(
        client, actor, base, application(draft, [debt["obligation"]["id"]], "25.00")
    )
    money = {"amount": "1.00", "currency": "CNY"}
    for account_id, nature in [
        (origin["creditOrigin"]["liabilityAccountId"], "liability"),
        (debt["obligation"]["receivableAccountId"], "asset"),
    ]:
        transfer: dict[str, Any] = {
            "kind": "internalTransfer",
            "sourceAccountId": account_id,
            "destinationAccountId": draft["fundingAccountId"],
            "amount": money,
            "transactionDate": "2026-10-27",
        }
        for path, command in [
            ("transactions", transfer),
            (
                "balance-adjustments",
                {
                    "accountId": account_id,
                    "transactionDate": "2026-10-27",
                    "expectedAccountNature": nature,
                    "expectedDerivedBalance": {
                        "amount": "25.00" if nature == "liability" else "75.00",
                        "currency": "CNY",
                    },
                    "targetBalance": money,
                },
            ),
        ]:
            rejected = await client.post(
                f"{base}/{path}", json=command, headers=submission_headers(actor.id)
            )
            assert (
                rejected.status_code == 409
                and rejected.json()["code"] == "finance_linked_record_protected"
            )
        for method, path, mutation in [
            ("PATCH", f"accounts/{account_id}", {"openingBalance": money}),
            ("POST", f"accounts/{account_id}/archive", None),
            ("PATCH", f"accounts/{account_id}", {"trackingStartDate": "2026-10-01"}),
        ]:
            rejected = await client.request(method, f"{base}/{path}", json=mutation)
            assert (
                rejected.status_code == 409
                and rejected.json()["code"] == "finance_linked_record_protected"
            )
    for transaction_id in detail["transactionIds"] + [origin["creditOrigin"]["transactionId"]]:
        for method in ("PUT", "DELETE"):
            rejected = await client.request(
                method,
                f"{base}/transactions/{transaction_id}",
                json=transfer if method == "PUT" else None,
            )
            assert (
                rejected.status_code == 409
                and rejected.json()["code"] == "finance_linked_record_protected"
            )
    assert (await client.delete(f"{base}/settlements/{detail['id']}")).status_code == 405
    assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
