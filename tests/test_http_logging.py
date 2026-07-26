"""Public HTTP request correlation and structured completion logging tests."""

import asyncio
import json
import logging
import re
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from core_console.logging_config import JsonFormatter
from core_console.request_context import get_request_id, request_id_context

_GENERATED_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")


def completion_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """Select HTTP completion records from captured application logs."""

    return [
        record for record in records if getattr(record, "event", None) == "http.request.completed"
    ]


@pytest.mark.anyio
async def test_missing_request_id_is_generated_and_returned(client: AsyncClient) -> None:
    response = await client.get("/api/helloWorld")

    assert _GENERATED_REQUEST_ID.fullmatch(response.headers["X-Request-ID"])


@pytest.mark.anyio
async def test_valid_request_id_is_reused(client: AsyncClient) -> None:
    request_id = "client.request_ID:123-abc"

    response = await client.get(
        "/api/helloWorld",
        headers={"X-Request-ID": request_id},
    )

    assert response.headers["X-Request-ID"] == request_id


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_request_id",
    [
        "",
        "a" * 129,
        "contains space",
        "contains/slash",
        "non-ascii-é",
        "line\nbreak",
        "control\x01character",
    ],
)
async def test_invalid_request_id_is_replaced(
    client: AsyncClient,
    invalid_request_id: str,
) -> None:
    raw_request_id = invalid_request_id.encode("latin-1")
    response = await client.get(
        "/api/helloWorld",
        headers=[(b"X-Request-ID", raw_request_id)],
    )

    adopted_request_id = response.headers["X-Request-ID"]
    assert adopted_request_id != invalid_request_id
    assert _GENERATED_REQUEST_ID.fullmatch(adopted_request_id)


@pytest.mark.anyio
async def test_request_context_is_isolated_between_concurrent_requests(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    @app.get("/__test_request_context/{delay}", include_in_schema=False)
    async def request_context_for_test(delay: float) -> dict[str, str | None]:
        await asyncio.sleep(delay)
        return {"request_id": get_request_id()}

    first, second = await asyncio.gather(
        client.get(
            "/__test_request_context/0.02",
            headers={"X-Request-ID": "concurrent-first"},
        ),
        client.get(
            "/__test_request_context/0",
            headers={"X-Request-ID": "concurrent-second"},
        ),
    )

    assert first.json() == {"request_id": "concurrent-first"}
    assert first.headers["X-Request-ID"] == "concurrent-first"
    assert second.json() == {"request_id": "concurrent-second"}
    assert second.headers["X-Request-ID"] == "concurrent-second"
    assert get_request_id() is None


@pytest.mark.anyio
async def test_application_log_inside_request_automatically_has_request_id(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    rendered_records: list[dict[str, object]] = []

    class RenderingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rendered_records.append(json.loads(JsonFormatter("test").format(record)))

    endpoint_logger = logging.getLogger("core_console.test.request_context")
    handler = RenderingHandler()
    endpoint_logger.addHandler(handler)

    @app.get("/__test_application_log", include_in_schema=False)
    async def application_log_for_test() -> None:
        endpoint_logger.info("request-scoped application log", extra={"event": "test.request.log"})

    try:
        response = await client.get(
            "/__test_application_log",
            headers={"X-Request-ID": "application-log-request"},
        )
    finally:
        endpoint_logger.removeHandler(handler)

    assert response.status_code == HTTPStatus.OK
    application_event = next(
        record for record in rendered_records if record["event"] == "test.request.log"
    )
    assert application_event["request_id"] == "application-log-request"


@pytest.mark.anyio
async def test_log_serialization_failure_does_not_fail_request(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    class UnserializableValue:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    endpoint_logger = logging.getLogger("core_console.test.serialization")

    @app.get("/__test_log_serialization", include_in_schema=False)
    async def log_serialization_for_test() -> None:
        endpoint_logger.info(
            "application log with invalid extra",
            extra={
                "event": "test.serialization",
                "invalid": UnserializableValue(),
            },
        )

    response = await client.get("/__test_log_serialization")

    assert response.status_code == HTTPStatus.OK


def test_json_formatter_falls_back_for_message_formatting_error() -> None:
    message_record = logging.LogRecord(
        "core_console.test.formatter",
        logging.INFO,
        __file__,
        1,
        "%s %s",
        ("missing-second-argument",),
        None,
    )

    formatter = JsonFormatter("test")
    with request_id_context("formatter-fallback-request"):
        payload = json.loads(formatter.format(message_record))

    assert payload == {
        "timestamp": payload["timestamp"],
        "level": "ERROR",
        "event": "logging.serialization_failed",
        "service": "core-console-backend",
        "environment": "test",
        "request_id": "formatter-fallback-request",
    }
    assert isinstance(payload["timestamp"], str)
    assert payload["timestamp"].endswith("Z")


@pytest.mark.anyio
async def test_completed_request_log_has_expected_json_fields(
    client: AsyncClient,
    application_log_records: pytest.LogCaptureFixture,
) -> None:
    response = await client.get(
        "/api/helloWorld",
        headers={"X-Request-ID": "json-log-request"},
    )

    payloads = [
        json.loads(JsonFormatter("test").format(record))
        for record in completion_records(application_log_records.records)
    ]
    assert response.status_code == HTTPStatus.OK
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["timestamp"].endswith("Z")
    assert payload["level"] == "INFO"
    assert payload["event"] == "http.request.completed"
    assert payload["service"] == "core-console-backend"
    assert payload["environment"] == "test"
    assert payload["request_id"] == "json-log-request"
    assert payload["method"] == "GET"
    assert payload["route"] == "/api/helloWorld"
    assert payload["status_code"] == HTTPStatus.OK
    assert isinstance(payload["duration_ms"], int | float)
    assert payload["duration_ms"] >= 0


@pytest.mark.anyio
async def test_dynamic_route_logs_template_and_404_logs_unmatched(
    app: FastAPI,
    client: AsyncClient,
    application_log_records: pytest.LogCaptureFixture,
) -> None:
    @app.get("/__test_users/{user_id}", include_in_schema=False)
    async def user_for_test(user_id: int) -> dict[str, int]:
        return {"user_id": user_id}

    matched = await client.get("/__test_users/123")
    missing = await client.get("/arbitrary/high-cardinality/path")

    assert matched.status_code == HTTPStatus.OK
    assert missing.status_code == HTTPStatus.NOT_FOUND
    records = completion_records(application_log_records.records)
    assert [getattr(record, "route", None) for record in records] == [
        "/__test_users/{user_id}",
        "unmatched",
    ]


@pytest.mark.anyio
async def test_request_log_does_not_include_payloads_or_query_string(
    client: AsyncClient,
    application_log_records: pytest.LogCaptureFixture,
) -> None:
    response = await client.post(
        "/missing?private_query=do-not-log",
        content="private-request-body",
        headers={
            "X-Request-ID": "safe-fields-only",
            "Authorization": "Bearer private-token",
            "Cookie": "session=private-session",
        },
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    output = "\n".join(
        JsonFormatter("test").format(record)
        for record in completion_records(application_log_records.records)
    )
    assert "private_query" not in output
    assert "do-not-log" not in output
    assert "private-request-body" not in output
    assert "private-token" not in output
    assert "private-session" not in output
    assert response.text not in output


@pytest.mark.anyio
async def test_2xx_and_4xx_logs_are_info_without_tracebacks(
    client: AsyncClient,
    application_log_records: pytest.LogCaptureFixture,
) -> None:
    success = await client.get("/api/helloWorld")
    not_found = await client.get("/missing")

    assert success.status_code == HTTPStatus.OK
    assert not_found.status_code == HTTPStatus.NOT_FOUND
    records = completion_records(application_log_records.records)
    assert [record.levelno for record in records] == [logging.INFO, logging.INFO]
    assert all(record.exc_info is None for record in application_log_records.records)


@pytest.mark.anyio
async def test_5xx_completion_log_is_error_without_duplicate_traceback(
    app: FastAPI,
    client: AsyncClient,
    application_log_records: pytest.LogCaptureFixture,
) -> None:
    @app.get("/__test_server_error", include_in_schema=False)
    async def server_error_for_test() -> None:
        raise RuntimeError("private server failure")

    response = await client.get(
        "/__test_server_error",
        headers={"X-Request-ID": "server-error-request"},
    )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.headers["X-Request-ID"] == "server-error-request"
    records = completion_records(application_log_records.records)
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert getattr(records[0], "status_code", None) == HTTPStatus.INTERNAL_SERVER_ERROR
    assert getattr(records[0], "request_id", None) == "server-error-request"
    assert records[0].exc_info is None
    exception_records = [
        record for record in application_log_records.records if record.exc_info is not None
    ]
    assert len(exception_records) == 1
    assert exception_records[0].name == "core_console.problems"
    assert getattr(exception_records[0], "request_id", None) == "server-error-request"
    events = [
        getattr(record, "event", None)
        for record in application_log_records.records
        if getattr(record, "event", None) in {"http.request.failed", "http.request.completed"}
    ]
    assert events == ["http.request.failed", "http.request.completed"]
    assert get_request_id() is None
