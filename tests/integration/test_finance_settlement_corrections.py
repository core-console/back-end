"""Cash receipt corrections through HTTP and guarded PostgreSQL observations."""

from typing import Any, cast

import pytest
from httpx import AsyncClient

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_credit_applications import application, apply_credit, retain_credit
from integration.test_finance_settlements import advance, advance_client, confirm, repayment
from integration.test_finance_surplus import accepted, confirm_income

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


def money(amount: str, currency: str = "CNY") -> dict[str, str]:
    return {"amount": amount, "currency": currency}


def correction(
    source: dict[str, Any], amount: str = "70.00", disposition: str = "none"
) -> dict[str, Any]:
    return {
        "settlementId": source["id"],
        "amount": money(amount),
        "reason": "Wrong original cash receipt amount",
        "excessDisposition": disposition,
        "categoryId": None,
    }


async def correct(
    client: AsyncClient, actor: User, base: str, body: dict[str, Any]
) -> dict[str, Any]:
    preview = await client.post(f"{base}/settlement-corrections/preview", json=body)
    assert preview.status_code == 200, preview.text
    receipt = await client.post(
        f"{base}/settlement-corrections",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["outcome"]["resource"]["type"] == "settlementCorrection"
    detail = await client.get(
        f"{base}/settlement-corrections/{receipt.json()['outcome']['resource']['id']}"
    )
    assert detail.status_code == 200, detail.text
    return cast(dict[str, Any], detail.json())


async def test_receipt_100_to_70_releases_reverse_original_order_and_preserves_history(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    source = await confirm(client, actor, base, repayment(draft, "100.00"))
    other = await advance(client, actor, base, draft, "20.00", "2026-10-21")
    unrelated = await confirm(client, actor, base, repayment(draft, "10.00"))
    detail = await correct(client, actor, base, correction(source))
    assert [a["cashPaid"] for a in detail["after"]["allocations"]] == [
        money("40.00"),
        money("30.00"),
    ]
    assert [a["originalCashPaid"] for a in detail["after"]["allocations"]] == [
        money("40.00"),
        money("60.00"),
    ]
    assert [o["remaining"] for o in detail["obligationsAfter"]] == [money("0.00"), money("30.00")]
    assert all(o["forgiven"] == money("0.00") for o in detail["obligationsAfter"])
    assert (await client.get(f"{base}/settlements/{unrelated['id']}")).json() == unrelated
    current = (await client.get(f"{base}/settlements/{source['id']}")).json()
    assert current["confirmedAt"] == source["confirmedAt"]
    assert current["confirmationOrder"] == source["confirmationOrder"]
    assert current["amount"] == money("70.00")
    assert [a["cashPaid"] for a in current["allocations"]] == [money("40.00"), money("30.00")]
    for src, remaining in [(first, "0.00"), (second, "30.00"), (other, "10.00")]:
        assert (await client.get(f"{base}/advances/{src['id']}")).json()["obligation"][
            "remaining"
        ] == money(remaining)
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"] == money("60.00")
    assert accounts[second["obligation"]["receivableAccountId"]]["currentBalance"] == money("30.00")
    history = (await client.get(f"{base}/settlements/{source['id']}/corrections")).json()
    assert history == [detail]
    assert detail["before"]["amount"] == money("100.00")
    assert detail["actorId"] == str(actor.id)
    assert all(
        e["transactionDate"] == source["transactionDate"] for e in detail["financialEffectsAfter"]
    )


@pytest.mark.parametrize("nature, expected", [("asset", "70.00"), ("liability", "-70.00")])
async def test_receipt_80_to_70_restates_account_signs_and_reopens_unforgiven_debt(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    nature: str,
    expected: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Receipt account",
            "nature": nature,
            "currency": "CNY",
            "openingBalance": money("0.00"),
            "trackingStartDate": "2026-10-01",
        },
    )
    receipt = await confirm(
        client,
        actor,
        base,
        {**repayment(draft, "80.00"), "receivingAccountId": account.json()["id"]},
    )
    detail = await correct(client, actor, base, correction(receipt))
    debt = (await client.get(f"{base}/advances/{source['id']}")).json()["obligation"]
    assert debt["cashPaid"] == money("70.00")
    assert debt["remaining"] == money("30.00")
    assert debt["forgiven"] == debt["creditApplied"] == money("0.00")
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[account.json()["id"]]["currentBalance"] == money(expected)
    assert accounts[debt["receivableAccountId"]]["currentBalance"] == money("30.00")
    assert all(e["transactionDate"] == "2026-10-22" for e in detail["financialEffectsAfter"])
    month = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    summary = month["monthSummaryByCurrency"][0]
    assert summary["income"] == summary["expense"] == money("0.00")
    positions = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()
    assert positions[0]["outstandingReceivables"] == money("30.00")
    assert positions[0]["availableCredits"] == money("0.00")


async def test_repeated_full_release_restore_and_credit_reduction_keep_original_allocations(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "40.00", "2026-10-20")
    await advance(client, actor, base, draft, "60.00", "2026-10-21")
    source = await confirm(client, actor, base, repayment(draft, "100.00"))
    history = []
    for amount, disposition, paid in [
        ("20.00", "none", ["20.00", "0.00"]),
        ("90.00", "none", ["40.00", "50.00"]),
        ("110.00", "retainCredit", ["40.00", "60.00"]),
        ("95.00", "none", ["40.00", "55.00"]),
    ]:
        detail = await correct(client, actor, base, correction(source, amount, disposition))
        history.append(detail)
        assert [a["cashPaid"]["amount"] for a in detail["after"]["allocations"]] == paid
        assert [a["originalCashPaid"]["amount"] for a in detail["after"]["allocations"]] == [
            "40.00",
            "60.00",
        ]
    assert [h["revision"] for h in history] == [1, 2, 3, 4]
    assert history[1]["before"] == history[0]["after"]
    assert (await client.get(f"{base}/settlements/{source['id']}/corrections")).json() == history
    credits = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    assert len(credits) == 1
    assert credits[0]["amount"] == credits[0]["available"] == money("0.00")
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[credits[0]["liabilityAccountId"]]["currentBalance"] == money("0.00")


@pytest.mark.parametrize("disposition", ["retainCredit", "acceptIncome"])
async def test_increased_receipt_requires_excess_disposition_and_never_expands_confirmed_scope(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    disposition: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "40.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "40.00"))
    other = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    incomplete = await client.post(
        f"{base}/settlement-corrections/preview", json=correction(source, "50.00")
    )
    assert incomplete.status_code == 422, incomplete.text
    detail = await correct(client, actor, base, correction(source, "50.00", disposition))
    assert [a["cashPaid"] for a in detail["after"]["allocations"]] == [money("40.00")]
    assert (await client.get(f"{base}/advances/{other['id']}")).json() == other
    field = "retainedCredit" if disposition == "retainCredit" else "acceptedSurplus"
    assert detail["after"][field] == money("10.00")
    month = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert month["monthSummaryByCurrency"][0]["income"] == money(
        "10.00" if disposition == "acceptIncome" else "0.00"
    )
    assert month["monthSummaryByCurrency"][0]["expense"] == money("0.00")


async def test_surplus_income_is_restated_at_original_date_and_can_be_removed_without_expense(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm_income(client, actor, base, accepted(draft))
    first = await correct(client, actor, base, correction(source, "101.00", "acceptIncome"))
    assert first["after"]["acceptedSurplus"] == money("1.00")
    second = await correct(client, actor, base, correction(source, "99.00"))
    assert second["before"]["acceptedSurplus"] == money("1.00")
    assert second["after"]["acceptedSurplus"] == money("0.00")
    assert second["obligationsAfter"][0]["remaining"] == money("1.00")
    month = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert month["monthSummaryByCurrency"][0]["income"] == money("0.00")
    assert month["monthSummaryByCurrency"][0]["expense"] == money("0.00")
    assert (
        await client.get(f"{base}/transactions/{source['incomeTransactionId']}")
    ).status_code == 404


async def test_credit_only_receipt_correction_preserves_origin_identity_and_order(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await retain_credit(client, actor, base, draft, "50.00")
    detail = await correct(client, actor, base, correction(source, "40.00", "retainCredit"))
    assert detail["after"]["allocations"] == []
    current = (await client.get(f"{base}/settlements/{source['id']}")).json()
    assert current["creditOrigin"]["transactionId"] == source["creditOrigin"]["transactionId"]
    assert current["creditOrigin"]["confirmationOrder"] == source["confirmationOrder"]
    assert current["creditOrigin"]["available"] == money("40.00")


async def test_corrected_credit_keeps_oldest_origin_selection_for_later_explicit_use(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    first = await retain_credit(client, actor, base, draft, "110.00")
    second = await retain_credit(client, actor, base, draft, "20.00")
    debt = await advance(client, actor, base, draft, "30.00", "2026-10-21")
    await correct(client, actor, base, correction(first, "105.00", "retainCredit"))
    before = (await client.get(f"{base}/accounts")).json()
    result = await apply_credit(
        client, actor, base, application(draft, [debt["obligation"]["id"]], "10.00")
    )
    assert [(c["originSettlementId"], c["amount"]) for c in result["origins"]] == [
        (first["id"], money("5.00")),
        (second["id"], money("5.00")),
    ]
    after = (await client.get(f"{base}/accounts")).json()
    cash_before = next(a for a in before if a["id"] == str(draft["fundingAccountId"]))
    cash_after = next(a for a in after if a["id"] == str(draft["fundingAccountId"]))
    assert cash_before["currentBalance"] == cash_after["currentBalance"]


async def test_selected_scope_and_reason_are_retained_without_expanding_to_unselected_debt(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    body = {**repayment(draft, "60.00"), "selectedObligationIds": [second["obligation"]["id"]]}
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    created = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "4"},
    )
    assert created.status_code == 201, created.text
    source = (
        await client.get(f"{base}/settlements/{created.json()['outcome']['resource']['id']}")
    ).json()
    detail = await correct(
        client,
        actor,
        base,
        {**correction(source, "70.00", "retainCredit"), "reason": "\U0001f600" * 500},
    )
    assert detail["selectedObligationIds"] == body["selectedObligationIds"]
    assert detail["reason"] == "\U0001f600" * 500
    assert detail["after"]["retainedCredit"] == money("10.00")
    assert (await client.get(f"{base}/advances/{first['id']}")).json() == first
