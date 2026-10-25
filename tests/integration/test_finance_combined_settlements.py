"""Cash-first combined resolution through HTTP and guarded PostgreSQL."""

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient

from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_forgiveness import forgiveness
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


def combined(
    draft: dict[str, object], ids: list[str], cash: str = "90.00", forgiven: str = "10.00"
) -> dict[str, Any]:
    return {
        **repayment(draft, cash),
        "kind": "combined",
        "selectedObligationIds": ids,
        "forgivenessAmount": {"amount": forgiven, "currency": "CNY"},
        "reason": "Explicitly waive the remaining amount",
    }


async def test_combined_allocates_all_cash_before_forgiveness_and_reconciles(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    ids = [first["obligation"]["id"], second["obligation"]["id"]]
    body = combined(draft, list(reversed(ids)))
    response = await client.post(f"{base}/settlements/preview", json=body)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["kind"] == "combined"
    assert preview["amount"]["amount"] == "90.00"
    assert (
        preview["expense"] == preview["forgivenessAmount"] == {"amount": "10.00", "currency": "CNY"}
    )
    assert preview["outstandingBefore"]["amount"] == "100.00"
    assert preview["outstandingAfter"]["amount"] == "0.00"
    assert preview["receivingBalance"]["amount"] == "0.00"
    assert preview["receivingBalanceAfter"]["amount"] == "90.00"
    assert [
        (
            a["kind"],
            a["obligationId"],
            a.get("cashPaid", a.get("forgiven"))["amount"],
            a["remainingAfter"]["amount"],
        )
        for a in preview["allocations"]
    ] == [
        ("cash", ids[0], "40.00", "0.00"),
        ("cash", ids[1], "50.00", "10.00"),
        ("forgiveness", ids[1], "10.00", "0.00"),
    ]
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "6"}
    command = {**body, "previewToken": preview["previewToken"]}
    created = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert created.status_code == 201, created.text
    detail = (
        await client.get(f"{base}/settlements/{created.json()['outcome']['resource']['id']}")
    ).json()
    assert detail["kind"] == "combined"
    assert detail["allocations"] == preview["allocations"]
    assert detail["forgivenessAmount"] == preview["expense"]
    assert detail["selectedObligationIds"] == sorted(ids)
    assert detail["reason"] == body["reason"]
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert [
        (o["cashPaid"]["amount"], o["forgiven"]["amount"], o["remaining"]["amount"])
        for o in obligations
    ] == [("40.00", "0.00", "0.00"), ("50.00", "10.00", "0.00")]
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"]["amount"] == "90.00"
    assert all(
        accounts[o["receivableAccountId"]]["currentBalance"]["amount"] == "0.00"
        for o in obligations
    )
    summary = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()[
        "monthSummaryByCurrency"
    ][0]
    assert summary["income"]["amount"] == "0.00"
    assert summary["expense"]["amount"] == "10.00"
    assert (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json() == []
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/settlements")
    ).json() == [detail]
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == created.json()


@pytest.mark.parametrize(
    "cash,forgiven,remainder", [("98.00", "2.00", "0.00"), ("98.00", "1.00", "1.00")]
)
async def test_explicit_difference_creates_only_the_requested_expense(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    cash: str,
    forgiven: str,
    remainder: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = combined(draft, [source["obligation"]["id"]], cash, forgiven)
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    assert preview["outstandingAfter"]["amount"] == remainder
    created = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "6"},
    )
    assert created.status_code == 201, created.text
    source = (await client.get(f"{base}/advances/{source['id']}")).json()
    assert (
        source["obligation"]["cashPaid"]["amount"],
        source["obligation"]["forgiven"]["amount"],
        source["obligation"]["remaining"]["amount"],
    ) == (cash, forgiven, remainder)
    summary = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()[
        "monthSummaryByCurrency"
    ][0]
    assert summary["expense"]["amount"] == forgiven
    assert summary["income"]["amount"] == "0.00"


async def test_selected_capacity_keeps_credit_unselected_debt_and_archived_history_separate(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    credit = {**repayment(draft, "20.00"), "excessDisposition": "retainCredit"}
    preview = (await client.post(f"{base}/settlements/preview", json=credit)).json()
    assert (
        await client.post(
            f"{base}/settlements",
            json={**credit, "previewToken": preview["previewToken"]},
            headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
        )
    ).status_code == 201
    credit_before = (
        await client.get(f"{base}/participants/{draft['participantId']}/credits")
    ).json()
    await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    await client.post(f"{base}/participants/{draft['participantId']}/archive")
    body = combined(draft, [second["obligation"]["id"]], "50.00", "5.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    assert preview["outstandingBefore"]["amount"] == "60.00"
    assert preview["outstandingAfter"]["amount"] == "5.00"
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "6"}
    command = {**body, "previewToken": preview["previewToken"]}
    result = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert result.status_code == 201, result.text
    url = f"{base}/settlements/{result.json()['outcome']['resource']['id']}"
    retained = (await client.get(url)).json()
    assert retained["participantId"] == draft["participantId"]
    assert retained["confirmationOrder"] > 0
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert [
        (o["cashPaid"]["amount"], o["forgiven"]["amount"], o["remaining"]["amount"])
        for o in obligations
    ] == [("0.00", "0.00", "40.00"), ("50.00", "5.00", "5.00")]
    position = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()[
        0
    ]
    assert position["outstandingReceivables"]["amount"] == "45.00"
    assert position["availableCredits"]["amount"] == "20.00"
    assert position["netExposure"]["amount"] == "25.00"
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/credits")
    ).json() == credit_before
    await client.patch(
        f"{base}/participants/{draft['participantId']}", json={"name": "Renamed debtor"}
    )
    assert (await client.get(url)).json() == retained
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == result.json()


async def test_same_date_order_and_confirmed_allocations_survive_backdated_debt(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-20")
    ids = [first["obligation"]["id"], second["obligation"]["id"]]
    body = combined(draft, list(reversed(ids)), "30.00", "20.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    assert [
        (a["kind"], a["obligationId"], a["remainingAfter"]["amount"])
        for a in preview["allocations"]
    ] == [
        ("cash", ids[0], "10.00"),
        ("forgiveness", ids[0], "0.00"),
        ("forgiveness", ids[1], "50.00"),
    ]
    assert (
        await client.post(
            f"{base}/settlements/preview", json={**body, "selectedObligationIds": ids}
        )
    ).json() == preview
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "6"}
    command = {**body, "previewToken": preview["previewToken"]}
    result = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert result.status_code == 201, result.text
    url = f"{base}/settlements/{result.json()['outcome']['resource']['id']}"
    before = (await client.get(url)).json()
    await advance(client, actor, base, draft, "5.00", "2026-10-19")
    assert (await client.get(url)).json() == before
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == result.json()


@pytest.mark.parametrize(
    "invalid",
    [
        "cash_zero",
        "cash_negative",
        "cash_number",
        "cash_precision",
        "cash_excess",
        "forgiven_zero",
        "forgiven_negative",
        "forgiven_precision",
        "forgiven_number",
        "forgiven_excess",
        "currency",
        "reason_empty",
        "reason_missing",
        "selection_empty",
        "selection_duplicate",
        "selection_unknown",
        "selection_other_participant",
        "date",
        "excess",
        "credit",
        "kind",
        "v1",
        "v2",
        "v3",
        "v4",
        "v5",
    ],
)
async def test_invalid_combination_never_changes_financial_state(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = combined(draft, [source["obligation"]["id"]])
    valid = (await client.post(f"{base}/settlements/preview", json=body)).json()
    if invalid.startswith(("cash_", "forgiven_")):
        target = "amount" if invalid.startswith("cash_") else "forgivenessAmount"
        body[target]["amount"] = {
            "zero": "0.00",
            "negative": "-1.00",
            "number": 1,
            "precision": "0.001",
            "excess": "100.01",
        }[invalid.split("_", 1)[1]]
    elif invalid == "currency":
        body["forgivenessAmount"]["currency"] = "USD"
    elif invalid == "reason_empty":
        body["reason"] = "   "
    elif invalid == "reason_missing":
        del body["reason"]
    elif invalid.startswith("selection_"):
        body["selectedObligationIds"] = {
            "empty": [],
            "duplicate": [source["obligation"]["id"]] * 2,
            "unknown": ["00000000-0000-0000-0000-000000000001"],
        }.get(invalid.split("_", 1)[1], [])
        if invalid == "selection_other_participant":
            other = (await client.post(f"{base}/participants", json={"name": "Other"})).json()
            foreign = await advance(
                client, actor, base, {**draft, "participantId": other["id"]}, "100.00", "2026-10-20"
            )
            body["selectedObligationIds"] = [foreign["obligation"]["id"]]
    elif invalid == "date":
        body["transactionDate"] = "2026-10-19"
    elif invalid == "excess":
        body["excessDisposition"] = "retainCredit"
    elif invalid == "credit":
        body["useCredit"] = True
    elif invalid == "kind":
        body["kind"] = "forgiveness"
    paths = ["accounts", "transactions", f"participants/{draft['participantId']}/obligations"]
    before = [(await client.get(f"{base}/{path}")).json() for path in paths]
    if not invalid.startswith("v"):
        preview = await client.post(f"{base}/settlements/preview", json=body)
        assert preview.status_code == 422, preview.text
    headers = {
        **submission_headers(actor.id),
        "Finance-Command-Version": invalid[1:] if invalid.startswith("v") else "6",
    }
    result = await client.post(
        f"{base}/settlements", json={**body, "previewToken": valid["previewToken"]}, headers=headers
    )
    assert result.status_code in (409, 422), result.text
    if result.status_code == 422:
        proof = result.json()["commandValidationRejection"]
        assert proof["commandVersion"] == headers["Finance-Command-Version"]
        assert proof["attemptedBody"] == {**body, "previewToken": valid["previewToken"]}
    assert [(await client.get(f"{base}/{path}")).json() for path in paths] == before
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/settlements")
    ).json() == []


@pytest.mark.parametrize(
    "change",
    [
        "cash",
        "forgiveness",
        "cash_amount",
        "forgiven_amount",
        "selection",
        "reason",
        "date",
        "receiving_balance",
    ],
)
async def test_changed_state_or_intent_requires_new_preview_and_new_submission(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    change: str,
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    body = combined(
        draft, [first["obligation"]["id"], second["obligation"]["id"]], "80.00", "10.00"
    )
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    if change == "cash":
        await confirm(client, actor, base, repayment(draft, "80.00"))
    elif change == "forgiveness":
        other = forgiveness(draft, [second["obligation"]["id"]], "50.00")
        other_preview = (await client.post(f"{base}/settlements/preview", json=other)).json()
        assert (
            await client.post(
                f"{base}/settlements",
                json={**other, "previewToken": other_preview["previewToken"]},
                headers={**submission_headers(actor.id), "Finance-Command-Version": "5"},
            )
        ).status_code == 201
    elif change == "cash_amount":
        body["amount"]["amount"] = "70.00"
    elif change == "forgiven_amount":
        body["forgivenessAmount"]["amount"] = "5.00"
    elif change == "selection":
        body["selectedObligationIds"] = [second["obligation"]["id"]]
    elif change == "reason":
        body["reason"] = "Another explicit reason"
    elif change == "date":
        body["transactionDate"] = "2026-10-23"
    else:
        money = {"amount": "1.00", "currency": "CNY"}
        assert (
            await client.post(
                f"{base}/transactions",
                json={
                    "kind": "income",
                    "accountId": draft["fundingAccountId"],
                    "transactionDate": "2026-10-22",
                    "economicAmount": money,
                    "categoryAllocations": [{"amount": money}],
                },
                headers=submission_headers(actor.id),
            )
        ).status_code == 201
    before = (await client.get(f"{base}/accounts")).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "6"}
    command = {**body, "previewToken": preview["previewToken"]}
    stale = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert (
        stale.status_code == 409 and stale.json()["code"] == "finance_settlement_preview_stale"
    ), stale.text
    assert (await client.post(f"{base}/settlements", json=command, headers=headers)).json()[
        "submissionReceipt"
    ] == stale.json()["submissionReceipt"]
    fresh = await client.post(f"{base}/settlements/preview", json=body)
    if change in ("cash", "forgiveness", "selection"):
        assert fresh.status_code == 422
    else:
        assert fresh.status_code == 200
        renewed = {**body, "previewToken": fresh.json()["previewToken"]}
        conflict = await client.post(f"{base}/settlements", json=renewed, headers=headers)
        assert conflict.json()["code"] == "finance_submission_content_conflict"
        assert (await client.get(f"{base}/accounts")).json() == before
        assert (
            await client.post(
                f"{base}/settlements",
                json=renewed,
                headers={**submission_headers(actor.id), "Finance-Command-Version": "6"},
            )
        ).status_code == 201


@pytest.mark.parametrize("competitor", ["cash", "forgiveness", "combined", "same_key"])
async def test_concurrent_resolution_serializes_cash_and_forgiveness_as_one_action(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    competitor: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    ids = [source["obligation"]["id"]]
    body = combined(draft, ids, "70.00", "10.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "6"}
    other_body = (
        repayment(draft, "80.00")
        if competitor == "cash"
        else forgiveness(draft, ids, "80.00")
        if competitor == "forgiveness"
        else body
    )
    other_preview = (await client.post(f"{base}/settlements/preview", json=other_body)).json()
    other_headers = (
        headers
        if competitor == "same_key"
        else {
            **submission_headers(actor.id),
            "Finance-Command-Version": "1"
            if competitor == "cash"
            else "5"
            if competitor == "forgiveness"
            else "6",
        }
    )
    attempts = [
        (command, headers),
        ({**other_body, "previewToken": other_preview["previewToken"]}, other_headers),
    ]
    results = await asyncio.gather(
        *[client.post(f"{base}/settlements", json=b, headers=h) for b, h in attempts]
    )
    for i, result in enumerate(results):
        if result.status_code == 503 or result.json().get("code") == "finance_submission_busy":
            b, h = attempts[i]
            results[i] = await client.post(f"{base}/settlements", json=b, headers=h)
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
    assert (obligation["cashPaid"]["amount"], obligation["forgiven"]["amount"]) in [
        ("70.00", "10.00"),
        ("80.00", "0.00"),
        ("0.00", "80.00"),
    ]
    assert (
        len((await client.get(f"{base}/participants/{draft['participantId']}/settlements")).json())
        == 1
    )
