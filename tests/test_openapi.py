"""Backend-owned API contract tests."""

from fastapi import FastAPI


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
