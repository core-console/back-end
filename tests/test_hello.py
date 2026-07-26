"""Frontend hello world contract tests."""

from http import HTTPStatus

import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_hello_world_matches_existing_contract(client: AsyncClient) -> None:
    response = await client.get("/api/helloWorld")

    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"message": "Hello, world!"}
