"""Pure Advance behavior through HTTP and the guarded real PostgreSQL target."""

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_models import FinanceAdvance, FinanceObligation
from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceTransaction,
)
from core_console.modules.users.models import User
from core_console.problems import ProblemDetails
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_api import finance_client

pytestmark = pytest.mark.anyio


@pytest.fixture
async def advance_client(
    postgres_database_url: str, postgres_session: AsyncSession
) -> AsyncIterator[tuple[AsyncClient, User, str, dict[str, object]]]:
    actor = User(
        identity_issuer="https://identity.example.test/advances",
        identity_subject="owner",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post(
            "/api/finance/ledgers", json={"name": "Home"}, headers=submission_headers(actor.id)
        )
        assert ledger.status_code == 201
        base = f"/api/finance/ledgers/{ledger.json()['outcome']['resource']['id']}"
        funding = await create_finance_resource(
            client,
            f"{base}/accounts",
            owner_id=actor.id,
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "100.00", "currency": "CNY"},
                "trackingStartDate": "2026-10-01",
            },
        )
        assert funding.status_code == 201
        participant = await client.post(f"{base}/participants", json={"name": "Alex"})
        assert participant.status_code == 201
        draft: dict[str, object] = {
            "fundingAccountId": funding.json()["id"],
            "participantId": participant.json()["id"],
            "transactionDate": "2026-10-20",
            "amount": {"amount": "12.30", "currency": "CNY"},
            "ownerShare": {"amount": "0.00", "currency": "CNY"},
            "participantShare": {"amount": "12.30", "currency": "CNY"},
            "note": "Train ticket",
        }
        yield client, actor, base, draft


async def test_pure_advance_retains_source_and_unpaid_composition_with_no_expense(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    preview = await client.post(f"{base}/advances/preview", json=draft)
    assert preview.status_code == 200
    assert preview.json()["fundingBalanceAfter"] == {"amount": "87.70", "currency": "CNY"}
    assert preview.json()["ownerExpense"] == {"amount": "0.00", "currency": "CNY"}
    assert len((await client.get(f"{base}/accounts")).json()) == 1
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json() == []
    created = await client.post(
        f"{base}/advances",
        json={**draft, "previewToken": preview.json()["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert created.status_code == 201, created.text
    resource = created.json()["outcome"]["resource"]
    assert resource["type"] == "advance"
    detail = (await client.get(f"{base}/advances/{resource['id']}")).json()
    assert detail["fundingAccountId"] == draft["fundingAccountId"]
    assert detail["transactionDate"] == "2026-10-20"
    assert detail["amount"] == {"amount": "12.30", "currency": "CNY"}
    assert detail["ownerShare"] == {"amount": "0.00", "currency": "CNY"}
    obligation = detail["obligation"]
    assert obligation["advanceId"] == resource["id"]
    assert obligation["participantId"] == draft["participantId"]
    assert (
        obligation["principal"] == obligation["remaining"] == {"amount": "12.30", "currency": "CNY"}
    )
    assert (
        obligation["cashPaid"]
        == obligation["forgiven"]
        == obligation["creditApplied"]
        == {"amount": "0.00", "currency": "CNY"}
    )
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json() == [obligation]
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"] == {
        "amount": "87.70",
        "currency": "CNY",
    }
    assert accounts[obligation["receivableAccountId"]]["currentBalance"] == {
        "amount": "12.30",
        "currency": "CNY",
    }
    history = (await client.get(f"{base}/transactions")).json()["items"]
    assert len(history) == 1
    assert history[0]["kind"] == "internalTransfer"


@pytest.mark.parametrize(
    "phase", ["admission_ack", "partial_write", "before_commit", "terminal_ack", "response_loss"]
)
async def test_original_action_recovers_without_partial_or_duplicate_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    preview = await client.post(f"{base}/advances/preview", json=draft)
    command = {**draft, "previewToken": preview.json()["previewToken"]}
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
        partial = any(isinstance(row, FinanceAdvance) for row in session.new)
        await real_flush(session, objects)
        if partial:
            raise RuntimeError("failure after financial aggregate but before Obligation")

    handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await handle(transport, request)
        assert response.status_code == 201
        raise ReadError("committed response lost", request=request)

    with monkeypatch.context() as faults:
        if phase == "response_loss":
            faults.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/advances", json=command, headers=headers)
        else:
            faults.setattr(
                AsyncSession,
                "flush" if phase == "partial_write" else "commit",
                flush_fault if phase == "partial_write" else commit_fault,
            )
            failed = await client.post(f"{base}/advances", json=command, headers=headers)
            assert failed.status_code == 500
            assert "submissionReceipt" not in failed.json()
            assert "commandValidationRejection" not in failed.json()
    # An independent real PostgreSQL session observes committed effects before retry.
    committed = phase in ("terminal_ack", "response_loss")
    counts = [
        await postgres_session.scalar(select(func.count()).select_from(model))
        for model in (
            FinanceAdvance,
            FinanceObligation,
            FinanceTransaction,
            FinanceAccountMovement,
            FinanceAccount,
        )
    ]
    assert counts == ([1, 1, 1, 2, 2] if committed else [0, 0, 0, 0, 1])
    await postgres_session.rollback()
    lookup = await client.get(
        f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
    )
    assert lookup.headers["cache-control"] == "no-store"
    assert lookup.json()["state"] == ("terminal" if committed else "unfinished")
    retry = await client.post(f"{base}/advances", json=command, headers=headers)
    assert retry.status_code == 201, retry.text
    if committed:
        assert retry.json() == lookup.json()["receipt"]
    else:
        assert retry.json()["admittedAt"] == lookup.json()["admittedAt"]
    assert len((await client.get(f"{base}/transactions")).json()["items"]) == 1
    assert len((await client.get(f"{base}/accounts")).json()) == 2
    assert (
        await client.post(f"{base}/advances", json=command, headers=headers)
    ).json() == retry.json()


async def test_linked_financial_records_cannot_be_mutated_through_manual_paths(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    created = await client.post(
        f"{base}/advances",
        json={**draft, "previewToken": preview["previewToken"]},
        headers=submission_headers(actor.id),
    )
    advance_id = created.json()["outcome"]["resource"]["id"]
    original = (await client.get(f"{base}/advances/{advance_id}")).json()
    receivable = original["obligation"]["receivableAccountId"]
    money = {"amount": "1.00", "currency": "CNY"}
    expense = {
        "kind": "expense",
        "accountId": receivable,
        "transactionDate": "2026-10-21",
        "economicAmount": money,
        "categoryAllocations": [{"amount": money}],
        "note": None,
    }
    transfer = {
        "kind": "internalTransfer",
        "sourceAccountId": draft["fundingAccountId"],
        "destinationAccountId": receivable,
        "transactionDate": "2026-10-21",
        "amount": money,
        "note": None,
    }
    adjustment = {
        "accountId": receivable,
        "transactionDate": "2026-10-21",
        "expectedDerivedBalance": {"amount": "12.30", "currency": "CNY"},
        "targetBalance": money,
        "expectedAccountNature": "asset",
        "note": None,
    }
    for path, body in [
        ("transactions", expense),
        ("transactions", transfer),
        ("balance-adjustments", adjustment),
    ]:
        headers = submission_headers(actor.id)
        blocked = await client.post(f"{base}/{path}", json=body, headers=headers)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "finance_linked_record_protected"
        assert blocked.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
        assert (
            await client.post(f"{base}/{path}", json=body, headers=headers)
        ).json() == blocked.json()
    for method, path, mutation_body in [
        ("DELETE", f"transactions/{original['transactionId']}", None),
        ("PUT", f"transactions/{original['transactionId']}", transfer),
        ("PATCH", f"accounts/{receivable}", {"openingBalance": money}),
        ("PATCH", f"accounts/{receivable}", {"trackingStartDate": "2026-10-01"}),
        ("POST", f"accounts/{receivable}/archive", None),
        ("POST", f"accounts/{receivable}/unarchive", None),
    ]:
        blocked = await client.request(method, f"{base}/{path}", json=mutation_body)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "finance_linked_record_protected"
        if method == "DELETE" or path.endswith(("/archive", "/unarchive")):
            contract = (await client.get("/api/openapi.json")).json()
            contract_path = (
                "/finance/ledgers/{ledgerId}/transactions/{transactionId}"
                if method == "DELETE"
                else "/finance/ledgers/{ledgerId}/accounts/{accountId}/" + path.rsplit("/", 1)[1]
            )
            declared = contract["paths"][contract_path][method.lower()]["responses"][
                str(blocked.status_code)
            ]
            assert set(declared["content"]) == {"application/problem+json"}
            assert declared["content"]["application/problem+json"]["schema"] == {
                "$ref": "#/components/schemas/ProblemDetails"
            }
            assert blocked.headers["content-type"] == "application/problem+json"
            problem = ProblemDetails.model_validate(blocked.json())
            assert problem.status == 409
            assert problem.code == "finance_linked_record_protected"
    assert (await client.delete(f"{base}/advances/{advance_id}")).status_code == 405
    assert (await client.get(f"{base}/advances/{advance_id}")).json() == original
    # Unrelated manual bookkeeping against funding remains available.
    ordinary = await client.post(
        f"{base}/transactions",
        json={**expense, "accountId": draft["fundingAccountId"]},
        headers=submission_headers(actor.id),
    )
    assert ordinary.status_code == 201
    ordinary_id = ordinary.json()["outcome"]["resource"]["id"]
    blocked = await client.put(f"{base}/transactions/{ordinary_id}", json=expense)
    assert blocked.status_code == 409
    assert (await client.delete(f"{base}/transactions/{ordinary_id}")).status_code == 204
    overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert overview["monthSummaryByCurrency"] == [
        {
            "currency": "CNY",
            "income": {"amount": "0.00", "currency": "CNY"},
            "expense": {"amount": "0.00", "currency": "CNY"},
            "net": {"amount": "0.00", "currency": "CNY"},
        }
    ]
    assert overview["financialPositionByCurrency"][0]["assetTotal"] == {
        "amount": "100.00",
        "currency": "CNY",
    }


@pytest.mark.parametrize(
    "invalid",
    [
        "zero",
        "negative",
        "precision",
        "float",
        "share",
        "owner",
        "currency",
        "early",
        "date",
        "participant",
        "account",
        "extra",
    ],
)
async def test_invalid_advance_has_no_partial_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    command = deepcopy(draft)
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    command["previewToken"] = preview["previewToken"]
    if invalid in ("zero", "negative", "precision", "float"):
        value = {"zero": "0.00", "negative": "-1.00", "precision": "12.300", "float": 12.3}[invalid]
        command["amount"] = {"amount": value, "currency": "CNY"}
        command["participantShare"] = {"amount": value, "currency": "CNY"}
    elif invalid in ("share", "owner"):
        command["participantShare" if invalid == "share" else "ownerShare"] = {
            "amount": "1.00",
            "currency": "CNY",
        }
    elif invalid == "currency":
        command["amount"] = command["participantShare"] = {"amount": "12.30", "currency": "USD"}
        command["ownerShare"] = {"amount": "0.00", "currency": "USD"}
    elif invalid in ("early", "date"):
        command["transactionDate"] = "2026-09-30" if invalid == "early" else "2026-10-20T00:00:00Z"
    elif invalid in ("participant", "account"):
        command["participantId" if invalid == "participant" else "fundingAccountId"] = str(uuid4())
    else:
        command["settlement"] = {}
    headers = submission_headers(actor.id)
    result = await client.post(f"{base}/advances", json=command, headers=headers)
    assert result.status_code == (404 if invalid in ("participant", "account") else 422), (
        result.text
    )
    syntactic = invalid not in ("early", "currency", "participant", "account")
    assert ("commandValidationRejection" in result.json()) == syntactic
    assert ("submissionReceipt" in result.json()) != syntactic
    if syntactic:
        proof = result.json()["commandValidationRejection"]
        assert proof["attemptedBody"] == command
        assert proof["operation"] == "createFinanceAdvance"
    else:
        assert (
            await client.post(f"{base}/advances", json=command, headers=headers)
        ).json() == result.json()
    assert len((await client.get(f"{base}/accounts")).json()) == 1
    assert (await client.get(f"{base}/transactions")).json()["items"] == []
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json() == []


@pytest.mark.parametrize(
    "change", ["balance", "opening", "rename", "archive_participant", "archive_account"]
)
async def test_changed_preview_requires_explicit_new_confirmation_and_retains_rejection(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    change: str,
) -> None:
    client, actor, base, draft = advance_client
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    command = {**draft, "previewToken": preview["previewToken"]}
    if change == "balance":
        money = {"amount": "1.00", "currency": "CNY"}
        changed = await client.post(
            f"{base}/transactions",
            json={
                "kind": "expense",
                "accountId": draft["fundingAccountId"],
                "transactionDate": "2026-10-20",
                "economicAmount": money,
                "categoryAllocations": [{"amount": money}],
            },
            headers=submission_headers(actor.id),
        )
        assert changed.status_code == 201
    elif change == "opening":
        assert (
            await client.patch(
                f"{base}/accounts/{draft['fundingAccountId']}",
                json={"openingBalance": {"amount": "200.00", "currency": "CNY"}},
            )
        ).status_code == 200
    elif change == "rename":
        assert (
            await client.patch(
                f"{base}/participants/{draft['participantId']}", json={"name": "Renamed Alex"}
            )
        ).status_code == 200
    else:
        target = (
            f"participants/{draft['participantId']}"
            if change == "archive_participant"
            else f"accounts/{draft['fundingAccountId']}"
        )
        assert (await client.post(f"{base}/{target}/archive")).status_code == 200
    headers = submission_headers(actor.id)
    rejected = await client.post(f"{base}/advances", json=command, headers=headers)
    assert rejected.status_code == 409, rejected.text
    expected = {
        "archive_participant": "finance_participant_archived",
        "archive_account": "finance_account_archived",
    }.get(change, "finance_advance_preview_stale")
    assert rejected.json()["code"] == expected
    assert rejected.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    assert (
        await client.post(f"{base}/advances", json=command, headers=headers)
    ).json() == rejected.json()
    assert len((await client.get(f"{base}/accounts")).json()) == 1
    refreshed = await client.post(f"{base}/advances/preview", json=draft)
    if change.startswith("archive"):
        assert refreshed.status_code == 409
    else:
        revised = {**draft, "previewToken": refreshed.json()["previewToken"]}
        conflict = await client.post(f"{base}/advances", json=revised, headers=headers)
        assert conflict.json()["code"] == "finance_submission_content_conflict"
        confirmed = await client.post(
            f"{base}/advances", json=revised, headers=submission_headers(actor.id)
        )
        assert confirmed.status_code == 201


async def test_parallel_retries_bind_one_action_and_archive_preserves_attribution(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    command = {**draft, "previewToken": preview["previewToken"]}
    headers = submission_headers(actor.id)
    replies = await asyncio.gather(
        *[client.post(f"{base}/advances", json=command, headers=headers) for _ in range(4)]
    )
    assert all(reply.status_code in (201, 409) for reply in replies)
    confirmed = await client.post(f"{base}/advances", json=command, headers=headers)
    assert confirmed.status_code == 201
    assert all(reply.json() == confirmed.json() for reply in replies if reply.status_code == 201)
    assert len((await client.get(f"{base}/accounts")).json()) == 2
    assert len((await client.get(f"{base}/transactions")).json()["items"]) == 1
    advance_id = confirmed.json()["outcome"]["resource"]["id"]
    before = (await client.get(f"{base}/advances/{advance_id}")).json()
    await client.patch(f"{base}/participants/{draft['participantId']}", json={"name": "New name"})
    await client.post(f"{base}/participants/{draft['participantId']}/archive")
    assert (await client.get(f"{base}/advances/{advance_id}")).json() == before
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json() == [before["obligation"]]
    assert (
        await client.post(f"{base}/advances", json=command, headers=headers)
    ).json() == confirmed.json()


async def test_owned_ledger_isolation_and_other_owner_cannot_inspect_or_recover(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    client, actor, base, draft = advance_client
    second = await client.post(
        "/api/finance/ledgers", json={"name": "Other ledger"}, headers=submission_headers(actor.id)
    )
    other_base = f"/api/finance/ledgers/{second.json()['outcome']['resource']['id']}"
    for path in ["advances/preview", "advances"]:
        body = draft if path.endswith("preview") else {**draft, "previewToken": "0" * 64}
        missing = await client.post(
            f"{other_base}/{path}", json=body, headers=submission_headers(actor.id)
        )
        assert missing.status_code == 404, missing.text
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    headers = submission_headers(actor.id)
    command = {**draft, "previewToken": preview["previewToken"]}
    created = await client.post(f"{base}/advances", json=command, headers=headers)
    advance_id = created.json()["outcome"]["resource"]["id"]
    assert (await client.get(f"{other_base}/advances/{advance_id}")).status_code == 404
    assert (
        await client.get(f"{other_base}/participants/{draft['participantId']}/obligations")
    ).status_code == 404
    cross_operation = await client.post(
        f"{base}/transactions",
        json={
            "kind": "internalTransfer",
            "sourceAccountId": draft["fundingAccountId"],
            "destinationAccountId": str(uuid4()),
            "amount": draft["amount"],
            "transactionDate": draft["transactionDate"],
        },
        headers=headers,
    )
    assert cross_operation.json()["code"] == "finance_submission_content_conflict"
    other = User(
        identity_issuer="https://identity.example.test/advances",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(other)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=other) as foreign:
        assert (await foreign.get(f"{base}/advances/{advance_id}")).status_code == 404
        assert (
            await foreign.get(f"{base}/participants/{draft['participantId']}/obligations")
        ).status_code == 404
        assert (await foreign.post(f"{base}/advances/preview", json=draft)).status_code == 404
        assert (
            await foreign.post(
                f"{base}/advances", json=command, headers=submission_headers(other.id)
            )
        ).status_code == 404
        lookup = await foreign.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}",
            headers=submission_headers(other.id),
        )
        assert lookup.status_code == 404
        assert "submissionReceipt" not in lookup.json()
        assert "commandValidationRejection" not in lookup.json()


@pytest.mark.parametrize(
    "currency,amount", [("JPY", "123"), ("USD", "123456789012345678901234567890.12")]
)
async def test_supported_currency_and_large_exact_money_are_not_rounded(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    currency: str,
    amount: str,
) -> None:
    client, actor, base, draft = advance_client
    zero = "0" if currency == "JPY" else "0.00"
    funding = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": currency,
            "nature": "asset",
            "currency": currency,
            "openingBalance": {"amount": zero, "currency": currency},
            "trackingStartDate": "2026-10-01",
        },
    )
    intent = {
        **draft,
        "fundingAccountId": funding.json()["id"],
        "amount": {"amount": amount, "currency": currency},
        "participantShare": {"amount": amount, "currency": currency},
        "ownerShare": {"amount": zero, "currency": currency},
    }
    preview = await client.post(f"{base}/advances/preview", json=intent)
    assert preview.status_code == 200
    assert preview.json()["fundingBalanceAfter"] == {"amount": f"-{amount}", "currency": currency}
    created = await client.post(
        f"{base}/advances",
        json={**intent, "previewToken": preview.json()["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert created.status_code == 201
    advance_id = created.json()["outcome"]["resource"]["id"]
    detail = (await client.get(f"{base}/advances/{advance_id}")).json()
    assert detail["obligation"]["remaining"] == {"amount": amount, "currency": currency}


async def test_liability_funding_and_missing_headers_cannot_dispatch_advance(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
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
    invalid = {**draft, "fundingAccountId": card.json()["id"]}
    assert (await client.post(f"{base}/advances/preview", json=invalid)).status_code == 422
    result = await client.post(
        f"{base}/advances",
        json={**invalid, "previewToken": "0" * 64},
        headers=submission_headers(actor.id),
    )
    assert result.status_code == 422
    assert result.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    command = {**draft, "previewToken": preview["previewToken"]}
    for header in ("Idempotency-Key", "Finance-Command-Version", "Finance-Submission-Owner"):
        headers = submission_headers(actor.id)
        del headers[header]
        assert (
            await client.post(f"{base}/advances", json=command, headers=headers)
        ).status_code == 400
    assert (await client.get(f"{base}/transactions")).json()["items"] == []
