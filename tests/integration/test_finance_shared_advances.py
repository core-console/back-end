"""Shared Advance composition and grouped recovery through the producer HTTP API."""

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from decimal import localcontext
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance import submissions
from core_console.modules.finance.advance_models import (
    FinanceAdvance,
    FinanceAdvanceTransaction,
    FinanceObligation,
)
from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategoryAllocation,
    FinanceParticipant,
    FinanceTransaction,
)
from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_advances import advance_client as advance_client
from integration.test_finance_api import finance_client

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shared_client(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> AsyncIterator[tuple[AsyncClient, User, str, dict[str, Any]]]:
    client, actor, base, original = advance_client
    blair = await client.post(f"{base}/participants", json={"name": "Blair"})
    assert blair.status_code == 201
    draft: dict[str, Any] = {
        "fundingAccountId": original["fundingAccountId"],
        "transactionDate": "2026-10-20",
        "amount": {"amount": "300.00", "currency": "CNY"},
        "ownerShare": {"amount": "100.00", "currency": "CNY"},
        "participantShares": [
            {
                "participantId": original["participantId"],
                "amount": {"amount": "120.00", "currency": "CNY"},
            },
            {
                "participantId": blair.json()["id"],
                "amount": {"amount": "80.00", "currency": "CNY"},
            },
        ],
        "note": "Shared purchase",
    }
    yield client, actor, base, draft


@pytest.mark.parametrize("nature", ["asset", "liability"])
async def test_shared_purchase_has_exact_owner_expense_receivables_and_funding(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]], nature: str
) -> None:
    client, actor, base, draft = shared_client
    if nature == "liability":
        card = await create_finance_resource(
            client,
            f"{base}/accounts",
            owner_id=actor.id,
            json={
                "name": "Card",
                "nature": "liability",
                "currency": "CNY",
                "openingBalance": {"amount": "0.00", "currency": "CNY"},
                "trackingStartDate": "2026-10-01",
            },
        )
        assert card.status_code == 201
        draft = {**draft, "fundingAccountId": card.json()["id"]}
    preview = await client.post(f"{base}/advances/preview", json=draft)
    assert preview.status_code == 200, preview.text
    assert preview.json()["ownerExpense"] == {"amount": "100.00", "currency": "CNY"}
    assert preview.json()["receivableIncrease"] == {"amount": "200.00", "currency": "CNY"}
    assert sorted(p["principal"]["amount"] for p in preview.json()["participantReceivables"]) == [
        "120.00",
        "80.00",
    ]
    funding_after = "-200.00" if nature == "asset" else "300.00"
    assert preview.json()["fundingBalanceAfter"] == {"amount": funding_after, "currency": "CNY"}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    created = await client.post(
        f"{base}/advances",
        json={**draft, "previewToken": preview.json()["previewToken"]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["commandVersion"] == "2"
    advance_id = created.json()["outcome"]["resource"]["id"]
    detail = (await client.get(f"{base}/advances/{advance_id}")).json()
    assert detail["amount"] == draft["amount"]
    assert detail["ownerShare"] == draft["ownerShare"]
    assert len(detail["transactionIds"]) == 3
    assert len(detail["obligations"]) == 2
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[draft["fundingAccountId"]]["currentBalance"]["amount"] == funding_after
    for obligation in detail["obligations"]:
        expected = next(
            s["amount"]
            for s in draft["participantShares"]
            if s["participantId"] == obligation["participantId"]
        )
        assert obligation["principal"] == obligation["remaining"] == expected
        assert accounts[obligation["receivableAccountId"]]["currentBalance"] == expected
        assert (
            await client.get(f"{base}/participants/{obligation['participantId']}/obligations")
        ).json() == [obligation]
    history = (await client.get(f"{base}/transactions")).json()["items"]
    assert sorted(t["kind"] for t in history) == ["expense", "internalTransfer", "internalTransfer"]
    overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert overview["monthSummaryByCurrency"] == [
        {
            "currency": "CNY",
            "income": {"amount": "0.00", "currency": "CNY"},
            "expense": {"amount": "100.00", "currency": "CNY"},
            "net": {"amount": "-100.00", "currency": "CNY"},
        }
    ]


@pytest.mark.parametrize(
    "phase",
    ["admission_ack", "first_share", "expense", "before_commit", "terminal_ack", "response_loss"],
)
@pytest.mark.parametrize("nature", ["asset", "liability"])
async def test_shared_confirmation_recovers_all_effects_as_one_action(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    nature: str,
) -> None:
    client, actor, base, draft = shared_client
    if nature == "liability":
        funding = await create_finance_resource(
            client,
            f"{base}/accounts",
            owner_id=actor.id,
            json={
                "name": "Recovery card",
                "nature": "liability",
                "currency": "CNY",
                "openingBalance": {"amount": "0.00", "currency": "CNY"},
                "trackingStartDate": "2026-10-01",
            },
        )
        assert funding.status_code == 201
        draft = {**draft, "fundingAccountId": funding.json()["id"]}
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    command = {**draft, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    real_commit, real_flush = AsyncSession.commit, AsyncSession.flush
    commits = 0

    async def commit_fault(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_commit":
            raise ConnectionError("terminal commit did not reach PostgreSQL")
        await real_commit(session)
        if commits == (1 if phase == "admission_ack" else 2):
            raise ConnectionError("PostgreSQL acknowledgement lost")

    async def flush_fault(session: AsyncSession, objects: Any = None) -> None:
        target = FinanceObligation if phase == "first_share" else FinanceCategoryAllocation
        partial = any(isinstance(row, target) for row in session.new)
        await real_flush(session, objects)
        if partial:
            raise RuntimeError("group interrupted after durable tentative effects")

    handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await handle(transport, request)
        assert response.status_code == 201
        raise ReadError("committed shared purchase response lost", request=request)

    with monkeypatch.context() as faults:
        if phase == "response_loss":
            faults.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/advances", json=command, headers=headers)
        else:
            partial = phase in ("first_share", "expense")
            faults.setattr(
                AsyncSession,
                "flush" if partial else "commit",
                flush_fault if partial else commit_fault,
            )
            failed = await client.post(f"{base}/advances", json=command, headers=headers)
            assert failed.status_code == 500
            assert "submissionReceipt" not in failed.json()
            assert "commandValidationRejection" not in failed.json()
    committed = phase in ("terminal_ack", "response_loss")
    # Independent PostgreSQL observation establishes commit before any recovery retry.
    models = (
        FinanceAdvance,
        FinanceObligation,
        FinanceTransaction,
        FinanceAccountMovement,
        FinanceAccount,
        FinanceCategoryAllocation,
        FinanceAdvanceTransaction,
        FinanceParticipant,
    )
    counts = [
        await postgres_session.scalar(select(func.count()).select_from(model)) for model in models
    ]
    initial_accounts = 1 if nature == "asset" else 2
    assert counts == (
        [1, 2, 3, 5, initial_accounts + 2, 1, 3, 2]
        if committed
        else [0, 0, 0, 0, initial_accounts, 0, 0, 2]
    )
    await postgres_session.rollback()
    lookup = await client.get(
        f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
    )
    assert lookup.json()["state"] == ("terminal" if committed else "unfinished")
    assert lookup.headers["cache-control"] == "no-store"
    monkeypatch.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
    retry = await client.post(f"{base}/advances", json=command, headers=headers)
    assert retry.status_code == 201, retry.text
    if committed:
        assert retry.json() == lookup.json()["receipt"]
    else:
        assert retry.json()["admittedAt"] == lookup.json()["admittedAt"]
    assert (
        await client.post(f"{base}/advances", json=command, headers=headers)
    ).json() == retry.json()
    assert len((await client.get(f"{base}/transactions")).json()["items"]) == 3
    accounts = (await client.get(f"{base}/accounts")).json()
    assert len(accounts) == initial_accounts + 2
    funding_account = next(a for a in accounts if a["id"] == draft["fundingAccountId"])
    assert funding_account["currentBalance"] == {
        "amount": "-200.00" if nature == "asset" else "300.00",
        "currency": "CNY",
    }
    closed = await client.post(
        f"{base}/advances",
        json=command,
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert closed.status_code == 409
    assert closed.json()["code"] == "finance_command_version_closed"
    assert "submissionReceipt" not in closed.json()
    assert "commandValidationRejection" not in closed.json()


@pytest.mark.parametrize(
    "invalid",
    [
        "total",
        "owner_negative",
        "zero_share",
        "negative_share",
        "no_participants",
        "duplicate_participant",
        "currency",
        "float",
        "precision",
        "extra",
        "v1_grammar",
    ],
)
async def test_invalid_shared_intent_is_correlated_non_admission_with_no_effects(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
    invalid: str,
    postgres_session: AsyncSession,
) -> None:
    client, actor, base, draft = shared_client
    command = {**deepcopy(draft), "previewToken": "0" * 64}
    if invalid == "total":
        command["amount"]["amount"] = "299.99"
    elif invalid == "owner_negative":
        command["ownerShare"]["amount"] = "-100.00"
    elif invalid in ("zero_share", "negative_share", "float", "precision"):
        command["participantShares"][1]["amount"]["amount"] = {
            "zero_share": "0.00",
            "negative_share": "-80.00",
            "float": 80,
            "precision": "80.000",
        }[invalid]
    elif invalid == "no_participants":
        command["participantShares"] = []
    elif invalid == "duplicate_participant":
        command["participantShares"][1]["participantId"] = command["participantShares"][0][
            "participantId"
        ]
    elif invalid == "currency":
        command["participantShares"][1]["amount"]["currency"] = "USD"
    elif invalid == "extra":
        command["settlement"] = {}
    headers = {
        **submission_headers(actor.id),
        "Finance-Command-Version": "1" if invalid == "v1_grammar" else "2",
    }
    result = await client.post(f"{base}/advances", json=command, headers=headers)
    assert result.status_code == 422, result.text
    proof = result.json()["commandValidationRejection"]
    assert proof["commandVersion"] == headers["Finance-Command-Version"]
    assert proof["attemptedBody"] == command
    assert proof["submissionId"] == headers["Idempotency-Key"]
    assert "submissionReceipt" not in result.json()
    assert (await client.get(f"{base}/transactions")).json()["items"] == []
    assert len((await client.get(f"{base}/accounts")).json()) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceAdvance)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceObligation)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceParticipant)) == 2


@pytest.mark.parametrize("change", ["second_rename", "second_archive", "balance", "opening"])
async def test_shared_preview_binds_all_participants_and_funding(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
    change: str,
) -> None:
    client, actor, base, draft = shared_client
    preview = await client.post(f"{base}/advances/preview", json=draft)
    command = {**draft, "previewToken": preview.json()["previewToken"]}
    second = draft["participantShares"][1]["participantId"]
    if change == "second_rename":
        changed = await client.patch(
            f"{base}/participants/{second}", json={"name": "Blair renamed"}
        )
    elif change == "second_archive":
        changed = await client.post(f"{base}/participants/{second}/archive")
    elif change == "opening":
        changed = await client.patch(
            f"{base}/accounts/{draft['fundingAccountId']}",
            json={"openingBalance": {"amount": "101.00", "currency": "CNY"}},
        )
    else:
        changed = await client.post(
            f"{base}/transactions",
            headers=submission_headers(actor.id),
            json={
                "kind": "expense",
                "accountId": draft["fundingAccountId"],
                "transactionDate": "2026-10-20",
                "economicAmount": {"amount": "1.00", "currency": "CNY"},
                "categoryAllocations": [{"amount": {"amount": "1.00", "currency": "CNY"}}],
            },
        )
    assert changed.status_code in (200, 201)
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    rejected = await client.post(f"{base}/advances", json=command, headers=headers)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == (
        "finance_participant_archived"
        if change == "second_archive"
        else "finance_advance_preview_stale"
    )
    assert rejected.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    assert (
        await client.post(f"{base}/advances", json=command, headers=headers)
    ).json() == rejected.json()
    assert len((await client.get(f"{base}/accounts")).json()) == 1
    for share in draft["participantShares"]:
        assert (
            await client.get(f"{base}/participants/{share['participantId']}/obligations")
        ).json() == []


async def test_shared_same_key_concurrency_canonical_order_and_conflicting_content(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
) -> None:
    client, actor, base, draft = shared_client
    reordered = {**draft, "participantShares": list(reversed(draft["participantShares"]))}
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    assert (await client.post(f"{base}/advances/preview", json=reordered)).json() == preview
    command = {**draft, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    results = await asyncio.gather(
        *[client.post(f"{base}/advances", json=command, headers=headers) for _ in range(2)]
    )
    assert all(r.status_code in (201, 409) for r in results)
    replay = await client.post(
        f"{base}/advances",
        json={**reordered, "previewToken": command["previewToken"]},
        headers=headers,
    )
    assert replay.status_code == 201
    assert all(r.json() == replay.json() for r in results if r.status_code == 201)
    conflict = await client.post(
        f"{base}/advances", json={**command, "note": "Changed"}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "finance_submission_content_conflict"
    mismatch = await client.post(
        f"{base}/advances", json=command, headers={**headers, "Finance-Command-Version": "1"}
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "finance_submission_version_mismatch"
    assert len((await client.get(f"{base}/transactions")).json()["items"]) == 3
    for share in draft["participantShares"]:
        assert (
            await client.post(f"{base}/participants/{share['participantId']}/archive")
        ).status_code == 200
    assert (
        await client.post(f"{base}/advances", json=command, headers=headers)
    ).json() == replay.json()


async def test_competing_shared_confirmations_serialize_and_require_reconfirmation(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
) -> None:
    client, actor, base, draft = shared_client
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    command = {**draft, "previewToken": preview["previewToken"]}
    headers = [{**submission_headers(actor.id), "Finance-Command-Version": "2"} for _ in range(2)]
    await asyncio.gather(
        *[client.post(f"{base}/advances", json=command, headers=identity) for identity in headers]
    )
    # Any bounded lock timeout remains unresolved and is retried under the same key.
    outcomes = [await client.post(f"{base}/advances", json=command, headers=h) for h in headers]
    assert sorted(r.status_code for r in outcomes) == [201, 409]
    rejected = next(r for r in outcomes if r.status_code == 409)
    assert rejected.json()["code"] == "finance_advance_preview_stale"
    assert rejected.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    assert len((await client.get(f"{base}/transactions")).json()["items"]) == 3
    assert len((await client.get(f"{base}/accounts")).json()) == 3


async def test_every_shared_effect_and_receivable_is_protected_from_manual_mutation(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
) -> None:
    client, actor, base, draft = shared_client
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    created = await client.post(
        f"{base}/advances",
        json={**draft, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
    )
    assert created.status_code == 201
    advance_id = created.json()["outcome"]["resource"]["id"]
    detail = (await client.get(f"{base}/advances/{advance_id}")).json()
    money = {"amount": "1.00", "currency": "CNY"}
    for transaction_id in detail["transactionIds"]:
        transaction = (await client.get(f"{base}/transactions/{transaction_id}")).json()
        replacement = {key: transaction[key] for key in ("kind", "transactionDate", "note")}
        if transaction["kind"] == "expense":
            replacement.update(
                accountId=draft["fundingAccountId"],
                economicAmount=money,
                categoryAllocations=[{"amount": money}],
            )
        else:
            replacement.update(
                sourceAccountId=draft["fundingAccountId"],
                destinationAccountId=detail["obligations"][0]["receivableAccountId"],
                amount=money,
            )
        for method, body in [("DELETE", None), ("PUT", replacement)]:
            blocked = await client.request(
                method, f"{base}/transactions/{transaction_id}", json=body
            )
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["code"] == "finance_linked_record_protected"
    for obligation in detail["obligations"]:
        receivable = obligation["receivableAccountId"]
        expense = {
            "kind": "expense",
            "accountId": receivable,
            "transactionDate": "2026-10-20",
            "economicAmount": money,
            "categoryAllocations": [{"amount": money}],
        }
        transfer = {
            "kind": "internalTransfer",
            "sourceAccountId": draft["fundingAccountId"],
            "destinationAccountId": receivable,
            "transactionDate": "2026-10-20",
            "amount": money,
        }
        adjustment = {
            "accountId": receivable,
            "transactionDate": "2026-10-20",
            "expectedDerivedBalance": obligation["principal"],
            "targetBalance": money,
            "expectedAccountNature": "asset",
        }
        for path, body in [
            ("transactions", expense),
            ("transactions", transfer),
            ("balance-adjustments", adjustment),
        ]:
            result = await client.post(
                f"{base}/{path}", json=body, headers=submission_headers(actor.id)
            )
            assert result.status_code == 409, result.text
            assert (
                result.json()["submissionReceipt"]["outcome"]["problem"]["code"]
                == "finance_linked_record_protected"
            )
        for method, path, body in [
            ("PATCH", f"accounts/{receivable}", {"openingBalance": money}),
            ("POST", f"accounts/{receivable}/archive", None),
        ]:
            result = await client.request(method, f"{base}/{path}", json=body)
            assert result.status_code == 409
    assert (await client.get(f"{base}/advances/{advance_id}")).json() == detail


async def test_shared_participant_and_funding_references_and_recovery_are_owner_isolated(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
) -> None:
    client, actor, base, draft = shared_client
    ledger = await client.post(
        "/api/finance/ledgers", json={"name": "Other"}, headers=submission_headers(actor.id)
    )
    other_base = f"/api/finance/ledgers/{ledger.json()['outcome']['resource']['id']}"
    participant = await client.post(
        f"{other_base}/participants", json={"name": "Foreign participant"}
    )
    invalid = deepcopy(draft)
    invalid["participantShares"][1]["participantId"] = participant.json()["id"]
    for target, intent in [(base, invalid), (other_base, draft)]:
        assert (await client.post(f"{target}/advances/preview", json=intent)).status_code == 404
        rejected = await client.post(
            f"{target}/advances",
            json={**intent, "previewToken": "0" * 64},
            headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
        )
        assert rejected.status_code == 404
        assert rejected.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    assert (await client.get(f"{base}/transactions")).json()["items"] == []
    assert (await client.get(f"{other_base}/transactions")).json()["items"] == []
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    command = {**draft, "previewToken": preview["previewToken"]}
    created = await client.post(f"{base}/advances", json=command, headers=headers)
    assert created.status_code == 201
    advance_id = created.json()["outcome"]["resource"]["id"]
    assert (await client.get(f"{other_base}/advances/{advance_id}")).status_code == 404
    other = User(
        identity_issuer="https://identity.example.test/shared",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(other)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=other) as foreign:
        assert (await foreign.get(f"{base}/advances/{advance_id}")).status_code == 404
        assert (await foreign.post(f"{base}/advances/preview", json=draft)).status_code == 404
        assert (
            await foreign.post(
                f"{base}/advances",
                json=command,
                headers={**submission_headers(other.id), "Finance-Command-Version": "2"},
            )
        ).status_code == 404
        assert (
            await foreign.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}",
                headers={"Finance-Submission-Owner": str(other.id)},
            )
        ).status_code == 404


@pytest.mark.parametrize(
    "currency,total,owner,alex,blair",
    [
        ("JPY", "300", "100", "120", "80"),
        ("USD", "0.03", "0.01", "0.01", "0.01"),
        ("CNY", "200.00", "0.00", "120.00", "80.00"),
        (
            "CNY",
            "1000000000000000000000000000300.03",
            "1000000000000000000000000000100.01",
            "120.01",
            "80.01",
        ),
    ],
)
async def test_shared_money_remains_exact_with_zero_owner_and_low_decimal_precision(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
    currency: str,
    total: str,
    owner: str,
    alex: str,
    blair: str,
) -> None:
    client, actor, base, draft = shared_client
    zero = "0" if currency == "JPY" else "0.00"
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Exact funding",
            "nature": "liability",
            "currency": currency,
            "openingBalance": {"amount": zero, "currency": currency},
            "trackingStartDate": "2026-10-01",
        },
    )
    assert account.status_code == 201
    intent = deepcopy(draft)
    intent.update(
        fundingAccountId=account.json()["id"],
        amount={"amount": total, "currency": currency},
        ownerShare={"amount": owner, "currency": currency},
    )
    for share, principal in zip(intent["participantShares"], (alex, blair), strict=True):
        share["amount"] = {"amount": principal, "currency": currency}
    with localcontext() as context:
        context.prec = 3
        preview = await client.post(f"{base}/advances/preview", json=intent)
        assert preview.status_code == 200, preview.text
        assert preview.json()["ownerExpense"] == intent["ownerShare"]
        assert preview.json()["fundingBalanceAfter"] == intent["amount"]
        created = await client.post(
            f"{base}/advances",
            json={**intent, "previewToken": preview.json()["previewToken"]},
            headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
        )
        assert created.status_code == 201, created.text
        detail = (
            await client.get(f"{base}/advances/{created.json()['outcome']['resource']['id']}")
        ).json()
    assert detail["amount"] == intent["amount"]
    assert detail["ownerShare"] == intent["ownerShare"]
    assert len(detail["transactionIds"]) == (2 if owner == zero else 3)
    assert sorted(o["principal"]["amount"] for o in detail["obligations"]) == sorted([alex, blair])
