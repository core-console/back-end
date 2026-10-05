"""Durable ordinary Transaction creation through HTTP and protected PostgreSQL."""

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from time import monotonic
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, ReadError, Request, Response
from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance import api, submissions
from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceTransaction,
)
from core_console.modules.finance.money import Money
from core_console.modules.finance.service import (
    InvalidFinanceTransactionError,
    create_finance_account,
)
from core_console.modules.finance.submission_commands import TransactionCommandV1
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import _user, finance_client

pytestmark = pytest.mark.anyio


@pytest.fixture
async def transaction_scope(postgres_session: AsyncSession) -> tuple[User, UUID, UUID, UUID]:
    from datetime import date

    actor = _user("transaction-submission")
    postgres_session.add(actor)
    await postgres_session.flush()
    ledger = FinanceLedger(owner_id=actor.id, name="Home", name_key="home")
    postgres_session.add(ledger)
    await postgres_session.commit()
    accounts = []
    definitions: list[tuple[str, Literal["asset", "liability"]]] = [
        ("Cash", "asset"),
        ("Card", "liability"),
    ]
    for name, nature in definitions:
        account = await create_finance_account(
            postgres_session,
            owner_id=actor.id,
            ledger_id=ledger.id,
            name=name,
            nature=nature,
            currency="CNY",
            opening_balance=Money.parse(amount="0", currency="CNY"),
            tracking_start_date=date(2026, 10, 1),
        )
        accounts.append(account.account.id)
    return actor, ledger.id, accounts[0], accounts[1]


def transaction_body(kind: str, source: UUID, destination: UUID) -> dict[str, object]:
    common: dict[str, object] = {"kind": kind, "transactionDate": "2026-10-02", "note": " note "}
    money = {"amount": "0012.3", "currency": "CNY"}
    if kind == "internalTransfer":
        return {
            **common,
            "sourceAccountId": str(source),
            "destinationAccountId": str(destination),
            "amount": money,
        }
    return {
        **common,
        "accountId": str(source),
        "economicAmount": money,
        "categoryAllocations": [{"amount": money}],
    }


@pytest.mark.parametrize("kind", ["income", "expense", "internalTransfer"])
@pytest.mark.parametrize("liability_source", [False, True])
async def test_transaction_replay_retains_one_event_and_receipt(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    kind: str,
    liability_source: bool,
) -> None:
    actor, ledger, source, destination = transaction_scope
    if liability_source:
        source, destination = destination, source
    path = f"/api/finance/ledgers/{ledger}/transactions"
    body = transaction_body(kind, source, destination)
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        created = await client.post(path, json=body, headers=headers)
        assert created.status_code == 201
        receipt = created.json()
        assert receipt["operation"] == "createFinanceTransaction"
        assert receipt["outcome"]["resource"]["type"] == "transaction"
        assert (await client.post(path, json=body, headers=headers)).json() == receipt
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json() == {"state": "terminal", "receipt": receipt}
        history = (await client.get(path)).json()["items"]
        assert len(history) == 1
        assert history[0]["id"] == receipt["outcome"]["resource"]["id"]
        accounts = (await client.get(f"/api/finance/ledgers/{ledger}/accounts")).json()
        by_id = {account["id"]: account["currentBalance"]["amount"] for account in accounts}
        source_sign = (kind == "income") != liability_source
        assert by_id[str(source)] == ("12.30" if source_sign else "-12.30")
        assert by_id[str(destination)] == (
            ("12.30" if liability_source else "-12.30") if kind == "internalTransfer" else "0.00"
        )


@pytest.mark.parametrize("kind", ["income", "expense", "internalTransfer"])
async def test_receipt_survives_replacement_archive_and_physical_deletion(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    kind: str,
) -> None:
    actor, ledger, source, destination = transaction_scope
    body = transaction_body(kind, source, destination)
    headers = submission_headers(actor.id)
    path = f"/api/finance/ledgers/{ledger}/transactions"
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        receipt = (await client.post(path, json=body, headers=headers)).json()
        resource_path = f"{path}/{receipt['outcome']['resource']['id']}"
        edited = await client.put(
            resource_path, json={**body, "note": "changed", "transactionDate": "2026-10-03"}
        )
        assert edited.status_code == 200
        assert edited.json()["note"] == "changed"
        for account in (source, destination):
            assert (
                await client.post(f"/api/finance/ledgers/{ledger}/accounts/{account}/archive")
            ).status_code == 200
        assert (await client.post(path, json=body, headers=headers)).json() == receipt
        assert (await client.delete(resource_path)).status_code == 204
        assert (await client.get(resource_path)).status_code == 404
        assert (await client.post(path, json=body, headers=headers)).json() == receipt
        assert (await client.get(path)).json()["items"] == []
        assert (
            await client.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
        ).json() == {"state": "terminal", "receipt": receipt}


@pytest.mark.parametrize("kind", ["income", "expense", "internalTransfer"])
@pytest.mark.parametrize(
    "phase",
    [
        "admission_ack",
        "terminal_ack",
        "before_terminal_commit",
        "partial_write",
        "projection",
        "receipt",
        "business_after_write",
        "unknown_after_write",
        "response_loss",
    ],
)
async def test_fault_recovery_rolls_back_tentative_work_or_recovers_real_commit(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    phase: str,
) -> None:
    actor, ledger, source, destination = transaction_scope
    body = transaction_body(kind, source, destination)
    headers = submission_headers(actor.id)
    path = f"/api/finance/ledgers/{ledger}/transactions"
    real_commit = AsyncSession.commit
    commits = 0

    async def commit_fault(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_terminal_commit":
            raise RuntimeError("before terminal commit")
        await real_commit(session)
        if commits == (1 if phase == "admission_ack" else 2):
            raise ConnectionError("lost acknowledgement")

    name = (
        "execute_create_internal_transfer_transaction"
        if kind == "internalTransfer"
        else "execute_create_finance_transaction"
    )
    execute = getattr(submissions, name)

    async def after_write(*args: Any, **kwargs: Any) -> Any:
        await execute(*args, **kwargs)
        if phase == "business_after_write":
            raise InvalidFinanceTransactionError("recognized tentative rejection")
        raise RuntimeError("unexpected failure after aggregate flush")

    def fail_projection(_value: object) -> Any:
        raise RuntimeError("projection failure")

    flush = AsyncSession.flush

    async def partial_write(session: AsyncSession, objects: Any = None) -> None:
        parent_pending = any(isinstance(value, FinanceTransaction) for value in session.new)
        await flush(session, objects)
        if parent_pending:
            raise RuntimeError("parent persisted before movements and allocations")

    handle = ASGITransport.handle_async_request

    async def drop(transport: ASGITransport, request: Request) -> Response:
        reply = await handle(transport, request)
        assert reply.status_code == 201
        raise ReadError("lost committed response", request=request)

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        with monkeypatch.context() as faults:
            if phase in ("admission_ack", "terminal_ack", "before_terminal_commit"):
                faults.setattr(AsyncSession, "commit", commit_fault)
            elif phase in ("business_after_write", "unknown_after_write"):
                faults.setattr(submissions, name, after_write)
            elif phase == "projection":
                faults.setattr(api, "_to_transaction_response", fail_projection)
            elif phase == "receipt":
                faults.setattr(submissions, "_receipt", fail_projection)
            elif phase == "partial_write":
                faults.setattr(AsyncSession, "flush", partial_write)
            else:
                faults.setattr(ASGITransport, "handle_async_request", drop)
            if phase == "response_loss":
                with pytest.raises(ReadError):
                    await client.post(path, json=body, headers=headers)
            else:
                reply = await client.post(path, json=body, headers=headers)
                assert reply.status_code == (422 if phase == "business_after_write" else 500)
                assert "commandValidationRejection" not in reply.json()
                assert ("submissionReceipt" in reply.json()) == (phase == "business_after_write")
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        committed = phase in ("terminal_ack", "response_loss")
        terminal = committed or phase == "business_after_write"
        assert lookup.json()["state"] == ("terminal" if terminal else "unfinished")
        assert "body" not in lookup.json()
        counts = [
            await postgres_session.scalar(select(func.count()).select_from(model))
            for model in (FinanceTransaction, FinanceAccountMovement, FinanceCategoryAllocation)
        ]
        assert counts == (
            [1, 2 if kind == "internalTransfer" else 1, 0 if kind == "internalTransfer" else 1]
            if committed
            else [0, 0, 0]
        )
        await postgres_session.rollback()
        balances = (await client.get(f"/api/finance/ledgers/{ledger}/accounts")).json()
        if not committed:
            assert all(account["currentBalance"]["amount"] == "0.00" for account in balances)
        retry = await client.post(path, json=body, headers=headers)
        if phase == "business_after_write":
            assert retry.status_code == 422
            assert retry.json()["submissionReceipt"] == lookup.json()["receipt"]
            assert (await client.get(path)).json()["items"] == []
        else:
            assert retry.status_code == 201
            if committed:
                assert retry.json() == lookup.json()["receipt"]
            else:
                assert retry.json()["admittedAt"] == lookup.json()["admittedAt"]
            assert len((await client.get(path)).json()["items"]) == 1


@pytest.mark.parametrize("kind", ["income", "expense", "internalTransfer"])
async def test_concurrent_first_submissions_and_intentional_identical_entries(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    kind: str,
) -> None:
    actor, ledger, source, destination = transaction_scope
    path = f"/api/finance/ledgers/{ledger}/transactions"
    body = transaction_body(kind, source, destination)
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        replies = await asyncio.wait_for(
            asyncio.gather(*[client.post(path, json=body, headers=headers) for _ in range(3)]),
            timeout=5,
        )
        assert any(reply.status_code == 201 for reply in replies)
        for reply in replies:
            assert reply.status_code in (201, 409)
            if reply.status_code == 409:
                assert reply.json()["code"] == "finance_submission_busy"
        receipt = (await client.post(path, json=body, headers=headers)).json()
        assert all(reply.json() == receipt for reply in replies if reply.status_code == 201)
        fresh = await client.post(path, json=body, headers=submission_headers(actor.id))
        assert fresh.status_code == 201
        assert fresh.json()["outcome"]["resource"]["id"] != receipt["outcome"]["resource"]["id"]
        assert len((await client.get(path)).json()["items"]) == 2


@pytest.mark.parametrize("kind", ["income", "expense", "internalTransfer"])
@pytest.mark.parametrize("stage", ["admission", "submission", "account"])
async def test_postgresql_contention_is_bounded_without_terminal_evidence(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    kind: str,
    stage: str,
) -> None:
    actor, ledger, source, destination = transaction_scope
    key = uuid4()
    headers = submission_headers(actor.id, key)
    body = transaction_body(kind, source, destination)
    command: dict[str, object] = TypeAdapter(TransactionCommandV1).validate_python(body).canonical()
    row = FinanceSubmission(
        local_user_id=actor.id,
        submission_id=key,
        command_version="1",
        retention_ledger_id=ledger,
        canonical_command={
            "commandVersion": "1",
            "operation": "createFinanceTransaction",
            "targetLedgerId": str(ledger),
            "body": command,
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
                select(FinanceAccount).where(FinanceAccount.id == source).with_for_update()
            )
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/transactions"
        start = monotonic()
        busy = await asyncio.wait_for(client.post(path, json=body, headers=headers), timeout=3)
        assert 0.20 <= monotonic() - start < 2.0
        assert busy.status_code == 409
        assert busy.json()["code"] == "finance_submission_busy"
        assert "submissionReceipt" not in busy.json()
        assert "commandValidationRejection" not in busy.json()
        assert (await client.get(path)).json()["items"] == []
        await postgres_session.commit()
        lookup = await client.get(f"/api/finance/submissions/{key}", headers=headers)
        assert lookup.json()["state"] == "unfinished"
        assert (await client.post(path, json=body, headers=headers)).status_code == 201


@pytest.mark.parametrize("kind", ["income", "expense", "internalTransfer"])
@pytest.mark.parametrize(
    "invalid",
    [
        "precision",
        "zero",
        "number",
        "date",
        "extra",
        "note",
        "uuid",
        "structure",
        "currency",
        "range",
    ],
)
async def test_q29_is_exact_frozen_invalidity_without_admission(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    kind: str,
    invalid: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_console.modules.finance.money import SUPPORTED_CURRENCY_MINOR_UNITS

    actor, ledger, source, destination = transaction_scope
    body = transaction_body(kind, source, destination)
    money_field = "amount" if kind == "internalTransfer" else "economicAmount"
    changes: dict[str, object] = {
        "precision": {"amount": "12.301", "currency": "CNY"},
        "zero": {"amount": "-000.00", "currency": "CNY"},
        "number": {"amount": 12.3, "currency": "CNY"},
        "currency": {"amount": "12.30", "currency": "EUR"},
        "range": {"amount": "1" + "0" * 131072, "currency": "CNY"},
    }
    if invalid in changes:
        body[money_field] = changes[invalid]
    elif invalid == "date":
        body["transactionDate"] = "2026-02-30"
    elif invalid == "extra":
        body["extra"] = [None, True, {"text": "<script>"}]
    elif invalid == "note":
        body["note"] = "x" * 501
    elif invalid == "uuid":
        body["sourceAccountId" if kind == "internalTransfer" else "accountId"] = "not-a-uuid"
    elif kind == "internalTransfer":
        body["destinationAccountId"] = str(source)
    else:
        body["categoryAllocations"] = [{"amount": {"amount": "12.30", "currency": "USD"}}]
    # A future mutable catalog relaxation cannot change Q29's v1 proof.
    monkeypatch.setitem(SUPPORTED_CURRENCY_MINOR_UNITS, "CNY", 3)
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/transactions"
        reply = await client.post(path, json=body, headers=headers)
        assert reply.status_code == 422
        assert reply.json()["commandValidationRejection"] == {
            "kind": "definitivelyNotAdmitted",
            "submissionId": headers["Idempotency-Key"],
            "commandVersion": "1",
            "ownerId": str(actor.id),
            "operation": "createFinanceTransaction",
            "targetLedgerId": str(ledger),
            "attemptedBody": body,
        }
        assert "submissionReceipt" not in reply.json()
        assert (
            await client.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
        ).status_code == 404
        assert (await client.get(path)).json()["items"] == []


@pytest.mark.parametrize("kind", ["income", "expense", "internalTransfer"])
async def test_canonical_equivalents_match_and_every_meaningful_difference_conflicts(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    kind: str,
) -> None:
    actor, ledger, source, destination = transaction_scope
    body = transaction_body(kind, source, destination)
    body.pop("note")
    headers = submission_headers(actor.id)
    equivalent = deepcopy(body)
    equivalent["note"] = "  "
    if kind == "internalTransfer":
        equivalent["sourceAccountId"] = str(source).upper()
        equivalent["amount"] = {"currency": "CNY", "amount": "12.30"}
    else:
        equivalent["accountId"] = str(source).upper()
        equivalent["economicAmount"] = {"currency": "CNY", "amount": "12.30"}
        equivalent["categoryAllocations"] = [
            {"categoryId": None, "amount": {"amount": "12.30", "currency": "CNY"}}
        ]
    changes: list[dict[str, object]] = [{"transactionDate": "2026-10-03"}, {"note": "changed"}]
    if kind == "internalTransfer":
        changes += [
            {"sourceAccountId": str(destination), "destinationAccountId": str(source)},
            {"amount": {"amount": "12.31", "currency": "CNY"}},
        ]
    else:
        changes += [
            {"accountId": str(destination)},
            {"kind": "expense" if kind == "income" else "income"},
            {
                "categoryAllocations": [
                    {"categoryId": str(uuid4()), "amount": {"amount": "12.30", "currency": "CNY"}}
                ]
            },
            {
                "economicAmount": {"amount": "12.31", "currency": "CNY"},
                "categoryAllocations": [{"amount": {"amount": "12.31", "currency": "CNY"}}],
            },
        ]
    changes += [{"unexpected": None}]
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/transactions"
        receipt = (await client.post(path, json=body, headers=headers)).json()
        assert (await client.post(path, json=equivalent, headers=headers)).json() == receipt
        for change in changes:
            conflict = await client.post(path, json={**body, **change}, headers=headers)
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "finance_submission_content_conflict"
            assert "submissionReceipt" not in conflict.json()
            assert "commandValidationRejection" not in conflict.json()
        for new_path, other_body in [
            ("/api/finance/ledgers", {"name": "Other"}),
            (f"/api/finance/ledgers/{ledger}/categories", {"name": "Other"}),
        ]:
            assert (await client.post(new_path, json=other_body, headers=headers)).json()[
                "code"
            ] == "finance_submission_content_conflict"
        mismatch = await client.post(
            path, content="invalid", headers={**headers, "Finance-Command-Version": "99"}
        )
        assert mismatch.json()["code"] == "finance_submission_version_mismatch"
        assert (await client.post(path, json=body, headers=headers)).json() == receipt
        assert len((await client.get(path)).json()["items"]) == 1


@pytest.mark.parametrize("kind", ["income", "expense", "internalTransfer"])
async def test_mutable_business_rejection_remains_terminal_after_account_eligibility_changes(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    kind: str,
) -> None:
    actor, ledger, source, destination = transaction_scope
    body = transaction_body(kind, source, destination)
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        account_path = f"/api/finance/ledgers/{ledger}/accounts/{source}"
        await client.post(f"{account_path}/archive")
        path = f"/api/finance/ledgers/{ledger}/transactions"
        rejected = await client.post(path, json=body, headers=headers)
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "finance_account_archived"
        assert "commandValidationRejection" not in rejected.json()
        receipt = rejected.json()["submissionReceipt"]
        assert receipt["outcome"]["kind"] == "rejected"
        await client.post(f"{account_path}/unarchive")
        assert (await client.post(path, json=body, headers=headers)).json() == rejected.json()
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.status_code == 200
        assert lookup.json() == {"state": "terminal", "receipt": receipt}
        assert (await client.get(path)).json()["items"] == []
        assert (
            await client.post(path, json=body, headers=submission_headers(actor.id))
        ).status_code == 201


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
async def test_transaction_guards_never_claim_terminal_or_q29_evidence(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
) -> None:
    actor, ledger, source, destination = transaction_scope
    headers: Any = submission_headers(actor.id)
    content = json.dumps(transaction_body("expense", source, destination))
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
        path = f"/api/finance/ledgers/{ledger}/transactions"
        reply = await client.post(path, content=content, headers=headers)
        assert reply.status_code == status
        assert "submissionReceipt" not in reply.json()
        assert "commandValidationRejection" not in reply.json()
        assert (await client.get(path)).json()["items"] == []


async def test_delayed_invalid_attempt_cannot_prove_q29_over_a_committed_binding(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
) -> None:
    actor, ledger, source, destination = transaction_scope
    headers = submission_headers(actor.id)
    body = transaction_body("expense", source, destination)
    arrived, release = asyncio.Event(), asyncio.Event()

    async def delayed_body() -> AsyncIterator[bytes]:
        arrived.set()
        await release.wait()
        yield json.dumps({**body, "transactionDate": "invalid"}).encode()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/transactions"
        delayed = asyncio.create_task(client.post(path, content=delayed_body(), headers=headers))
        await asyncio.wait_for(arrived.wait(), timeout=3)
        try:
            absent = await client.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
            assert absent.status_code == 404
            receipt = (await client.post(path, json=body, headers=headers)).json()
        finally:
            release.set()
        conflict = await asyncio.wait_for(delayed, timeout=3)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "finance_submission_content_conflict"
        assert "commandValidationRejection" not in conflict.json()
        assert (await client.post(path, json=body, headers=headers)).json() == receipt


async def test_different_transfer_submissions_share_sorted_account_locks_and_recover(
    postgres_database_url: str,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, ledger, source, destination = transaction_scope
    first_headers, second_headers = submission_headers(actor.id), submission_headers(actor.id)
    first_body = transaction_body("internalTransfer", source, destination)
    second_body = transaction_body("internalTransfer", destination, source)
    executed, release = asyncio.Event(), asyncio.Event()
    name = "execute_create_internal_transfer_transaction"
    execute = getattr(submissions, name)
    first = True

    async def hold(*args: Any, **kwargs: Any) -> Any:
        nonlocal first
        is_first = first
        first = False
        result = await execute(*args, **kwargs)
        if is_first:
            executed.set()
            await release.wait()
        return result

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger}/transactions"
        with monkeypatch.context() as faults:
            faults.setattr(submissions, "execute_create_internal_transfer_transaction", hold)
            pending = asyncio.create_task(client.post(path, json=first_body, headers=first_headers))
            try:
                await asyncio.wait_for(executed.wait(), timeout=3)
                # The row lock is already held while Finance account locks are held.
                same_key = await client.post(path, json=first_body, headers=first_headers)
                assert same_key.json()["code"] == "finance_submission_busy"
                sharing = await client.post(path, json=second_body, headers=second_headers)
                assert sharing.json()["code"] == "finance_submission_busy"
                assert (await client.get(path)).json()["items"] == []
            finally:
                release.set()
            first_reply = await asyncio.wait_for(pending, timeout=3)
        assert first_reply.status_code == 201
        assert (
            await client.post(path, json=first_body, headers=first_headers)
        ).json() == first_reply.json()
        assert (
            await client.post(path, json=second_body, headers=second_headers)
        ).status_code == 201
        accounts = (await client.get(f"/api/finance/ledgers/{ledger}/accounts")).json()
        assert all(account["currentBalance"]["amount"] == "0.00" for account in accounts)
        assert len((await client.get(path)).json()["items"]) == 2


async def test_disabled_user_after_transaction_admission_preserves_unfinished_evidence(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, ledger, source, destination = transaction_scope
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
        path = f"/api/finance/ledgers/{ledger}/transactions"
        body = transaction_body("expense", source, destination)
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
        assert (await client.get(path)).json()["items"] == []
        assert (await client.post(path, json=body, headers=headers)).status_code == 201
