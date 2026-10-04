"""Explicit protocol creates and resource reads for lifecycle test setup."""

from uuid import UUID, uuid4

from httpx import AsyncClient, Response


def submission_headers(owner_id: UUID, key: UUID | None = None) -> dict[str, str]:
    return {
        "Idempotency-Key": str(key or uuid4()),
        "Finance-Command-Version": "1",
        "Finance-Submission-Owner": str(owner_id),
    }


async def create_finance_resource(
    client: AsyncClient, path: str, *, json: dict[str, object], owner_id: UUID
) -> Response:
    """Assert a fresh nested-create receipt, then read its current resource.

    Existing lifecycle tests need Account/Category projections for subsequent
    edits and Transactions. Submission tests use POST directly to test evidence.
    Failed creates retain their real Problem Details, including terminal evidence.
    """
    headers = submission_headers(owner_id)
    response = await client.post(path, json=json, headers=headers)
    if response.status_code != 201:
        return response
    receipt = response.json()
    resource_type = "account" if path.endswith("/accounts") else "category"
    assert receipt["submissionId"] == headers["Idempotency-Key"]
    assert receipt["operation"] == f"createFinance{resource_type.title()}"
    assert receipt["targetLedgerId"] == path.split("/")[-2]
    resource = receipt["outcome"]["resource"]
    assert resource["type"] == resource_type
    listed = await client.get(path)
    assert listed.status_code == 200
    current = next(item for item in listed.json() if item["id"] == resource["id"])
    return Response(201, json=current, request=response.request)
