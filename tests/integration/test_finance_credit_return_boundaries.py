"""Return eligibility, ownership, exact financial effects and bypass protection."""

from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_credit_applications import retain_credit
from integration.test_finance_credit_returns import credit_return, return_credit
from integration.test_finance_credits import confirm_credit
from integration.test_finance_settlements import advance_client, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "invalid",
    [
        "zero",
        "negative",
        "precision",
        "number",
        "excess",
        "currency",
        "missing_account",
        "unknown_participant",
        "before_origin",
        "extra",
        "selection",
        "receipt",
        "reason",
        "managed_account",
        "v1",
        "v2",
        "v3",
        "v4",
        "v5",
        "v6",
        "v7",
    ],
)
async def test_invalid_return_has_no_partial_financial_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "50.00")
    body = credit_return(draft, "25.00")
    plan = (await client.post(f"{base}/settlements/preview", json=body)).json()
    if invalid in ("zero", "negative", "precision", "number", "excess"):
        body["amount"]["amount"] = {
            "zero": "0.00",
            "negative": "-1.00",
            "precision": "1.001",
            "number": 1,
            "excess": "51.00",
        }[invalid]
    elif invalid == "currency":
        body["amount"]["currency"] = "USD"
    elif invalid == "missing_account":
        body["payingAccountId"] = str(uuid4())
    elif invalid == "unknown_participant":
        body["participantId"] = str(uuid4())
    elif invalid == "before_origin":
        body["transactionDate"] = "2026-10-21"
    elif invalid == "managed_account":
        body["payingAccountId"] = origin["creditOrigin"]["liabilityAccountId"]
    elif invalid in ("extra", "selection", "receipt", "reason"):
        key, value = {
            "extra": ("excessDisposition", "acceptIncome"),
            "selection": ("selectedObligationIds", [str(uuid4())]),
            "receipt": ("receivingAccountId", draft["fundingAccountId"]),
            "reason": ("reason", "not forgiveness"),
        }[invalid]
        body[key] = value
    version = invalid[1:] if invalid.startswith("v") else "8"
    paths = [
        "accounts",
        "transactions",
        f"participants/{draft['participantId']}/credits",
        f"participants/{draft['participantId']}/settlements",
    ]
    before = [(await client.get(f"{base}/{p}")).json() for p in paths]
    rejected = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": plan["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": version},
    )
    assert rejected.status_code in (404, 409, 422), rejected.text
    assert "submissionReceipt" in rejected.json() or "commandValidationRejection" in rejected.json()
    assert [(await client.get(f"{base}/{p}")).json() for p in paths] == before


@pytest.mark.parametrize(
    "change",
    [
        "credit",
        "balance",
        "amount",
        "date",
        "account",
        "note",
        "participant_archive",
        "account_archive",
    ],
)
async def test_changed_return_requires_reconfirmation_with_new_submission_identity(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    change: str,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00")
    body = credit_return(draft, "25.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    if change == "credit":
        await return_credit(client, actor, base, credit_return(draft, "5.00"))
    elif change == "balance":
        await create_finance_resource(
            client,
            f"{base}/transactions",
            owner_id=actor.id,
            json={
                "kind": "expense",
                "accountId": draft["fundingAccountId"],
                "transactionDate": "2026-10-29",
                "economicAmount": {"amount": "1.00", "currency": "CNY"},
                "categoryAllocations": [{"amount": {"amount": "1.00", "currency": "CNY"}}],
            },
        )
    elif change == "amount":
        body["amount"]["amount"] = "20.00"
    elif change == "date":
        body["transactionDate"] = "2026-10-30"
    elif change == "account":
        account = await create_finance_resource(
            client,
            f"{base}/accounts",
            owner_id=actor.id,
            json={
                "name": "Other cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "0.00", "currency": "CNY"},
                "trackingStartDate": "2026-10-01",
            },
        )
        body["payingAccountId"] = account.json()["id"]
    elif change == "note":
        body["note"] = "Updated return"
    elif change == "participant_archive":
        await client.post(f"{base}/participants/{draft['participantId']}/archive")
    else:
        await client.post(f"{base}/accounts/{draft['fundingAccountId']}/archive")
    command = {**body, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "8"}
    rejected = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "finance_settlement_preview_stale"
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == rejected.json()
    if change == "account_archive":
        assert (await client.post(f"{base}/settlements/preview", json=body)).status_code == 409
        await client.post(f"{base}/accounts/{draft['fundingAccountId']}/unarchive")
    fresh = (await client.post(f"{base}/settlements/preview", json=body)).json()
    conflict = await client.post(
        f"{base}/settlements", json={**body, "previewToken": fresh["previewToken"]}, headers=headers
    )
    assert (
        conflict.status_code == 409
        and conflict.json()["code"] == "finance_submission_content_conflict"
    )
    await return_credit(client, actor, base, body)


@pytest.mark.parametrize(
    "nature,currency,amount",
    [
        ("asset", "JPY", "2"),
        ("liability", "USD", "2.00"),
        ("liability", "CNY", "123456789012345678901234567890.01"),
    ],
)
async def test_exact_future_return_uses_paying_account_nature_without_profit_loss(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    nature: str,
    currency: str,
    amount: str,
) -> None:
    client, actor, base, draft = advance_client
    zero = "0" if currency == "JPY" else "0.00"
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Payment",
            "nature": nature,
            "currency": currency,
            "openingBalance": {"amount": zero, "currency": currency},
            "trackingStartDate": "2026-10-01",
        },
    )
    account_id = account.json()["id"]
    origin = await confirm_credit(
        client,
        actor,
        base,
        {
            **repayment(draft),
            "receivingAccountId": account_id,
            "amount": {"amount": amount, "currency": currency},
            "excessDisposition": "retainCredit",
        },
    )
    body = {
        **credit_return(draft),
        "payingAccountId": account_id,
        "amount": {"amount": amount, "currency": currency},
        "transactionDate": "2026-11-02",
    }
    plan = (await client.post(f"{base}/settlements/preview", json=body)).json()
    assert plan["payingBalance"]["amount"] == (amount if nature == "asset" else f"-{amount}")
    assert plan["payingBalanceAfter"]["amount"] == zero
    detail = await return_credit(client, actor, base, body)
    assert detail["transactionDate"] == "2026-11-02"
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[account_id]["currentBalance"]["amount"] == zero
    assert (
        accounts[origin["creditOrigin"]["liabilityAccountId"]]["currentBalance"]["amount"] == zero
    )
    summary = (await client.get(f"{base}/overview", params={"month": "2026-11"})).json()
    assert all(
        s["income"]["amount"] in ("0", "0.00") and s["expense"]["amount"] in ("0", "0.00")
        for s in summary["monthSummaryByCurrency"]
    )
    before_tracking = await client.post(
        f"{base}/settlements/preview", json={**body, "transactionDate": "2026-09-30"}
    )
    assert before_tracking.status_code == 422


async def test_return_records_and_recovery_are_private_and_linked_transfers_protected(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "50.00")
    body = credit_return(draft, "25.00")
    plan = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "8"}
    command = {**body, "previewToken": plan["previewToken"]}
    receipt = await client.post(f"{base}/settlements", json=command, headers=headers)
    detail = (
        await client.get(f"{base}/settlements/{receipt.json()['outcome']['resource']['id']}")
    ).json()
    ledger = await client.post(
        "/api/finance/ledgers", headers=submission_headers(actor.id), json={"name": "Other"}
    )
    other_base = f"/api/finance/ledgers/{ledger.json()['outcome']['resource']['id']}"
    assert (await client.post(f"{other_base}/settlements/preview", json=body)).status_code == 404
    assert (await client.get(f"{other_base}/settlements/{detail['id']}")).status_code == 404
    other_participant = (await client.post(f"{base}/participants", json={"name": "Other"})).json()
    assert (
        await client.post(
            f"{base}/settlements/preview", json={**body, "participantId": other_participant["id"]}
        )
    ).status_code == 422
    outsider = User(
        identity_issuer="https://identity.example.test/t11",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(outsider)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=outsider) as private:
        assert (await private.post(f"{base}/settlements/preview", json=body)).status_code == 404
        assert (await private.get(f"{base}/settlements/{detail['id']}")).status_code == 404
        assert (
            await private.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
        ).status_code == 403
    transfer: dict[str, Any] = {
        "kind": "internalTransfer",
        "sourceAccountId": draft["fundingAccountId"],
        "destinationAccountId": origin["creditOrigin"]["liabilityAccountId"],
        "amount": {"amount": "1.00", "currency": "CNY"},
        "transactionDate": "2026-10-29",
    }
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
    account_id = origin["creditOrigin"]["liabilityAccountId"]
    for method, path, mutation in [
        (
            "PATCH",
            f"accounts/{account_id}",
            {"openingBalance": {"amount": "1.00", "currency": "CNY"}},
        ),
        ("PATCH", f"accounts/{account_id}", {"trackingStartDate": "2026-10-01"}),
        ("POST", f"accounts/{account_id}/archive", None),
        ("POST", "transactions", transfer),
        (
            "POST",
            "balance-adjustments",
            {
                "accountId": account_id,
                "transactionDate": "2026-10-29",
                "expectedAccountNature": "liability",
                "expectedDerivedBalance": {"amount": "25.00", "currency": "CNY"},
                "targetBalance": {"amount": "1.00", "currency": "CNY"},
            },
        ),
    ]:
        rejected = await client.request(
            method, f"{base}/{path}", json=mutation, headers=submission_headers(actor.id)
        )
        assert (
            rejected.status_code == 409
            and rejected.json()["code"] == "finance_linked_record_protected"
        )
    assert (await client.delete(f"{base}/settlements/{detail['id']}")).status_code == 405
    assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == receipt.json()
