"""Standalone forgiveness through the public API and guarded PostgreSQL."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


def forgiveness(draft: dict[str, object], ids: list[str], amount: str) -> dict[str, Any]:
    return {
        "kind": "forgiveness",
        "participantId": draft["participantId"],
        "selectedObligationIds": ids,
        "transactionDate": "2026-10-22",
        "amount": {"amount": amount, "currency": "CNY"},
        "reason": "Explicitly waive this debt",
    }


async def test_selected_forgiveness_reduces_receivable_and_reports_expense_without_cash(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    selected = [second["obligation"]["id"]]
    body = forgiveness(draft, selected, "20.00")
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["expense"] == {"amount": "20.00", "currency": "CNY"}
    assert preview.json()["outstandingBefore"]["amount"] == "60.00"
    assert preview.json()["outstandingAfter"]["amount"] == "40.00"
    assert "receivingBalance" not in preview.json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "5"}
    command = {**body, "previewToken": preview.json()["previewToken"]}
    response = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert response.status_code == 201, response.text
    detail = (
        await client.get(f"{base}/settlements/{response.json()['outcome']['resource']['id']}")
    ).json()
    assert detail["kind"] == "forgiveness"
    assert detail["reason"] == body["reason"]
    assert detail["selectedObligationIds"] == selected
    assert "receivingAccountId" not in detail
    assert detail["allocations"] == preview.json()["allocations"]
    assert detail["allocations"][0]["kind"] == "forgiveness"
    assert detail["allocations"][0]["forgiven"]["amount"] == "20.00"
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert [
        (o["cashPaid"]["amount"], o["forgiven"]["amount"], o["remaining"]["amount"])
        for o in obligations
    ] == [("0.00", "0.00", "40.00"), ("0.00", "20.00", "40.00")]
    assert (await client.get(f"{base}/advances/{second['id']}")).json()[
        "obligation"
    ] == obligations[1]
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"]["amount"] == "0.00"
    assert (
        accounts[first["obligation"]["receivableAccountId"]]["currentBalance"]["amount"] == "40.00"
    )
    assert (
        accounts[second["obligation"]["receivableAccountId"]]["currentBalance"]["amount"] == "40.00"
    )
    assert len(accounts) == 3
    overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    summary = overview["monthSummaryByCurrency"][0]
    assert summary["expense"]["amount"] == "20.00"
    assert summary["income"]["amount"] == "0.00"
    transactions = (await client.get(f"{base}/transactions")).json()["items"]
    assert len(transactions) == 3
    assert sorted(t["kind"] for t in transactions) == [
        "expense",
        "internalTransfer",
        "internalTransfer",
    ]
    expense = (await client.get(f"{base}/transactions/{detail['transactionIds'][0]}")).json()
    assert expense["kind"] == "expense"
    assert expense["transactionDate"] == "2026-10-22"
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == response.json()
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/settlements")
    ).json() == [detail]
    credit_body = {
        **repayment(draft, "80.00"),
        "selectedObligationIds": selected,
        "excessDisposition": "retainCredit",
    }
    credit_preview = (await client.post(f"{base}/settlements/preview", json=credit_body)).json()
    credit = await client.post(
        f"{base}/settlements",
        json={**credit_body, "previewToken": credit_preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "4"},
    )
    assert credit.status_code == 201, credit.text
    position = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()[
        0
    ]
    assert position["outstandingReceivables"]["amount"] == "40.00"
    assert position["availableCredits"]["amount"] == "40.00"
    assert position["netExposure"]["amount"] == "0.00"
    still_owed = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()[0]
    assert still_owed["remaining"]["amount"] == "40.00"
    assert still_owed["cashPaid"]["amount"] == still_owed["forgiven"]["amount"] == "0.00"


async def test_cash_and_forgiveness_keep_composition_capacity_and_frozen_order(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-20")
    await confirm(client, actor, base, repayment(draft, "30.00"))
    ids = [second["obligation"]["id"], first["obligation"]["id"]]
    body = forgiveness(draft, ids, "50.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    assert [a["forgiven"]["amount"] for a in preview["allocations"]] == ["10.00", "40.00"]
    assert [a["obligationId"] for a in preview["allocations"]] == list(reversed(ids))
    assert (
        await client.post(
            f"{base}/settlements/preview",
            json={**body, "selectedObligationIds": list(reversed(ids))},
        )
    ).json() == preview
    await client.post(f"{base}/participants/{draft['participantId']}/archive")
    # Archived identities can still resolve existing debt; state change needs another preview.
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "5"},
    )
    assert result.status_code == 201, result.text
    detail_url = f"{base}/settlements/{result.json()['outcome']['resource']['id']}"
    retained = (await client.get(detail_url)).json()
    # A later cash receipt only sees the remaining 20; surplus becomes credit explicitly.
    credit_body = {**repayment(draft, "60.00"), "excessDisposition": "retainCredit"}
    credit_preview = (await client.post(f"{base}/settlements/preview", json=credit_body)).json()
    assert credit_preview["allocations"][0]["cashPaid"]["amount"] == "20.00"
    receipt = await client.post(
        f"{base}/settlements",
        json={**credit_body, "previewToken": credit_preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
    )
    assert receipt.status_code == 201, receipt.text
    third = await advance(
        client,
        actor,
        base,
        {
            **draft,
            "participantId": (
                await client.post(f"{base}/participants", json={"name": "Blair"})
            ).json()["id"],
        },
        "5.00",
        "2026-10-19",
    )
    assert third["obligation"]["remaining"]["amount"] == "5.00"
    assert (await client.get(detail_url)).json() == retained
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert [
        (o["cashPaid"]["amount"], o["forgiven"]["amount"], o["remaining"]["amount"])
        for o in obligations
    ] == [("30.00", "10.00", "0.00"), ("20.00", "40.00", "0.00")]


@pytest.mark.parametrize(
    "invalid",
    [
        "excess",
        "zero",
        "negative",
        "precision",
        "number",
        "empty_reason",
        "missing_reason",
        "empty_selection",
        "duplicate",
        "unknown",
        "participant",
        "currency",
        "date",
        "paid",
        "receivingAccountId",
        "excessDisposition",
        "credit",
        "v1",
        "v2",
        "v3",
        "v4",
    ],
)
async def test_invalid_forgiveness_cannot_create_any_financial_effect(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = forgiveness(draft, [source["obligation"]["id"]], "50.00")
    if invalid in ("excess", "zero", "negative", "precision", "number"):
        body["amount"]["amount"] = {
            "excess": "100.01",
            "zero": "0.00",
            "negative": "-1.00",
            "precision": "1.001",
            "number": 1.25,
        }[invalid]
    elif invalid == "empty_reason":
        body["reason"] = "  "
    elif invalid == "missing_reason":
        del body["reason"]
    elif invalid == "empty_selection":
        body["selectedObligationIds"] = []
    elif invalid == "duplicate":
        body["selectedObligationIds"] *= 2
    elif invalid == "unknown":
        body["selectedObligationIds"] = [str(uuid4())]
    elif invalid == "participant":
        body["participantId"] = (
            await client.post(f"{base}/participants", json={"name": "Other"})
        ).json()["id"]
    elif invalid == "currency":
        body["amount"]["currency"] = "USD"
    elif invalid == "date":
        body["transactionDate"] = "2026-10-19"
    elif invalid == "paid":
        await confirm(client, actor, base, repayment(draft, "100.00"))
    elif invalid == "receivingAccountId":
        body[invalid] = draft["fundingAccountId"]
    elif invalid == "excessDisposition":
        body[invalid] = "retainCredit"
    elif invalid == "credit":
        body["useCredit"] = True
    before_accounts = (await client.get(f"{base}/accounts")).json()
    before_transactions = (await client.get(f"{base}/transactions")).json()
    headers = {
        **submission_headers(actor.id),
        "Finance-Command-Version": invalid[1:] if invalid.startswith("v") else "5",
    }
    result = await client.post(
        f"{base}/settlements", json={**body, "previewToken": "0" * 64}, headers=headers
    )
    assert result.status_code in (409, 422), result.text
    assert result.headers["content-type"] == "application/problem+json"
    assert (await client.get(f"{base}/accounts")).json() == before_accounts
    assert (await client.get(f"{base}/transactions")).json() == before_transactions
    if not invalid.startswith("v"):
        assert (await client.post(f"{base}/settlements/preview", json=body)).status_code == 422


@pytest.mark.parametrize("change", ["cash", "forgiveness", "selection", "reason", "amount", "date"])
async def test_changed_debt_or_intent_requires_reconfirmation_and_retains_rejection(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    change: str,
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    body = forgiveness(draft, [second["obligation"]["id"]], "50.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    if change == "cash":
        await confirm(client, actor, base, repayment(draft, "70.00"))
    elif change == "forgiveness":
        done = await client.post(
            f"{base}/settlements",
            json={**body, "previewToken": preview["previewToken"]},
            headers={**submission_headers(actor.id), "Finance-Command-Version": "5"},
        )
        assert done.status_code == 201
    elif change == "selection":
        body["selectedObligationIds"].append(first["obligation"]["id"])
    elif change == "amount":
        body["amount"]["amount"] = "40.00"
    elif change == "reason":
        body["reason"] = "Another reason"
    else:
        body["transactionDate"] = "2026-10-23"
    before = (await client.get(f"{base}/accounts")).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "5"}
    command = {**body, "previewToken": preview["previewToken"]}
    stale = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert stale.status_code == 409 and stale.json()["code"] == "finance_settlement_preview_stale"
    assert (await client.post(f"{base}/settlements", json=command, headers=headers)).json()[
        "submissionReceipt"
    ] == stale.json()["submissionReceipt"]
    assert (await client.get(f"{base}/accounts")).json() == before


async def test_forgiveness_protects_linked_expense_and_receivable_from_manual_bypasses(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = forgiveness(draft, [source["obligation"]["id"]], "20.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "5"},
    )
    assert result.status_code == 201
    url = f"{base}/settlements/{result.json()['outcome']['resource']['id']}"
    detail = (await client.get(url)).json()
    receivable = source["obligation"]["receivableAccountId"]
    money = {"amount": "1.00", "currency": "CNY"}
    expense = {
        "kind": "expense",
        "accountId": receivable,
        "transactionDate": "2026-10-22",
        "economicAmount": money,
        "categoryAllocations": [{"amount": money}],
    }
    for path, command in [
        ("transactions", expense),
        (
            "transactions",
            {
                "kind": "internalTransfer",
                "sourceAccountId": receivable,
                "destinationAccountId": draft["fundingAccountId"],
                "amount": money,
                "transactionDate": "2026-10-22",
            },
        ),
        (
            "balance-adjustments",
            {
                "accountId": receivable,
                "transactionDate": "2026-10-22",
                "expectedAccountNature": "asset",
                "expectedDerivedBalance": {"amount": "80.00", "currency": "CNY"},
                "targetBalance": money,
            },
        ),
    ]:
        blocked = await client.post(
            f"{base}/{path}", json=command, headers=submission_headers(actor.id)
        )
        assert (
            blocked.status_code == 409
            and blocked.json()["code"] == "finance_linked_record_protected"
        ), blocked.text
    for method, path, mutation in [
        ("PUT", f"transactions/{detail['transactionIds'][0]}", expense),
        ("DELETE", f"transactions/{detail['transactionIds'][0]}", None),
        ("PATCH", f"accounts/{receivable}", {"openingBalance": money}),
        ("PATCH", f"accounts/{receivable}", {"trackingStartDate": "2026-10-02"}),
        ("POST", f"accounts/{receivable}/archive", None),
    ]:
        blocked = await client.request(method, f"{base}/{path}", json=mutation)
        assert (
            blocked.status_code == 409
            and blocked.json()["code"] == "finance_linked_record_protected"
        ), blocked.text
    assert (await client.delete(url)).status_code == 405
    assert (await client.get(url)).json() == detail
    assert (await client.get(f"{base}/participants/{draft['participantId']}/obligations")).json()[
        0
    ]["remaining"]["amount"] == "80.00"


async def test_forgiveness_ownership_and_original_action_identity_remain_isolated(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = forgiveness(draft, [source["obligation"]["id"]], "20.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "5"}
    result = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert result.status_code == 201
    id = result.json()["outcome"]["resource"]["id"]
    conflict = await client.post(
        f"{base}/settlements", json={**command, "reason": "different"}, headers=headers
    )
    assert conflict.json()["code"] == "finance_submission_content_conflict"
    other_ledger = await client.post(
        "/api/finance/ledgers", json={"name": "Other"}, headers=submission_headers(actor.id)
    )
    other_base = f"/api/finance/ledgers/{other_ledger.json()['outcome']['resource']['id']}"
    assert (await client.post(f"{other_base}/settlements/preview", json=body)).status_code == 404
    assert (await client.get(f"{other_base}/settlements/{id}")).status_code == 404
    other = User(
        identity_issuer="https://identity.example.test/forgiveness",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(other)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=other) as foreign:
        assert (await foreign.get(f"{base}/settlements/{id}")).status_code == 404
        assert (await foreign.post(f"{base}/settlements/preview", json=body)).status_code == 404
        assert (
            await foreign.post(
                f"{base}/settlements",
                json=command,
                headers={**submission_headers(other.id), "Finance-Command-Version": "5"},
            )
        ).status_code == 404
        assert (
            await foreign.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}",
                headers=submission_headers(other.id),
            )
        ).status_code == 404


@pytest.mark.parametrize(
    "currency,principal,forgiven,remainder",
    [
        ("JPY", "100", "98", "2"),
        ("USD", "100.00", "98.00", "2.00"),
        ("CNY", "123456789012345678901234567890.02", "123456789012345678901234567890.01", "0.01"),
    ],
)
async def test_forgiveness_uses_exact_currency_amounts_and_preserves_funding_position(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    currency: str,
    principal: str,
    forgiven: str,
    remainder: str,
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
    source_body = {
        **draft,
        "fundingAccountId": account.json()["id"],
        "amount": {"amount": principal, "currency": currency},
        "participantShare": {"amount": principal, "currency": currency},
        "ownerShare": {"amount": "0" if currency == "JPY" else "0.00", "currency": currency},
    }
    del source_body["participantId"]
    del source_body["participantShare"]
    source_body["participantShares"] = [
        {
            "participantId": draft["participantId"],
            "amount": {"amount": principal, "currency": currency},
        }
    ]
    source_preview_response = await client.post(f"{base}/advances/preview", json=source_body)
    assert source_preview_response.status_code == 200, source_preview_response.text
    source_preview = source_preview_response.json()
    source = await client.post(
        f"{base}/advances",
        json={**source_body, "previewToken": source_preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
    )
    assert source.status_code == 201, source.text
    obligation = (
        await client.get(f"{base}/advances/{source.json()['outcome']['resource']['id']}")
    ).json()["obligations"][0]
    body = {
        **forgiveness(draft, [obligation["id"]], forgiven),
        "amount": {"amount": forgiven, "currency": currency},
    }
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "5"},
    )
    assert result.status_code == 201, result.text
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[account.json()["id"]]["currentBalance"]["amount"] == principal
    assert accounts[obligation["receivableAccountId"]]["currentBalance"]["amount"] == remainder
    remaining = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()[0]
    assert remaining["forgiven"]["amount"] == forgiven
    assert remaining["remaining"]["amount"] == remainder


@pytest.mark.parametrize("competitor", ["cash", "forgiveness", "same_key"])
async def test_concurrent_forgiveness_serializes_selected_capacity_without_duplicate_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    competitor: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = forgiveness(draft, [source["obligation"]["id"]], "80.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "5"}
    other_headers = (
        headers
        if competitor == "same_key"
        else {**submission_headers(actor.id), "Finance-Command-Version": "5"}
    )
    other_command = command
    if competitor == "cash":
        cash_body = repayment(draft, "80.00")
        cash_preview = (await client.post(f"{base}/settlements/preview", json=cash_body)).json()
        other_command = {**cash_body, "previewToken": cash_preview["previewToken"]}
        other_headers["Finance-Command-Version"] = "1"
    attempts = [(command, headers), (other_command, other_headers)]
    results = await asyncio.gather(
        *[
            client.post(f"{base}/settlements", json=body, headers=identity)
            for body, identity in attempts
        ]
    )
    for index, response in enumerate(results):
        if response.status_code == 503 or response.json().get("code") == "finance_submission_busy":
            body, identity = attempts[index]
            results[index] = await client.post(f"{base}/settlements", json=body, headers=identity)
    if competitor == "same_key":
        assert results[0].status_code == results[1].status_code == 201
        assert results[0].json() == results[1].json()
    else:
        assert sorted(r.status_code for r in results) == [201, 409]
        assert (
            next(r for r in results if r.status_code == 409).json()["code"]
            == "finance_settlement_preview_stale"
        )
    obligation = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()[0]
    assert obligation["remaining"]["amount"] == "20.00"
    assert (obligation["cashPaid"]["amount"], obligation["forgiven"]["amount"]) in (
        ("0.00", "80.00"),
        ("80.00", "0.00"),
    )
    assert (
        len((await client.get(f"{base}/participants/{draft['participantId']}/settlements")).json())
        == 1
    )
