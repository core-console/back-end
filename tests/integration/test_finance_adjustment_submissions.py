"""Durable Adjustment outcomes through HTTP and protected PostgreSQL."""

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance import api, submissions
from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategoryAllocation,
    FinanceTransaction,
)
from core_console.modules.finance.service import InvalidFinanceTransactionError
from core_console.modules.finance.submission_commands import AdjustmentCommandV1
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_transaction_submissions import transaction_scope as transaction_scope

pytestmark = pytest.mark.anyio


def adjustment_body(
    account: UUID, *, target: str = "-12.30", nature: str = "asset"
) -> dict[str, object]:
    return {
        "accountId": str(account),
        "transactionDate": "2026-10-02",
        "expectedDerivedBalance": {"amount": "-000.00", "currency": "CNY"},
        "expectedAccountNature": nature,
        "targetBalance": {"amount": target, "currency": "CNY"},
        "note": " note ",
    }


@pytest.mark.parametrize("target", ["-12.30", "0", "12.30"])
@pytest.mark.parametrize("nature", ["asset", "liability"])
async def test_adjustment_success_and_replay_keep_signed_balance_and_no_change(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    target: str,
    nature: str,
) -> None:
    actor, ledger, asset, liability = transaction_scope
    account = asset if nature == "asset" else liability
    body = adjustment_body(account, target=target, nature=nature)
    headers = submission_headers(actor.id)
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        created = await client.post(path, json=body, headers=headers)
        assert created.status_code == 200
        receipt = created.json()
        assert receipt["operation"] == "createBalanceAdjustment"
        assert receipt["submissionId"] == headers["Idempotency-Key"]
        assert receipt["targetLedgerId"] == str(ledger)
        if target == "0":
            assert receipt["outcome"] == {"kind": "noChange"}
        else:
            assert receipt["outcome"]["resource"]["type"] == "transaction"
        replay = await client.post(path, json=body, headers=headers)
        assert replay.status_code == 200
        assert replay.json() == receipt
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json() == {"state": "terminal", "receipt": receipt}
        assert lookup.headers["cache-control"] == "no-store"
        history = (await client.get(f"/api/finance/ledgers/{ledger}/transactions")).json()["items"]
        assert len(history) == (0 if target == "0" else 1)
        accounts = (await client.get(f"/api/finance/ledgers/{ledger}/accounts")).json()
        assert next(item for item in accounts if item["id"] == str(account))["currentBalance"][
            "amount"
        ] == ("0.00" if target == "0" else target)


async def test_no_change_is_immutable_after_later_balance_changes_and_archiving(
    postgres_database_url: str, transaction_scope: tuple[User, UUID, UUID, UUID]
) -> None:
    actor, ledger, account, _ = transaction_scope
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    body = adjustment_body(account, target="0")
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        receipt = (await client.post(path, json=body, headers=headers)).json()
        assert receipt["outcome"] == {"kind": "noChange"}
        changed = await client.post(
            path, json=adjustment_body(account), headers=submission_headers(actor.id)
        )
        assert changed.status_code == 200
        await client.post(f"/api/finance/ledgers/{ledger}/accounts/{account}/archive")
        replay = await client.post(path, json=body, headers=headers)
        assert replay.status_code == 200 and replay.json() == receipt
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["receipt"] == receipt
        history = (await client.get(f"/api/finance/ledgers/{ledger}/transactions")).json()["items"]
        assert len(history) == 1


@pytest.mark.parametrize("replacement", ["updated", "removed", "deleted"])
async def test_created_receipt_survives_adjustment_replacement_and_deletion(
    postgres_database_url: str, transaction_scope: tuple[User, UUID, UUID, UUID], replacement: str
) -> None:
    actor, ledger, account, _ = transaction_scope
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    body = adjustment_body(account)
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        receipt = (await client.post(path, json=body, headers=headers)).json()
        resource_id = receipt["outcome"]["resource"]["id"]
        detail_path = f"/api/finance/ledgers/{ledger}/transactions/{resource_id}"
        if replacement == "deleted":
            assert (await client.delete(detail_path)).status_code == 204
        else:
            replaced = await client.put(
                f"{path}/{resource_id}",
                json={
                    **body,
                    "targetBalance": {
                        "amount": "0" if replacement == "removed" else "20",
                        "currency": "CNY",
                    },
                },
            )
            assert replaced.status_code == 200
            assert replaced.json()["outcome"] == replacement
        assert (await client.post(path, json=body, headers=headers)).json() == receipt
        if replacement == "updated":
            assert (await client.get(detail_path)).json()["correctionDelta"]["amount"] == "20.00"
            assert (await client.delete(detail_path)).status_code == 204
        assert (await client.get(detail_path)).status_code == 404
        assert (await client.post(path, json=body, headers=headers)).json() == receipt
        assert (await client.get(f"/api/finance/ledgers/{ledger}/transactions")).json()[
            "items"
        ] == []


@pytest.mark.parametrize(
    "reason",
    ["balance", "nature", "archived", "missing", "date", "target_currency", "expected_currency"],
)
async def test_mutable_rejection_is_terminal_after_eligibility_is_restored(
    postgres_database_url: str, transaction_scope: tuple[User, UUID, UUID, UUID], reason: str
) -> None:
    actor, ledger, account, _ = transaction_scope
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    account_path = f"/api/finance/ledgers/{ledger}/accounts/{account}"
    body = adjustment_body(account, target="0")
    if reason == "balance":
        body["expectedDerivedBalance"] = {"amount": "1", "currency": "CNY"}
    elif reason == "nature":
        body["expectedAccountNature"] = "liability"
    elif reason == "missing":
        body["accountId"] = str(uuid4())
    elif reason == "date":
        body["transactionDate"] = "2026-09-30"
    elif reason.endswith("currency"):
        body["targetBalance" if reason == "target_currency" else "expectedDerivedBalance"] = {
            "amount": "0",
            "currency": "USD",
        }
    codes = {
        "balance": "account_balance_changed",
        "nature": "finance_account_semantics_changed",
        "archived": "finance_account_archived",
        "missing": "finance_account_not_found",
        "date": "validation_error",
        "target_currency": "validation_error",
        "expected_currency": "account_balance_changed",
    }
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        if reason == "archived":
            await client.post(f"{account_path}/archive")
        rejected = await client.post(path, json=body, headers=headers)
        assert rejected.status_code == (
            404 if reason == "missing" else 422 if reason in ("date", "target_currency") else 409
        )
        assert rejected.json()["code"] == codes[reason]
        receipt = rejected.json()["submissionReceipt"]
        assert receipt["outcome"]["kind"] == "rejected"
        assert "commandValidationRejection" not in rejected.json()
        assert (await client.get(f"/api/finance/ledgers/{ledger}/transactions")).json()[
            "items"
        ] == []
        if reason == "archived":
            await client.post(f"{account_path}/unarchive")
        elif reason == "balance":
            await client.post(
                path,
                json=adjustment_body(account, target="1"),
                headers=submission_headers(actor.id),
            )
        elif reason == "nature":
            assert (
                await client.patch(account_path, json={"nature": "liability"})
            ).status_code == 200
        elif reason == "date":
            assert (
                await client.patch(account_path, json={"trackingStartDate": "2026-09-30"})
            ).status_code == 200
        replay = await client.post(path, json=body, headers=headers)
        assert replay.status_code == rejected.status_code and replay.json() == rejected.json()
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.status_code == 200 and lookup.json()["receipt"] == receipt


@pytest.mark.parametrize("target", ["0", "-12.30"])
@pytest.mark.parametrize(
    "phase",
    [
        "admission_ack",
        "terminal_ack",
        "before_commit",
        "projection",
        "receipt",
        "business_after_write",
        "unknown_after_write",
        "partial_write",
        "response_loss",
    ],
)
async def test_fault_recovery_preserves_atomic_effects_and_real_commit_outcome(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    phase: str,
) -> None:
    actor, ledger, account, _ = transaction_scope
    body = adjustment_body(account, target=target)
    headers = submission_headers(actor.id)
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    real_commit = AsyncSession.commit
    commits = 0

    async def commit_fault(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_commit":
            raise RuntimeError("before terminal commit")
        await real_commit(session)
        if commits == (1 if phase == "admission_ack" else 2):
            raise ConnectionError("lost acknowledgement")

    name = "execute_create_balance_adjustment"
    execute = getattr(submissions, name)

    async def after_execution(*args: Any, **kwargs: Any) -> Any:
        await execute(*args, **kwargs)
        if phase == "business_after_write":
            raise InvalidFinanceTransactionError("recognized tentative rejection")
        raise RuntimeError("unknown failure after execution")

    def fail_projection(_value: object) -> Any:
        raise RuntimeError("projection failure")

    flush = AsyncSession.flush

    async def partial_write(session: AsyncSession, objects: Any = None) -> None:
        pending = any(isinstance(value, FinanceTransaction) for value in session.new)
        await flush(session, objects)
        if pending:
            raise RuntimeError("parent persisted before movement")

    handle = ASGITransport.handle_async_request

    async def drop(transport: ASGITransport, request: Request) -> Response:
        response = await handle(transport, request)
        assert response.status_code == 200
        raise ReadError("lost committed response", request=request)

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        with monkeypatch.context() as faults:
            if phase in ("admission_ack", "terminal_ack", "before_commit"):
                faults.setattr(AsyncSession, "commit", commit_fault)
            elif phase in ("business_after_write", "unknown_after_write"):
                faults.setattr(submissions, "execute_create_balance_adjustment", after_execution)
            elif phase == "projection":
                faults.setattr(api, "_to_balance_adjustment_result_response", fail_projection)
            elif phase == "receipt":
                faults.setattr(submissions, "_receipt", fail_projection)
            elif phase == "partial_write" and target != "0":
                faults.setattr(AsyncSession, "flush", partial_write)
            elif phase == "response_loss":
                faults.setattr(ASGITransport, "handle_async_request", drop)
            if phase == "response_loss":
                with pytest.raises(ReadError):
                    await client.post(path, json=body, headers=headers)
            else:
                reply = await client.post(path, json=body, headers=headers)
                assert reply.status_code == (
                    422
                    if phase == "business_after_write"
                    else 200
                    if phase == "partial_write" and target == "0"
                    else 500
                )
        committed = phase in ("terminal_ack", "response_loss") or (
            phase == "partial_write" and target == "0"
        )
        counts = [
            await postgres_session.scalar(select(func.count()).select_from(model))
            for model in (FinanceTransaction, FinanceAccountMovement, FinanceCategoryAllocation)
        ]
        assert counts == ([1, 1, 0] if committed and target != "0" else [0, 0, 0])
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == (
            "terminal" if committed or phase == "business_after_write" else "unfinished"
        )
        retry = await client.post(path, json=body, headers=headers)
        assert retry.status_code == (422 if phase == "business_after_write" else 200)
        if committed:
            assert retry.json() == lookup.json()["receipt"]
        elif phase == "business_after_write":
            assert retry.json()["submissionReceipt"] == lookup.json()["receipt"]
        else:
            assert retry.json()["admittedAt"] == lookup.json()["admittedAt"]
        history = (await client.get(f"/api/finance/ledgers/{ledger}/transactions")).json()["items"]
        assert len(history) == (1 if target != "0" and phase != "business_after_write" else 0)


@pytest.mark.parametrize("target", ["0", "-12.30"])
async def test_concurrent_identical_adjustments_converge_on_one_outcome(
    postgres_database_url: str, transaction_scope: tuple[User, UUID, UUID, UUID], target: str
) -> None:
    actor, ledger, account, _ = transaction_scope
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    body = adjustment_body(account, target=target)
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        replies = await asyncio.wait_for(
            asyncio.gather(*[client.post(path, json=body, headers=headers) for _ in range(3)]),
            timeout=5,
        )
        assert any(reply.status_code == 200 for reply in replies)
        receipt = (await client.post(path, json=body, headers=headers)).json()
        for reply in replies:
            if reply.status_code == 200:
                assert reply.json() == receipt
            else:
                assert (
                    reply.status_code == 409 and reply.json()["code"] == "finance_submission_busy"
                )
        history = (await client.get(f"/api/finance/ledgers/{ledger}/transactions")).json()["items"]
        assert len(history) == (0 if target == "0" else 1)


async def test_account_lock_race_validates_authoritative_state_before_no_change(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, ledger, account, _ = transaction_scope
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    first_headers, second_headers = submission_headers(actor.id), submission_headers(actor.id)
    body = adjustment_body(account)
    entered, release = asyncio.Event(), asyncio.Event()
    name = "execute_create_balance_adjustment"
    execute = getattr(submissions, name)
    first = True

    async def hold(*args: Any, **kwargs: Any) -> Any:
        nonlocal first
        is_first = first
        first = False
        result = await execute(*args, **kwargs)
        if is_first:
            entered.set()
            await release.wait()
        return result

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        with monkeypatch.context() as faults:
            faults.setattr(submissions, "execute_create_balance_adjustment", hold)
            pending = asyncio.create_task(client.post(path, json=body, headers=first_headers))
            try:
                await asyncio.wait_for(entered.wait(), timeout=3)
                for headers, command in [
                    (first_headers, body),
                    (second_headers, adjustment_body(account, target="0")),
                ]:
                    reply = await client.post(path, json=command, headers=headers)
                    assert (
                        reply.status_code == 409
                        and reply.json()["code"] == "finance_submission_busy"
                    )
                    assert "submissionReceipt" not in reply.json()
            finally:
                release.set()
            assert (await asyncio.wait_for(pending, timeout=3)).status_code == 200
        stale = await client.post(
            path, json=adjustment_body(account, target="0"), headers=second_headers
        )
        assert stale.status_code == 409 and stale.json()["code"] == "account_balance_changed"
        assert stale.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"


@pytest.mark.parametrize("stage", ["admission", "submission", "account"])
async def test_adjustment_serialization_waits_are_bounded_and_recoverable(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    stage: str,
) -> None:
    actor, ledger, account, _ = transaction_scope
    body = adjustment_body(account)
    key = uuid4()
    row = FinanceSubmission(
        local_user_id=actor.id,
        submission_id=key,
        command_version="1",
        retention_ledger_id=ledger,
        canonical_command={
            "commandVersion": "1",
            "operation": "createBalanceAdjustment",
            "targetLedgerId": str(ledger),
            "body": AdjustmentCommandV1.model_validate(body).canonical(),
        },
    )
    postgres_session.add(row)
    await postgres_session.flush()
    if stage != "admission":
        await postgres_session.commit()
        if stage == "submission":
            await postgres_session.execute(
                select(FinanceSubmission)
                .where(FinanceSubmission.submission_id == key)
                .with_for_update()
            )
        else:
            await postgres_session.execute(
                select(FinanceAccount).where(FinanceAccount.id == account).with_for_update()
            )
    headers = submission_headers(actor.id, key)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
        start = monotonic()
        reply = await asyncio.wait_for(client.post(path, json=body, headers=headers), timeout=3)
        assert 0.20 <= monotonic() - start < 2
        assert reply.status_code == 409 and reply.json()["code"] == "finance_submission_busy"
        assert "submissionReceipt" not in reply.json()
        await postgres_session.commit()
        assert (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json()[
            "state"
        ] == "unfinished"
        assert (await client.post(path, json=body, headers=headers)).status_code == 200


async def test_closed_v1_keeps_terminal_replay_and_unfinished_resume(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, ledger, account, _ = transaction_scope
    body = adjustment_body(account, target="0")
    terminal_headers = submission_headers(actor.id)
    unfinished_headers = submission_headers(actor.id)
    key = UUID(unfinished_headers["Idempotency-Key"])
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
        receipt = (await client.post(path, json=body, headers=terminal_headers)).json()
        postgres_session.add(
            FinanceSubmission(
                local_user_id=actor.id,
                submission_id=key,
                command_version="1",
                retention_ledger_id=ledger,
                canonical_command={
                    "commandVersion": "1",
                    "operation": "createBalanceAdjustment",
                    "targetLedgerId": str(ledger),
                    "body": AdjustmentCommandV1.model_validate(body).canonical(),
                },
            )
        )
        await postgres_session.commit()
        monkeypatch.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
        assert (await client.post(path, json=body, headers=terminal_headers)).json() == receipt
        unfinished = await client.get(f"/api/finance/submissions/{key}", headers=unfinished_headers)
        assert unfinished.json()["state"] == "unfinished"
        resumed = await client.post(path, json=body, headers=unfinished_headers)
        assert resumed.status_code == 200 and resumed.json()["outcome"] == {"kind": "noChange"}
        assert resumed.json()["admittedAt"] == unfinished.json()["admittedAt"]
        closed = await client.post(path, json=body, headers=submission_headers(actor.id))
        assert (
            closed.status_code == 409 and closed.json()["code"] == "finance_command_version_closed"
        )
        assert (
            "submissionReceipt" not in closed.json()
            and "commandValidationRejection" not in closed.json()
        )


async def test_adjustment_owner_namespaces_and_scope_nondisclosure(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
) -> None:
    from integration.test_finance_api import _user

    actor, ledger, account, _ = transaction_scope
    foreign = _user("foreign-adjustment")
    postgres_session.add(foreign)
    await postgres_session.commit()
    key = uuid4()
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        receipt = (
            await client.post(
                path,
                json=adjustment_body(account, target="0"),
                headers=submission_headers(actor.id, key),
            )
        ).json()
    async with finance_client(database_url=postgres_database_url, actor=foreign) as client:
        headers = submission_headers(foreign.id, key)
        for version in ("1", "99"):
            denied = await client.post(
                path, json={"bad": True}, headers={**headers, "Finance-Command-Version": version}
            )
            assert denied.status_code == 404
            assert (
                "submissionReceipt" not in denied.json()
                and "commandValidationRejection" not in denied.json()
            )
        existing = await client.get(f"/api/finance/submissions/{key}", headers=headers)
        absent = await client.get(f"/api/finance/submissions/{uuid4()}", headers=headers)
        assert existing.status_code == absent.status_code == 404
        assert existing.json()["detail"] == absent.json()["detail"]
        assert existing.headers["cache-control"] == absent.headers["cache-control"] == "no-store"
        owned_ledger = (
            await client.post(
                "/api/finance/ledgers",
                json={"name": "Foreign"},
                headers=submission_headers(foreign.id),
            )
        ).json()["outcome"]["resource"]["id"]
        owned_account = (
            await client.post(
                f"/api/finance/ledgers/{owned_ledger}/accounts",
                json={
                    "name": "Cash",
                    "nature": "asset",
                    "currency": "CNY",
                    "openingBalance": {"amount": "0", "currency": "CNY"},
                    "trackingStartDate": "2026-10-01",
                },
                headers=submission_headers(foreign.id),
            )
        ).json()["outcome"]["resource"]["id"]
        independent = await client.post(
            f"/api/finance/ledgers/{owned_ledger}/balance-adjustments",
            json=adjustment_body(UUID(owned_account), target="0"),
            headers=headers,
        )
        assert independent.status_code == 200 and independent.json()["submissionId"] == str(key)
        assert independent.json()["targetLedgerId"] != receipt["targetLedgerId"]


async def test_adjustment_canonical_equivalents_and_all_immutable_fields(
    postgres_database_url: str, transaction_scope: tuple[User, UUID, UUID, UUID]
) -> None:
    actor, ledger, account, other = transaction_scope
    body = adjustment_body(account)
    body.pop("note")
    equivalent = {
        **body,
        "accountId": str(account).upper(),
        "expectedDerivedBalance": {"amount": "0", "currency": "CNY"},
        "targetBalance": {"amount": "-0012.3", "currency": "CNY"},
        "note": "  ",
    }
    changes: list[dict[str, object]] = [
        {"accountId": str(other)},
        {"transactionDate": "2026-10-03"},
        {"note": "new"},
        {"expectedAccountNature": "liability"},
        {"expectedDerivedBalance": {"amount": "1", "currency": "CNY"}},
        {"targetBalance": {"amount": "0", "currency": "CNY"}},
        {"targetBalance": {"amount": "-12.30", "currency": "USD"}},
        {"extra": None},
    ]
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
        receipt = (await client.post(path, json=body, headers=headers)).json()
        assert (await client.post(path, json=equivalent, headers=headers)).json() == receipt
        for change in changes:
            reply = await client.post(path, json={**body, **change}, headers=headers)
            assert (
                reply.status_code == 409
                and reply.json()["code"] == "finance_submission_content_conflict"
            )
            assert (
                "commandValidationRejection" not in reply.json()
                and "submissionReceipt" not in reply.json()
            )
        new_ledger = (
            await client.post(
                "/api/finance/ledgers", json={"name": "Other"}, headers=submission_headers(actor.id)
            )
        ).json()["outcome"]["resource"]["id"]
        for new_path, new_body in [
            (f"/api/finance/ledgers/{new_ledger}/balance-adjustments", body),
            ("/api/finance/ledgers", {"name": "Other"}),
        ]:
            assert (await client.post(new_path, json=new_body, headers=headers)).json()[
                "code"
            ] == "finance_submission_content_conflict"
        mismatch = await client.post(
            path, content="invalid", headers={**headers, "Finance-Command-Version": "99"}
        )
        assert mismatch.json()["code"] == "finance_submission_version_mismatch"
        assert (await client.post(path, json=body, headers=headers)).json() == receipt


@pytest.mark.parametrize("field", ["expectedDerivedBalance", "targetBalance"])
@pytest.mark.parametrize("invalid", ["precision", "number", "currency", "range"])
async def test_adjustment_money_q29_is_frozen_exact_invalidity(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid: str,
) -> None:
    from core_console.modules.finance.money import SUPPORTED_CURRENCY_MINOR_UNITS

    actor, ledger, account, _ = transaction_scope
    body = adjustment_body(account)
    body[field] = {
        "precision": {"amount": "0.000", "currency": "CNY"},
        "number": {"amount": 0, "currency": "CNY"},
        "currency": {"amount": "0", "currency": "EUR"},
        "range": {"amount": "1" + "0" * 131072, "currency": "CNY"},
    }[invalid]
    monkeypatch.setitem(SUPPORTED_CURRENCY_MINOR_UNITS, "CNY", 3)
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        reply = await client.post(
            f"/api/finance/ledgers/{ledger}/balance-adjustments", json=body, headers=headers
        )
        assert reply.status_code == 422
        assert reply.json()["commandValidationRejection"] == {
            "kind": "definitivelyNotAdmitted",
            "submissionId": headers["Idempotency-Key"],
            "commandVersion": "1",
            "ownerId": str(actor.id),
            "operation": "createBalanceAdjustment",
            "targetLedgerId": str(ledger),
            "attemptedBody": body,
        }
        assert "submissionReceipt" not in reply.json()
        assert (
            await client.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
        ).status_code == 404


async def test_delayed_invalid_adjustment_cannot_emit_q29_over_a_valid_binding(
    postgres_database_url: str, transaction_scope: tuple[User, UUID, UUID, UUID]
) -> None:
    actor, ledger, account, _ = transaction_scope
    body = adjustment_body(account)
    headers = submission_headers(actor.id)
    arrived, release = asyncio.Event(), asyncio.Event()

    async def delayed_body() -> AsyncIterator[bytes]:
        arrived.set()
        await release.wait()
        yield json.dumps({**body, "transactionDate": "invalid"}).encode()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
        pending = asyncio.create_task(client.post(path, content=delayed_body(), headers=headers))
        try:
            await asyncio.wait_for(arrived.wait(), timeout=3)
            assert (
                await client.get(
                    f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
                )
            ).status_code == 404
            receipt = (await client.post(path, json=body, headers=headers)).json()
        finally:
            release.set()
        conflict = await asyncio.wait_for(pending, timeout=3)
        assert (
            conflict.status_code == 409
            and conflict.json()["code"] == "finance_submission_content_conflict"
        )
        assert "commandValidationRejection" not in conflict.json()
        assert (await client.post(path, json=body, headers=headers)).json() == receipt


async def test_concurrent_stale_submissions_replay_one_rejection_after_state_changes(
    postgres_database_url: str, transaction_scope: tuple[User, UUID, UUID, UUID]
) -> None:
    actor, ledger, account, _ = transaction_scope
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    body = {**adjustment_body(account, target="0"), "expectedAccountNature": "liability"}
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        replies = await asyncio.wait_for(
            asyncio.gather(*[client.post(path, json=body, headers=headers) for _ in range(3)]),
            timeout=5,
        )
        rejected = await client.post(path, json=body, headers=headers)
        assert (
            rejected.status_code == 409
            and rejected.json()["code"] == "finance_account_semantics_changed"
        )
        for reply in replies:
            assert reply.status_code == 409
            if reply.json()["code"] != "finance_submission_busy":
                assert reply.json() == rejected.json()
        assert (
            await client.patch(
                f"/api/finance/ledgers/{ledger}/accounts/{account}", json={"nature": "liability"}
            )
        ).status_code == 200
        assert (await client.post(path, json=body, headers=headers)).json() == rejected.json()
        assert (await client.get(f"/api/finance/ledgers/{ledger}/transactions")).json()[
            "items"
        ] == []


@pytest.mark.parametrize("currency", ["CNY", "USD", "JPY"])
async def test_signed_expected_balance_and_zero_target_preserve_currency_scale(
    postgres_database_url: str, transaction_scope: tuple[User, UUID, UUID, UUID], currency: str
) -> None:
    actor, ledger, _, _other = transaction_scope
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        created = await client.post(
            f"/api/finance/ledgers/{ledger}/accounts",
            json={
                "name": currency,
                "nature": "liability",
                "currency": currency,
                "openingBalance": {"amount": "-1", "currency": currency},
                "trackingStartDate": "2026-10-01",
            },
            headers=submission_headers(actor.id),
        )
        assert created.status_code == 201
        account = UUID(created.json()["outcome"]["resource"]["id"])
        body = {
            **adjustment_body(account, target="0", nature="liability"),
            "expectedDerivedBalance": {"amount": "-01", "currency": currency},
            "targetBalance": {"amount": "-000", "currency": currency},
        }
        headers = submission_headers(actor.id)
        path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
        receipt = (await client.post(path, json=body, headers=headers)).json()
        assert receipt["outcome"]["kind"] == "created"
        current = await client.get(
            f"/api/finance/ledgers/{ledger}/transactions/{receipt['outcome']['resource']['id']}"
        )
        assert current.json()["correctionDelta"] == {
            "amount": "1" if currency == "JPY" else "1.00",
            "currency": currency,
        }
        body["expectedDerivedBalance"] = {"amount": "0", "currency": currency}
        unchanged = await client.post(path, json=body, headers=submission_headers(actor.id))
        assert unchanged.status_code == 200 and unchanged.json()["outcome"] == {"kind": "noChange"}


@pytest.mark.parametrize(
    "invalid",
    [
        {"expectedAccountNature": "other"},
        {"transactionDate": "2026-02-30"},
        {"transactionDate": "2026-10-02T00:00:00Z"},
        {"accountId": "bad"},
        {"note": "x" * 501},
        {"extra": True},
    ],
)
async def test_adjustment_q29_covers_only_immutable_body_rules(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    invalid: dict[str, object],
) -> None:
    actor, ledger, account, _ = transaction_scope
    body = {**adjustment_body(account), **invalid}
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        reply = await client.post(
            f"/api/finance/ledgers/{ledger}/balance-adjustments", json=body, headers=headers
        )
        assert reply.status_code == 422
        assert reply.json()["commandValidationRejection"]["attemptedBody"] == body
        assert "submissionReceipt" not in reply.json()
        assert (
            await client.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
        ).status_code == 404


async def test_adjustment_accepts_the_full_durable_money_range_without_truncation(
    postgres_database_url: str, transaction_scope: tuple[User, UUID, UUID, UUID]
) -> None:
    actor, ledger, account, _ = transaction_scope
    target = "-" + "9" * 131072 + ".99"
    headers = submission_headers(actor.id)
    body = adjustment_body(account, target=target)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
        response = await client.post(path, json=body, headers=headers)
        assert response.status_code == 200
        receipt = response.json()
        current = await client.get(
            f"/api/finance/ledgers/{ledger}/transactions/{receipt['outcome']['resource']['id']}"
        )
        assert current.json()["correctionDelta"] == {"amount": target, "currency": "CNY"}
        assert (await client.post(path, json=body, headers=headers)).json() == receipt
        body["expectedDerivedBalance"] = {"amount": target, "currency": "CNY"}
        unchanged = await client.post(path, json=body, headers=submission_headers(actor.id))
        assert unchanged.status_code == 200 and unchanged.json()["outcome"] == {"kind": "noChange"}


@pytest.mark.parametrize(
    "guard",
    [
        "missing",
        "owner",
        "unsupported",
        "closed",
        "repeated",
        "malformed_json",
        "non_object",
        "non_finite",
    ],
)
async def test_adjustment_guards_never_claim_terminal_or_q29_evidence(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
) -> None:
    actor, ledger, source, _ = transaction_scope
    headers: Any = submission_headers(actor.id)
    content = json.dumps(adjustment_body(source))
    status = 400
    if guard == "missing":
        headers = {}
    elif guard == "owner":
        headers["Finance-Submission-Owner"] = str(uuid4())
        status = 403
    elif guard == "unsupported":
        headers["Finance-Command-Version"] = "99"
        status = 422
    elif guard == "closed":
        monkeypatch.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
        status = 409
    elif guard == "repeated":
        headers = [*headers.items(), ("Idempotency-Key", str(uuid4()))]
    else:
        content = {"malformed_json": "{", "non_object": "[]", "non_finite": '{"extra":1e999}'}[
            guard
        ]
        status = 422
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
        reply = await client.post(path, content=content, headers=headers)
        assert reply.status_code == status
        assert "submissionReceipt" not in reply.json()
        assert "commandValidationRejection" not in reply.json()
        assert (await client.get(f"/api/finance/ledgers/{ledger}/transactions")).json()[
            "items"
        ] == []


async def test_disabled_user_after_adjustment_admission_preserves_unfinished_evidence(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, ledger, source, _ = transaction_scope
    headers = submission_headers(actor.id)
    commit = AsyncSession.commit
    first = True

    async def disable(session: AsyncSession) -> None:
        nonlocal first
        await commit(session)
        if first:
            first = False
            actor.status = "disabled"
            await commit(postgres_session)

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
        body = adjustment_body(source)
        with monkeypatch.context() as faults:
            faults.setattr(AsyncSession, "commit", disable)
            denied = await client.post(path, json=body, headers=headers)
        assert denied.status_code == 403
        lookup_path = f"/api/finance/submissions/{headers['Idempotency-Key']}"
        denied_lookup = await client.get(lookup_path, headers=headers)
        assert denied_lookup.status_code == 403
        assert denied_lookup.headers["cache-control"] == "no-store"
        actor.status = "active"
        await postgres_session.commit()
        lookup = await client.get(lookup_path, headers=headers)
        assert lookup.json()["state"] == "unfinished"
        assert (await client.get(f"/api/finance/ledgers/{ledger}/transactions")).json()[
            "items"
        ] == []
        assert (await client.post(path, json=body, headers=headers)).status_code == 200
