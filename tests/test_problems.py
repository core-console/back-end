"""Problem Details error translation tests."""

from http import HTTPStatus

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from core_console.problems import ApplicationProblem
from core_console.request_context import get_request_id


@pytest.mark.anyio
async def test_not_found_uses_problem_details(client: AsyncClient) -> None:
    response = await client.get("/missing")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Not Found",
        "instance": "/missing",
        "code": "not_found",
    }
    assert response.json()["status"] == response.status_code


@pytest.mark.anyio
async def test_request_validation_uses_problem_details(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    @app.get("/__test_validation", include_in_schema=False)
    async def validation_for_test(limit: int) -> None:
        del limit

    response = await client.get("/__test_validation", params={"limit": "invalid"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == response.status_code
    assert response.json()["code"] == "validation_error"
    assert response.json()["errors"]


@pytest.mark.anyio
async def test_expected_application_error_uses_problem_details(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    @app.get("/__test_expected_error", include_in_schema=False)
    async def expected_error_for_test() -> None:
        raise ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="The requested state conflicts with the current state.",
            code="state_conflict",
        )

    response = await client.get("/__test_expected_error")

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == response.status_code
    assert response.json()["code"] == "state_conflict"


@pytest.mark.anyio
async def test_unexpected_error_is_logged_without_leaking_to_client(
    app: FastAPI,
    client: AsyncClient,
    application_log_records: pytest.LogCaptureFixture,
) -> None:
    @app.get("/__test_error", include_in_schema=False)
    async def fail_for_test() -> None:
        raise RuntimeError("sensitive implementation detail")

    response = await client.get(
        "/__test_error",
        headers={"X-Request-ID": "unexpected-error-request"},
    )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == response.status_code
    assert response.json()["detail"] == "An unexpected error occurred."
    assert "sensitive" not in response.text
    assert response.headers["X-Request-ID"] == "unexpected-error-request"
    records = [
        record
        for record in application_log_records.records
        if record.name == "core_console.problems"
    ]
    assert len(records) == 1
    assert getattr(records[0], "request_id", None) == "unexpected-error-request"
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], RuntimeError)
    assert "sensitive implementation detail" in str(records[0].exc_info[1])
    assert get_request_id() is None
