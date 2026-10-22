"""Receipt-backed credit through public HTTP and guarded PostgreSQL evidence."""

from typing import Any

import pytest
from httpx import AsyncClient

from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advances import advance_client as advance_client
from integration.test_finance_settlements import advance, repayment

pytestmark = pytest.mark.anyio


def retained(draft: dict[str, object], amount: str = "102.00") -> dict[str, object]:
    return {**repayment(draft, amount), "excessDisposition": "retainCredit"}


async def confirm_credit(
    client: AsyncClient, actor: User, base: str, body: dict[str, object]
) -> dict[str, Any]:
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    command = {**body, "previewToken": preview.json()["previewToken"]}
    response = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["commandVersion"] == "2"
    replay = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert replay.json() == response.json()
    result: dict[str, Any] = (
        await client.get(f"{base}/settlements/{response.json()['outcome']['resource']['id']}")
    ).json()
    return result


async def test_receive_102_against_100_retains_two_as_gross_liability_without_income(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = retained(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["retainedCredit"] == {"amount": "2.00", "currency": "CNY"}
    assert preview.json()["outstandingAfter"]["amount"] == "0.00"
    assert preview.json()["availableCreditAfter"]["amount"] == "2.00"
    detail = await confirm_credit(client, actor, base, body)
    assert detail["amount"]["amount"] == "102.00"
    assert detail["allocations"][0]["cashPaid"]["amount"] == "100.00"
    origin = detail["creditOrigin"]
    assert origin["settlementId"] == detail["id"]
    assert origin["confirmationOrder"] == detail["confirmationOrder"]
    assert origin["amount"]["amount"] == origin["available"]["amount"] == "2.00"
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"]["amount"] == "102.00"
    credit_account = accounts[origin["liabilityAccountId"]]
    assert credit_account["nature"] == "liability"
    assert credit_account["currentBalance"]["amount"] == "2.00"
    overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    summary = overview["monthSummaryByCurrency"][0]
    assert summary["income"]["amount"] == summary["expense"]["amount"] == "0.00"
    position = overview["financialPositionByCurrency"][0]
    assert position["assetTotal"]["amount"] == "102.00"
    assert position["liabilityTotal"]["amount"] == "2.00"


async def test_unallocated_receipt_and_new_advance_preserve_gross_zero_net_and_origin_order(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await confirm_credit(client, actor, base, retained(draft, "50.00"))
    assert first["allocations"] == []
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json() == []
    prefix = f"{base}/participants/{draft['participantId']}"
    positions = await client.get(f"{prefix}/positions")
    assert positions.status_code == 200, positions.text
    assert positions.json() == [
        {
            "currency": "CNY",
            "outstandingReceivables": {"amount": "0.00", "currency": "CNY"},
            "availableCredits": {"amount": "50.00", "currency": "CNY"},
            "netExposure": {"amount": "-50.00", "currency": "CNY"},
        }
    ]
    await advance(client, actor, base, draft, "50.00", "2026-10-23")
    position = (await client.get(f"{prefix}/positions")).json()[0]
    assert position["outstandingReceivables"]["amount"] == "50.00"
    assert position["availableCredits"]["amount"] == "50.00"
    assert position["netExposure"]["amount"] == "0.00"
    # An earlier financial date never changes immutable receipt confirmation order.
    second = await confirm_credit(
        client, actor, base, {**retained(draft, "20.00"), "transactionDate": "2026-10-21"}
    )
    origins = (await client.get(f"{prefix}/credits")).json()
    assert origins == [first["creditOrigin"], second["creditOrigin"]]
    assert origins[0]["confirmationOrder"] < origins[1]["confirmationOrder"]
    assert (await client.get(f"{base}/settlements/{first['id']}")).json() == first
    await client.patch(f"{prefix}", json={"name": "Renamed"})
    await client.post(f"{prefix}/archive")
    assert (await client.get(f"{prefix}/credits")).json() == origins
    assert (await client.get(f"{prefix}/positions")).json()[0]["availableCredits"][
        "amount"
    ] == "70.00"
