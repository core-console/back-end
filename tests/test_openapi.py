"""Backend-owned API contract tests."""

from typing import Any

import pytest
from fastapi import FastAPI

from core_console.openapi import CoreConsoleApp
from core_console.problems import ProblemDetails


def test_openapi_preserves_frontend_hello_contract(app: FastAPI) -> None:
    schema = app.openapi()
    operation = schema["paths"]["/helloWorld"]["get"]
    hello_schema = schema["components"]["schemas"]["HelloWorldResponse"]
    problem_schema = schema["components"]["schemas"]["ProblemDetails"]

    assert schema["openapi"] == "3.1.1"
    assert schema["servers"] == [
        {
            "url": "/api",
            "description": "Same-origin API gateway",
        }
    ]
    assert "/api/helloWorld" not in schema["paths"]
    assert "/health/live" not in schema["paths"]
    assert operation["operationId"] == "getHelloWorld"
    assert operation["security"] == []
    assert set(operation["responses"]) == {"200", "500"}
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/HelloWorldResponse"
    )
    assert set(operation["responses"]["500"]["content"]) == {"application/problem+json"}
    assert hello_schema["additionalProperties"] is False
    assert hello_schema["required"] == ["message"]
    assert hello_schema["properties"]["message"]["minLength"] == 1
    assert hello_schema["properties"]["message"]["examples"] == ["Hello, world!"]
    assert problem_schema["additionalProperties"] is True
    assert set(problem_schema["required"]) == {"type", "title", "status"}

    operation_ids = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_openapi_describes_current_user_contract(app: FastAPI) -> None:
    schema = app.openapi()
    operation = schema["paths"]["/me"]["get"]
    me_schema = schema["components"]["schemas"]["MeResponse"]

    assert operation["operationId"] == "getCurrentUser"
    assert operation["security"] == []
    assert set(operation["responses"]) == {"200", "403", "500", "503"}
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/MeResponse"
    )
    for status in ("403", "500", "503"):
        content = operation["responses"][status]["content"]
        assert set(content) == {"application/problem+json"}
        assert (
            content["application/problem+json"]["schema"]["$ref"]
            == "#/components/schemas/ProblemDetails"
        )

    assert me_schema["additionalProperties"] is False
    assert set(me_schema["required"]) == {"id", "username", "displayName", "email"}
    assert set(me_schema["properties"]) == {"id", "username", "displayName", "email"}
    assert me_schema["properties"]["id"]["format"] == "uuid"
    for property_name in ("username", "displayName", "email"):
        assert {"type": "null"} in me_schema["properties"][property_name]["anyOf"]


def test_openapi_describes_users_v1_management_contract(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    operations = (
        paths["/users"]["get"],
        paths["/users"]["post"],
        paths["/users/{userId}"]["patch"],
        paths["/users/{userId}/deactivate"]["post"],
        paths["/users/{userId}/reactivate"]["post"],
    )

    assert paths["/users"]["get"]["operationId"] == "listUsers"
    assert paths["/users"]["post"]["operationId"] == "createUser"
    assert paths["/users/{userId}"]["patch"]["operationId"] == "updateUser"
    assert paths["/users/{userId}/deactivate"]["post"]["operationId"] == "deactivateUser"
    assert paths["/users/{userId}/reactivate"]["post"]["operationId"] == "reactivateUser"
    assert set(paths["/users"]) == {"get", "post"}
    assert set(paths["/users/{userId}"]) == {"patch"}
    assert "parameters" not in paths["/users"]["get"]

    user_schema = schema["components"]["schemas"]["UserResponse"]
    assert user_schema["additionalProperties"] is False
    assert set(user_schema["required"]) == {
        "id",
        "displayName",
        "username",
        "email",
        "identityIssuer",
        "identitySubject",
        "status",
    }
    assert set(user_schema["properties"]) == set(user_schema["required"])
    assert user_schema["properties"]["status"]["enum"] == ["active", "inactive"]

    create_schema = schema["components"]["schemas"]["CreateUserRequest"]
    assert set(create_schema["properties"]) == {
        "displayName",
        "username",
        "email",
        "identityIssuer",
        "identitySubject",
    }
    assert set(create_schema["required"]) == set(create_schema["properties"])

    update_schema = schema["components"]["schemas"]["UpdateUserRequest"]
    assert set(update_schema["properties"]) == {"displayName", "username", "email"}
    assert "required" not in update_schema

    for operation in operations:
        for status, response in operation["responses"].items():
            if status.startswith("2"):
                continue
            assert set(response["content"]) == {"application/problem+json"}
            assert (
                response["content"]["application/problem+json"]["schema"]["$ref"]
                == "#/components/schemas/ProblemDetails"
            )


def test_openapi_describes_finance_currency_and_ledger_contract(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    operations = (
        paths["/finance/currencies"]["get"],
        paths["/finance/ledgers"]["get"],
        paths["/finance/ledgers"]["post"],
        paths["/finance/ledgers/{ledgerId}"]["patch"],
    )

    assert paths["/finance/currencies"]["get"]["operationId"] == "listFinanceCurrencies"
    assert paths["/finance/ledgers"]["get"]["operationId"] == "listFinanceLedgers"
    assert paths["/finance/ledgers"]["post"]["operationId"] == "createFinanceLedger"
    assert paths["/finance/ledgers/{ledgerId}"]["patch"]["operationId"] == "updateFinanceLedger"
    assert set(paths["/finance/currencies"]) == {"get"}
    assert set(paths["/finance/ledgers"]) == {"get", "post"}
    assert set(paths["/finance/ledgers/{ledgerId}"]) == {"patch"}

    currency_schema = schema["components"]["schemas"]["CurrencyResponse"]
    assert currency_schema["additionalProperties"] is False
    assert set(currency_schema["required"]) == {"code", "minorUnit"}
    assert set(currency_schema["properties"]) == {"code", "minorUnit"}

    ledger_schema = schema["components"]["schemas"]["LedgerResponse"]
    assert ledger_schema["additionalProperties"] is False
    assert set(ledger_schema["required"]) == {"id", "name"}
    assert set(ledger_schema["properties"]) == {"id", "name"}

    create_schema = schema["components"]["schemas"]["CreateLedgerRequest"]
    update_schema = schema["components"]["schemas"]["UpdateLedgerRequest"]
    assert create_schema["additionalProperties"] is False
    assert update_schema["additionalProperties"] is False
    assert set(create_schema["properties"]) == {"name"}
    assert create_schema["required"] == ["name"]
    assert set(update_schema["properties"]) == {"name"}
    assert "required" not in update_schema

    for operation in operations:
        assert operation["security"] == []
        for status, response in operation["responses"].items():
            if status.startswith("2"):
                continue
            assert set(response["content"]) == {"application/problem+json"}
            assert (
                response["content"]["application/problem+json"]["schema"]["$ref"]
                == "#/components/schemas/ProblemDetails"
            )


def test_openapi_describes_finance_account_lifecycle_contract(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    collection_path = paths["/finance/ledgers/{ledgerId}/accounts"]
    account_path = paths["/finance/ledgers/{ledgerId}/accounts/{accountId}"]
    archive_path = paths["/finance/ledgers/{ledgerId}/accounts/{accountId}/archive"]
    unarchive_path = paths["/finance/ledgers/{ledgerId}/accounts/{accountId}/unarchive"]
    operations = (
        collection_path["get"],
        collection_path["post"],
        account_path["patch"],
        archive_path["post"],
        unarchive_path["post"],
    )

    assert collection_path["get"]["operationId"] == "listFinanceAccounts"
    assert collection_path["post"]["operationId"] == "createFinanceAccount"
    assert account_path["patch"]["operationId"] == "updateFinanceAccount"
    assert archive_path["post"]["operationId"] == "archiveFinanceAccount"
    assert unarchive_path["post"]["operationId"] == "unarchiveFinanceAccount"
    assert set(collection_path) == {"get", "post"}
    assert set(account_path) == {"patch"}
    assert set(archive_path) == {"post"}
    assert set(unarchive_path) == {"post"}

    account_schema = schema["components"]["schemas"]["AccountResponse"]
    assert account_schema["additionalProperties"] is False
    assert set(account_schema["properties"]) == {
        "id",
        "name",
        "nature",
        "currency",
        "openingBalance",
        "trackingStartDate",
        "currentBalance",
        "status",
    }
    assert set(account_schema["required"]) == set(account_schema["properties"])
    assert account_schema["properties"]["nature"]["enum"] == ["asset", "liability"]
    assert account_schema["properties"]["status"]["enum"] == ["active", "archived"]

    money_request_schema = schema["components"]["schemas"]["MoneyRequest"]
    money_response_schema = schema["components"]["schemas"]["MoneyResponse"]
    currency_code_schema = schema["components"]["schemas"]["CurrencyCode"]
    assert currency_code_schema["enum"] == ["CNY", "JPY", "USD"]
    assert currency_code_schema["type"] == "string"
    for money_schema in (money_request_schema, money_response_schema):
        assert money_schema["additionalProperties"] is False
        assert set(money_schema["properties"]) == {"amount", "currency"}
        assert set(money_schema["required"]) == {"amount", "currency"}
        assert money_schema["properties"]["amount"]["type"] == "string"
        assert money_schema["properties"]["currency"]["$ref"] == (
            "#/components/schemas/CurrencyCode"
        )

    create_schema = schema["components"]["schemas"]["CreateAccountRequest"]
    assert set(create_schema["properties"]) == {
        "name",
        "nature",
        "currency",
        "openingBalance",
        "trackingStartDate",
    }
    assert set(create_schema["required"]) == set(create_schema["properties"])

    update_schema = schema["components"]["schemas"]["UpdateAccountRequest"]
    assert set(update_schema["properties"]) == {
        "name",
        "openingBalance",
        "trackingStartDate",
    }
    assert "required" not in update_schema

    patch_operation = account_path["patch"]
    assert patch_operation["requestBody"]["required"] is True
    assert patch_operation["requestBody"]["content"]["application/json"]["schema"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/UpdateAccountRequest"},
            {"$ref": "#/components/schemas/CorrectAccountSemanticsRequest"},
        ],
        "title": "Request",
    }
    correction_schema = schema["components"]["schemas"]["CorrectAccountSemanticsRequest"]
    assert correction_schema["additionalProperties"] is False
    assert set(correction_schema["properties"]) == {"nature", "currency"}
    assert "required" not in correction_schema
    assert correction_schema["anyOf"] == [
        {"required": ["nature"]},
        {"required": ["currency"]},
    ]
    assert correction_schema["properties"]["nature"]["enum"] == ["asset", "liability"]
    assert correction_schema["properties"]["currency"] == {
        "$ref": "#/components/schemas/CurrencyCode"
    }
    assert "409" in patch_operation["responses"]

    for operation in operations:
        assert operation["security"] == []
        assert {parameter["name"] for parameter in operation["parameters"]} <= {
            "ledgerId",
            "accountId",
        }
        for status, response in operation["responses"].items():
            if status.startswith("2"):
                continue
            assert set(response["content"]) == {"application/problem+json"}
            assert (
                response["content"]["application/problem+json"]["schema"]["$ref"]
                == "#/components/schemas/ProblemDetails"
            )


def test_openapi_describes_finance_category_lifecycle_contract(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    collection_path = paths["/finance/ledgers/{ledgerId}/categories"]
    category_path = paths["/finance/ledgers/{ledgerId}/categories/{categoryId}"]
    archive_path = paths["/finance/ledgers/{ledgerId}/categories/{categoryId}/archive"]
    unarchive_path = paths["/finance/ledgers/{ledgerId}/categories/{categoryId}/unarchive"]
    operations = (
        collection_path["get"],
        collection_path["post"],
        category_path["patch"],
        archive_path["post"],
        unarchive_path["post"],
    )

    assert collection_path["get"]["operationId"] == "listFinanceCategories"
    assert collection_path["post"]["operationId"] == "createFinanceCategory"
    assert category_path["patch"]["operationId"] == "updateFinanceCategory"
    assert archive_path["post"]["operationId"] == "archiveFinanceCategory"
    assert unarchive_path["post"]["operationId"] == "unarchiveFinanceCategory"
    assert set(collection_path) == {"get", "post"}
    assert set(category_path) == {"patch"}
    assert set(archive_path) == {"post"}
    assert set(unarchive_path) == {"post"}

    category_schema = schema["components"]["schemas"]["CategoryResponse"]
    assert category_schema["additionalProperties"] is False
    assert set(category_schema["properties"]) == {"id", "name", "status"}
    assert set(category_schema["required"]) == set(category_schema["properties"])
    assert category_schema["properties"]["status"]["enum"] == ["active", "archived"]

    create_schema = schema["components"]["schemas"]["CreateCategoryRequest"]
    update_schema = schema["components"]["schemas"]["UpdateCategoryRequest"]
    assert create_schema["additionalProperties"] is False
    assert update_schema["additionalProperties"] is False
    assert set(create_schema["properties"]) == {"name"}
    assert create_schema["required"] == ["name"]
    assert set(update_schema["properties"]) == {"name"}
    assert "required" not in update_schema

    for operation in operations:
        assert operation["security"] == []
        assert {parameter["name"] for parameter in operation["parameters"]} <= {
            "ledgerId",
            "categoryId",
        }
        for status, response in operation["responses"].items():
            if status.startswith("2"):
                continue
            assert set(response["content"]) == {"application/problem+json"}
            assert (
                response["content"]["application/problem+json"]["schema"]["$ref"]
                == "#/components/schemas/ProblemDetails"
            )


def test_openapi_describes_transaction_create_and_detail_contract(
    app: FastAPI,
) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    collection_path = paths["/finance/ledgers/{ledgerId}/transactions"]
    detail_path = paths["/finance/ledgers/{ledgerId}/transactions/{transactionId}"]
    create_operation = collection_path["post"]
    detail_operation = detail_path["get"]

    assert set(collection_path) == {"post"}
    assert set(detail_path) == {"get"}
    assert create_operation["operationId"] == "createFinanceTransaction"
    assert detail_operation["operationId"] == "getFinanceTransaction"
    assert create_operation["security"] == []
    assert detail_operation["security"] == []

    request_union = create_operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_union["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "income": "#/components/schemas/CreateIncomeTransactionRequest",
            "expense": "#/components/schemas/CreateExpenseTransactionRequest",
            "internalTransfer": ("#/components/schemas/CreateInternalTransferTransactionRequest"),
        },
    }
    assert request_union["oneOf"] == [
        {"$ref": "#/components/schemas/CreateIncomeTransactionRequest"},
        {"$ref": "#/components/schemas/CreateExpenseTransactionRequest"},
        {"$ref": "#/components/schemas/CreateInternalTransferTransactionRequest"},
    ]

    transaction_union = schema["components"]["schemas"]["FinanceTransactionResponse"]
    assert transaction_union["discriminator"]["propertyName"] == "kind"
    assert set(transaction_union["discriminator"]["mapping"]) == {
        "income",
        "expense",
        "internalTransfer",
    }
    assert transaction_union["oneOf"] == [
        {"$ref": "#/components/schemas/IncomeTransactionResponse"},
        {"$ref": "#/components/schemas/ExpenseTransactionResponse"},
        {"$ref": "#/components/schemas/InternalTransferTransactionResponse"},
    ]

    expected_transaction_fields = {
        "id",
        "ledgerId",
        "kind",
        "transactionDate",
        "note",
        "account",
        "economicAmount",
        "categoryAllocations",
    }
    expected_request_fields = {
        "kind",
        "accountId",
        "transactionDate",
        "economicAmount",
        "categoryAllocations",
        "note",
    }
    for kind in ("Income", "Expense"):
        request_schema = schema["components"]["schemas"][f"Create{kind}TransactionRequest"]
        response_schema = schema["components"]["schemas"][f"{kind}TransactionResponse"]
        assert request_schema["additionalProperties"] is False
        assert response_schema["additionalProperties"] is False
        assert set(request_schema["properties"]) == expected_request_fields
        assert set(response_schema["properties"]) == expected_transaction_fields
        allocation_array = request_schema["properties"]["categoryAllocations"]
        assert allocation_array["minItems"] == 1
        assert allocation_array["maxItems"] == 1
        response_allocations = response_schema["properties"]["categoryAllocations"]
        assert response_allocations["minItems"] == 1
        assert response_allocations["maxItems"] == 1
        assert request_schema["properties"]["note"]["maxLength"] == 500
        assert response_schema["properties"]["note"]["anyOf"][0]["maxLength"] == 500

    transfer_request = schema["components"]["schemas"]["CreateInternalTransferTransactionRequest"]
    transfer_response = schema["components"]["schemas"]["InternalTransferTransactionResponse"]
    assert transfer_request["additionalProperties"] is False
    assert set(transfer_request["properties"]) == {
        "kind",
        "sourceAccountId",
        "destinationAccountId",
        "amount",
        "transactionDate",
        "note",
    }
    assert transfer_response["additionalProperties"] is False
    assert set(transfer_response["properties"]) == {
        "id",
        "ledgerId",
        "kind",
        "transactionDate",
        "note",
        "sourceAccount",
        "sourceAmount",
        "destinationAccount",
        "destinationAmount",
    }

    for operation in (create_operation, detail_operation):
        for status, response in operation["responses"].items():
            if status.startswith("2"):
                assert response["content"]["application/json"]["schema"] == {
                    "$ref": "#/components/schemas/FinanceTransactionResponse"
                }
                continue
            assert set(response["content"]) == {"application/problem+json"}
            assert (
                response["content"]["application/problem+json"]["schema"]["$ref"]
                == "#/components/schemas/ProblemDetails"
            )


def test_openapi_converts_problem_details_for_any_operation(app: FastAPI) -> None:
    @app.get(
        "/api/__test_problem",
        include_in_schema=True,
        responses={
            418: {
                "model": ProblemDetails,
                "description": "A test problem occurred.",
            }
        },
    )
    async def problem_for_test() -> None:
        pass

    schema = app.openapi()
    content = schema["paths"]["/__test_problem"]["get"]["responses"]["418"]["content"]

    assert set(content) == {"application/problem+json"}
    assert (
        content["application/problem+json"]["schema"]["$ref"]
        == "#/components/schemas/ProblemDetails"
    )


def test_openapi_normalization_ignores_path_item_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def problem_response() -> dict[str, Any]:
        return {
            "description": "A problem occurred.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetails"},
                }
            },
        }

    def generated_schema(**_: object) -> dict[str, Any]:
        return {
            "paths": {
                "/api/test": {
                    "get": {
                        "operationId": "getTest",
                        "responses": {"503": problem_response()},
                    },
                    "parameters": [{"name": "request-id", "in": "header"}],
                    "summary": "Path summary",
                    "description": "Path description",
                    "servers": [{"url": "https://example.test"}],
                    "x-metadata": {
                        "responses": {"503": problem_response()},
                    },
                }
            }
        }

    monkeypatch.setattr("core_console.openapi.get_openapi", generated_schema)
    schema = CoreConsoleApp().openapi()
    path_item = schema["paths"]["/test"]

    operation_content = path_item["get"]["responses"]["503"]["content"]
    extension_content = path_item["x-metadata"]["responses"]["503"]["content"]
    assert set(operation_content) == {"application/problem+json"}
    assert set(extension_content) == {"application/json"}
    assert path_item["parameters"] == [{"name": "request-id", "in": "header"}]
    assert path_item["summary"] == "Path summary"
    assert path_item["description"] == "Path description"
    assert path_item["servers"] == [{"url": "https://example.test"}]
