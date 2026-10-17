"""Participant lifecycle behavior through the public API and protected PostgreSQL."""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_api import finance_client

pytestmark = pytest.mark.anyio


@pytest.fixture
async def participant_client(
    postgres_database_url: str, postgres_session: AsyncSession
) -> AsyncIterator[tuple[AsyncClient, User, str]]:
    actor = User(
        identity_issuer="https://identity.example.test/participants",
        identity_subject="owner",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post(
            "/api/finance/ledgers", json={"name": "Personal"}, headers=submission_headers(actor.id)
        )
        assert ledger.status_code == 201
        ledger_id = ledger.json()["outcome"]["resource"]["id"]
        yield client, actor, f"/api/finance/ledgers/{ledger_id}/participants"


async def test_owner_tracks_private_participant_without_another_local_user(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/participants",
        identity_subject="owner",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger_response = await client.post(
            "/api/finance/ledgers", json={"name": "Personal"}, headers=submission_headers(actor.id)
        )
        assert ledger_response.status_code == 201
        ledger = ledger_response.json()["outcome"]["resource"]
        path = f"/api/finance/ledgers/{ledger['id']}/participants"
        assert (await client.get(path)).json() == []
        created = await client.post(path, json={"name": "  小明  "})
        assert created.status_code == 201
        participant = created.json()
        UUID(participant["id"])
        assert participant == {
            "id": participant["id"],
            "ledgerId": ledger["id"],
            "name": "小明",
            "status": "active",
        }
        assert (await client.get(path)).json() == [participant]
        detail = await client.get(f"{path}/{participant['id']}")
        assert detail.status_code == 200
        assert detail.json() == participant


async def test_rename_and_archive_preserve_identity_and_recorded_finances(
    postgres_database_url: str, postgres_session: AsyncSession
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/participants",
        identity_subject="lifecycle",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post(
            "/api/finance/ledgers", json={"name": "Personal"}, headers=submission_headers(actor.id)
        )
        ledger_id = ledger.json()["outcome"]["resource"]["id"]
        base = f"/api/finance/ledgers/{ledger_id}"
        account = await create_finance_resource(
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
        assert account.status_code == 201
        expense = await create_finance_resource(
            client,
            f"{base}/transactions",
            owner_id=actor.id,
            json={
                "kind": "expense",
                "accountId": account.json()["id"],
                "transactionDate": "2026-10-02",
                "note": None,
                "economicAmount": {"amount": "25.00", "currency": "CNY"},
                "categoryAllocations": [
                    {
                        "amount": {"amount": "25.00", "currency": "CNY"},
                        "categoryId": None,
                    }
                ],
            },
        )
        assert expense.status_code == 201
        financial_paths = [
            f"{base}/accounts",
            f"{base}/transactions",
            f"{base}/overview?month=2026-10",
        ]
        before = [(await client.get(path)).json() for path in financial_paths]
        created = await client.post(f"{base}/participants", json={"name": "Original"})
        assert created.status_code == 201
        participant = created.json()
        path = f"{base}/participants/{participant['id']}"
        renamed = await client.patch(path, json={"name": "  Renamed  "})
        assert renamed.status_code == 200
        assert renamed.json() == {**participant, "name": "Renamed"}
        archived = await client.post(f"{path}/archive")
        assert archived.status_code == 200
        expected = {**participant, "name": "Renamed", "status": "archived"}
        assert archived.json() == expected
        assert (await client.post(f"{path}/archive")).json() == expected
        assert (await client.get(path)).json() == expected
        assert (await client.get(f"{base}/participants")).json() == [expected]
        assert (await client.delete(path)).status_code == 405
        assert (await client.get(path)).json() == expected
        assert [(await client.get(path)).json() for path in financial_paths] == before

    # A new app/session must still resolve the same archived identity.
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        assert (await client.get(path)).json() == expected


async def test_same_names_remain_distinct_and_archive_retains_addressable_identity(
    participant_client: tuple[AsyncClient, User, str],
) -> None:
    client, _, path = participant_client
    responses = [
        await client.post(path, json={"name": name}) for name in ["Zebra", "alice", "ALICE"]
    ]
    assert all(response.status_code == 201 for response in responses)
    participants = [response.json() for response in responses]
    assert len({item["id"] for item in participants}) == 3
    alices = sorted(participants[1:], key=lambda item: item["id"])
    assert (await client.get(path)).json() == [*alices, participants[0]]
    archived = await client.post(f"{path}/{alices[0]['id']}/archive")
    assert archived.status_code == 200
    assert (await client.get(path)).json() == [alices[1], participants[0], archived.json()]
    renamed = await client.patch(f"{path}/{alices[0]['id']}", json={"name": "alice"})
    assert renamed.status_code == 200
    assert renamed.json() == {**archived.json(), "name": "alice"}


async def test_participants_cannot_cross_ledgers_even_for_the_same_owner(
    participant_client: tuple[AsyncClient, User, str],
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    client, actor, path = participant_client
    original = (await client.post(path, json={"name": "Private"})).json()
    second = await client.post(
        "/api/finance/ledgers", json={"name": "Second"}, headers=submission_headers(actor.id)
    )
    second_id = second.json()["outcome"]["resource"]["id"]
    second_path = f"/api/finance/ledgers/{second_id}/participants"
    assert (await client.get(second_path)).json() == []
    same_name = (await client.post(second_path, json={"name": "Private"})).json()
    assert same_name["id"] != original["id"]
    assert same_name["ledgerId"] == second_id
    wrong = f"{second_path}/{original['id']}"
    for response in [
        await client.get(wrong),
        await client.patch(wrong, json={"name": "Stolen"}),
        await client.post(f"{wrong}/archive"),
    ]:
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "finance_participant_not_found"
    assert (await client.get(f"{path}/{original['id']}")).json() == original

    other = User(
        identity_issuer=actor.identity_issuer, identity_subject="other-owner", status="active"
    )
    postgres_session.add(other)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=other) as other_client:
        missing_path = f"/api/finance/ledgers/{uuid4()}/participants"
        for base in [path, missing_path]:
            detail_path = f"{base}/{original['id']}"
            responses = [
                await other_client.get(base),
                await other_client.post(base, json={"name": "Intruder"}),
                await other_client.get(detail_path),
                await other_client.patch(detail_path, json={"name": "Intruder"}),
                await other_client.post(f"{detail_path}/archive"),
            ]
            for response in responses:
                assert response.status_code == 404
                assert response.headers["content-type"] == "application/problem+json"
                assert response.json()["code"] == "finance_ledger_not_found"
    assert (await client.get(path)).json() == [original]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": " \t\n　"},
        {"name": "x" * 101},
        {"name": None},
        {"name": 123},
        {"name": ["Alice"]},
        {"name": "Alice", "status": "archived"},
        {"name": "Alice", "ledgerId": str(uuid4())},
        {"name": "Alice", "userId": str(uuid4())},
        {"name": "Alice", "id": str(uuid4())},
    ],
)
async def test_invalid_create_and_rename_leave_the_participant_unchanged(
    participant_client: tuple[AsyncClient, User, str],
    payload: dict[str, object],
) -> None:
    client, _, path = participant_client
    created = await client.post(path, json={"name": "Original"})
    original = created.json()
    for response in [
        await client.post(path, json=payload),
        await client.patch(f"{path}/{original['id']}", json=payload),
    ]:
        assert response.status_code == 422
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "validation_error"
    assert (await client.get(path)).json() == [original]


async def test_missing_and_invalid_participant_addresses_use_problem_details(
    participant_client: tuple[AsyncClient, User, str],
) -> None:
    client, _, path = participant_client
    for identifier, status, code in [
        (str(uuid4()), 404, "finance_participant_not_found"),
        ("not-a-uuid", 422, "validation_error"),
    ]:
        detail = f"{path}/{identifier}"
        for response in [
            await client.get(detail),
            await client.patch(detail, json={"name": "Name"}),
            await client.post(f"{detail}/archive"),
        ]:
            assert response.status_code == status
            assert response.headers["content-type"] == "application/problem+json"
            assert response.json()["code"] == code
    assert (await client.get(path)).json() == []


async def test_concurrent_rename_and_archive_preserve_both_changes(
    participant_client: tuple[AsyncClient, User, str],
) -> None:
    client, _, path = participant_client
    participant = (await client.post(path, json={"name": "Before"})).json()
    detail = f"{path}/{participant['id']}"
    renamed, archived = await asyncio.gather(
        client.patch(detail, json={"name": "After"}), client.post(f"{detail}/archive")
    )
    assert renamed.status_code == archived.status_code == 200
    assert (await client.get(detail)).json() == {
        **participant,
        "name": "After",
        "status": "archived",
    }


async def test_disabled_owner_cannot_use_any_participant_operation(
    participant_client: tuple[AsyncClient, User, str],
    postgres_session: AsyncSession,
) -> None:
    client, actor, path = participant_client
    participant = (await client.post(path, json={"name": "Private"})).json()
    actor.status = "disabled"
    await postgres_session.commit()
    detail = f"{path}/{participant['id']}"
    for response in [
        await client.get(path),
        await client.post(path, json={"name": "New"}),
        await client.get(detail),
        await client.patch(detail, json={"name": "Changed"}),
        await client.post(f"{detail}/archive"),
    ]:
        assert response.status_code == 403
        assert response.headers["content-type"] == "application/problem+json"
