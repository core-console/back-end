"""Account and Category durable submissions through HTTP and PostgreSQL."""

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, ReadError, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance import submissions
from core_console.modules.finance.models import FinanceAccount, FinanceCategory, FinanceLedger
from core_console.modules.finance.service import (
    FinanceCategoryNameConflictError,
    InvalidFinanceAccountMoneyError,
)
from core_console.modules.finance.submission_commands import AccountCommandV1
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import _user, finance_client

pytestmark = pytest.mark.anyio

ACCOUNT: dict[str, object] = {
    "name": " Cash ",
    "nature": "asset",
    "currency": "CNY",
    "openingBalance": {"amount": "0012.3", "currency": "CNY"},
    "trackingStartDate": "2026-10-01",
}


async def test_account_retry_retains_one_opening_position(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = _user("account-submission")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post(
            "/api/finance/ledgers", json={"name": "Home"}, headers=submission_headers(actor.id)
        )
        ledger_id = ledger.json()["outcome"]["resource"]["id"]
        path = f"/api/finance/ledgers/{ledger_id}/accounts"
        original = await client.post(path, json=ACCOUNT, headers=headers)
        assert original.status_code == 201
        receipt = original.json()
        assert receipt["submissionId"] == headers["Idempotency-Key"]
        assert receipt["operation"] == "createFinanceAccount"
        assert receipt["targetLedgerId"] == ledger_id
        replay = await client.post(
            path,
            json={
                **ACCOUNT,
                "name": "Cash",
                "openingBalance": {"amount": "12.30", "currency": "CNY"},
            },
            headers=headers,
        )
        assert replay.json() == receipt
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json() == {"state": "terminal", "receipt": receipt}
        accounts = (await client.get(path)).json()
        assert len(accounts) == 1
        assert accounts[0]["openingBalance"] == {"amount": "12.30", "currency": "CNY"}
        assert accounts[0]["currentBalance"] == accounts[0]["openingBalance"]
        assert accounts[0]["id"] == receipt["outcome"]["resource"]["id"]
        assert (await client.get(f"/api/finance/ledgers/{ledger_id}/transactions")).json()[
            "items"
        ] == []


async def test_category_name_rejection_survives_later_eligibility(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = _user("category-rejection")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post(
            "/api/finance/ledgers", json={"name": "Home"}, headers=submission_headers(actor.id)
        )
        ledger_id = ledger.json()["outcome"]["resource"]["id"]
        path = f"/api/finance/ledgers/{ledger_id}/categories"
        created = await client.post(
            path, json={"name": "Food"}, headers=submission_headers(actor.id)
        )
        rejected = await client.post(path, json={"name": " FOOD "}, headers=headers)
        assert rejected.status_code == 409
        evidence = rejected.json()["submissionReceipt"]
        assert evidence["operation"] == "createFinanceCategory"
        assert evidence["targetLedgerId"] == ledger_id
        assert evidence["outcome"]["problem"]["code"] == "finance_category_name_conflict"
        assert "instance" not in evidence["outcome"]["problem"]
        resource_id = created.json()["outcome"]["resource"]["id"]
        assert (
            await client.patch(f"{path}/{resource_id}", json={"name": "Renamed"})
        ).status_code == 200
        replay = await client.post(path, json={"name": "FOOD"}, headers=headers)
        assert replay.json() == rejected.json()
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json() == {"state": "terminal", "receipt": evidence}
        assert lookup.headers["cache-control"] == "no-store"
        assert len((await client.get(path)).json()) == 1
        assert (
            await client.post(path, json={"name": "FOOD"}, headers=submission_headers(actor.id))
        ).status_code == 201


@pytest.fixture
async def nested_scope(postgres_session: AsyncSession) -> tuple[User, UUID]:
    actor = _user("nested-owner")
    postgres_session.add(actor)
    await postgres_session.flush()
    ledger = FinanceLedger(owner_id=actor.id, name="Home", name_key="home")
    postgres_session.add(ledger)
    await postgres_session.commit()
    return actor, ledger.id


def _body(resource: str) -> dict[str, object]:
    return ACCOUNT if resource == "accounts" else {"name": "Food"}


def _bound(actor: User, ledger_id: UUID, key: UUID, resource: str) -> FinanceSubmission:
    return FinanceSubmission(
        local_user_id=actor.id,
        submission_id=key,
        command_version="1",
        retention_ledger_id=ledger_id,
        canonical_command={
            "commandVersion": "1",
            "operation": "createFinanceAccount"
            if resource == "accounts"
            else "createFinanceCategory",
            "targetLedgerId": str(ledger_id),
            "body": AccountCommandV1.model_validate(ACCOUNT).canonical()
            if resource == "accounts"
            else {"name": "Food"},
        },
    )


@pytest.mark.parametrize("resource", ["accounts", "categories"])
@pytest.mark.parametrize("different", [False, True])
async def test_concurrent_nested_admissions_have_one_effect(
    postgres_database_url: str, nested_scope: tuple[User, UUID], resource: str, different: bool
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    body = _body(resource)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        replies = await asyncio.wait_for(
            asyncio.gather(
                client.post(path, json=body, headers=headers),
                client.post(
                    path,
                    json={**body, "name": "Different" if different else body["name"]},
                    headers=headers,
                ),
            ),
            timeout=5,
        )
        assert any(reply.status_code == 201 for reply in replies)
        for reply in replies:
            assert reply.status_code in (201, 409)
            if reply.status_code == 409:
                assert reply.json()["code"] in {
                    "finance_submission_busy",
                    "finance_submission_content_conflict",
                }
        settled = [
            await client.post(path, json=attempt, headers=headers)
            for attempt in (body, {**body, "name": "Different" if different else body["name"]})
        ]
        assert sorted(reply.status_code for reply in settled) == (
            [201, 409] if different else [201, 201]
        )
        if different:
            assert (
                next(reply for reply in settled if reply.status_code == 409).json()["code"]
                == "finance_submission_content_conflict"
            )
        else:
            assert settled[0].json() == settled[1].json()
        assert len((await client.get(path)).json()) == 1
        if resource == "accounts":
            assert (await client.get(path)).json()[0]["currentBalance"]["amount"] == "12.30"
            # A fresh key deliberately creates another identical position.
            additional = await client.post(path, json=body, headers=submission_headers(actor.id))
            assert additional.status_code == 201
            assert len((await client.get(path)).json()) == 2


@pytest.mark.parametrize("resource", ["accounts", "categories"])
@pytest.mark.parametrize("phase", ["admission_ack", "terminal_ack", "before_terminal_commit"])
async def test_nested_failure_recovery_preserves_atomic_effect_and_receipt(
    postgres_database_url: str,
    nested_scope: tuple[User, UUID],
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
    phase: str,
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    real_commit = AsyncSession.commit
    commits = 0

    async def fault(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_terminal_commit":
            raise RuntimeError("fault after financial and terminal flush")
        await real_commit(session)
        if commits == (1 if phase == "admission_ack" else 2):
            raise ConnectionError("lost acknowledgement")

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        with monkeypatch.context() as faults:
            faults.setattr(AsyncSession, "commit", fault)
            failure = await client.post(path, json=_body(resource), headers=headers)
        assert failure.status_code == 500
        assert "submissionReceipt" not in failure.json()
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        evidence = lookup.json()
        assert evidence["state"] == ("terminal" if phase == "terminal_ack" else "unfinished")
        assert "body" not in evidence
        assert len((await client.get(path)).json()) == (1 if phase == "terminal_ack" else 0)
        if phase != "terminal_ack":
            monkeypatch.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
        retry = await client.post(path, json=_body(resource), headers=headers)
        assert retry.status_code == 201
        if phase == "terminal_ack":
            assert retry.json() == evidence["receipt"]
        else:
            assert retry.json()["admittedAt"] == evidence["admittedAt"]
        assert len((await client.get(path)).json()) == 1


@pytest.mark.parametrize("resource", ["accounts", "categories"])
async def test_recognized_rejection_rolls_back_tentative_nested_writes(
    postgres_database_url: str,
    nested_scope: tuple[User, UUID],
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    name = (
        "execute_create_finance_account"
        if resource == "accounts"
        else "execute_create_finance_category"
    )
    execute = getattr(submissions, name)

    async def reject(*args: Any, **kwargs: Any) -> Any:
        await execute(*args, **kwargs)
        if resource == "accounts":
            raise InvalidFinanceAccountMoneyError(
                "Opening Balance currency must match the Account currency."
            )
        raise FinanceCategoryNameConflictError

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        with monkeypatch.context() as faults:
            faults.setattr(submissions, name, reject)
            failure = await client.post(path, json=_body(resource), headers=headers)
        assert failure.status_code == (422 if resource == "accounts" else 409)
        assert failure.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
        assert "commandValidationRejection" not in failure.json()
        assert (await client.get(path)).json() == []
        retry = await client.post(path, json=_body(resource), headers=headers)
        assert retry.json() == failure.json()
        assert (await client.get(path)).json() == []


@pytest.mark.parametrize("resource", ["accounts", "categories"])
@pytest.mark.parametrize("stage", ["admission", "execution"])
async def test_nested_postgresql_waits_are_bounded_and_recoverable(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
    resource: str,
    stage: str,
) -> None:
    actor, ledger_id = nested_scope
    key = uuid4()
    headers = submission_headers(actor.id, key)
    postgres_session.add(_bound(actor, ledger_id, key, resource))
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
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        start = monotonic()
        busy = await asyncio.wait_for(
            client.post(path, json=_body(resource), headers=headers), timeout=3
        )
        assert 0.20 <= monotonic() - start < 2.0
        assert busy.status_code == 409
        assert busy.json()["code"] == "finance_submission_busy"
        assert "submissionReceipt" not in busy.json()
        assert "commandValidationRejection" not in busy.json()
        assert (await client.get(path)).json() == []
        await postgres_session.commit()
        lookup = await client.get(f"/api/finance/submissions/{key}", headers=headers)
        assert lookup.json()["state"] == "unfinished"
        assert lookup.json()["targetLedgerId"] == str(ledger_id)
        retry = await client.post(path, json=_body(resource), headers=headers)
        assert retry.status_code == 201
        assert retry.json()["submissionId"] == str(key)


@pytest.mark.parametrize("resource", ["accounts", "categories"])
async def test_nested_identity_survives_edits_archive_and_resource_deletion(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
    resource: str,
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        original = await client.post(path, json=_body(resource), headers=headers)
        receipt = original.json()
        resource_id = receipt["outcome"]["resource"]["id"]
        assert (
            await client.patch(f"{path}/{resource_id}", json={"name": "Edited"})
        ).status_code == 200
        assert (await client.post(f"{path}/{resource_id}/archive")).status_code == 200
        assert (await client.post(path, json=_body(resource), headers=headers)).json() == receipt
        model = FinanceAccount if resource == "accounts" else FinanceCategory
        # Internal deletion exercises evidence lifetime; there is no new delete API.
        await postgres_session.execute(delete(model).where(model.id == UUID(resource_id)))
        await postgres_session.commit()
        assert (await client.post(path, json=_body(resource), headers=headers)).json() == receipt
        assert (await client.get(path)).json() == []
        assert (
            await client.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
        ).json() == {"state": "terminal", "receipt": receipt}


@pytest.mark.parametrize(
    "invalid",
    [
        {"name": " "},
        {"name": "界" * 101},
        {"nature": "equity"},
        {"currency": "EUR"},
        {"openingBalance": {"amount": 10, "currency": "CNY"}},
        {"openingBalance": {"amount": True, "currency": "CNY"}},
        {"openingBalance": {"amount": "1.001", "currency": "CNY"}},
        {"openingBalance": {"amount": "1e2", "currency": "CNY"}},
        {"openingBalance": {"amount": "NaN", "currency": "CNY"}},
        {"openingBalance": {"amount": "10", "currency": "USD"}},
        {"openingBalance": {"amount": "10", "currency": "CNY", "extra": None}},
        {"openingBalance": {"amount": "1" + "0" * 131072, "currency": "CNY"}},
        {"trackingStartDate": "2026-02-30"},
        {"trackingStartDate": "0000-01-01"},
        {"trackingStartDate": "2026-10-01T00:00:00"},
        {"trackingStartDate": 1790812800},
        {"extra": [True, None, {"x": "<script>"}]},
    ],
)
async def test_account_q29_proves_frozen_invalidity_and_echoes_exact_object(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
    invalid: dict[str, object],
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    body = {**ACCOUNT, **invalid}
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/accounts"
        rejected = await client.post(path, json=body, headers=headers)
        assert rejected.status_code == 422
        assert rejected.headers["content-type"].startswith("application/problem+json")
        assert rejected.json()["commandValidationRejection"] == {
            "kind": "definitivelyNotAdmitted",
            "submissionId": headers["Idempotency-Key"],
            "commandVersion": "1",
            "ownerId": str(actor.id),
            "operation": "createFinanceAccount",
            "targetLedgerId": str(ledger_id),
            "attemptedBody": body,
        }
        assert "submissionReceipt" not in rejected.json()
        assert (await client.get(path)).json() == []
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.status_code == 404
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceSubmission)) == 0


@pytest.mark.parametrize("resource", ["accounts", "categories"])
@pytest.mark.parametrize(
    "raw", ["[]", "null", "{", '{"name":"Food","extra":NaN}', '{"name":"Food","extra":1e999}']
)
async def test_nested_uncorrelatable_json_is_ordinary_unresolved_validation(
    postgres_database_url: str, nested_scope: tuple[User, UUID], resource: str, raw: str
) -> None:
    actor, ledger_id = nested_scope
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        reply = await client.post(
            f"/api/finance/ledgers/{ledger_id}/{resource}",
            content=raw,
            headers=submission_headers(actor.id),
        )
        assert reply.status_code == 422
        assert reply.json()["code"] == "validation_error"
        assert "commandValidationRejection" not in reply.json()
        assert "submissionReceipt" not in reply.json()


@pytest.mark.parametrize(
    ("currency", "amount", "equivalent", "expected"),
    [
        ("CNY", "-000.0", "0.00", "0.00"),
        ("USD", "00012", "12.00", "12.00"),
        ("JPY", "-0012", "-12", "-12"),
        (
            "USD",
            "-123456789012345678901234567890123456789.01",
            "-0123456789012345678901234567890123456789.01",
            "-123456789012345678901234567890123456789.01",
        ),
    ],
)
@pytest.mark.parametrize("nature", ["asset", "liability"])
async def test_account_money_canonicalization_preserves_exact_position_and_sign(
    postgres_database_url: str,
    nested_scope: tuple[User, UUID],
    currency: str,
    amount: str,
    equivalent: str,
    expected: str,
    nature: str,
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    body = {
        **ACCOUNT,
        "nature": nature,
        "currency": currency,
        "openingBalance": {"amount": amount, "currency": currency},
    }
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/accounts"
        created = await client.post(path, json=body, headers=headers)
        assert created.status_code == 201
        retry = await client.post(
            path,
            json={**body, "openingBalance": {"currency": currency, "amount": equivalent}},
            headers=headers,
        )
        assert retry.json() == created.json()
        accounts = (await client.get(path)).json()
        assert len(accounts) == 1
        assert accounts[0]["openingBalance"] == {"amount": expected, "currency": currency}
        assert accounts[0]["currentBalance"] == accounts[0]["openingBalance"]
        assert accounts[0]["nature"] == nature


@pytest.mark.parametrize("resource", ["accounts", "categories"])
async def test_common_namespace_version_and_scope_precedence(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
    resource: str,
) -> None:
    actor, ledger_id = nested_scope
    other = FinanceLedger(owner_id=actor.id, name="Other", name_key="other")
    postgres_session.add(other)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        created = await client.post(path, json=_body(resource), headers=headers)
        assert created.status_code == 201
        other_resource = "categories" if resource == "accounts" else "accounts"
        for new_path, body in [
            ("/api/finance/ledgers", {"name": "New"}),
            (f"/api/finance/ledgers/{other.id}/{resource}", _body(resource)),
            (f"/api/finance/ledgers/{ledger_id}/{other_resource}", _body(other_resource)),
            (path, {**_body(resource), "name": "Different"}),
            (path, {"name": " "}),
        ]:
            conflict = await client.post(new_path, json=body, headers=headers)
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "finance_submission_content_conflict"
            assert "submissionReceipt" not in conflict.json()
            assert "commandValidationRejection" not in conflict.json()
        mismatched = await client.post(
            path, content="invalid", headers={**headers, "Finance-Command-Version": "99"}
        )
        assert mismatched.json()["code"] == "finance_submission_version_mismatch"
        unsupported = await client.post(
            path,
            content="invalid",
            headers={**submission_headers(actor.id), "Finance-Command-Version": "99"},
        )
        assert unsupported.json()["code"] == "finance_command_version_unsupported"
        inaccessible = await client.post(
            f"/api/finance/ledgers/{uuid4()}/{resource}",
            json={"name": " "},
            headers={**headers, "Finance-Command-Version": "99"},
        )
        assert inaccessible.status_code == 404
        assert inaccessible.json()["code"] == "finance_ledger_not_found"
        assert "commandValidationRejection" not in inaccessible.json()
        assert (
            await client.post(path, json=_body(resource), headers=headers)
        ).json() == created.json()


@pytest.mark.parametrize("field", ["nature", "currency", "openingBalance", "trackingStartDate"])
async def test_changed_account_meaning_conflicts_without_touching_position(
    postgres_database_url: str, nested_scope: tuple[User, UUID], field: str
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    changes: dict[str, object] = {
        "nature": "liability",
        "currency": "USD",
        "openingBalance": {"amount": "12.31", "currency": "CNY"},
        "trackingStartDate": "2026-10-02",
    }
    body = {**ACCOUNT, field: changes[field]}
    if field == "currency":
        body["openingBalance"] = {"amount": "12.30", "currency": "USD"}
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/accounts"
        original = await client.post(path, json=ACCOUNT, headers=headers)
        conflict = await client.post(path, json=body, headers=headers)
        assert conflict.json()["code"] == "finance_submission_content_conflict"
        assert (await client.post(path, json=ACCOUNT, headers=headers)).json() == original.json()
        assert (await client.get(path)).json()[0]["openingBalance"]["amount"] == "12.30"


@pytest.mark.parametrize("resource", ["accounts", "categories"])
async def test_disabled_user_after_admission_preserves_undisclosed_binding(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    real_commit = AsyncSession.commit
    first = True

    async def lose_access(session: AsyncSession) -> None:
        nonlocal first
        await real_commit(session)
        if first:
            first = False
            actor.status = "disabled"
            await real_commit(postgres_session)

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        with monkeypatch.context() as faults:
            faults.setattr(AsyncSession, "commit", lose_access)
            denied = await client.post(path, json=_body(resource), headers=headers)
        assert denied.status_code == 403
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.status_code == 403
        assert "submissionReceipt" not in denied.json()
    actor.status = "active"
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == "unfinished"
        assert (await client.get(path)).json() == []
        assert (await client.post(path, json=_body(resource), headers=headers)).status_code == 201


@pytest.mark.parametrize("resource", ["accounts", "categories"])
@pytest.mark.parametrize(
    "guard",
    ["missing_headers", "wrong_owner", "unsupported_version", "closed_version", "invalid_version"],
)
async def test_nested_guards_never_supply_terminal_or_q29_evidence(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
    guard: str,
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    expected = 400
    if guard == "missing_headers":
        headers = {}
    elif guard == "wrong_owner":
        headers["Finance-Submission-Owner"] = str(uuid4())
        expected = 403
    elif guard == "unsupported_version":
        headers["Finance-Command-Version"] = "99"
        expected = 422
    elif guard == "closed_version":
        monkeypatch.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
        expected = 409
    else:
        headers["Finance-Command-Version"] = " 1"
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        failure = await client.post(path, json={"name": " "}, headers=headers)
        assert failure.status_code == expected
        assert "commandValidationRejection" not in failure.json()
        assert "submissionReceipt" not in failure.json()
        assert (await client.get(path)).json() == []
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceSubmission)) == 0


@pytest.mark.parametrize("resource", ["accounts", "categories"])
async def test_different_users_have_independent_nested_submission_namespaces(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
    resource: str,
) -> None:
    actor, ledger_id = nested_scope
    other = _user("independent-nested-owner")
    postgres_session.add(other)
    await postgres_session.flush()
    other_ledger = FinanceLedger(owner_id=other.id, name="Home", name_key="home")
    postgres_session.add(other_ledger)
    await postgres_session.commit()
    key = uuid4()
    receipts = []
    for user, ledger in [(actor, ledger_id), (other, other_ledger.id)]:
        headers = submission_headers(user.id, key)
        async with finance_client(database_url=postgres_database_url, actor=user) as client:
            path = f"/api/finance/ledgers/{ledger}/{resource}"
            created = await client.post(path, json=_body(resource), headers=headers)
            assert created.status_code == 201
            receipts.append(created.json())
            assert (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json()[
                "receipt"
            ] == created.json()
            foreign_ledger_id = ledger_id if user is other else other_ledger.id
            foreign = await client.post(
                f"/api/finance/ledgers/{foreign_ledger_id}/{resource}",
                json=_body(resource),
                headers=headers,
            )
            assert foreign.status_code == 404
            assert "submissionReceipt" not in foreign.json()
    assert receipts[0]["outcome"]["resource"]["id"] != receipts[1]["outcome"]["resource"]["id"]


async def test_old_account_q29_rules_do_not_follow_mutable_money_catalog(
    postgres_database_url: str, nested_scope: tuple[User, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_console.modules.finance.money import SUPPORTED_CURRENCY_MINOR_UNITS

    actor, ledger_id = nested_scope
    monkeypatch.setitem(SUPPORTED_CURRENCY_MINOR_UNITS, "JPY", 2)
    body = {**ACCOUNT, "currency": "JPY", "openingBalance": {"amount": "1.01", "currency": "JPY"}}
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        failure = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json=body,
            headers=submission_headers(actor.id),
        )
        assert failure.status_code == 422
        assert failure.json()["commandValidationRejection"]["attemptedBody"] == body


@pytest.mark.parametrize("resource", ["accounts", "categories"])
async def test_committed_nested_response_loss_recovers_original_receipt(
    postgres_database_url: str,
    nested_scope: tuple[User, UUID],
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    real_handle = ASGITransport.handle_async_request

    async def drop(transport: ASGITransport, request: Request) -> Response:
        reply = await real_handle(transport, request)
        assert reply.status_code == 201
        raise ReadError("lost response after committed create", request=request)

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        with monkeypatch.context() as faults:
            faults.setattr(ASGITransport, "handle_async_request", drop)
            with pytest.raises(ReadError):
                await client.post(path, json=_body(resource), headers=headers)
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == "terminal"
        replay = await client.post(path, json=_body(resource), headers=headers)
        assert replay.json() == lookup.json()["receipt"]
        assert len((await client.get(path)).json()) == 1


@pytest.mark.parametrize("resource", ["accounts", "categories"])
async def test_delayed_invalid_nested_attempt_cannot_prove_q29_over_admitted_binding(
    postgres_database_url: str, nested_scope: tuple[User, UUID], resource: str
) -> None:
    actor, ledger_id = nested_scope
    headers = submission_headers(actor.id)
    arrived = asyncio.Event()
    release = asyncio.Event()

    async def delayed_body() -> AsyncIterator[bytes]:
        arrived.set()
        await release.wait()
        yield json.dumps({**_body(resource), "name": " "}).encode()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        path = f"/api/finance/ledgers/{ledger_id}/{resource}"
        delayed = asyncio.create_task(client.post(path, content=delayed_body(), headers=headers))
        await asyncio.wait_for(arrived.wait(), timeout=3)
        created = await client.post(path, json=_body(resource), headers=headers)
        assert created.status_code == 201
        release.set()
        conflict = await asyncio.wait_for(delayed, timeout=3)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "finance_submission_content_conflict"
        assert "commandValidationRejection" not in conflict.json()
        assert "submissionReceipt" not in conflict.json()
        assert (
            await client.post(path, json=_body(resource), headers=headers)
        ).json() == created.json()
