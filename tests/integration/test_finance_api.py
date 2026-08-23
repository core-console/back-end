"""Real PostgreSQL coverage for the Finance Ledger HTTP slice."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.app import create_app
from core_console.config import AuthMode, Environment, Settings
from core_console.modules.users.models import User

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def finance_client(
    *,
    database_url: str,
    actor: User,
) -> AsyncIterator[AsyncClient]:
    """Call Finance routes as one persisted active Local User."""

    app = create_app(
        Settings(
            environment=Environment.TEST,
            auth_mode=AuthMode.DEVELOPMENT,
            dev_identity_issuer=actor.identity_issuer,
            dev_identity_subject=actor.identity_subject,
            database_url=SecretStr(database_url),
            database_connect_timeout_seconds=0.1,
        )
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def test_user_can_create_list_and_rename_only_their_own_ledgers(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("actor")
    other_user = _user("other")
    postgres_session.add_all([actor, other_user])
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        beta_response = await client.post("/api/finance/ledgers", json={"name": "  Beta  "})
        alpha_response = await client.post("/api/finance/ledgers", json={"name": "alpha"})
        zebra_response = await client.post("/api/finance/ledgers", json={"name": "zebra"})
        umlaut_response = await client.post("/api/finance/ledgers", json={"name": "Äpfel"})
        listed_response = await client.get("/api/finance/ledgers")
        unchanged_response = await client.patch(
            f"/api/finance/ledgers/{beta_response.json()['id']}",
            json={},
        )
        renamed_response = await client.patch(
            f"/api/finance/ledgers/{beta_response.json()['id']}",
            json={"name": "  Personal  "},
        )

    async with finance_client(database_url=postgres_database_url, actor=other_user) as client:
        other_create_response = await client.post(
            "/api/finance/ledgers",
            json={"name": "ALPHA"},
        )
        other_listed_response = await client.get("/api/finance/ledgers")

    assert beta_response.status_code == HTTPStatus.CREATED
    assert beta_response.json()["name"] == "Beta"
    assert alpha_response.status_code == HTTPStatus.CREATED
    assert listed_response.json() == [
        alpha_response.json(),
        beta_response.json(),
        zebra_response.json(),
        umlaut_response.json(),
    ]
    assert unchanged_response.json() == beta_response.json()
    assert renamed_response.json() == {
        "id": beta_response.json()["id"],
        "name": "Personal",
    }
    assert other_create_response.status_code == HTTPStatus.CREATED
    assert other_listed_response.json() == [other_create_response.json()]


async def test_case_folded_ledger_names_conflict_only_for_the_same_owner(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("casefold")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        created_response = await client.post(
            "/api/finance/ledgers",
            json={"name": "Straße"},
        )
        duplicate_response = await client.post(
            "/api/finance/ledgers",
            json={"name": "STRASSE"},
        )
        work_response = await client.post("/api/finance/ledgers", json={"name": "Work"})
        rename_response = await client.patch(
            f"/api/finance/ledgers/{work_response.json()['id']}",
            json={"name": "strasse"},
        )

    assert created_response.status_code == HTTPStatus.CREATED
    for response in (duplicate_response, rename_response):
        assert response.status_code == HTTPStatus.CONFLICT
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "finance_ledger_name_conflict"


async def test_missing_and_non_owned_ledgers_have_the_same_safe_result(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("lookup-actor")
    other_user = _user("lookup-other")
    postgres_session.add_all([actor, other_user])
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=other_user) as client:
        other_ledger = await client.post("/api/finance/ledgers", json={"name": "Private"})

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        non_owned_response = await client.patch(
            f"/api/finance/ledgers/{other_ledger.json()['id']}",
            json={"name": "Disclosed"},
        )
        missing_response = await client.patch(
            f"/api/finance/ledgers/{uuid4()}",
            json={"name": "Missing"},
        )

    assert non_owned_response.status_code == HTTPStatus.NOT_FOUND
    assert missing_response.status_code == HTTPStatus.NOT_FOUND
    assert non_owned_response.json()["code"] == "finance_ledger_not_found"
    assert missing_response.json()["code"] == "finance_ledger_not_found"
    assert non_owned_response.json()["detail"] == missing_response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    (
        {"name": " \t "},
        {"name": "界" * 101},
        {"name": None},
        {"name": "Personal", "default": True},
    ),
)
async def test_create_ledger_rejects_invalid_or_unknown_fields(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    body: dict[str, object],
) -> None:
    actor = _user(uuid4().hex)
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post("/api/finance/ledgers", json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"


async def test_malformed_ledger_id_uses_validation_problem(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("malformed-id")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.patch(
            "/api/finance/ledgers/not-a-uuid",
            json={"name": "Personal"},
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


async def test_user_can_manage_account_lifecycle_with_account_relative_balances(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("account-lifecycle")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        ledger_id = ledger.json()["id"]
        liability = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "  Credit Card  ",
                "nature": "liability",
                "currency": "CNY",
                "openingBalance": {"amount": "250", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        asset = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "JPY",
                "openingBalance": {"amount": "100", "currency": "JPY"},
                "trackingStartDate": "2026-08-02",
            },
        )
        unchanged = await client.patch(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}",
            json={},
        )
        wrong_currency = await client.patch(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}",
            json={"openingBalance": {"amount": "250.00", "currency": "USD"}},
        )
        archived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}/archive"
        )
        archived_again = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}/archive"
        )
        listed = await client.get(f"/api/finance/ledgers/{ledger_id}/accounts")
        updated = await client.patch(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}",
            json={
                "name": "Card",
                "openingBalance": {"amount": "275.5", "currency": "CNY"},
                "trackingStartDate": "2026-07-31",
            },
        )
        unarchived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}/unarchive"
        )
        unarchived_again = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}/unarchive"
        )

    assert liability.status_code == HTTPStatus.CREATED
    assert liability.json() == {
        "id": liability.json()["id"],
        "name": "Credit Card",
        "nature": "liability",
        "currency": "CNY",
        "openingBalance": {"amount": "250.00", "currency": "CNY"},
        "trackingStartDate": "2026-08-01",
        "currentBalance": {"amount": "250.00", "currency": "CNY"},
        "status": "active",
    }
    assert asset.status_code == HTTPStatus.CREATED
    assert unchanged.json() == liability.json()
    assert wrong_currency.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert wrong_currency.json()["code"] == "validation_error"
    assert archived.json()["status"] == "archived"
    assert archived_again.json() == archived.json()
    assert listed.json() == [asset.json(), archived.json()]
    assert updated.json() == {
        **liability.json(),
        "name": "Card",
        "openingBalance": {"amount": "275.50", "currency": "CNY"},
        "trackingStartDate": "2026-07-31",
        "currentBalance": {"amount": "275.50", "currency": "CNY"},
        "status": "archived",
    }
    assert unarchived.json()["status"] == "active"
    assert unarchived_again.json() == unarchived.json()


async def test_user_can_correct_nature_on_an_unlocked_zero_position_account(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("account-nature-correction")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        account = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "0", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        corrected = await client.patch(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts/{account.json()['id']}",
            json={"nature": "liability"},
        )

    assert account.status_code == HTTPStatus.CREATED
    assert corrected.status_code == HTTPStatus.OK
    assert corrected.json() == {
        **account.json(),
        "nature": "liability",
        "openingBalance": {"amount": "0.00", "currency": "CNY"},
        "currentBalance": {"amount": "0.00", "currency": "CNY"},
    }


async def test_user_can_correct_currency_on_an_unlocked_zero_position_account(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("account-currency-correction")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        account = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "0", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        corrected = await client.patch(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts/{account.json()['id']}",
            json={"currency": "JPY"},
        )

    assert account.status_code == HTTPStatus.CREATED
    assert corrected.status_code == HTTPStatus.OK
    assert corrected.json() == {
        **account.json(),
        "currency": "JPY",
        "openingBalance": {"amount": "0", "currency": "JPY"},
        "currentBalance": {"amount": "0", "currency": "JPY"},
    }


async def test_user_can_correct_nature_and_currency_together_on_an_unlocked_account(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("account-combined-correction")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        account = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "0", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        corrected = await client.patch(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts/{account.json()['id']}",
            json={"nature": "liability", "currency": "USD"},
        )

    assert account.status_code == HTTPStatus.CREATED
    assert corrected.status_code == HTTPStatus.OK
    assert corrected.json() == {
        **account.json(),
        "nature": "liability",
        "currency": "USD",
        "openingBalance": {"amount": "0.00", "currency": "USD"},
        "currentBalance": {"amount": "0.00", "currency": "USD"},
    }


@pytest.mark.parametrize(
    "body",
    ({"nature": "liability"}, {"currency": "USD"}),
)
async def test_account_semantic_correction_rejects_nonzero_opening_balance(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    body: dict[str, str],
) -> None:
    actor = _user(f"account-opening-lock-{next(iter(body))}")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        account = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "25.00", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        account_path = f"/api/finance/ledgers/{ledger.json()['id']}/accounts/{account.json()['id']}"
        rejected = await client.patch(account_path, json=body)
        listed = await client.get(f"/api/finance/ledgers/{ledger.json()['id']}/accounts")

    assert account.status_code == HTTPStatus.CREATED
    assert rejected.status_code == HTTPStatus.CONFLICT
    assert rejected.headers["content-type"].startswith("application/problem+json")
    assert rejected.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": HTTPStatus.CONFLICT,
        "detail": (
            "Account Nature or Currency cannot change because Opening Balance is "
            "non-zero or Transaction history exists."
        ),
        "instance": account_path,
        "code": "finance_account_semantics_locked",
    }
    assert listed.json() == [account.json()]


@pytest.mark.parametrize(
    ("kind", "expected_balance"),
    (("income", "10.00"), ("expense", "-10.00")),
)
async def test_account_semantic_correction_rejects_any_durable_transaction_history(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    kind: str,
    expected_balance: str,
) -> None:
    actor = _user(f"account-history-lock-{kind}")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        account = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "0", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        transaction = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/transactions",
            json={
                "kind": kind,
                "accountId": account.json()["id"],
                "transactionDate": "2026-08-21",
                "economicAmount": {"amount": "10.00", "currency": "CNY"},
                "categoryAllocations": [{"amount": {"amount": "10.00", "currency": "CNY"}}],
            },
        )
        account_path = f"/api/finance/ledgers/{ledger.json()['id']}/accounts/{account.json()['id']}"
        rejected = await client.patch(account_path, json={"nature": "liability"})
        listed = await client.get(f"/api/finance/ledgers/{ledger.json()['id']}/accounts")

    assert account.status_code == HTTPStatus.CREATED
    assert transaction.status_code == HTTPStatus.CREATED
    assert rejected.status_code == HTTPStatus.CONFLICT
    assert rejected.headers["content-type"].startswith("application/problem+json")
    assert rejected.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": HTTPStatus.CONFLICT,
        "detail": (
            "Account Nature or Currency cannot change because Opening Balance is "
            "non-zero or Transaction history exists."
        ),
        "instance": account_path,
        "code": "finance_account_semantics_locked",
    }
    assert listed.json() == [
        {
            **account.json(),
            "currentBalance": {"amount": expected_balance, "currency": "CNY"},
        }
    ]


async def test_account_lookups_do_not_leak_across_ledger_or_owner_scope(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("account-scope-actor")
    other_user = _user("account-scope-other")
    postgres_session.add_all([actor, other_user])
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        first_ledger = await client.post("/api/finance/ledgers", json={"name": "First"})
        second_ledger = await client.post("/api/finance/ledgers", json={"name": "Second"})
        account = await client.post(
            f"/api/finance/ledgers/{first_ledger.json()['id']}/accounts",
            json={
                "name": "Private",
                "nature": "asset",
                "currency": "USD",
                "openingBalance": {"amount": "1.00", "currency": "USD"},
                "trackingStartDate": "2026-08-01",
            },
        )
    async with finance_client(database_url=postgres_database_url, actor=other_user) as client:
        non_owned_ledger = await client.get(
            f"/api/finance/ledgers/{first_ledger.json()['id']}/accounts"
        )
        non_owned_semantic = await client.patch(
            f"/api/finance/ledgers/{first_ledger.json()['id']}/accounts/{account.json()['id']}",
            json={"nature": "liability"},
        )

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        wrong_ledger = await client.patch(
            f"/api/finance/ledgers/{second_ledger.json()['id']}/accounts/{account.json()['id']}",
            json={"name": "Leaked"},
        )
        wrong_ledger_semantic = await client.patch(
            f"/api/finance/ledgers/{second_ledger.json()['id']}/accounts/{account.json()['id']}",
            json={"nature": "liability"},
        )
        missing_account = await client.patch(
            f"/api/finance/ledgers/{second_ledger.json()['id']}/accounts/{uuid4()}",
            json={"name": "Missing"},
        )
        missing_semantic = await client.patch(
            f"/api/finance/ledgers/{second_ledger.json()['id']}/accounts/{uuid4()}",
            json={"currency": "USD"},
        )

    assert non_owned_ledger.status_code == HTTPStatus.NOT_FOUND
    assert non_owned_semantic.status_code == HTTPStatus.NOT_FOUND
    assert non_owned_ledger.json()["code"] == "finance_ledger_not_found"
    assert non_owned_semantic.json()["code"] == "finance_ledger_not_found"
    assert wrong_ledger.status_code == HTTPStatus.NOT_FOUND
    assert wrong_ledger_semantic.status_code == HTTPStatus.NOT_FOUND
    assert missing_account.status_code == HTTPStatus.NOT_FOUND
    assert missing_semantic.status_code == HTTPStatus.NOT_FOUND
    assert wrong_ledger.json()["code"] == "finance_account_not_found"
    assert wrong_ledger_semantic.json()["code"] == "finance_account_not_found"
    assert missing_account.json()["code"] == "finance_account_not_found"
    assert missing_semantic.json()["code"] == "finance_account_not_found"
    assert wrong_ledger.json()["detail"] == missing_account.json()["detail"]
    assert wrong_ledger_semantic.json()["detail"] == missing_semantic.json()["detail"]


async def test_account_list_order_uses_status_case_folded_name_and_identifier(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("account-order")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Order"})
        ledger_id = ledger.json()["id"]
        created = []
        for name in ("beta", "Alpha", "alpha"):
            response = await client.post(
                f"/api/finance/ledgers/{ledger_id}/accounts",
                json={
                    "name": name,
                    "nature": "asset",
                    "currency": "CNY",
                    "openingBalance": {"amount": "0", "currency": "CNY"},
                    "trackingStartDate": "2026-08-01",
                },
            )
            created.append(response.json())
        archived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{created[0]['id']}/archive"
        )
        listed = await client.get(f"/api/finance/ledgers/{ledger_id}/accounts")

    active = sorted(created[1:], key=lambda account: account["id"])
    assert listed.json() == [*active, archived.json()]


async def test_income_and_expense_are_readable_and_derive_account_relative_balances(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("ordinary-transactions")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        ledger_id = ledger.json()["id"]
        asset = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "100.00", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        liability = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "Card",
                "nature": "liability",
                "currency": "CNY",
                "openingBalance": {"amount": "200.00", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        category = await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories",
            json={"name": "Salary"},
        )

        commands = (
            ("income", asset.json()["id"], "25.00", category.json()["id"], "  Pay  "),
            ("expense", asset.json()["id"], "10.00", None, "  "),
            ("income", liability.json()["id"], "30.00", None, None),
            ("expense", liability.json()["id"], "5.00", None, None),
        )
        created = []
        for kind, account_id, amount, category_id, note in commands:
            response = await client.post(
                f"/api/finance/ledgers/{ledger_id}/transactions",
                json={
                    "kind": kind,
                    "accountId": account_id,
                    "transactionDate": "2026-08-21",
                    "economicAmount": {"amount": amount, "currency": "CNY"},
                    "categoryAllocations": [
                        {
                            "amount": {"amount": amount, "currency": "CNY"},
                            "categoryId": category_id,
                        }
                    ],
                    "note": note,
                },
            )
            created.append(response)

        details = [
            await client.get(
                f"/api/finance/ledgers/{ledger_id}/transactions/{response.json()['id']}"
            )
            for response in created
        ]
        accounts = await client.get(f"/api/finance/ledgers/{ledger_id}/accounts")

    assert [response.status_code for response in created] == [HTTPStatus.CREATED] * 4
    assert [response.json()["kind"] for response in created] == [
        "income",
        "expense",
        "income",
        "expense",
    ]
    assert (
        details[0].json()
        == created[0].json()
        == {
            "id": created[0].json()["id"],
            "ledgerId": ledger_id,
            "kind": "income",
            "transactionDate": "2026-08-21",
            "note": "Pay",
            "account": {
                "id": asset.json()["id"],
                "name": "Cash",
                "status": "active",
            },
            "economicAmount": {"amount": "25.00", "currency": "CNY"},
            "categoryAllocations": [
                {
                    "amount": {"amount": "25.00", "currency": "CNY"},
                    "category": {
                        "id": category.json()["id"],
                        "name": "Salary",
                        "status": "active",
                    },
                }
            ],
        }
    )
    assert details[1].json() == created[1].json()
    assert created[1].json()["note"] is None
    assert created[1].json()["categoryAllocations"][0]["category"] is None
    balances_by_id = {
        account["id"]: account["currentBalance"]["amount"] for account in accounts.json()
    }
    assert balances_by_id == {
        asset.json()["id"]: "115.00",
        liability.json()["id"]: "175.00",
    }


async def test_transaction_creation_enforces_active_scoped_references(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("transaction-scope")
    other_user = _user("transaction-scope-other")
    postgres_session.add_all([actor, other_user])
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        other_ledger = await client.post("/api/finance/ledgers", json={"name": "Other"})
        account = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "USD",
                "openingBalance": {"amount": "0", "currency": "USD"},
                "trackingStartDate": "2026-08-01",
            },
        )
        other_account = await client.post(
            f"/api/finance/ledgers/{other_ledger.json()['id']}/accounts",
            json={
                "name": "Other Cash",
                "nature": "asset",
                "currency": "USD",
                "openingBalance": {"amount": "0", "currency": "USD"},
                "trackingStartDate": "2026-08-01",
            },
        )
        category = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/categories",
            json={"name": "Food"},
        )
        archived_account = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts/{account.json()['id']}/archive"
        )
        archived_account_write = await _post_transaction(
            client,
            ledger_id=ledger.json()["id"],
            account_id=archived_account.json()["id"],
        )
        await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/accounts/{account.json()['id']}/unarchive"
        )
        categorized = await _post_transaction(
            client,
            ledger_id=ledger.json()["id"],
            account_id=account.json()["id"],
            category_id=category.json()["id"],
        )
        archived_category = await client.post(
            f"/api/finance/ledgers/{ledger.json()['id']}/categories/{category.json()['id']}/archive"
        )
        archived_history = await client.get(
            f"/api/finance/ledgers/{ledger.json()['id']}/transactions/{categorized.json()['id']}"
        )
        wrong_transaction_scope = await client.get(
            f"/api/finance/ledgers/{other_ledger.json()['id']}/transactions/{categorized.json()['id']}"
        )
        missing_transaction = await client.get(
            f"/api/finance/ledgers/{other_ledger.json()['id']}/transactions/{uuid4()}"
        )
        archived_category_write = await _post_transaction(
            client,
            ledger_id=ledger.json()["id"],
            account_id=account.json()["id"],
            category_id=archived_category.json()["id"],
        )
        wrong_account_scope = await _post_transaction(
            client,
            ledger_id=other_ledger.json()["id"],
            account_id=account.json()["id"],
        )
        wrong_category_scope = await _post_transaction(
            client,
            ledger_id=other_ledger.json()["id"],
            account_id=other_account.json()["id"],
            category_id=category.json()["id"],
        )

    async with finance_client(database_url=postgres_database_url, actor=other_user) as client:
        non_owned_ledger = await _post_transaction(
            client,
            ledger_id=ledger.json()["id"],
            account_id=account.json()["id"],
        )

    assert archived_account_write.status_code == HTTPStatus.CONFLICT
    assert archived_account_write.json()["code"] == "finance_account_archived"
    assert archived_category_write.status_code == HTTPStatus.CONFLICT
    assert archived_category_write.json()["code"] == "finance_category_archived"
    assert archived_history.json()["categoryAllocations"][0]["category"] == {
        "id": category.json()["id"],
        "name": "Food",
        "status": "archived",
    }
    assert wrong_transaction_scope.status_code == HTTPStatus.NOT_FOUND
    assert missing_transaction.status_code == HTTPStatus.NOT_FOUND
    assert wrong_transaction_scope.json()["code"] == "finance_transaction_not_found"
    assert missing_transaction.json()["code"] == "finance_transaction_not_found"
    assert wrong_transaction_scope.json()["detail"] == missing_transaction.json()["detail"]
    assert wrong_account_scope.status_code == HTTPStatus.NOT_FOUND
    assert wrong_account_scope.json()["code"] == "finance_account_not_found"
    assert wrong_category_scope.status_code == HTTPStatus.NOT_FOUND
    assert wrong_category_scope.json()["code"] == "finance_category_not_found"
    assert non_owned_ledger.status_code == HTTPStatus.NOT_FOUND
    assert non_owned_ledger.json()["code"] == "finance_ledger_not_found"


async def test_future_transactions_apply_immediately_and_bound_tracking_start_edits(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("transaction-dates")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Dates"})
        ledger_id = ledger.json()["id"]
        account = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "JPY",
                "openingBalance": {"amount": "100", "currency": "JPY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        before_tracking = await _post_transaction(
            client,
            ledger_id=ledger_id,
            account_id=account.json()["id"],
            transaction_date="2026-07-31",
            amount="20",
            currency="JPY",
        )
        future = await _post_transaction(
            client,
            ledger_id=ledger_id,
            account_id=account.json()["id"],
            transaction_date="2030-01-01",
            amount="20",
            currency="JPY",
        )
        moved_tracking_start = await client.patch(
            f"/api/finance/ledgers/{ledger_id}/accounts/{account.json()['id']}",
            json={"trackingStartDate": "2030-01-02"},
        )
        accounts = await client.get(f"/api/finance/ledgers/{ledger_id}/accounts")

    assert before_tracking.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert before_tracking.json()["code"] == "validation_error"
    assert future.status_code == HTTPStatus.CREATED
    assert future.json()["transactionDate"] == "2030-01-01"
    assert moved_tracking_start.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert moved_tracking_start.json()["code"] == "validation_error"
    assert accounts.json()[0]["trackingStartDate"] == "2026-08-01"
    assert accounts.json()[0]["currentBalance"] == {"amount": "120", "currency": "JPY"}


async def test_internal_transfer_create_detail_balances_and_validation(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("internal-transfer-http")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Transfers"})
        ledger_id = ledger.json()["id"]
        source = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "Checking",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "5.00", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        destination = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "Card",
                "nature": "liability",
                "currency": "CNY",
                "openingBalance": {"amount": "0.00", "currency": "CNY"},
                "trackingStartDate": "2026-08-10",
            },
        )
        command = {
            "kind": "internalTransfer",
            "sourceAccountId": source.json()["id"],
            "destinationAccountId": destination.json()["id"],
            "amount": {"amount": "10", "currency": "CNY"},
            "transactionDate": "2030-01-01",
            "note": "  pay card  ",
        }
        created = await client.post(f"/api/finance/ledgers/{ledger_id}/transactions", json=command)
        detail = await client.get(
            f"/api/finance/ledgers/{ledger_id}/transactions/{created.json()['id']}"
        )
        accounts = await client.get(f"/api/finance/ledgers/{ledger_id}/accounts")

        identical = await client.post(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            json={**command, "destinationAccountId": source.json()["id"]},
        )
        before_source = await client.post(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            json={**command, "transactionDate": "2026-07-31"},
        )
        before_destination = await client.post(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            json={**command, "transactionDate": "2026-08-09"},
        )
        await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{destination.json()['id']}/archive"
        )
        archived_destination = await client.post(
            f"/api/finance/ledgers/{ledger_id}/transactions", json=command
        )

    assert created.status_code == HTTPStatus.CREATED
    assert created.json() == detail.json()
    assert created.json() == {
        "id": created.json()["id"],
        "ledgerId": ledger_id,
        "kind": "internalTransfer",
        "transactionDate": "2030-01-01",
        "note": "pay card",
        "sourceAccount": {
            "id": source.json()["id"],
            "name": "Checking",
            "status": "active",
        },
        "sourceAmount": {"amount": "10.00", "currency": "CNY"},
        "destinationAccount": {
            "id": destination.json()["id"],
            "name": "Card",
            "status": "active",
        },
        "destinationAmount": {"amount": "10.00", "currency": "CNY"},
    }
    balances = {account["name"]: account["currentBalance"] for account in accounts.json()}
    assert balances == {
        "Checking": {"amount": "-5.00", "currency": "CNY"},
        "Card": {"amount": "-10.00", "currency": "CNY"},
    }
    for invalid in (identical, before_source, before_destination):
        assert invalid.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert invalid.json()["code"] == "validation_error"
    assert archived_destination.status_code == HTTPStatus.CONFLICT
    assert archived_destination.json()["code"] == "finance_account_archived"


async def test_internal_transfer_enforces_currency_archive_and_account_scope(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("internal-transfer-scope")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        first = await client.post("/api/finance/ledgers", json={"name": "First"})
        second = await client.post("/api/finance/ledgers", json={"name": "Second"})
        first_id = first.json()["id"]
        source = await _create_account(client, first_id, "Source", "CNY")
        destination = await _create_account(client, first_id, "Destination", "CNY")
        usd = await _create_account(client, first_id, "USD", "USD")
        out_of_scope = await _create_account(client, second.json()["id"], "Private", "CNY")
        command = {
            "kind": "internalTransfer",
            "sourceAccountId": source["id"],
            "destinationAccountId": destination["id"],
            "amount": {"amount": "1.00", "currency": "CNY"},
            "transactionDate": "2026-08-01",
        }
        mismatched_accounts = await client.post(
            f"/api/finance/ledgers/{first_id}/transactions",
            json={**command, "destinationAccountId": usd["id"]},
        )
        mismatched_amount = await client.post(
            f"/api/finance/ledgers/{first_id}/transactions",
            json={**command, "amount": {"amount": "1.00", "currency": "USD"}},
        )
        wrong_source = await client.post(
            f"/api/finance/ledgers/{first_id}/transactions",
            json={**command, "sourceAccountId": out_of_scope["id"]},
        )
        wrong_destination = await client.post(
            f"/api/finance/ledgers/{first_id}/transactions",
            json={**command, "destinationAccountId": out_of_scope["id"]},
        )
        await client.post(f"/api/finance/ledgers/{first_id}/accounts/{source['id']}/archive")
        archived_source = await client.post(
            f"/api/finance/ledgers/{first_id}/transactions", json=command
        )

    for invalid in (mismatched_accounts, mismatched_amount):
        assert invalid.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert invalid.json()["code"] == "validation_error"
    for hidden in (wrong_source, wrong_destination):
        assert hidden.status_code == HTTPStatus.NOT_FOUND
        assert hidden.json()["code"] == "finance_account_not_found"
    assert archived_source.status_code == HTTPStatus.CONFLICT
    assert archived_source.json()["code"] == "finance_account_archived"


async def test_user_can_manage_the_complete_category_lifecycle(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("category-lifecycle")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        ledger_id = ledger.json()["id"]
        created = await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories",
            json={"name": "  Food  "},
        )
        unchanged = await client.patch(
            f"/api/finance/ledgers/{ledger_id}/categories/{created.json()['id']}",
            json={},
        )
        renamed = await client.patch(
            f"/api/finance/ledgers/{ledger_id}/categories/{created.json()['id']}",
            json={"name": "  Groceries  "},
        )
        archived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories/{created.json()['id']}/archive"
        )
        archived_again = await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories/{created.json()['id']}/archive"
        )
        listed = await client.get(f"/api/finance/ledgers/{ledger_id}/categories")
        unarchived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories/{created.json()['id']}/unarchive"
        )
        unarchived_again = await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories/{created.json()['id']}/unarchive"
        )

    assert created.status_code == HTTPStatus.CREATED
    assert created.json() == {
        "id": created.json()["id"],
        "name": "Food",
        "status": "active",
    }
    assert unchanged.json() == created.json()
    assert renamed.json() == {**created.json(), "name": "Groceries"}
    assert archived.json() == {**renamed.json(), "status": "archived"}
    assert archived_again.json() == archived.json()
    assert listed.json() == [archived.json()]
    assert unarchived.json() == {**renamed.json(), "status": "active"}
    assert unarchived_again.json() == unarchived.json()


async def test_category_names_remain_unique_while_archived_within_one_ledger(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("category-uniqueness")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        first_ledger = await client.post("/api/finance/ledgers", json={"name": "First"})
        second_ledger = await client.post("/api/finance/ledgers", json={"name": "Second"})
        first_ledger_id = first_ledger.json()["id"]
        category = await client.post(
            f"/api/finance/ledgers/{first_ledger_id}/categories",
            json={"name": "Straße"},
        )
        archived = await client.post(
            f"/api/finance/ledgers/{first_ledger_id}/categories/{category.json()['id']}/archive"
        )
        duplicate = await client.post(
            f"/api/finance/ledgers/{first_ledger_id}/categories",
            json={"name": "STRASSE"},
        )
        other = await client.post(
            f"/api/finance/ledgers/{first_ledger_id}/categories",
            json={"name": "Other"},
        )
        rename_conflict = await client.patch(
            f"/api/finance/ledgers/{first_ledger_id}/categories/{other.json()['id']}",
            json={"name": "strasse"},
        )
        unarchived = await client.post(
            f"/api/finance/ledgers/{first_ledger_id}/categories/{category.json()['id']}/unarchive"
        )
        same_name_other_ledger = await client.post(
            f"/api/finance/ledgers/{second_ledger.json()['id']}/categories",
            json={"name": "STRASSE"},
        )

    assert archived.json()["status"] == "archived"
    for response in (duplicate, rename_conflict):
        assert response.status_code == HTTPStatus.CONFLICT
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "finance_category_name_conflict"
    assert unarchived.status_code == HTTPStatus.OK
    assert unarchived.json()["status"] == "active"
    assert same_name_other_ledger.status_code == HTTPStatus.CREATED


async def test_category_lookups_do_not_leak_across_ledger_or_owner_scope(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("category-scope-actor")
    other_user = _user("category-scope-other")
    postgres_session.add_all([actor, other_user])
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        first_ledger = await client.post("/api/finance/ledgers", json={"name": "First"})
        second_ledger = await client.post("/api/finance/ledgers", json={"name": "Second"})
        category = await client.post(
            f"/api/finance/ledgers/{first_ledger.json()['id']}/categories",
            json={"name": "Private"},
        )

    async with finance_client(database_url=postgres_database_url, actor=other_user) as client:
        non_owned_ledger = await client.get(
            f"/api/finance/ledgers/{first_ledger.json()['id']}/categories"
        )

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        wrong_ledger = await client.patch(
            f"/api/finance/ledgers/{second_ledger.json()['id']}/categories/{category.json()['id']}",
            json={"name": "Leaked"},
        )
        missing_category = await client.patch(
            f"/api/finance/ledgers/{second_ledger.json()['id']}/categories/{uuid4()}",
            json={"name": "Missing"},
        )

    assert non_owned_ledger.status_code == HTTPStatus.NOT_FOUND
    assert non_owned_ledger.json()["code"] == "finance_ledger_not_found"
    assert wrong_ledger.status_code == HTTPStatus.NOT_FOUND
    assert missing_category.status_code == HTTPStatus.NOT_FOUND
    assert wrong_ledger.json()["code"] == "finance_category_not_found"
    assert missing_category.json()["code"] == "finance_category_not_found"
    assert wrong_ledger.json()["detail"] == missing_category.json()["detail"]


async def test_category_list_order_uses_status_case_folded_name_and_identifier(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("category-order")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Order"})
        ledger_id = ledger.json()["id"]
        created = []
        for name in ("beta", "Alpha", "zebra", "Äpfel"):
            response = await client.post(
                f"/api/finance/ledgers/{ledger_id}/categories",
                json={"name": name},
            )
            created.append(response.json())
        archived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories/{created[0]['id']}/archive"
        )
        listed = await client.get(f"/api/finance/ledgers/{ledger_id}/categories")

    assert listed.json() == [created[1], created[2], created[3], archived.json()]


async def _post_transaction(
    client: AsyncClient,
    *,
    ledger_id: str,
    account_id: str,
    category_id: str | None = None,
    transaction_date: str = "2026-08-21",
    amount: str = "10.00",
    currency: str = "USD",
) -> Response:
    """Record one valid Income command for focused boundary tests."""

    return await client.post(
        f"/api/finance/ledgers/{ledger_id}/transactions",
        json={
            "kind": "income",
            "accountId": account_id,
            "transactionDate": transaction_date,
            "economicAmount": {"amount": amount, "currency": currency},
            "categoryAllocations": [
                {
                    "amount": {"amount": amount, "currency": currency},
                    "categoryId": category_id,
                }
            ],
        },
    )


async def _create_account(
    client: AsyncClient,
    ledger_id: str,
    name: str,
    currency: str,
) -> dict[str, object]:
    response = await client.post(
        f"/api/finance/ledgers/{ledger_id}/accounts",
        json={
            "name": name,
            "nature": "asset",
            "currency": currency,
            "openingBalance": {"amount": "0", "currency": currency},
            "trackingStartDate": "2026-08-01",
        },
    )
    assert response.status_code == HTTPStatus.CREATED
    payload: dict[str, object] = response.json()
    return payload


def _user(subject: str) -> User:
    return User(
        identity_issuer="https://identity.example.test/finance-api",
        identity_subject=subject,
        status="active",
    )
