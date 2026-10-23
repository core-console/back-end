"""Explicit surplus Income through the public Settlement and Finance APIs."""

from typing import Any

import pytest
from httpx import AsyncClient

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_advances import advance_client as advance_client
from integration.test_finance_credits import confirm_credit, retained
from integration.test_finance_settlements import advance, repayment

pytestmark = pytest.mark.anyio


def accepted(
    draft: dict[str, object], receipt: str = "102.00", surplus: str = "2.00"
) -> dict[str, object]:
    return {
        **repayment(draft, receipt),
        "excessDisposition": "acceptIncome",
        "categoryAllocations": [{"amount": {"amount": surplus, "currency": "CNY"}}],
    }


async def confirm_income(
    client: AsyncClient, actor: User, base: str, body: dict[str, object]
) -> dict[str, Any]:
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "3"}
    command = {**body, "previewToken": preview.json()["previewToken"]}
    response = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["commandVersion"] == "3"
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == response.json()
    result: dict[str, Any] = (
        await client.get(f"{base}/settlements/{response.json()['outcome']['resource']['id']}")
    ).json()
    return result


async def test_receive_102_against_100_explicitly_accepts_only_two_as_income(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = accepted(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["acceptedSurplus"] == {"amount": "2.00", "currency": "CNY"}
    assert preview.json()["outstandingAfter"]["amount"] == "0.00"
    assert preview.json()["receivingBalanceAfter"]["amount"] == "102.00"
    detail = await confirm_income(client, actor, base, body)
    assert detail["amount"]["amount"] == "102.00"
    assert detail["acceptedSurplus"]["amount"] == "2.00"
    assert detail["allocations"][0]["cashPaid"]["amount"] == "100.00"
    assert "creditOrigin" not in detail
    income = (await client.get(f"{base}/transactions/{detail['incomeTransactionId']}")).json()
    assert income["kind"] == "income"
    assert income["economicAmount"] == {"amount": "2.00", "currency": "CNY"}
    assert income["categoryAllocations"] == [
        {"amount": {"amount": "2.00", "currency": "CNY"}, "category": None}
    ]
    assert detail["incomeTransactionId"] in detail["transactionIds"]
    prefix = f"{base}/participants/{draft['participantId']}"
    assert (await client.get(f"{prefix}/credits")).json() == []
    assert (await client.get(f"{prefix}/settlements")).json() == [detail]
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"]["amount"] == "102.00"
    overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert overview["monthSummaryByCurrency"][0]["income"]["amount"] == "2.00"
    assert overview["monthSummaryByCurrency"][0]["expense"]["amount"] == "0.00"
    assert overview["financialPositionByCurrency"][0]["liabilityTotal"]["amount"] == "0.00"


async def test_income_classification_is_explicit_exact_and_cannot_reclassify_retained_credit(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    credit = await confirm_credit(client, actor, base, retained(draft, "50.00"))
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    before = (await client.get(f"{base}/accounts")).json()
    for body in [
        repayment(draft, "102.00"),
        {**repayment(draft, "102.00"), "excessDisposition": "acceptIncome"},
        accepted(draft, "102.00", "1.00"),
        accepted(draft, "100.00", "2.00"),
        accepted(draft, "98.00", "2.00"),
    ]:
        response = await client.post(f"{base}/settlements/preview", json=body)
        assert response.status_code == 422, response.text
        assert response.headers["content-type"] == "application/problem+json"
    assert (await client.get(f"{base}/accounts")).json() == before
    preview = (await client.post(f"{base}/settlements/preview", json=accepted(draft))).json()
    assert preview["availableCreditBefore"]["amount"] == "50.00"
    assert preview["availableCreditAfter"]["amount"] == "50.00"
    await confirm_income(client, actor, base, accepted(draft))
    assert (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json() == [
        credit["creditOrigin"]
    ]


async def test_category_and_changed_consequences_require_new_deliberate_confirmation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    category = await create_finance_resource(
        client, f"{base}/categories", owner_id=actor.id, json={"name": "Surplus"}
    )
    body = {
        **accepted(draft),
        "categoryAllocations": [
            {"amount": {"amount": "2.00", "currency": "CNY"}, "categoryId": category.json()["id"]}
        ],
    }
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    await client.post(f"{base}/categories/{category.json()['id']}/archive")
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "3"}
    stale = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers=headers,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "finance_settlement_preview_stale"
    assert stale.json()["submissionReceipt"]["commandVersion"] == "3"
    assert (await client.post(f"{base}/settlements/preview", json=body)).json()[
        "code"
    ] == "finance_category_archived"
    await client.post(f"{base}/categories/{category.json()['id']}/unarchive")
    detail = await confirm_income(client, actor, base, body)
    income = (await client.get(f"{base}/transactions/{detail['incomeTransactionId']}")).json()
    assert income["categoryAllocations"][0]["category"]["id"] == category.json()["id"]
    # The old rejected identity remains rejected, even after resource state changes.
    assert (
        await client.post(
            f"{base}/settlements",
            json={**body, "previewToken": preview["previewToken"]},
            headers=headers,
        )
    ).json() == stale.json()


async def test_linked_income_cannot_be_independently_replaced_or_deleted(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    detail = await confirm_income(client, actor, base, accepted(draft, "2.00"))
    money = {"amount": "1.00", "currency": "CNY"}
    replacement = {
        "kind": "income",
        "accountId": draft["fundingAccountId"],
        "transactionDate": "2026-10-22",
        "economicAmount": money,
        "categoryAllocations": [{"amount": money}],
    }
    for method in ("PUT", "DELETE"):
        response = await client.request(
            method,
            f"{base}/transactions/{detail['incomeTransactionId']}",
            json=replacement if method == "PUT" else None,
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "finance_linked_record_protected"
    assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
