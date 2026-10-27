"""Explicit credit application at the public Finance API boundary."""

from typing import Any, cast

import pytest
from httpx import AsyncClient

from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_settlements import advance, advance_client, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


async def retain_credit(
    client: AsyncClient,
    actor: User,
    base: str,
    draft: dict[str, object],
    amount: str,
    date: str = "2026-10-22",
) -> dict[str, Any]:
    body = {
        **repayment(draft, amount),
        "excessDisposition": "retainCredit",
        "transactionDate": date,
    }
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    receipt = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
    )
    assert receipt.status_code == 201, receipt.text
    return cast(
        dict[str, Any],
        (
            await client.get(f"{base}/settlements/{receipt.json()['outcome']['resource']['id']}")
        ).json(),
    )


def application(draft: dict[str, object], ids: list[str], amount: str) -> dict[str, Any]:
    return {
        "kind": "creditApplication",
        "participantId": draft["participantId"],
        "selectedObligationIds": ids,
        "transactionDate": "2026-10-27",
        "amount": {"amount": amount, "currency": "CNY"},
    }


async def test_apply_two_credit_to_ten_debt_without_cash_or_profit_loss(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "2.00")
    debt = await advance(client, actor, base, draft, "10.00", "2026-10-22")
    assert debt["obligation"]["remaining"]["amount"] == "10.00"
    body = application(draft, [debt["obligation"]["id"]], "2.00")
    before = (await client.get(f"{base}/accounts")).json()
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["availableCreditBefore"]["amount"] == "2.00"
    assert plan["availableCreditAfter"]["amount"] == "0.00"
    assert plan["outstandingAfter"]["amount"] == "8.00"
    assert plan["origins"][0]["originSettlementId"] == origin["id"]
    assert plan["allocations"][0]["creditApplied"]["amount"] == "2.00"
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "7"}
    command = {**body, "previewToken": plan["previewToken"]}
    result = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert result.status_code == 201, result.text
    detail = (
        await client.get(f"{base}/settlements/{result.json()['outcome']['resource']['id']}")
    ).json()
    assert detail["kind"] == "creditApplication"
    assert detail["allocations"] == plan["allocations"]
    assert detail["origins"] == plan["origins"]
    assert "receivingAccountId" not in detail
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert obligations[0]["creditApplied"]["amount"] == "2.00"
    assert obligations[0]["remaining"]["amount"] == "8.00"
    assert obligations[0]["cashPaid"]["amount"] == "0.00"
    assert obligations[0]["forgiven"]["amount"] == "0.00"
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert len(accounts) == len(before)
    cash_id = str(draft["fundingAccountId"])
    assert accounts[cash_id] == next(a for a in before if a["id"] == cash_id)
    assert accounts[debt["obligation"]["receivableAccountId"]]["currentBalance"]["amount"] == "8.00"
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
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == result.json()


async def apply_credit(
    client: AsyncClient, actor: User, base: str, body: dict[str, Any]
) -> dict[str, Any]:
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "7"},
    )
    assert result.status_code == 201, result.text
    return cast(
        dict[str, Any],
        (
            await client.get(f"{base}/settlements/{result.json()['outcome']['resource']['id']}")
        ).json(),
    )


async def test_oldest_origin_consumption_selected_debt_and_history_survive_later_actions(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    older = await retain_credit(client, actor, base, draft, "20.00", "2026-10-24")
    newer = await retain_credit(client, actor, base, draft, "30.00", "2026-10-23")
    unselected = await advance(client, actor, base, draft, "50.00", "2026-10-19")
    first = await advance(client, actor, base, draft, "10.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "40.00", "2026-10-21")
    ids = [second["obligation"]["id"], first["obligation"]["id"]]
    body = application(draft, ids, "25.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    assert (
        await client.post(
            f"{base}/settlements/preview",
            json={**body, "selectedObligationIds": list(reversed(ids))},
        )
    ).json() == preview
    assert [o["amount"]["amount"] for o in preview["origins"]] == ["20.00", "5.00"]
    assert [o["originSettlementId"] for o in preview["origins"]] == [older["id"], newer["id"]]
    assert [
        (a["obligationId"], a["creditApplied"]["amount"], a["remainingAfter"]["amount"])
        for a in preview["allocations"]
    ] == [(ids[1], "10.00", "0.00"), (ids[0], "10.00", "30.00"), (ids[0], "5.00", "25.00")]
    detail = await apply_credit(client, actor, base, body)
    assert detail["origins"] == preview["origins"]
    credits = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    assert [o["available"]["amount"] for o in credits] == ["0.00", "25.00"]
    assert [o["amount"]["amount"] for o in credits] == ["20.00", "30.00"]
    assert (await client.get(f"{base}/advances/{unselected['id']}")).json() == unselected
    await client.patch(f"{base}/participants/{draft['participantId']}", json={"name": "Renamed"})
    await client.post(f"{base}/participants/{draft['participantId']}/archive")
    await apply_credit(client, actor, base, application(draft, [ids[0]], "5.00"))
    await advance(
        client,
        actor,
        base,
        {
            **draft,
            "participantId": (
                await client.post(f"{base}/participants", json={"name": "Other"})
            ).json()["id"],
        },
        "5.00",
        "2026-10-18",
    )
    assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
    history = (await client.get(f"{base}/participants/{draft['participantId']}/settlements")).json()
    assert next(s for s in history if s["id"] == detail["id"]) == detail
    position = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()[
        0
    ]
    assert position["outstandingReceivables"]["amount"] == "70.00"
    assert position["availableCredits"]["amount"] == "20.00"
    assert position["netExposure"]["amount"] == "50.00"
