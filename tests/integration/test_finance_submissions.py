"""Ledger submission protocol through HTTP and protected PostgreSQL."""

import asyncio
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, ReadError, Request, Response
from sqlalchemy import Connection, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance import submissions
from core_console.modules.finance.models import FinanceLedger
from core_console.modules.finance.money import Money
from core_console.modules.finance.service import (
    create_finance_account,
    create_finance_category,
    create_finance_transaction,
    execute_create_finance_ledger,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.finance.submission_schemas import (
    LedgerCreatedReceipt,
    LedgerTerminalProblem,
    LedgerValidationProblem,
)
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import _user, finance_client

pytestmark = pytest.mark.anyio


async def test_terminal_receipt_replays_after_rename(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = _user("submission-replay")
    postgres_session.add(actor)
    await postgres_session.commit()
    key = str(uuid4())
    headers = {
        "Idempotency-Key": key,
        "Finance-Command-Version": "1",
        "Finance-Submission-Owner": str(actor.id),
    }
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        original = await client.post(
            "/api/finance/ledgers", json={"name": " Home "}, headers=headers
        )
        assert original.status_code == 201
        receipt = original.json()
        assert receipt["submissionId"] == key
        resource_id = receipt["outcome"]["resource"]["id"]
        await client.patch(f"/api/finance/ledgers/{resource_id}", json={"name": "Renamed"})
        replay = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        lookup = await client.get(f"/api/finance/submissions/{key}", headers=headers)
        assert replay.status_code == 201
        assert replay.json() == receipt
        assert lookup.json() == {"state": "terminal", "receipt": receipt}
        assert lookup.headers["cache-control"] == "no-store"
        assert (await client.get("/api/finance/ledgers")).json() == [
            {"id": resource_id, "name": "Renamed"}
        ]


@pytest.mark.parametrize("different", [{"name": "Other"}, {"name": "home"}, {"name": " "}])
async def test_bound_content_conflict_never_has_q29_or_rebinds(
    postgres_database_url: str, postgres_session: AsyncSession, different: dict[str, object]
) -> None:
    actor = _user("bound-conflict")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        original = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        changed = await client.post("/api/finance/ledgers", json=different, headers=headers)
        assert changed.status_code == 409
        assert changed.json()["code"] == "finance_submission_content_conflict"
        assert "submissionReceipt" not in changed.json()
        assert "commandValidationRejection" not in changed.json()
        assert (
            await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        ).json() == original.json()


async def test_version_precedence_and_closed_version_keep_original_evidence(
    postgres_database_url: str, postgres_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = _user("versions")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        original = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        mismatched = await client.post(
            "/api/finance/ledgers",
            content="not JSON",
            headers={**headers, "Finance-Command-Version": "99"},
        )
        assert mismatched.json()["code"] == "finance_submission_version_mismatch"
        unsupported = await client.post(
            "/api/finance/ledgers",
            content="not JSON",
            headers={**submission_headers(actor.id), "Finance-Command-Version": "99"},
        )
        assert unsupported.status_code == 422
        assert unsupported.json()["code"] == "finance_command_version_unsupported"
        monkeypatch.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
        closed = await client.post(
            "/api/finance/ledgers", json={"name": " "}, headers=submission_headers(actor.id)
        )
        assert closed.json()["code"] == "finance_command_version_closed"
        replay = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        assert replay.json() == original.json()
        for response in (mismatched, unsupported, closed):
            assert "submissionReceipt" not in response.json()
            assert "commandValidationRejection" not in response.json()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": " \t "},
        {"name": "界" * 101},
        {"name": None},
        {"name": 10},
        {"name": "Home", "unknown": [True, None, {"x": "<script>"}]},
    ],
)
async def test_q29_echoes_exact_invalid_object_without_backend_record(
    postgres_database_url: str, postgres_session: AsyncSession, body: dict[str, object]
) -> None:
    actor = _user("q29")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post("/api/finance/ledgers", json=body, headers=headers)
        assert response.status_code == 422
        parsed = LedgerValidationProblem.model_validate(response.json())
        assert parsed.commandValidationRejection.attempted_body == body
        assert response.json()["commandValidationRejection"] == {
            "kind": "definitivelyNotAdmitted",
            "submissionId": headers["Idempotency-Key"],
            "commandVersion": "1",
            "ownerId": str(actor.id),
            "operation": "createFinanceLedger",
            "targetLedgerId": None,
            "attemptedBody": body,
        }
        assert "submissionReceipt" not in response.json()
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.status_code == 404
        assert (await client.get("/api/finance/ledgers")).json() == []
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceSubmission)) == 0


async def test_q29_does_not_claim_unrepresentable_json_numbers(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = _user("q29-overflow")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = {
        **submission_headers(actor.id),
        "Content-Type": "application/json",
    }
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(
            "/api/finance/ledgers",
            content=b'{"name": 10, "unknown": 1e999}',
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
        assert "commandValidationRejection" not in response.json()
        assert "submissionReceipt" not in response.json()
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceSubmission)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceLedger)) == 0


async def test_q29_does_not_win_when_same_key_is_admitted_while_body_is_read(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = _user("q29-binding-race")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    body_started = asyncio.Event()
    release_body = asyncio.Event()

    async def invalid_body() -> AsyncIterator[bytes]:
        body_started.set()
        await release_body.wait()
        yield b'{"name":" "}'

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        invalid_attempt = asyncio.create_task(
            client.post("/api/finance/ledgers", content=invalid_body(), headers=headers)
        )
        await asyncio.wait_for(body_started.wait(), timeout=1)
        created = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        assert created.status_code == 201
        release_body.set()

        invalid = await asyncio.wait_for(invalid_attempt, timeout=2)
        assert invalid.status_code == 409
        assert invalid.json()["code"] == "finance_submission_content_conflict"
        assert "commandValidationRejection" not in invalid.json()
        assert len((await client.get("/api/finance/ledgers")).json()) == 1


@pytest.mark.parametrize("content", ['{"name":', "[]", "null", '"Home"'])
async def test_uncorrelatable_body_is_generic_nonterminal_validation(
    postgres_database_url: str, postgres_session: AsyncSession, content: str
) -> None:
    actor = _user("uncorrelatable")
    postgres_session.add(actor)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(
            "/api/finance/ledgers", content=content, headers=submission_headers(actor.id)
        )
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
        assert "commandValidationRejection" not in response.json()


@pytest.mark.parametrize(
    "fault",
    [
        "missing-key",
        "missing-version",
        "missing-owner",
        "bad-key",
        "bad-owner",
        "bad-version",
        "repeat-key",
        "repeat-version",
        "repeat-owner",
        "owner-mismatch",
    ],
)
async def test_headers_fail_before_body_interpretation_or_admission(
    postgres_database_url: str, postgres_session: AsyncSession, fault: str
) -> None:
    actor = _user("headers")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    header_name = {
        "key": "Idempotency-Key",
        "version": "Finance-Command-Version",
        "owner": "Finance-Submission-Owner",
    }
    expected = "finance_submission_protocol_invalid"
    mode, field = fault.split("-", 1)
    if mode == "missing":
        del headers[header_name[field]]
        expected = "finance_submission_protocol_required"
    elif mode == "bad":
        headers[header_name[field]] = "not valid"
    elif fault == "owner-mismatch":
        headers["Finance-Submission-Owner"] = str(uuid4())
        expected = "finance_submission_owner_mismatch"
    pairs = list(headers.items())
    if mode == "repeat":
        pairs.append((header_name[field], headers[header_name[field]]))
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post("/api/finance/ledgers", content="not JSON", headers=pairs)
        assert response.status_code == (403 if fault == "owner-mismatch" else 400)
        assert response.json()["code"] == expected
        assert "commandValidationRejection" not in response.json()
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceSubmission)) == 0


async def test_same_uuid_has_independent_user_namespaces_and_lookup_no_disclosure(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actors = [_user("namespace-a"), _user("namespace-b")]
    postgres_session.add_all(actors)
    await postgres_session.commit()
    key = uuid4()
    receipts = []
    for actor in actors:
        async with finance_client(database_url=postgres_database_url, actor=actor) as client:
            headers = submission_headers(actor.id, key)
            response = await client.post(
                "/api/finance/ledgers", json={"name": "Home"}, headers=headers
            )
            assert response.status_code == 201
            receipts.append(response.json())
            assert (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json()[
                "receipt"
            ] == response.json()
    assert receipts[0]["outcome"]["resource"]["id"] != receipts[1]["outcome"]["resource"]["id"]
    actor = actors[0]
    private_key = uuid4()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        await client.post(
            "/api/finance/ledgers",
            json={"name": "Private"},
            headers=submission_headers(actor.id, private_key),
        )
    async with finance_client(database_url=postgres_database_url, actor=actors[1]) as client:
        responses = [
            await client.get(
                f"/api/finance/submissions/{identifier}", headers=submission_headers(actors[1].id)
            )
            for identifier in (private_key, uuid4())
        ]
        assert all(response.status_code == 404 for response in responses)
        assert responses[0].json()["detail"] == responses[1].json()["detail"]
        assert all(response.headers["cache-control"] == "no-store" for response in responses)


async def test_business_rejection_is_replayed_after_name_becomes_available(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = _user("rejected")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        created = await client.post(
            "/api/finance/ledgers", json={"name": "Home"}, headers=submission_headers(actor.id)
        )
        rejected = await client.post("/api/finance/ledgers", json={"name": "HOME"}, headers=headers)
        assert rejected.status_code == 409
        parsed = LedgerTerminalProblem.model_validate(rejected.json())
        assert parsed.submissionReceipt.outcome.kind == "rejected"
        resource = created.json()["outcome"]["resource"]["id"]
        await client.patch(f"/api/finance/ledgers/{resource}", json={"name": "Renamed"})
        replay = await client.post("/api/finance/ledgers", json={"name": "HOME"}, headers=headers)
        assert replay.json() == rejected.json()
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.status_code == 200
        assert lookup.json() == {
            "state": "terminal",
            "receipt": rejected.json()["submissionReceipt"],
        }
        assert (await client.get("/api/finance/ledgers")).json() == [
            {"id": resource, "name": "Renamed"}
        ]
        fresh = await client.post(
            "/api/finance/ledgers", json={"name": "HOME"}, headers=submission_headers(actor.id)
        )
        assert fresh.status_code == 201


@pytest.mark.parametrize("fail_commit", [1, 2])
async def test_lost_commit_acknowledgement_reads_actual_outcome_on_retry(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    fail_commit: int,
) -> None:
    actor = _user("lost-ack")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    real_commit = AsyncSession.commit
    commits = 0

    async def lose_ack(session: AsyncSession) -> None:
        nonlocal commits
        await real_commit(session)
        commits += 1
        if commits == fail_commit:
            raise ConnectionError("test transport lost commit acknowledgement")

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        with monkeypatch.context() as faults:
            faults.setattr(AsyncSession, "commit", lose_ack)
            response = await client.post(
                "/api/finance/ledgers", json={"name": "Home"}, headers=headers
            )
        assert response.status_code == 500
        assert "submissionReceipt" not in response.json()
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        state = lookup.json()
        assert state["state"] == ("unfinished" if fail_commit == 1 else "terminal")
        assert "canonicalCommand" not in state and "body" not in state
        if fail_commit == 1:
            assert (await client.get("/api/finance/ledgers")).json() == []
            monkeypatch.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
        replay = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        assert replay.status_code == 201
        LedgerCreatedReceipt.model_validate(replay.json())
        if fail_commit == 2:
            assert replay.json() == state["receipt"]
        else:
            assert replay.json()["admittedAt"] == state["admittedAt"]
        assert len((await client.get("/api/finance/ledgers")).json()) == 1


async def test_failure_after_financial_flush_rolls_back_only_execution(
    postgres_database_url: str, postgres_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = _user("failed-projection")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    real_flush = AsyncSession.flush

    async def failed_projection(session: AsyncSession, objects: Any = None) -> None:
        await real_flush(session, objects)
        if any(isinstance(row, FinanceLedger) for row in session.identity_map.values()):
            raise RuntimeError("test projection failure after Ledger flush")

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        with monkeypatch.context() as faults:
            faults.setattr(AsyncSession, "flush", failed_projection)
            response = await client.post(
                "/api/finance/ledgers", json={"name": "Home"}, headers=headers
            )
        assert response.status_code == 500
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == "unfinished"
        assert (await client.get("/api/finance/ledgers")).json() == []
        assert (
            await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        ).status_code == 201


def _unfinished(owner_id: UUID, key: UUID, name: str = "Home") -> FinanceSubmission:
    """Hold external DB state at the durable admission boundary for fault tests."""
    return FinanceSubmission(
        local_user_id=owner_id,
        submission_id=key,
        command_version="1",
        canonical_command={
            "commandVersion": "1",
            "operation": "createFinanceLedger",
            "targetLedgerId": None,
            "body": {"name": name},
        },
    )


@pytest.mark.parametrize("different_content", [False, True])
async def test_concurrent_first_admissions_converge_under_submission_key_lock(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    different_content: bool,
) -> None:
    actor = _user("admission-race")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        responses = await asyncio.wait_for(
            asyncio.gather(
                client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers),
                client.post(
                    "/api/finance/ledgers",
                    json={"name": "Different" if different_content else " Home "},
                    headers=headers,
                ),
            ),
            timeout=5,
        )
        assert any(response.status_code == 201 for response in responses)
        for response in responses:
            assert response.status_code in (201, 409)
            if response.status_code == 409:
                assert response.json()["code"] in {
                    "finance_submission_busy",
                    "finance_submission_content_conflict",
                }
        # A bounded busy response is allowed under load. Once the competing
        # request finishes, explicit retry must converge to replay or conflict.
        settled = [
            await client.post("/api/finance/ledgers", json={"name": name}, headers=headers)
            for name in ("Home", "Different" if different_content else " Home ")
        ]
        assert sorted(response.status_code for response in settled) == (
            [201, 409] if different_content else [201, 201]
        )
        if different_content:
            conflict = next(response for response in settled if response.status_code == 409)
            assert conflict.json()["code"] == "finance_submission_content_conflict"
        else:
            assert settled[0].json() == settled[1].json()
        assert len((await client.get("/api/finance/ledgers")).json()) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceSubmission)) == 1


@pytest.mark.parametrize("stage", ["admission", "execution"])
async def test_real_lock_contention_is_bounded_and_retry_retains_identity(
    postgres_database_url: str, postgres_session: AsyncSession, stage: str
) -> None:
    actor = _user("bounded-" + stage)
    postgres_session.add(actor)
    await postgres_session.commit()
    key = uuid4()
    headers = submission_headers(actor.id, key)
    held = _unfinished(actor.id, key)
    postgres_session.add(held)
    await postgres_session.flush()
    if stage == "execution":
        await postgres_session.commit()
        await postgres_session.execute(
            select(FinanceSubmission)
            .where(
                FinanceSubmission.local_user_id == actor.id, FinanceSubmission.submission_id == key
            )
            .with_for_update()
        )
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        lookup = await client.get(f"/api/finance/submissions/{key}", headers=headers)
        assert lookup.status_code == (404 if stage == "admission" else 200)
        start = monotonic()
        busy = await asyncio.wait_for(
            client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers), timeout=3
        )
        elapsed = monotonic() - start
        assert 0.20 <= elapsed < 2.0
        assert busy.status_code == 409
        assert busy.json()["code"] == "finance_submission_busy"
        assert (
            "submissionReceipt" not in busy.json()
            and "commandValidationRejection" not in busy.json()
        )
        assert (await client.get("/api/finance/ledgers")).json() == []
        # The original admission can still commit after an absent lookup/busy.
        await postgres_session.commit()
        lookup = await client.get(f"/api/finance/submissions/{key}", headers=headers)
        assert lookup.json()["state"] == "unfinished"
        receipt = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        assert receipt.status_code == 201
        assert receipt.json()["submissionId"] == str(key)
        assert len((await client.get("/api/finance/ledgers")).json()) == 1


async def test_disabled_user_cannot_disclose_or_execute_known_binding(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = _user("disabled-submission")
    postgres_session.add(actor)
    await postgres_session.commit()
    key = uuid4()
    postgres_session.add(_unfinished(actor.id, key))
    await postgres_session.commit()
    actor.status = "disabled"
    await postgres_session.commit()
    headers = submission_headers(actor.id, key)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        lookup = await client.get(f"/api/finance/submissions/{key}", headers=headers)
        post = await client.post("/api/finance/ledgers", json={"name": " "}, headers=headers)
        assert lookup.status_code == post.status_code == 403
        assert lookup.headers["cache-control"] == "no-store"
        assert lookup.json()["code"] == post.json()["code"] == "access_denied"
        assert "commandValidationRejection" not in post.json()
    actor.status = "active"
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        assert (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json()[
            "state"
        ] == "unfinished"
        assert (
            await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        ).status_code == 201


async def test_lookup_errors_are_never_cacheable(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = _user("lookup-cache")
    postgres_session.add(actor)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        responses = [
            await client.get(f"/api/finance/submissions/{uuid4()}"),
            await client.get(
                f"/api/finance/submissions/{uuid4()}",
                headers={"Finance-Submission-Owner": str(uuid4())},
            ),
            await client.get(
                "/api/finance/submissions/not-a-uuid", headers=submission_headers(actor.id)
            ),
        ]
        assert [response.status_code for response in responses] == [400, 403, 422]
        assert all(response.headers["cache-control"] == "no-store" for response in responses)


async def test_terminal_evidence_is_immutable_and_retention_blocks_cascade_loss(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = _user("retention")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        created = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        resource_id = created.json()["outcome"]["resource"]["id"]
        for sql in (
            "UPDATE finance_submissions SET canonical_command = '{}'::jsonb",
            "UPDATE finance_submissions SET resolved_at = resolved_at + interval '1 second'",
            "UPDATE finance_submissions SET terminal_outcome = NULL, "
            "resolved_at = NULL, retention_ledger_id = NULL",
            f"DELETE FROM finance_ledgers WHERE id = '{resource_id}'",
            f"DELETE FROM users WHERE id = '{actor.id}'",
        ):
            with pytest.raises(IntegrityError):
                await postgres_session.execute(text(sql))
            await postgres_session.rollback()
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["receipt"] == created.json()


async def test_submission_migration_preserves_populated_finance_and_refuses_evidence_erasure(
    postgres_engine: AsyncEngine, postgres_session: AsyncSession, postgres_database_url: str
) -> None:
    actor = _user("populated-migration")
    postgres_session.add(actor)
    await postgres_session.flush()
    ledger = FinanceLedger(owner_id=actor.id, name="Historical", name_key="historical")
    postgres_session.add(ledger)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        account = await create_finance_account(
            postgres_session,
            owner_id=actor.id,
            ledger_id=ledger.id,
            name="Cash",
            nature="asset",
            currency="CNY",
            opening_balance=Money.parse(amount="100.00", currency="CNY"),
            tracking_start_date=date(2026, 8, 1),
        )
        category = await create_finance_category(
            postgres_session, owner_id=actor.id, ledger_id=ledger.id, name="Food"
        )
        # Historical events have no reliable submission identity. Seed through
        # the existing service seam so the additive relation remains empty.
        transaction_id = await create_finance_transaction(
            postgres_session,
            owner_id=actor.id,
            ledger_id=ledger.id,
            kind="expense",
            account_id=account.account.id,
            transaction_date=date(2026, 8, 1),
            economic_amount=Money.parse(amount="12.34", currency="CNY"),
            allocation_amount=Money.parse(amount="12.34", currency="CNY"),
            category_id=category.id,
            note=None,
            project=lambda detail: detail.transaction.id,
        )
        assert (
            await client.get(f"/api/finance/ledgers/{ledger.id}/transactions/{transaction_id}")
        ).status_code == 200
        paths = [
            "/api/finance/ledgers",
            *(
                f"/api/finance/ledgers/{ledger.id}/{resource}"
                for resource in ("accounts", "categories", "transactions")
            ),
        ]
        before = [(await client.get(path)).json() for path in paths]
    # Downgrade only the empty additive relation, then apply to populated data.
    async with postgres_engine.begin() as connection:

        def apply(sync_connection: Connection) -> None:
            config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            config.attributes["connection"] = sync_connection
            command.downgrade(config, "20260825_01")
            command.upgrade(config, "head")

        await connection.run_sync(apply)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        assert [(await client.get(path)).json() for path in paths] == before
    assert (
        await postgres_session.scalar(
            select(FinanceLedger.name).where(FinanceLedger.id == ledger.id)
        )
        == "Historical"
    )
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceSubmission)) == 0
    postgres_session.add(_unfinished(actor.id, uuid4()))
    await postgres_session.commit()
    async with postgres_engine.connect() as connection:

        def erase(sync_connection: Connection) -> None:
            config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            config.attributes["connection"] = sync_connection
            command.downgrade(config, "20260825_01")

        with pytest.raises(Exception, match="enforcement must be preserved"):
            await connection.run_sync(erase)
        await connection.rollback()
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceSubmission)) == 1


async def test_committed_http_response_loss_is_recovered_without_another_ledger(
    postgres_database_url: str, postgres_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = _user("response-loss")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    real_handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await real_handle(transport, request)
        assert response.status_code == 201
        raise ReadError("test transport dropped response after backend completion", request=request)

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        with monkeypatch.context() as faults:
            faults.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == "terminal"
        replay = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        assert replay.status_code == 201
        assert replay.json() == lookup.json()["receipt"]
        assert len((await client.get("/api/finance/ledgers")).json()) == 1


async def test_recognized_rejection_rolls_back_tentative_financial_work_before_receipt(
    postgres_database_url: str, postgres_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = _user("rejection-rollback")
    postgres_session.add(actor)
    await postgres_session.commit()
    real_execute = execute_create_finance_ledger

    async def tentative_then_conflict(
        session: AsyncSession, *, owner_id: UUID, name: str
    ) -> FinanceLedger:
        await real_execute(session, owner_id=owner_id, name="Tentative")
        return await real_execute(session, owner_id=owner_id, name=name)

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        created = await client.post(
            "/api/finance/ledgers", json={"name": "Home"}, headers=submission_headers(actor.id)
        )
        headers = submission_headers(actor.id)
        with monkeypatch.context() as faults:
            faults.setattr(submissions, "execute_create_finance_ledger", tentative_then_conflict)
            rejected = await client.post(
                "/api/finance/ledgers", json={"name": "Home"}, headers=headers
            )
        assert rejected.status_code == 409
        assert rejected.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
        assert (await client.get("/api/finance/ledgers")).json() == [
            {"id": created.json()["outcome"]["resource"]["id"], "name": "Home"}
        ]


async def test_access_loss_after_admission_preserves_unfinished_binding(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _user("access-lost")
    postgres_session.add(actor)
    await postgres_session.commit()
    owner_id = actor.id
    headers = submission_headers(owner_id)
    real_commit = AsyncSession.commit

    async def disable_after_admission(session: AsyncSession) -> None:
        await real_commit(session)
        async with AsyncSession(postgres_engine) as other:
            await other.execute(update(User).where(User.id == owner_id).values(status="disabled"))
            await real_commit(other)

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        with monkeypatch.context() as faults:
            faults.setattr(AsyncSession, "commit", disable_after_admission)
            denied = await client.post(
                "/api/finance/ledgers", json={"name": "Home"}, headers=headers
            )
        assert denied.status_code == 403
        assert denied.json()["code"] == "access_denied"
        assert "submissionReceipt" not in denied.json()
    await postgres_session.execute(update(User).where(User.id == owner_id).values(status="active"))
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == "unfinished"
        assert (await client.get("/api/finance/ledgers")).json() == []
        assert (
            await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        ).status_code == 201
