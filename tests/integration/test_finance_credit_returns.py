"""Actual credit returns through the public API and guarded PostgreSQL."""

from typing import Any, cast

import pytest
from httpx import AsyncClient

from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_credit_applications import application, apply_credit, retain_credit
from integration.test_finance_settlements import advance, advance_client

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


def credit_return(draft: dict[str, object], amount: str = "2.00") -> dict[str, Any]:
    return {
        "kind": "creditReturn",
        "participantId": draft["participantId"],
        "payingAccountId": draft["fundingAccountId"],
        "transactionDate": "2026-10-29",
        "amount": {"amount": amount, "currency": "CNY"},
    }


async def return_credit(
    client: AsyncClient, actor: User, base: str, body: dict[str, Any]
) -> dict[str, Any]:
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "8"},
    )
    assert result.status_code == 201, result.text
    return cast(
        dict[str, Any],
        (
            await client.get(f"{base}/settlements/{result.json()['outcome']['resource']['id']}")
        ).json(),
    )


async def test_return_two_reduces_actual_cash_and_credit_without_expense(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "2.00")
    body = credit_return(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["payingBalance"]["amount"] == "102.00"
    assert plan["payingBalanceAfter"]["amount"] == "100.00"
    assert plan["availableCreditAfter"]["amount"] == "0.00"
    detail = await return_credit(client, actor, base, body)
    assert detail["kind"] == "creditReturn"
    assert detail["payingAccountId"] == draft["fundingAccountId"]
    assert detail["origins"] == plan["origins"]
    assert detail["origins"][0]["originSettlementId"] == origin["id"]
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"]["amount"] == "100.00"
    assert (
        accounts[origin["creditOrigin"]["liabilityAccountId"]]["currentBalance"]["amount"] == "0.00"
    )
    credits = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    assert credits[0]["amount"]["amount"] == "2.00"
    assert credits[0]["available"]["amount"] == "0.00"
    summary = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert summary["monthSummaryByCurrency"][0]["income"]["amount"] == "0.00"
    assert summary["monthSummaryByCurrency"][0]["expense"]["amount"] == "0.00"
    tx = (await client.get(f"{base}/transactions/{detail['transactionIds'][0]}")).json()
    assert tx["kind"] == "internalTransfer"


async def test_oldest_origins_and_return_history_survive_later_consumption(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    older = await retain_credit(client, actor, base, draft, "20.00", "2026-10-24")
    newer = await retain_credit(client, actor, base, draft, "30.00", "2026-10-23")
    detail = await return_credit(client, actor, base, credit_return(draft, "25.00"))
    assert [o["originSettlementId"] for o in detail["origins"]] == [older["id"], newer["id"]]
    assert [o["amount"]["amount"] for o in detail["origins"]] == ["20.00", "5.00"]
    assert [o["availableAfter"]["amount"] for o in detail["origins"]] == ["0.00", "25.00"]
    assert len(detail["transactionIds"]) == 2
    debt = await advance(client, actor, base, draft, "40.00", "2026-10-22")
    await apply_credit(client, actor, base, application(draft, [debt["obligation"]["id"]], "10.00"))
    await client.patch(f"{base}/participants/{draft['participantId']}", json={"name": "Renamed"})
    await client.post(f"{base}/participants/{draft['participantId']}/archive")
    await return_credit(client, actor, base, credit_return(draft, "15.00"))
    assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
    history = (await client.get(f"{base}/participants/{draft['participantId']}/settlements")).json()
    assert next(s for s in history if s["id"] == detail["id"]) == detail
    position = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()[
        0
    ]
    assert position["outstandingReceivables"]["amount"] == "30.00"
    assert position["availableCredits"]["amount"] == "0.00"
    credits = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    assert [o["amount"]["amount"] for o in credits] == ["20.00", "30.00"]
    assert [o["available"]["amount"] for o in credits] == ["0.00", "0.00"]
    rejected = await client.post(
        f"{base}/settlements/preview", json=application(draft, [debt["obligation"]["id"]], "1.00")
    )
    assert rejected.status_code == 422


async def test_return_note_normalization_and_replay_preserve_non_bmp_codepoints(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "2.00")
    body = {**credit_return(draft), "note": "  " + "😀" * 500 + "  "}
    plan = await client.post(f"{base}/settlements/preview", json=body)
    assert plan.status_code == 200, plan.text
    command = {**body, "previewToken": plan.json()["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "8"}
    receipt = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert receipt.status_code == 201, receipt.text
    detail = (
        await client.get(f"{base}/settlements/{receipt.json()['outcome']['resource']['id']}")
    ).json()
    assert detail["note"] == "😀" * 500
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == receipt.json()
    invalid = await client.post(f"{base}/settlements/preview", json={**body, "note": "😀" * 501})
    assert invalid.status_code == 422
