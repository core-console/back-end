"""Finance HTTP behavior that does not require real PostgreSQL."""

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from psycopg import OperationalError as PsycopgOperationalError
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.users.dependencies import get_current_user
from core_console.modules.users.identity import CurrentUser

pytestmark = pytest.mark.anyio


def _active_user() -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        username="finance-user",
        display_name="Finance User",
        email=None,
        status="active",
    )


async def test_active_user_can_list_the_deterministic_currency_catalog(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    app.dependency_overrides[get_current_user] = _active_user

    response = await client.get("/api/finance/currencies")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == [
        {"code": "CNY", "minorUnit": 2},
        {"code": "JPY", "minorUnit": 0},
        {"code": "USD", "minorUnit": 2},
    ]


async def test_currency_catalog_uses_the_active_user_boundary(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    app.dependency_overrides[get_current_user] = lambda: None

    response = await client.get("/api/finance/currencies")

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "access_denied"


@pytest.mark.parametrize(
    "body",
    (
        {"name": " \t "},
        {"name": None},
        {"name": "Personal", "isDefault": True},
    ),
)
async def test_create_ledger_rejects_invalid_public_requests_without_database_work(
    app: FastAPI,
    client: AsyncClient,
    body: dict[str, object],
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))

    async def fake_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_current_user] = _active_user
    app.dependency_overrides[get_session] = fake_session

    response = await client.post("/api/finance/ledgers", json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"
    cast(AsyncMock, session.commit).assert_not_awaited()


@pytest.mark.parametrize(
    "opening_balance",
    (
        {"amount": 10, "currency": "CNY"},
        {"amount": "1e2", "currency": "CNY"},
        {"amount": "1.001", "currency": "CNY"},
        {"amount": "10.00", "currency": "EUR"},
        {"amount": "10.00", "currency": "USD"},
    ),
)
async def test_create_account_rejects_invalid_money_without_database_work(
    app: FastAPI,
    client: AsyncClient,
    opening_balance: dict[str, object],
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))

    async def fake_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_current_user] = _active_user
    app.dependency_overrides[get_session] = fake_session

    response = await client.post(
        f"/api/finance/ledgers/{uuid4()}/accounts",
        json={
            "name": "Cash",
            "nature": "asset",
            "currency": "CNY",
            "openingBalance": opening_balance,
            "trackingStartDate": "2026-08-01",
        },
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"
    cast(AsyncMock, session.commit).assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    (
        {"nature": "liability"},
        {"currency": "USD"},
        {"status": "archived"},
        {"currentBalance": {"amount": "0.00", "currency": "CNY"}},
    ),
)
async def test_account_patch_rejects_semantics_and_managed_fields_without_database_work(
    app: FastAPI,
    client: AsyncClient,
    body: dict[str, object],
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))

    async def fake_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_current_user] = _active_user
    app.dependency_overrides[get_session] = fake_session

    response = await client.patch(
        f"/api/finance/ledgers/{uuid4()}/accounts/{uuid4()}",
        json=body,
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"
    cast(AsyncMock, session.commit).assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    (
        (
            "POST",
            "",
            {
                "name": "Cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "0", "currency": "CNY"},
                "trackingStartDate": "2026-08-01T00:00:00",
            },
        ),
        (
            "PATCH",
            f"/{uuid4()}",
            {"trackingStartDate": "2026-08-01T00:00:00"},
        ),
    ),
)
async def test_account_requests_require_exact_calendar_tracking_start_dates(
    app: FastAPI,
    client: AsyncClient,
    method: str,
    path_suffix: str,
    body: dict[str, object],
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))

    async def fake_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_current_user] = _active_user
    app.dependency_overrides[get_session] = fake_session

    response = await client.request(
        method,
        f"/api/finance/ledgers/{uuid4()}/accounts{path_suffix}",
        json=body,
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"
    cast(AsyncMock, session.commit).assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    (
        ("POST", "", {"name": None}),
        ("POST", "", {"name": "Food", "kind": "expense"}),
        ("PATCH", f"/{uuid4()}", {"status": "archived"}),
        ("PATCH", f"/{uuid4()}", {"name": "界" * 101}),
    ),
)
async def test_category_requests_reject_undeclared_or_invalid_fields_without_database_work(
    app: FastAPI,
    client: AsyncClient,
    method: str,
    path_suffix: str,
    body: dict[str, object],
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))

    async def fake_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_current_user] = _active_user
    app.dependency_overrides[get_session] = fake_session

    response = await client.request(
        method,
        f"/api/finance/ledgers/{uuid4()}/categories{path_suffix}",
        json=body,
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"
    cast(AsyncMock, session.commit).assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    (
        {
            "kind": "income",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21T00:00:00",
            "economicAmount": {"amount": "10.00", "currency": "CNY"},
            "categoryAllocations": [{"amount": {"amount": "10.00", "currency": "CNY"}}],
        },
        {
            "kind": "expense",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21",
            "economicAmount": {"amount": "0.00", "currency": "CNY"},
            "categoryAllocations": [{"amount": {"amount": "0.00", "currency": "CNY"}}],
        },
        {
            "kind": "income",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21",
            "economicAmount": {"amount": "10.00", "currency": "CNY"},
            "categoryAllocations": [],
        },
        {
            "kind": "income",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21",
            "economicAmount": {"amount": 10, "currency": "CNY"},
            "categoryAllocations": [{"amount": {"amount": "10.00", "currency": "CNY"}}],
        },
        {
            "kind": "income",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21",
            "economicAmount": {"amount": "10.00", "currency": "CNY"},
            "categoryAllocations": [{"amount": {"amount": "10.00", "currency": "USD"}}],
        },
        {
            "kind": "income",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21",
            "economicAmount": {"amount": "10.00", "currency": "CNY"},
            "categoryAllocations": [
                {"amount": {"amount": "10.00", "currency": "CNY"}},
                {"amount": {"amount": "10.00", "currency": "CNY"}},
            ],
        },
        {
            "kind": "expense",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21",
            "economicAmount": {"amount": "10.00", "currency": "CNY"},
            "categoryAllocations": [{"amount": {"amount": "9.00", "currency": "CNY"}}],
        },
        {
            "kind": "internalTransfer",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21",
            "economicAmount": {"amount": "10.00", "currency": "CNY"},
            "categoryAllocations": [{"amount": {"amount": "10.00", "currency": "CNY"}}],
        },
        {
            "kind": "income",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21",
            "economicAmount": {"amount": "10.00", "currency": "CNY"},
            "categoryAllocations": [{"amount": {"amount": "10.00", "currency": "CNY"}}],
            "accountMovement": {"amount": "10.00", "currency": "CNY"},
        },
        {
            "kind": "income",
            "accountId": str(uuid4()),
            "transactionDate": "2026-08-21",
            "economicAmount": {"amount": "10.00", "currency": "CNY"},
            "categoryAllocations": [{"amount": {"amount": "10.00", "currency": "CNY"}}],
            "note": "界" * 501,
        },
    ),
)
async def test_create_transaction_rejects_invalid_public_contract_without_database_work(
    app: FastAPI,
    client: AsyncClient,
    body: dict[str, object],
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))

    async def fake_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_current_user] = _active_user
    app.dependency_overrides[get_session] = fake_session

    response = await client.post(
        f"/api/finance/ledgers/{uuid4()}/transactions",
        json=body,
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"
    cast(AsyncMock, session.commit).assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    (
        (
            OperationalError(
                None,
                None,
                PsycopgOperationalError("connection refused"),
                connection_invalidated=True,
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_unavailable",
        ),
        (RuntimeError("query mapping defect"), HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error"),
    ),
)
async def test_list_ledgers_uses_established_database_failure_taxonomy(
    app: FastAPI,
    client: AsyncClient,
    failure: Exception,
    expected_status: HTTPStatus,
    expected_code: str,
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))
    cast(AsyncMock, session.scalars).side_effect = failure

    async def failing_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_current_user] = _active_user
    app.dependency_overrides[get_session] = failing_session

    response = await client.get("/api/finance/ledgers")

    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == expected_code
