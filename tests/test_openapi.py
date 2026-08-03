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
