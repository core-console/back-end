"""Public submission schemas cannot encode incompatible Ledger evidence."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.finance.submission_schemas import (
    LedgerCreatedReceipt,
    LedgerValidationProblem,
)
from core_console.modules.users.dependencies import get_current_user
from core_console.modules.users.identity import CurrentUser


def test_ledger_protocol_openapi_requires_headers_and_distinguishes_evidence(app: FastAPI) -> None:
    schema = app.openapi()
    create = schema["paths"]["/finance/ledgers"]["post"]
    lookup = schema["paths"]["/finance/submissions/{submissionId}"]["get"]
    assert create["operationId"] == "createFinanceLedger"
    assert {
        (header["name"], header["in"], header["required"]) for header in create["parameters"]
    } == {
        ("Idempotency-Key", "header", True),
        ("Finance-Command-Version", "header", True),
        ("Finance-Submission-Owner", "header", True),
    }
    assert lookup["operationId"] == "getFinanceSubmission"
    assert {
        (parameter["name"], parameter["in"], parameter["required"])
        for parameter in lookup["parameters"]
    } == {
        ("submissionId", "path", True),
        ("Finance-Submission-Owner", "header", True),
    }
    assert create["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/LedgerCreatedReceipt"
    }
    components = schema["components"]["schemas"]
    assert (
        components["LedgerSubmissionReceipt"]["properties"]["outcome"]["discriminator"][
            "propertyName"
        ]
        == "kind"
    )
    assert components["FinanceSubmissionResponse"]["discriminator"]["propertyName"] == "state"
    assert "submissionReceipt" in components["LedgerTerminalProblem"]["required"]
    assert "commandValidationRejection" in components["LedgerValidationProblem"]["required"]
    for operation in (create, lookup):
        for status, response in operation["responses"].items():
            if not status.startswith("2"):
                assert set(response["content"]) == {"application/problem+json"}
    # Other creates remain on their previously generated resource contracts.
    for path, response in (
        ("/finance/ledgers/{ledgerId}/accounts", "AccountResponse"),
        ("/finance/ledgers/{ledgerId}/categories", "CategoryResponse"),
        ("/finance/ledgers/{ledgerId}/transactions", "FinanceTransactionResponse"),
    ):
        operation = schema["paths"][path]["post"]
        assert all(parameter["in"] != "header" for parameter in operation.get("parameters", []))
        assert (
            operation["responses"]["201"]["content"]["application/json"]["schema"]["$ref"]
            == f"#/components/schemas/{response}"
        )


@pytest.mark.parametrize(
    "outcome",
    [
        {"kind": "noChange"},
        {"kind": "created", "resource": {"type": "account", "id": str(uuid4())}},
        {"kind": "created", "resource": {"type": "ledger", "id": str(uuid4())}, "problem": {}},
        {"kind": "rejected", "problem": {}},
    ],
)
def test_created_ledger_receipt_cannot_represent_impossible_outcomes(
    outcome: dict[str, object],
) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        LedgerCreatedReceipt.model_validate(
            {
                "submissionId": uuid4(),
                "commandVersion": "1",
                "operation": "createFinanceLedger",
                "targetLedgerId": None,
                "admittedAt": now,
                "resolvedAt": now,
                "outcome": outcome,
            }
        )


def test_q29_and_submission_receipt_cannot_coexist() -> None:
    with pytest.raises(ValidationError):
        LedgerValidationProblem.model_validate(
            {
                "type": "about:blank",
                "title": "Unprocessable Entity",
                "status": 422,
                "code": "validation_error",
                "errors": [],
                "commandValidationRejection": {
                    "kind": "definitivelyNotAdmitted",
                    "submissionId": uuid4(),
                    "commandVersion": "1",
                    "ownerId": uuid4(),
                    "operation": "createFinanceLedger",
                    "targetLedgerId": None,
                    "attemptedBody": {"name": " "},
                },
                "submissionReceipt": {},
            }
        )


@pytest.mark.anyio
@pytest.mark.parametrize("configured", [False, True])
async def test_lookup_infrastructure_and_unknown_errors_have_no_store(
    app: FastAPI, client: AsyncClient, configured: bool
) -> None:
    actor = CurrentUser(id=uuid4(), username=None, display_name=None, email=None, status="active")
    app.dependency_overrides[get_current_user] = lambda: actor
    if configured:
        session = cast(AsyncSession, AsyncMock(spec=AsyncSession))
        cast(AsyncMock, session.scalar).side_effect = RuntimeError("unknown database failure")

        async def fake_session() -> AsyncIterator[AsyncSession]:
            yield session

        app.dependency_overrides[get_session] = fake_session
    response = await client.get(
        f"/api/finance/submissions/{uuid4()}", headers={"Finance-Submission-Owner": str(actor.id)}
    )
    assert response.status_code == (500 if configured else 503)
    assert response.headers["cache-control"] == "no-store"
    assert "submissionReceipt" not in response.json()
    assert "commandValidationRejection" not in response.json()
