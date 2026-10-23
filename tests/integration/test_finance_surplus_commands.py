"""Closed Income intent, operation-specific versioning and retained recovery."""

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance import submissions
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_advances import advance_client as advance_client
from integration.test_finance_api import finance_client
from integration.test_finance_surplus import accepted, confirm_income

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "invalid",
    [
        "disposition",
        "missing",
        "split",
        "zero",
        "negative",
        "float",
        "currency",
        "precision",
        "extra",
        "subset",
        "credit",
        "forgiveness",
        "v1",
        "v2",
    ],
)
async def test_invalid_income_intent_is_definitively_not_admitted(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]], invalid: str
) -> None:
    client, actor, base, draft = advance_client
    body = accepted(draft, "2.00")
    mutations: dict[str, dict[str, object]] = {
        "disposition": {"excessDisposition": "retainCredit"},
        "missing": {"categoryAllocations": []},
        "split": {"categoryAllocations": [{"amount": {"amount": "1.00", "currency": "CNY"}}] * 2},
        "extra": {"income": True},
        "subset": {"obligationIds": []},
        "credit": {"useCredit": True},
        "forgiveness": {"forgiven": "1.00"},
        "v1": {},
        "v2": {},
    }
    if invalid in mutations:
        body.update(mutations[invalid])
    else:
        money: dict[str, object] = {"amount": "2.00", "currency": "CNY"}
        if invalid == "currency":
            money["currency"] = "USD"
        else:
            money["amount"] = {
                "zero": "0.00",
                "negative": "-1.00",
                "float": 2.0,
                "precision": "1.001",
            }[invalid]
        body["categoryAllocations"] = [{"amount": money}]
    headers = {
        **submission_headers(actor.id),
        "Finance-Command-Version": {"v1": "1", "v2": "2"}.get(invalid, "3"),
    }
    command = {**body, "previewToken": "0" * 64}
    before = (await client.get(f"{base}/accounts")).json()
    response = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["commandValidationRejection"]["attemptedBody"] == command
    assert (
        response.json()["commandValidationRejection"]["commandVersion"]
        == headers["Finance-Command-Version"]
    )
    assert (await client.get(f"{base}/accounts")).json() == before
    assert (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).status_code == 404


async def test_bound_income_recovers_after_admission_closes_and_preserves_identity(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, actor, base, draft = advance_client
    body = accepted(draft, "2.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {
        **body,
        "note": body["note"],
        "categoryAllocations": [
            {"amount": {"amount": "2.00", "currency": "CNY"}, "categoryId": None}
        ],
        "previewToken": preview["previewToken"],
    }
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "3"}
    postgres_session.add(
        FinanceSubmission(
            local_user_id=actor.id,
            submission_id=UUID(headers["Idempotency-Key"]),
            command_version="3",
            retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
            canonical_command={
                "commandVersion": "3",
                "operation": "createFinanceSettlement",
                "targetLedgerId": base.rsplit("/", 1)[1],
                "body": command,
            },
        )
    )
    await postgres_session.commit()
    monkeypatch.setattr(submissions, "OPEN_ADMISSION_VERSIONS", frozenset())
    response = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert response.status_code == 201, response.text
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == response.json()
    assert (
        await client.post(
            f"{base}/settlements", json={**command, "note": "changed"}, headers=headers
        )
    ).json()["code"] == "finance_submission_content_conflict"
    assert (
        await client.post(
            f"{base}/settlements", json=command, headers={**headers, "Finance-Command-Version": "2"}
        )
    ).json()["code"] == "finance_submission_version_mismatch"
    closed = await client.post(
        f"{base}/settlements",
        json=command,
        headers={**submission_headers(actor.id), "Finance-Command-Version": "3"},
    )
    assert closed.json()["code"] == "finance_command_version_closed"
    assert "submissionReceipt" not in closed.json()


async def test_income_category_ownership_missing_and_archived_rejections_are_durable(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    client, actor, base, draft = advance_client
    category = await create_finance_resource(
        client, f"{base}/categories", owner_id=actor.id, json={"name": "Surplus"}
    )
    await client.post(f"{base}/categories/{category.json()['id']}/archive")
    other = await client.post(
        "/api/finance/ledgers", json={"name": "Other"}, headers=submission_headers(actor.id)
    )
    other_base = f"/api/finance/ledgers/{other.json()['outcome']['resource']['id']}"
    foreign = await create_finance_resource(
        client, f"{other_base}/categories", owner_id=actor.id, json={"name": "Foreign"}
    )
    for category_id, expected in [
        (str(uuid4()), "finance_category_not_found"),
        (foreign.json()["id"], "finance_category_not_found"),
        (category.json()["id"], "finance_category_archived"),
    ]:
        body = {
            **accepted(draft, "2.00"),
            "categoryAllocations": [
                {"amount": {"amount": "2.00", "currency": "CNY"}, "categoryId": category_id}
            ],
            "previewToken": "0" * 64,
        }
        # Missing references are rejected before a token can be established; archived
        # references at confirmation first invalidate a previously valid token.
        response = await client.post(
            f"{base}/settlements",
            json=body,
            headers=(headers := {**submission_headers(actor.id), "Finance-Command-Version": "3"}),
        )
        assert response.status_code == (404 if "not_found" in expected else 409), response.text
        assert response.json()["code"] == (
            expected if "not_found" in expected else "finance_settlement_preview_stale"
        )
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["receipt"] == response.json()["submissionReceipt"]
    detail = await confirm_income(client, actor, base, accepted(draft, "2.00"))
    intruder = User(
        identity_issuer="https://identity.example.test/surplus",
        identity_subject="intruder",
        status="active",
    )
    postgres_session.add(intruder)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=intruder) as outsider:
        assert (await outsider.get(f"{base}/settlements/{detail['id']}")).status_code == 404
        assert (
            await outsider.post(f"{base}/settlements/preview", json=accepted(draft, "2.00"))
        ).status_code == 404
    assert (
        await client.post(
            f"{base}/advances",
            json=draft,
            headers={**submission_headers(actor.id), "Finance-Command-Version": "3"},
        )
    ).json()["code"] == "finance_command_version_unsupported"


@pytest.mark.parametrize(
    "nature,currency,amount",
    [
        ("asset", "JPY", "2"),
        ("liability", "USD", "2.00"),
        ("asset", "CNY", "123456789012345678901234567890.12"),
    ],
)
async def test_income_receipt_preserves_exact_money_account_nature_and_financial_date(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    nature: str,
    currency: str,
    amount: str,
) -> None:
    client, actor, base, draft = advance_client
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Receiving",
            "nature": nature,
            "currency": currency,
            "openingBalance": {
                "amount": "0" if currency == "JPY" else "0.00",
                "currency": currency,
            },
            "trackingStartDate": "2026-10-01",
        },
    )
    body = {
        **accepted(draft, amount, amount),
        "receivingAccountId": account.json()["id"],
        "transactionDate": "2026-11-01",
        "amount": {"amount": amount, "currency": currency},
        "categoryAllocations": [{"amount": {"amount": amount, "currency": currency}}],
    }
    detail = await confirm_income(client, actor, base, body)
    assert detail["allocations"] == []
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert (
        accounts[account.json()["id"]]["currentBalance"]["amount"]
        == ("-" if nature == "liability" else "") + amount
    )
    overview = (await client.get(f"{base}/overview", params={"month": "2026-11"})).json()
    assert (
        next(s for s in overview["monthSummaryByCurrency"] if s["currency"] == currency)["income"][
            "amount"
        ]
        == amount
    )
    assert (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json() == []
