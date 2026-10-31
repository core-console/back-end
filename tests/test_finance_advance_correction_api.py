"""Correction HTTP failures and additive, operation-specific OpenAPI evidence."""

from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from psycopg import OperationalError as DriverError
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.users.dependencies import get_current_user
from core_console.modules.users.identity import CurrentUser


@pytest.mark.anyio
@pytest.mark.parametrize("unavailable", [True, False])
@pytest.mark.parametrize("boundary", ["preview", "inspect", "history"])
async def test_correction_read_failures_never_assert_a_terminal_outcome(
    app: FastAPI,
    client: AsyncClient,
    unavailable: bool,
    boundary: str,
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))
    cast(AsyncMock, session.scalar).side_effect = (
        OperationalError(None, None, DriverError("connection refused"), connection_invalidated=True)
        if unavailable
        else RuntimeError("projection defect")
    )

    async def failing_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = failing_session
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid4(), username=None, display_name=None, email=None, status="active"
    )
    base = f"/api/finance/ledgers/{uuid4()}"
    response = (
        await client.post(f"{base}/advance-corrections/preview", json={})
        if boundary == "preview"
        else await client.get(f"{base}/advance-corrections/{uuid4()}")
        if boundary == "inspect"
        else await client.get(f"{base}/advances/{uuid4()}/corrections")
    )
    assert response.status_code == (503 if unavailable else 500)
    assert response.json()["code"] == ("database_unavailable" if unavailable else "internal_error")
    assert response.headers["content-type"] == "application/problem+json"
    assert "submissionReceipt" not in response.json()
    assert "commandValidationRejection" not in response.json()


def test_correction_contract_exposes_complete_plans_and_own_recovery_evidence(app: FastAPI) -> None:
    schema = app.openapi()
    base = "/finance/ledgers/{ledgerId}"
    for path, method, operation in [
        ("/advance-corrections/preview", "post", "previewFinanceAdvanceCorrection"),
        ("/advance-corrections", "post", "createFinanceAdvanceCorrection"),
        ("/advance-corrections/{correctionId}", "get", "getFinanceAdvanceCorrection"),
        ("/advances/{advanceId}/corrections", "get", "listFinanceAdvanceCorrections"),
    ]:
        endpoint = schema["paths"][base + path][method]
        assert endpoint["operationId"] == operation
        for status, response in endpoint["responses"].items():
            if int(status) >= 400:
                assert set(response["content"]) == {"application/problem+json"}
    create = schema["paths"][base + "/advance-corrections"]["post"]
    assert {p["name"] for p in create["parameters"] if p["in"] == "header"} == {
        "Idempotency-Key",
        "Finance-Command-Version",
        "Finance-Submission-Owner",
    }
    intent = create["requestBody"]["content"]["application/json"]["schema"]
    assert intent["additionalProperties"] is False
    assert {"advanceId", "reason", "participantShares", "previewToken"} <= set(intent["required"])
    components = schema["components"]["schemas"]
    preview = components["AdvanceCorrectionPreview"]["properties"]
    assert {
        "before",
        "after",
        "financialEffectsBefore",
        "financialEffectsAfter",
        "accountEffects",
    } <= preview.keys()
    receipt = components["AdvanceCorrectionCreatedReceipt"]["properties"]
    assert receipt["operation"]["const"] == "createFinanceAdvanceCorrection"
    assert receipt["commandVersion"]["const"] == "1"
    recovery = components["SubmissionReceipt"]
    assert "createFinanceAdvanceCorrection" in recovery["discriminator"]["mapping"]
