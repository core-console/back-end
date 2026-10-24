"""Selected repayment scope through HTTP and guarded PostgreSQL."""

from typing import Any

import pytest
from httpx import AsyncClient

from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_settlements import advance, advance_client, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


async def test_only_second_debt_is_paid_and_confirmed_scope_is_retained(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    selected = [second["obligation"]["id"]]
    body = {**repayment(draft, "60.00"), "selectedObligationIds": selected}
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert [a["obligationId"] for a in preview.json()["allocations"]] == selected
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "4"}
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers=headers,
    )
    assert result.status_code == 201, result.text
    detail: dict[str, Any] = (
        await client.get(f"{base}/settlements/{result.json()['outcome']['resource']['id']}")
    ).json()
    assert detail["selectedObligationIds"] == selected
    assert detail["allocations"] == preview.json()["allocations"]
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert [(o["id"], o["remaining"]["amount"]) for o in obligations] == [
        (first["obligation"]["id"], "40.00"),
        (selected[0], "0.00"),
    ]


@pytest.mark.parametrize(
    "disposition,receipt",
    [("retainCredit", "80.00"), ("acceptIncome", "80.00"), ("retainCredit", "100.00")],
)
async def test_selected_excess_never_spills_and_gross_debt_remains_visible(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    disposition: str,
    receipt: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    body = {**repayment(draft, receipt), "selectedObligationIds": [second["obligation"]["id"]]}
    assert (await client.post(f"{base}/settlements/preview", json=body)).status_code == 422
    body["excessDisposition"] = disposition
    if disposition == "acceptIncome":
        body["categoryAllocations"] = [{"amount": {"amount": "20.00", "currency": "CNY"}}]
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert [a["cashPaid"]["amount"] for a in preview.json()["allocations"]] == ["60.00"]
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "4"}
    command = {**body, "previewToken": preview.json()["previewToken"]}
    result = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert result.status_code == 201, result.text
    detail = (
        await client.get(f"{base}/settlements/{result.json()['outcome']['resource']['id']}")
    ).json()
    assert detail["selectedObligationIds"] == body["selectedObligationIds"]
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == result.json()
    position = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()[
        0
    ]
    assert position["outstandingReceivables"]["amount"] == "40.00"
    assert position["availableCredits"]["amount"] == (
        "0.00" if disposition == "acceptIncome" else "40.00" if receipt == "100.00" else "20.00"
    )
    assert position["netExposure"]["amount"] == (
        "40.00" if disposition == "acceptIncome" else "0.00" if receipt == "100.00" else "20.00"
    )
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"]["amount"] == receipt
    overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert overview["monthSummaryByCurrency"][0]["income"]["amount"] == (
        "20.00" if disposition == "acceptIncome" else "0.00"
    )
    for transaction_id in detail["transactionIds"]:
        assert (await client.delete(f"{base}/transactions/{transaction_id}")).json()[
            "code"
        ] == "finance_linked_record_protected"


async def test_expansion_and_competing_payment_require_new_preview_without_silent_scope_change(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    body = {
        **repayment(draft, "80.00"),
        "selectedObligationIds": [second["obligation"]["id"]],
        "excessDisposition": "retainCredit",
    }
    original = (await client.post(f"{base}/settlements/preview", json=body)).json()
    expanded = {
        **body,
        "selectedObligationIds": [second["obligation"]["id"], first["obligation"]["id"]],
    }
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "4"}
    stale = await client.post(
        f"{base}/settlements",
        json={**expanded, "previewToken": original["previewToken"]},
        headers=headers,
    )
    assert stale.status_code == 409 and stale.json()["code"] == "finance_settlement_preview_stale"
    refreshed = (await client.post(f"{base}/settlements/preview", json=expanded)).json()
    assert [a["cashPaid"]["amount"] for a in refreshed["allocations"]] == ["40.00", "40.00"]
    assert refreshed["retainedCredit"]["amount"] == "0.00"
    # Selection order is intent-as-a-set; FIFO still follows source dates.
    reversed_scope = {
        **expanded,
        "selectedObligationIds": [first["obligation"]["id"], second["obligation"]["id"]],
    }
    assert (
        await client.post(f"{base}/settlements/preview", json=reversed_scope)
    ).json() == refreshed
    done = await client.post(
        f"{base}/settlements",
        json={**expanded, "previewToken": refreshed["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "4"},
    )
    assert done.status_code == 201, done.text
    changed = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": original["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "4"},
    )
    assert (
        changed.status_code == 409 and changed.json()["code"] == "finance_settlement_preview_stale"
    )
    assert (
        len((await client.get(f"{base}/participants/{draft['participantId']}/settlements")).json())
        == 1
    )


@pytest.mark.parametrize(
    "invalid",
    ["empty", "duplicate", "foreign_participant", "currency", "date", "unknown", "v1", "v2", "v3"],
)
async def test_selected_scope_rejects_ineligible_or_legacy_commands(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    invalid: str,
) -> None:
    from uuid import uuid4

    client, actor, base, draft = advance_client
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    selected = [second["obligation"]["id"]]
    body = {**repayment(draft, "50.00"), "selectedObligationIds": selected}
    if invalid == "empty":
        body["selectedObligationIds"] = []
    elif invalid == "duplicate":
        body["selectedObligationIds"] = selected * 2
    elif invalid == "unknown":
        body["selectedObligationIds"] = [str(uuid4())]
    elif invalid == "foreign_participant":
        person = (await client.post(f"{base}/participants", json={"name": "Other"})).json()
        body["participantId"] = person["id"]
    elif invalid == "currency":
        body["amount"] = {"amount": "50.00", "currency": "USD"}
    elif invalid == "date":
        body["transactionDate"] = "2026-10-20"
    before = (await client.get(f"{base}/accounts")).json()
    response = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": "0" * 64},
        headers={
            **submission_headers(actor.id),
            "Finance-Command-Version": invalid[1:] if invalid.startswith("v") else "4",
        },
    )
    assert response.status_code in (409, 422), response.text
    assert (await client.get(f"{base}/accounts")).json() == before
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/settlements")
    ).json() == []
