"""Public submission schemas cannot encode incompatible Ledger evidence."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.finance.submission_schemas import (
    AccountCreatedReceipt,
    AdjustmentSuccessReceipt,
    CategoryCreatedReceipt,
    FinanceSubmissionResponse,
    LedgerCreatedReceipt,
    LedgerValidationProblem,
    TransactionCreatedReceipt,
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
    transaction = schema["paths"]["/finance/ledgers/{ledgerId}/transactions"]["post"]
    assert transaction["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/TransactionCreatedReceipt"
    }
    assert {header["name"] for header in transaction["parameters"] if header["in"] == "header"} == {
        "Idempotency-Key",
        "Finance-Command-Version",
        "Finance-Submission-Owner",
    }


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


@pytest.mark.parametrize(
    ("resource", "name"), [("accounts", "Account"), ("categories", "Category")]
)
def test_nested_contract_is_operation_specific_and_requires_protocol(
    app: FastAPI, resource: str, name: str
) -> None:
    schema = app.openapi()
    create = schema["paths"][f"/finance/ledgers/{{ledgerId}}/{resource}"]["post"]
    assert create["operationId"] == f"createFinance{name}"
    assert {(p["name"], p["in"], p["required"]) for p in create["parameters"]} == {
        ("ledgerId", "path", True),
        ("Idempotency-Key", "header", True),
        ("Finance-Command-Version", "header", True),
        ("Finance-Submission-Owner", "header", True),
    }
    assert create["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": f"#/components/schemas/{name}CreatedReceipt"
    }
    for status, response in create["responses"].items():
        if not status.startswith("2"):
            assert set(response["content"]) == {"application/problem+json"}
    components = schema["components"]["schemas"]
    assert "commandValidationRejection" in components[f"{name}ValidationProblem"]["required"]
    assert "submissionReceipt" in components[f"{name}TerminalProblem"]["required"]
    assert (
        components[f"{name}SubmissionReceipt"]["properties"]["outcome"]["discriminator"][
            "propertyName"
        ]
        == "kind"
    )
    assert components[f"{name}CreatedReceipt"]["properties"]["targetLedgerId"]["format"] == "uuid"
    request = create["requestBody"]["content"]["application/json"]["schema"]
    assert request["additionalProperties"] is False
    if resource == "accounts":
        assert set(request["required"]) == {
            "name",
            "nature",
            "currency",
            "openingBalance",
            "trackingStartDate",
        }
        assert request["properties"]["openingBalance"]["properties"]["amount"]["type"] == "string"
        assert request["properties"]["trackingStartDate"]["format"] == "date"


def test_cumulative_openapi_references_resolve(
    app: FastAPI,
) -> None:
    schema = app.openapi()

    def check(node: Any) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                assert ref.startswith("#/")
                resolved: Any = schema
                for part in ref[2:].split("/"):
                    resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


@pytest.mark.parametrize(
    "operation", ["createFinanceAccount", "createFinanceCategory", "createFinanceTransaction"]
)
@pytest.mark.parametrize(
    "invalid", ["null_scope", "wrong_resource", "no_change", "wrong_operation"]
)
def test_nested_receipt_and_lookup_cannot_encode_impossible_combinations(
    operation: str, invalid: str
) -> None:
    resource = {
        "createFinanceAccount": "account",
        "createFinanceCategory": "category",
        "createFinanceTransaction": "transaction",
    }[operation]
    now = datetime.now(UTC)
    receipt: dict[str, object] = {
        "submissionId": uuid4(),
        "commandVersion": "1",
        "operation": operation,
        "targetLedgerId": uuid4(),
        "admittedAt": now,
        "resolvedAt": now,
        "outcome": {"kind": "created", "resource": {"type": resource, "id": uuid4()}},
    }
    if invalid == "null_scope":
        receipt["targetLedgerId"] = None
    elif invalid == "wrong_resource":
        receipt["outcome"] = {"kind": "created", "resource": {"type": "ledger", "id": uuid4()}}
    elif invalid == "no_change":
        receipt["outcome"] = {"kind": "noChange"}
    else:
        receipt["operation"] = "createFinanceLedger"
    models: dict[
        str,
        type[AccountCreatedReceipt]
        | type[CategoryCreatedReceipt]
        | type[TransactionCreatedReceipt],
    ] = {
        "account": AccountCreatedReceipt,
        "category": CategoryCreatedReceipt,
        "transaction": TransactionCreatedReceipt,
    }
    model = models[resource]
    with pytest.raises(ValidationError):
        model.model_validate(receipt)
    with pytest.raises(ValidationError):
        FinanceSubmissionResponse.model_validate({"state": "terminal", "receipt": receipt})


@pytest.mark.parametrize(
    ("path", "operation", "status", "receipt"),
    [
        ("/finance/ledgers", "createFinanceLedger", "201", "LedgerCreatedReceipt"),
        (
            "/finance/ledgers/{ledgerId}/accounts",
            "createFinanceAccount",
            "201",
            "AccountCreatedReceipt",
        ),
        (
            "/finance/ledgers/{ledgerId}/categories",
            "createFinanceCategory",
            "201",
            "CategoryCreatedReceipt",
        ),
        (
            "/finance/ledgers/{ledgerId}/transactions",
            "createFinanceTransaction",
            "201",
            "TransactionCreatedReceipt",
        ),
        (
            "/finance/ledgers/{ledgerId}/balance-adjustments",
            "createBalanceAdjustment",
            "200",
            "AdjustmentSuccessReceipt",
        ),
    ],
)
def test_final_five_create_schema_header_status_and_lookup_matrix(
    app: FastAPI, path: str, operation: str, status: str, receipt: str
) -> None:
    schema = app.openapi()
    post = schema["paths"][path]["post"]
    assert post["operationId"] == operation
    assert {(p["name"], p["required"]) for p in post["parameters"] if p["in"] == "header"} == {
        ("Idempotency-Key", True),
        ("Finance-Command-Version", True),
        ("Finance-Submission-Owner", True),
    }
    assert post["responses"][status]["content"]["application/json"]["schema"] == {
        "$ref": f"#/components/schemas/{receipt}"
    }
    assert {"400", "403", "404", "409", "422", "500", "503"} <= post["responses"].keys()
    for code, response in post["responses"].items():
        if not code.startswith("2"):
            assert set(response["content"]) == {"application/problem+json"}
    terminal = schema["components"]["schemas"]["SubmissionReceipt"]
    assert set(terminal["discriminator"]["mapping"]) == {
        "createFinanceLedger",
        "createFinanceAccount",
        "createFinanceCategory",
        "createFinanceTransaction",
        "createBalanceAdjustment",
    }
    components = schema["components"]["schemas"]
    assert set(
        components["AdjustmentSubmissionReceipt"]["properties"]["outcome"]["discriminator"][
            "mapping"
        ]
    ) == {"created", "noChange", "rejected"}
    assert "submissionReceipt" in components["AdjustmentConflictTerminalProblem"]["required"]
    assert "commandValidationRejection" in components["AdjustmentValidationProblem"]["required"]


@pytest.mark.parametrize(
    "outcome",
    [
        {"kind": "noChange", "resource": {"type": "transaction", "id": str(uuid4())}},
        {"kind": "noChange", "problem": {}},
        {"kind": "created", "resource": {"type": "account", "id": str(uuid4())}},
        {"kind": "rejected", "problem": {}},
    ],
)
def test_adjustment_success_and_lookup_refuse_impossible_outcomes(
    outcome: dict[str, object],
) -> None:
    now = datetime.now(UTC)
    receipt = {
        "submissionId": uuid4(),
        "commandVersion": "1",
        "operation": "createBalanceAdjustment",
        "targetLedgerId": uuid4(),
        "admittedAt": now,
        "resolvedAt": now,
        "outcome": outcome,
    }
    with pytest.raises(ValidationError):
        AdjustmentSuccessReceipt.model_validate(receipt)
    with pytest.raises(ValidationError):
        FinanceSubmissionResponse.model_validate({"state": "terminal", "receipt": receipt})
