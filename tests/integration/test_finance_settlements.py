"""Accumulated cash repayment through HTTP and guarded PostgreSQL observations."""

import asyncio
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceTransaction,
)
from core_console.modules.finance.settlement_models import (
    FinanceSettlement,
    FinanceSettlementAllocation,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_advances import advance_client
from integration.test_finance_api import finance_client

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


async def advance(
    client: AsyncClient, actor: User, base: str, draft: dict[str, object], amount: str, day: str
) -> dict[str, Any]:
    body = {
        **draft,
        "amount": {"amount": amount, "currency": "CNY"},
        "participantShare": {"amount": amount, "currency": "CNY"},
        "transactionDate": day,
    }
    preview = await client.post(f"{base}/advances/preview", json=body)
    assert preview.status_code == 200, preview.text
    result = await client.post(
        f"{base}/advances",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert result.status_code == 201, result.text
    source_id = result.json()["outcome"]["resource"]["id"]
    return cast(dict[str, Any], (await client.get(f"{base}/advances/{source_id}")).json())


def repayment(draft: dict[str, object], amount: str = "50.00") -> dict[str, object]:
    return {
        "participantId": draft["participantId"],
        "receivingAccountId": draft["fundingAccountId"],
        "transactionDate": "2026-10-22",
        "amount": {"amount": amount, "currency": "CNY"},
        "note": "Accumulated repayment",
    }


async def test_cash_50_resolves_40_then_10_and_preserves_partial_source(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    body = repayment(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    allocations = preview.json()["allocations"]
    assert [
        (a["obligationId"], a["cashPaid"]["amount"], a["remainingAfter"]["amount"])
        for a in allocations
    ] == [
        (first["obligation"]["id"], "40.00", "0.00"),
        (second["obligation"]["id"], "10.00", "50.00"),
    ]
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert result.status_code == 201, result.text
    resource = result.json()["outcome"]["resource"]
    assert resource["type"] == "settlement"
    detail = (await client.get(f"{base}/settlements/{resource['id']}")).json()
    assert detail["allocations"] == allocations
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert [o["remaining"]["amount"] for o in obligations] == ["0.00", "50.00"]
    assert [o["cashPaid"]["amount"] for o in obligations] == ["40.00", "10.00"]
    source = (await client.get(f"{base}/advances/{second['id']}")).json()
    assert source["obligation"] == obligations[1]
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"]["amount"] == "50.00"
    assert (
        accounts[second["obligation"]["receivableAccountId"]]["currentBalance"]["amount"] == "50.00"
    )
    assert all(
        t["kind"] == "internalTransfer"
        for t in (await client.get(f"{base}/transactions")).json()["items"]
    )
    history = (await client.get(f"{base}/participants/{draft['participantId']}/settlements")).json()
    assert history == [detail]


async def test_competing_receipt_requires_reconfirmation_even_when_capacity_is_now_too_small(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft, "60.00")
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {**body, "previewToken": preview.json()["previewToken"]}
    first = await client.post(
        f"{base}/settlements", json=command, headers=submission_headers(actor.id)
    )
    assert first.status_code == 201, first.text
    headers = submission_headers(actor.id)
    stale = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "finance_settlement_preview_stale"
    assert stale.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    replay = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert replay.json()["submissionReceipt"] == stale.json()["submissionReceipt"]


async def confirm(
    client: AsyncClient, actor: User, base: str, body: dict[str, object]
) -> dict[str, Any]:
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert result.status_code == 201, result.text
    id = result.json()["outcome"]["resource"]["id"]
    return cast(dict[str, Any], (await client.get(f"{base}/settlements/{id}")).json())


async def test_same_date_ties_eligibility_backdating_and_retained_cash_history(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-20")
    future = await advance(client, actor, base, draft, "10.00", "2026-11-01")
    body = repayment(draft, "98.00")
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert (await client.post(f"{base}/settlements/preview", json=body)).json() == preview.json()
    detail = await confirm(client, actor, base, body)
    assert [(a["obligationId"], a["cashPaid"]["amount"]) for a in detail["allocations"]] == [
        (first["obligation"]["id"], "40.00"),
        (second["obligation"]["id"], "58.00"),
    ]
    assert detail["allocations"][1]["remainingAfter"]["amount"] == "2.00"
    backdated = await advance(client, actor, base, draft, "5.00", "2026-10-19")
    assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
    # Earlier event dates appear chronologically without replaying the first receipt.
    earlier = await confirm(
        client, actor, base, {**repayment(draft, "3.00"), "transactionDate": "2026-10-19"}
    )
    assert earlier["allocations"][0]["obligationId"] == backdated["obligation"]["id"]
    assert earlier["confirmationOrder"] > detail["confirmationOrder"]
    history = (await client.get(f"{base}/participants/{draft['participantId']}/settlements")).json()
    assert [s["id"] for s in history] == [earlier["id"], detail["id"]]
    assert (await client.get(f"{base}/advances/{future['id']}")).json()["obligation"]["cashPaid"][
        "amount"
    ] == "0.00"
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert [o["remaining"]["amount"] for o in obligations] == ["2.00", "0.00", "2.00", "10.00"]
    assert all(
        o["forgiven"]["amount"] == o["creditApplied"]["amount"] == "0.00" for o in obligations
    )


@pytest.mark.parametrize(
    "phase", ["admission_ack", "partial_write", "before_commit", "terminal_ack", "response_loss"]
)
async def test_grouped_receipt_faults_recover_original_action_without_duplicate_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "40.00", "2026-10-20")
    await advance(client, actor, base, draft, "60.00", "2026-10-21")
    body = repayment(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {**body, "previewToken": preview.json()["previewToken"]}
    headers = submission_headers(actor.id)
    real_commit, real_flush = AsyncSession.commit, AsyncSession.flush
    commits = 0

    async def commit_fault(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_commit":
            raise ConnectionError("terminal commit never reached PostgreSQL")
        await real_commit(session)
        if commits == (1 if phase == "admission_ack" else 2):
            raise ConnectionError("PostgreSQL acknowledgement lost")

    async def flush_fault(session: AsyncSession, objects: Any = None) -> None:
        partial = any(isinstance(row, FinanceSettlementAllocation) for row in session.new)
        await real_flush(session, objects)
        if partial:
            raise RuntimeError("first Allocation and transfer flushed; second not written")

    handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await handle(transport, request)
        assert response.status_code == 201
        raise ReadError("committed response lost", request=request)

    with monkeypatch.context() as faults:
        if phase == "response_loss":
            faults.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/settlements", json=command, headers=headers)
        else:
            faults.setattr(
                AsyncSession,
                "flush" if phase == "partial_write" else "commit",
                flush_fault if phase == "partial_write" else commit_fault,
            )
            failed = await client.post(f"{base}/settlements", json=command, headers=headers)
            assert failed.status_code == 500, failed.text
            assert "submissionReceipt" not in failed.json()
            assert "commandValidationRejection" not in failed.json()
    committed = phase in ("terminal_ack", "response_loss")
    counts = [
        await postgres_session.scalar(select(func.count()).select_from(model))
        for model in (
            FinanceSettlement,
            FinanceSettlementAllocation,
            FinanceTransaction,
            FinanceAccountMovement,
        )
    ]
    assert counts == ([1, 2, 4, 8] if committed else [0, 0, 2, 4])
    effects = (
        (
            await postgres_session.execute(
                select(FinanceAccountMovement.amount).where(
                    FinanceAccountMovement.account_id == UUID(str(draft["fundingAccountId"]))
                )
            )
        )
        .scalars()
        .all()
    )
    assert sum(effects) == (Decimal("-50.00") if committed else Decimal("-100.00"))
    binding = await postgres_session.get(
        FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
    )
    assert binding is not None
    assert (binding.terminal_outcome is not None) == committed
    await postgres_session.rollback()
    lookup = await client.get(
        f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
    )
    assert lookup.headers["cache-control"] == "no-store"
    assert lookup.json()["state"] == ("terminal" if committed else "unfinished")
    retry = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert retry.status_code == 201, retry.text
    if committed:
        assert retry.json() == lookup.json()["receipt"]
    else:
        assert retry.json()["admittedAt"] == lookup.json()["admittedAt"]
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == retry.json()
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceSettlement)) == 1
    assert (
        await postgres_session.scalar(select(func.count()).select_from(FinanceSettlementAllocation))
        == 2
    )


@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_receipts_serialize_without_double_spending_capacity(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    same_key: bool,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft, "98.00")
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {**body, "previewToken": preview.json()["previewToken"]}
    first_headers = submission_headers(actor.id)
    second_headers = first_headers if same_key else submission_headers(actor.id)
    responses = await asyncio.gather(
        *[
            client.post(f"{base}/settlements", json=command, headers=h)
            for h in (first_headers, second_headers)
        ]
    )
    for index, response in enumerate(responses):
        if response.status_code == 503 or response.json().get("code") == "finance_submission_busy":
            responses[index] = await client.post(
                f"{base}/settlements", json=command, headers=(first_headers, second_headers)[index]
            )
    if same_key:
        assert responses[0].status_code == responses[1].status_code == 201
        assert responses[0].json() == responses[1].json()
    else:
        assert sorted(r.status_code for r in responses) == [201, 409]
        rejected = next(r for r in responses if r.status_code == 409)
        assert rejected.json()["code"] == "finance_settlement_preview_stale"
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert obligations[0]["cashPaid"]["amount"] == "98.00"
    assert obligations[0]["remaining"]["amount"] == "2.00"


async def test_execution_lock_timeout_remains_unfinished_and_retryable(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {**body, "previewToken": preview.json()["previewToken"]}
    headers = submission_headers(actor.id)
    await postgres_session.scalar(
        select(FinanceAccount)
        .where(FinanceAccount.id == UUID(str(draft["fundingAccountId"])))
        .with_for_update()
    )
    waiting = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert waiting.status_code == 409, waiting.text
    assert waiting.json()["code"] == "finance_submission_busy"
    assert "submissionReceipt" not in waiting.json()
    await postgres_session.rollback()
    lookup = await client.get(
        f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
    )
    assert lookup.json()["state"] == "unfinished"
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).status_code == 201


@pytest.mark.parametrize(
    "invalid",
    [
        "zero",
        "negative",
        "precision",
        "float",
        "excess",
        "no_debt",
        "date",
        "early",
        "currency",
        "subset",
        "forgiveness",
        "credit",
        "participant",
        "account",
    ],
)
async def test_invalid_cash_intent_has_no_effect_and_only_correlated_evidence(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    if invalid != "no_debt":
        await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft)
    mutations: dict[str, dict[str, object]] = {
        "zero": {"amount": {"amount": "0.00", "currency": "CNY"}},
        "negative": {"amount": {"amount": "-1.00", "currency": "CNY"}},
        "precision": {"amount": {"amount": "1.001", "currency": "CNY"}},
        "float": {"amount": {"amount": 1.25, "currency": "CNY"}},
        "excess": {"amount": {"amount": "100.01", "currency": "CNY"}},
        "no_debt": {},
        "date": {"transactionDate": "2026-02-30"},
        "early": {"transactionDate": "2026-09-30"},
        "currency": {"amount": {"amount": "1.00", "currency": "USD"}},
        "subset": {"obligationIds": []},
        "forgiveness": {"forgiven": {"amount": "1.00", "currency": "CNY"}},
        "credit": {"useCredit": True},
        "participant": {"participantId": str(uuid4())},
        "account": {"receivingAccountId": str(uuid4())},
    }
    body.update(mutations[invalid])
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code in (404, 422), preview.text
    command = {**body, "previewToken": "0" * 64}
    headers = submission_headers(actor.id)
    rejected = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert rejected.status_code in (404, 409, 422), rejected.text
    proof = rejected.json().get("commandValidationRejection")
    if proof is not None:
        assert proof["operation"] == "createFinanceSettlement"
        assert proof["attemptedBody"] == command
        assert proof["submissionId"] == headers["Idempotency-Key"]
    else:
        assert rejected.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/settlements")
    ).json() == []


async def test_archived_participant_can_repay_and_linked_effects_remain_protected(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    assert (
        await client.post(f"{base}/participants/{draft['participantId']}/archive")
    ).status_code == 200
    detail = await confirm(client, actor, base, repayment(draft, "98.00"))
    money = {"amount": "1.00", "currency": "CNY"}
    receivable = source["obligation"]["receivableAccountId"]
    transfer = {
        "kind": "internalTransfer",
        "sourceAccountId": receivable,
        "destinationAccountId": draft["fundingAccountId"],
        "transactionDate": "2026-10-22",
        "amount": money,
    }
    transaction_id = detail["transactionIds"][0]
    expense = {
        "kind": "expense",
        "accountId": receivable,
        "transactionDate": "2026-10-22",
        "economicAmount": money,
        "categoryAllocations": [{"amount": money}],
    }
    adjustment = {
        "accountId": receivable,
        "transactionDate": "2026-10-22",
        "expectedDerivedBalance": {"amount": "2.00", "currency": "CNY"},
        "targetBalance": money,
        "expectedAccountNature": "asset",
    }
    for path, body in [
        ("transactions", transfer),
        ("transactions", expense),
        ("balance-adjustments", adjustment),
    ]:
        blocked = await client.post(
            f"{base}/{path}", json=body, headers=submission_headers(actor.id)
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "finance_linked_record_protected"
    for method, path, mutation in [
        ("PUT", f"transactions/{transaction_id}", transfer),
        ("DELETE", f"transactions/{transaction_id}", None),
        ("PATCH", f"accounts/{receivable}", {"openingBalance": money}),
        ("PATCH", f"accounts/{receivable}", {"trackingStartDate": "2026-10-01"}),
        ("POST", f"accounts/{receivable}/archive", None),
        ("POST", f"accounts/{receivable}/unarchive", None),
    ]:
        blocked = await client.request(method, f"{base}/{path}", json=mutation)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "finance_linked_record_protected"
    assert (await client.delete(f"{base}/settlements/{detail['id']}")).status_code == 405
    assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
    overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    summary = overview["monthSummaryByCurrency"][0]
    assert summary["income"]["amount"] == summary["expense"]["amount"] == "0.00"
    assert overview["financialPositionByCurrency"][0]["assetTotal"]["amount"] == "100.00"


async def test_receiving_liability_card_bill_and_future_receipt_keep_financial_meanings(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    card = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Card",
            "nature": "liability",
            "currency": "CNY",
            "openingBalance": {"amount": "100.00", "currency": "CNY"},
            "trackingStartDate": "2026-10-01",
        },
    )
    bill = await client.post(
        f"{base}/transactions",
        headers=submission_headers(actor.id),
        json={
            "kind": "internalTransfer",
            "sourceAccountId": draft["fundingAccountId"],
            "destinationAccountId": card.json()["id"],
            "transactionDate": "2026-10-21",
            "amount": {"amount": "20.00", "currency": "CNY"},
        },
    )
    assert bill.status_code == 201, bill.text
    unchanged = (await client.get(f"{base}/advances/{source['id']}")).json()["obligation"]
    assert unchanged["cashPaid"]["amount"] == "0.00"
    assert unchanged["remaining"]["amount"] == "100.00"
    detail = await confirm(
        client,
        actor,
        base,
        {
            **repayment(draft, "50.00"),
            "receivingAccountId": card.json()["id"],
            "transactionDate": "2099-01-01",
        },
    )
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[card.json()["id"]]["currentBalance"]["amount"] == "30.00"
    assert (
        accounts[source["obligation"]["receivableAccountId"]]["currentBalance"]["amount"] == "50.00"
    )
    assert detail["transactionDate"] == "2099-01-01"


async def test_owner_ledger_and_binding_isolation_and_immutable_content(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {**body, "previewToken": preview.json()["previewToken"]}
    headers = submission_headers(actor.id)
    created = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert created.status_code == 201
    id = created.json()["outcome"]["resource"]["id"]
    conflict = await client.post(
        f"{base}/settlements", json={**command, "note": "different"}, headers=headers
    )
    assert conflict.json()["code"] == "finance_submission_content_conflict"
    other_ledger = await client.post(
        "/api/finance/ledgers", json={"name": "Other"}, headers=submission_headers(actor.id)
    )
    other_base = f"/api/finance/ledgers/{other_ledger.json()['outcome']['resource']['id']}"
    assert (await client.get(f"{other_base}/settlements/{id}")).status_code == 404
    assert (await client.post(f"{other_base}/settlements/preview", json=body)).status_code == 404
    other = User(
        identity_issuer="https://identity.example.test/settlement",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(other)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=other) as foreign:
        for path in (f"settlements/{id}", f"participants/{draft['participantId']}/settlements"):
            assert (await foreign.get(f"{base}/{path}")).status_code == 404
        assert (await foreign.post(f"{base}/settlements/preview", json=body)).status_code == 404
        assert (
            await foreign.post(
                f"{base}/settlements", json=command, headers=submission_headers(other.id)
            )
        ).status_code == 404
        lookup = await foreign.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}",
            headers=submission_headers(other.id),
        )
        assert lookup.status_code == 404
    assert (await client.get(f"{base}/settlements/{uuid4()}")).status_code == 404


@pytest.mark.parametrize(
    "currency,principal,receipt,remainder",
    [
        ("JPY", "100", "98", "2"),
        ("USD", "100.00", "98.00", "2.00"),
        ("CNY", "123456789012345678901234567890.02", "123456789012345678901234567890.01", "0.01"),
    ],
)
async def test_currency_precision_and_large_exact_amounts_remain_unrounded(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    currency: str,
    principal: str,
    receipt: str,
    remainder: str,
) -> None:
    client, actor, base, draft = advance_client
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": currency,
            "nature": "asset",
            "currency": currency,
            "openingBalance": {"amount": principal, "currency": currency},
            "trackingStartDate": "2026-10-01",
        },
    )
    body = {
        **draft,
        "fundingAccountId": account.json()["id"],
        "amount": {"amount": principal, "currency": currency},
        "ownerShare": {"amount": "0" if currency == "JPY" else "0.00", "currency": currency},
        "participantShare": {"amount": principal, "currency": currency},
    }
    preview = await client.post(f"{base}/advances/preview", json=body)
    created = await client.post(
        f"{base}/advances",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert created.status_code == 201, created.text
    settled = await confirm(
        client,
        actor,
        base,
        {
            **repayment(draft),
            "receivingAccountId": account.json()["id"],
            "amount": {"amount": receipt, "currency": currency},
        },
    )
    assert settled["allocations"][0]["remainingAfter"] == {
        "amount": remainder,
        "currency": currency,
    }
    remaining = await confirm(
        client,
        actor,
        base,
        {
            **repayment(draft),
            "receivingAccountId": account.json()["id"],
            "amount": {"amount": remainder, "currency": currency},
        },
    )
    assert remaining["allocations"][0]["remainingAfter"]["amount"] == (
        "0" if currency == "JPY" else "0.00"
    )
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[account.json()["id"]]["currentBalance"]["amount"] == principal
    # Other currencies do not become eligible merely because the Participant matches.
    assert (
        await client.post(f"{base}/settlements/preview", json=repayment(draft, "1.00"))
    ).status_code == 422


async def test_receiving_balance_change_and_new_advance_require_new_preview(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft)
    for change in ("balance", "obligation"):
        preview = await client.post(f"{base}/settlements/preview", json=body)
        if change == "balance":
            money = {"amount": "1.00", "currency": "CNY"}
            ordinary = await client.post(
                f"{base}/transactions",
                headers=submission_headers(actor.id),
                json={
                    "kind": "income",
                    "accountId": draft["fundingAccountId"],
                    "transactionDate": "2026-10-21",
                    "economicAmount": money,
                    "categoryAllocations": [{"amount": money}],
                },
            )
            assert ordinary.status_code == 201, ordinary.text
        else:
            await advance(client, actor, base, draft, "10.00", "2026-10-19")
        stale = await client.post(
            f"{base}/settlements",
            headers=submission_headers(actor.id),
            json={**body, "previewToken": preview.json()["previewToken"]},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "finance_settlement_preview_stale"
    refreshed = await confirm(client, actor, base, body)
    assert [a["cashPaid"]["amount"] for a in refreshed["allocations"]] == ["10.00", "40.00"]


async def test_managed_or_archived_receiving_account_is_rejected(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    for receiving in (source["obligation"]["receivableAccountId"], draft["fundingAccountId"]):
        expected = "finance_linked_record_protected"
        if receiving == draft["fundingAccountId"]:
            assert (await client.post(f"{base}/accounts/{receiving}/archive")).status_code == 200
            expected = "finance_account_archived"
        body = {**repayment(draft), "receivingAccountId": receiving}
        preview = await client.post(f"{base}/settlements/preview", json=body)
        assert preview.status_code == 409, preview.text
        assert preview.json()["code"] == expected
        rejected = await client.post(
            f"{base}/settlements",
            json={**body, "previewToken": "0" * 64},
            headers=submission_headers(actor.id),
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["submissionReceipt"]["outcome"]["problem"]["code"] == expected


async def test_retained_command_recovers_after_admission_closes_without_changing_identity(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_console.modules.finance import submissions

    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {**body, "previewToken": preview.json()["previewToken"]}
    headers = submission_headers(actor.id)
    real_commit = AsyncSession.commit

    async def admission_ack_lost(session: AsyncSession) -> None:
        await real_commit(session)
        raise ConnectionError("admission acknowledgement lost")

    with monkeypatch.context() as fault:
        fault.setattr(AsyncSession, "commit", admission_ack_lost)
        assert (
            await client.post(f"{base}/settlements", json=command, headers=headers)
        ).status_code == 500
    lookup = await client.get(
        f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
    )
    assert lookup.json()["state"] == "unfinished"
    with monkeypatch.context() as closed:
        closed.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
        new = await client.post(
            f"{base}/settlements", json=command, headers=submission_headers(actor.id)
        )
        assert new.status_code == 409
        assert new.json()["code"] == "finance_command_version_closed"
        assert "commandValidationRejection" not in new.json()
        recovered = await client.post(f"{base}/settlements", json=command, headers=headers)
        assert recovered.status_code == 201, recovered.text
        assert recovered.json()["admittedAt"] == lookup.json()["admittedAt"]
        changed = await client.post(
            f"{base}/settlements", json={**command, "obligationIds": []}, headers=headers
        )
        assert changed.status_code == 409
        assert changed.json()["code"] == "finance_submission_content_conflict"
        assert "commandValidationRejection" not in changed.json()
