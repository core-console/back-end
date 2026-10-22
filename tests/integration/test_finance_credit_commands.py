"""Closed credit intent, exact Money and retained operation-specific versions."""

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance import submissions
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advances import advance_client as advance_client
from integration.test_finance_credits import confirm_credit, retained
from integration.test_finance_settlements import advance

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "income",
        "apply",
        "forgiveness",
        "subset",
        "zero",
        "negative",
        "float",
        "precision",
        "currency",
        "early",
        "v1",
    ],
)
async def test_invalid_or_implicit_credit_intent_has_no_financial_effect(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]], invalid: str
) -> None:
    client, actor, base, draft = advance_client
    body = retained(draft, "50.00")
    if invalid == "missing":
        del body["excessDisposition"]
    else:
        mutations: dict[str, dict[str, object]] = {
            "income": {"excessDisposition": "acceptIncome"},
            "apply": {"useCredit": True},
            "forgiveness": {"forgiven": {"amount": "1.00", "currency": "CNY"}},
            "subset": {"obligationIds": []},
            "zero": {"amount": {"amount": "0.00", "currency": "CNY"}},
            "negative": {"amount": {"amount": "-1.00", "currency": "CNY"}},
            "float": {"amount": {"amount": 50.0, "currency": "CNY"}},
            "precision": {"amount": {"amount": "1.001", "currency": "CNY"}},
            "currency": {"amount": {"amount": "50.00", "currency": "USD"}},
            "early": {"transactionDate": "2026-09-30"},
            "v1": {},
        }
        body.update(mutations[invalid])
    before = (await client.get(f"{base}/accounts")).json()
    headers = {
        **submission_headers(actor.id),
        "Finance-Command-Version": "1" if invalid == "v1" else "2",
    }
    command = {**body, "previewToken": "0" * 64}
    response = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert response.status_code in (409, 422), response.text
    assert response.headers["content-type"] == "application/problem+json"
    if invalid in ("currency", "early"):
        assert response.json()["submissionReceipt"]["commandVersion"] == "2"
    else:
        proof = response.json()["commandValidationRejection"]
        assert proof["attemptedBody"] == command
        assert proof["commandVersion"] == headers["Finance-Command-Version"]
    assert (await client.get(f"{base}/accounts")).json() == before
    assert (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json() == []


async def test_credit_command_does_not_shrink_exact_money_or_automatically_apply_existing_credit(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    large = "123456789012345678901234567890.12"
    detail = await confirm_credit(client, actor, base, retained(draft, large))
    assert detail["creditOrigin"]["available"]["amount"] == large
    await confirm_credit(client, actor, base, retained(draft, "0.01"))
    position = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()[
        0
    ]
    assert position["availableCredits"]["amount"] == "123456789012345678901234567890.13"
    assert position["netExposure"]["amount"] == "-123456789012345678901234567890.13"
    await advance(client, actor, base, draft, "100.00", "2026-10-21")
    body = retained(draft, "98.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    assert preview["retainedCredit"]["amount"] == "0.00"
    assert preview["outstandingAfter"]["amount"] == "2.00"
    result = await confirm_credit(client, actor, base, body)
    assert "creditOrigin" not in result
    assert result["allocations"][0]["cashPaid"]["amount"] == "98.00"
    assert (
        len((await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()) == 2
    )


async def test_bound_credit_v2_recovers_after_admission_closes_and_rejects_identity_changes(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, actor, base, draft = advance_client
    body = retained(draft, "50.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    postgres_session.add(
        FinanceSubmission(
            local_user_id=actor.id,
            submission_id=UUID(headers["Idempotency-Key"]),
            command_version="2",
            retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
            canonical_command={
                "commandVersion": "2",
                "operation": "createFinanceSettlement",
                "targetLedgerId": base.rsplit("/", 1)[1],
                "body": command,
            },
        )
    )
    await postgres_session.commit()
    monkeypatch.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
    result = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert result.status_code == 201, result.text
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == result.json()
    conflict = await client.post(
        f"{base}/settlements", json={**command, "note": "different"}, headers=headers
    )
    assert conflict.json()["code"] == "finance_submission_content_conflict"
    mismatch = await client.post(
        f"{base}/settlements", json=command, headers={**headers, "Finance-Command-Version": "1"}
    )
    assert mismatch.json()["code"] == "finance_submission_version_mismatch"
    closed = await client.post(
        f"{base}/settlements",
        json=command,
        headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
    )
    assert closed.json()["code"] == "finance_command_version_closed"
    assert "submissionReceipt" not in closed.json()
    assert "commandValidationRejection" not in closed.json()
