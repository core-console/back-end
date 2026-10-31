"""Original-fact Advance corrections through HTTP and guarded PostgreSQL."""

from typing import Any, cast

import pytest
from httpx import AsyncClient

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_credit_applications import application, apply_credit, retain_credit
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


def money(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "CNY"}


async def correct(
    client: AsyncClient, actor: User, base: str, body: dict[str, Any]
) -> dict[str, Any]:
    plan = await client.post(f"{base}/advance-corrections/preview", json=body)
    assert plan.status_code == 200, plan.text
    receipt = await client.post(
        f"{base}/advance-corrections",
        json={**body, "previewToken": plan.json()["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert receipt.status_code == 201, receipt.text
    response = await client.get(
        f"{base}/advance-corrections/{receipt.json()['outcome']['resource']['id']}"
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def correction(
    source: dict[str, Any], *, amount: str = "100.00", share: str = "80.00"
) -> dict[str, Any]:
    return {
        "advanceId": source["id"],
        "reason": "Incorrect original shares",
        "fundingAccountId": source["fundingAccountId"],
        "transactionDate": source["transactionDate"],
        "note": source["note"],
        "amount": {"amount": amount, "currency": "CNY"},
        "ownerShare": {"amount": "20.00", "currency": "CNY"},
        "participantShares": [
            {
                "participantId": source["obligation"]["participantId"],
                "amount": {"amount": share, "currency": "CNY"},
            }
        ],
    }


async def test_share_correction_restates_expense_without_changing_truthful_funding(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = correction(source)
    preview = await client.post(f"{base}/advance-corrections/preview", json=body)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["before"]["amount"]["amount"] == "100.00"
    assert plan["after"]["ownerShare"]["amount"] == "20.00"
    assert plan["fundingChanged"] is False
    assert plan["accountEffects"][0]["balanceAfter"]["amount"] == "0.00"
    headers = submission_headers(actor.id)
    result = await client.post(
        f"{base}/advance-corrections",
        json={**body, "previewToken": plan["previewToken"]},
        headers=headers,
    )
    assert result.status_code == 201, result.text
    current = (await client.get(f"{base}/advances/{source['id']}")).json()
    assert current["id"] == source["id"]
    assert current["obligations"][0]["remaining"]["amount"] == "80.00"
    assert current["obligations"][0]["id"] == source["obligation"]["id"]
    summary = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert summary["monthSummaryByCurrency"][0]["expense"]["amount"] == "20.00"
    assert summary["monthSummaryByCurrency"][0]["income"]["amount"] == "0.00"
    history = (await client.get(f"{base}/advances/{source['id']}/corrections")).json()
    assert history[0]["before"]["ownerShare"]["amount"] == "0.00"
    assert history[0]["after"]["ownerShare"]["amount"] == "20.00"
    assert history[0]["reason"] == body["reason"]


async def test_correct_all_shares_to_owner_preserves_zero_debt_attribution_and_history(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    detail = await correct(
        client,
        actor,
        base,
        {**correction(source), "ownerShare": money("100.00"), "participantShares": []},
    )
    assert detail["after"]["obligations"][0]["remaining"] == money("0.00")
    assert detail["after"]["obligations"][0]["participantId"] == draft["participantId"]
    assert len(detail["financialEffectsAfter"]) == 1
    assert detail["financialEffectsAfter"][0]["kind"] == "expense"
    summary = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert summary["monthSummaryByCurrency"][0]["expense"] == money("100.00")
    assert (await client.get(f"{base}/advances/{source['id']}")).json() == detail["after"]


async def test_correct_funding_amount_account_and_date_restates_original_months(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Card",
            "nature": "liability",
            "currency": "CNY",
            "openingBalance": money("0.00"),
            "trackingStartDate": "2026-09-01",
        },
    )
    result = await correct(
        client,
        actor,
        base,
        {
            **correction(source, amount="120.00", share="100.00"),
            "fundingAccountId": account.json()["id"],
            "transactionDate": "2026-09-20",
            "reason": "Wrong original amount, Account and date",
        },
    )
    assert result["fundingChanged"] is True
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"] == money("100.00")
    assert accounts[account.json()["id"]]["currentBalance"] == money("120.00")
    assert accounts[source["obligation"]["receivableAccountId"]]["currentBalance"] == money(
        "100.00"
    )
    september = (await client.get(f"{base}/overview", params={"month": "2026-09"})).json()
    october = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert september["monthSummaryByCurrency"][0]["expense"] == money("20.00")
    assert october["monthSummaryByCurrency"][0]["expense"] == money("0.00")
    assert all(t["transactionDate"] == "2026-09-20" for t in result["financialEffectsAfter"])


async def test_correct_attribution_repeatedly_retains_original_and_intermediate_history(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    other = (await client.post(f"{base}/participants", json={"name": "Blair"})).json()
    body = {
        **correction(source),
        "participantShares": [{"participantId": other["id"], "amount": money("80.00")}],
        "reason": "Wrong original debtor",
    }
    first = await correct(client, actor, base, body)
    debts = {o["participantId"]: o for o in first["after"]["obligations"]}
    assert debts[str(draft["participantId"])]["remaining"] == money("0.00")
    assert debts[other["id"]]["remaining"] == money("80.00")
    await client.post(f"{base}/participants/{other['id']}/archive")
    second = await correct(client, actor, base, correction(source))
    history = (await client.get(f"{base}/advances/{source['id']}/corrections")).json()
    assert history == [first, second]
    assert [h["revision"] for h in history] == [1, 2]
    assert history[0]["before"] == source
    assert second["before"] == first["after"]
    assert second["after"]["confirmedAt"] == source["confirmedAt"]
    assert second["actorId"] == str(actor.id)
    assert (
        next(
            o
            for o in second["after"]["obligations"]
            if o["participantId"] == str(draft["participantId"])
        )["id"]
        == source["obligation"]["id"]
    )


@pytest.mark.parametrize("activity", ["cash", "forgiveness", "credit"])
async def test_downstream_activity_rejects_preview_and_confirmation_without_changes(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    activity: str,
) -> None:
    client, actor, base, draft = advance_client
    if activity == "credit":
        await retain_credit(client, actor, base, draft, "50.00")
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = correction(source)
    plan = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    if activity == "cash":
        await confirm(client, actor, base, repayment(draft, "10.00"))
    elif activity == "credit":
        await apply_credit(
            client, actor, base, application(draft, [source["obligation"]["id"]], "10.00")
        )
    else:
        forgiveness = {
            "kind": "forgiveness",
            "participantId": draft["participantId"],
            "transactionDate": "2026-10-22",
            "amount": money("10.00"),
            "selectedObligationIds": [source["obligation"]["id"]],
            "reason": "Forgive ten",
        }
        p = await client.post(f"{base}/settlements/preview", json=forgiveness)
        assert p.status_code == 200, p.text
        r = await client.post(
            f"{base}/settlements",
            json={**forgiveness, "previewToken": p.json()["previewToken"]},
            headers={**submission_headers(actor.id), "Finance-Command-Version": "5"},
        )
        assert r.status_code == 201, r.text
    before = (await client.get(f"{base}/advances/{source['id']}")).json()
    rejected = await client.post(f"{base}/advance-corrections/preview", json=body)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "finance_advance_correction_ineligible"
    headers = submission_headers(actor.id)
    command = {**body, "previewToken": plan["previewToken"]}
    rejected = await client.post(f"{base}/advance-corrections", json=command, headers=headers)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    assert (
        await client.post(f"{base}/advance-corrections", json=command, headers=headers)
    ).json() == rejected.json()
    assert (await client.get(f"{base}/advances/{source['id']}")).json() == before
    assert (await client.get(f"{base}/advances/{source['id']}/corrections")).json() == []


async def test_stale_incomplete_and_replayed_plans_require_complete_reconfirmation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = correction(source)
    plan = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    headers = submission_headers(actor.id)
    invalid = await client.post(
        f"{base}/advance-corrections",
        json={**body, "ownerShare": money("19.00"), "previewToken": plan["previewToken"]},
        headers=headers,
    )
    assert invalid.status_code == 422, invalid.text
    assert (
        invalid.json()["commandValidationRejection"]["operation"]
        == "createFinanceAdvanceCorrection"
    )
    await client.patch(f"{base}/participants/{draft['participantId']}", json={"name": "Renamed"})
    stale = await client.post(
        f"{base}/advance-corrections",
        json={**body, "previewToken": plan["previewToken"]},
        headers=headers,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "finance_advance_correction_preview_stale"
    assert stale.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    fresh = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    conflict = await client.post(
        f"{base}/advance-corrections",
        json={**body, "previewToken": fresh["previewToken"]},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "finance_submission_content_conflict"
    detail = await correct(client, actor, base, body)
    assert detail["after"]["ownerShare"] == money("20.00")


async def test_removed_archived_participant_cannot_receive_new_debt_via_correction(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    await correct(
        client,
        actor,
        base,
        {**correction(source), "ownerShare": money("100.00"), "participantShares": []},
    )
    await client.post(f"{base}/participants/{draft['participantId']}/archive")
    rejected = await client.post(f"{base}/advance-corrections/preview", json=correction(source))
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "finance_participant_archived"


async def test_shared_correction_restates_all_effects_and_any_participant_settlement_blocks_it(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    other = (await client.post(f"{base}/participants", json={"name": "Blair"})).json()
    shared = {
        "fundingAccountId": draft["fundingAccountId"],
        "transactionDate": "2026-10-20",
        "amount": money("300.00"),
        "ownerShare": money("100.00"),
        "participantShares": [
            {"participantId": draft["participantId"], "amount": money("120.00")},
            {"participantId": other["id"], "amount": money("80.00")},
        ],
    }
    preview = (await client.post(f"{base}/advances/preview", json=shared)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    created = await client.post(
        f"{base}/advances",
        json={**shared, "previewToken": preview["previewToken"]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    source = (
        await client.get(f"{base}/advances/{created.json()['outcome']['resource']['id']}")
    ).json()
    body = {
        **shared,
        "advanceId": source["id"],
        "reason": "Correct original funding and both shares",
        "amount": money("250.00"),
        "ownerShare": money("20.00"),
        "participantShares": [
            {"participantId": draft["participantId"], "amount": money("120.00")},
            {"participantId": other["id"], "amount": money("110.00")},
        ],
    }
    detail = await correct(client, actor, base, body)
    assert len(detail["financialEffectsBefore"]) == len(detail["financialEffectsAfter"]) == 3
    assert {o["id"] for o in source["obligations"]} == {
        o["id"] for o in detail["after"]["obligations"]
    }
    debts = {o["participantId"]: o for o in detail["after"]["obligations"]}
    assert debts[str(draft["participantId"])]["remaining"] == money("120.00")
    assert debts[other["id"]]["remaining"] == money("110.00")
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"] == money("-150.00")
    await confirm(client, actor, base, repayment({**draft, "participantId": other["id"]}, "10.00"))
    rejected = await client.post(f"{base}/advance-corrections/preview", json=body)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "finance_advance_correction_ineligible"
    assert (await client.get(f"{base}/advances/{source['id']}/corrections")).json() == [detail]
