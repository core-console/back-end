"""Deterministic frontend business API contract generation."""

from typing import Any, cast

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from core_console import __version__
from core_console.problems import PROBLEM_MEDIA_TYPE

API_PREFIX = "/api"

OPENAPI_TAGS = [
    {
        "name": "Hello",
        "description": "Initial API connectivity endpoints",
    }
]


class CoreConsoleApp(FastAPI):
    """FastAPI application whose schema preserves the frontend server prefix."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is not None:
            return self.openapi_schema

        schema = get_openapi(
            title="Core Console API",
            version=__version__,
            description=(
                "Initial backend-owned API definition for Core Console. "
                "Operational endpoints are intentionally excluded."
            ),
            openapi_version="3.1.1",
            routes=self.routes,
            tags=OPENAPI_TAGS,
        )

        paths = cast(dict[str, Any], schema["paths"])
        contract_paths: dict[str, Any] = {}
        for path, operations in paths.items():
            if not path.startswith(f"{API_PREFIX}/"):
                continue
            contract_paths[path.removeprefix(API_PREFIX)] = operations
        schema["paths"] = contract_paths
        schema["jsonSchemaDialect"] = "https://json-schema.org/draft/2020-12/schema"
        schema["servers"] = [
            {
                "url": API_PREFIX,
                "description": "Same-origin API gateway",
            }
        ]

        hello_responses = contract_paths["/helloWorld"]["get"]["responses"]
        server_error = hello_responses["500"]
        json_schema = server_error["content"].pop("application/json")
        problem_content = server_error["content"].setdefault(PROBLEM_MEDIA_TYPE, {})
        problem_content["schema"] = json_schema["schema"]

        self.openapi_schema = schema
        return schema
