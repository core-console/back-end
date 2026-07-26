"""HTTP request context, response headers, timing, and completion logging."""

import logging
from time import perf_counter_ns
from typing import Final

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core_console.request_context import (
    REQUEST_ID_HEADER,
    REQUEST_ID_STATE_ATTRIBUTE,
    get_request_id,
    request_id_context,
    select_request_id,
)

logger = logging.getLogger(__name__)
_UNMATCHED_ROUTE: Final = "unmatched"
_SUCCESSFUL_HEALTH_ROUTES = frozenset({"/health/live", "/health/ready"})


class HttpRequestLoggingMiddleware:
    """Add request correlation and emit one completion summary per HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = select_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})[REQUEST_ID_STATE_ATTRIBUTE] = request_id
        status_code: int | None = None
        started_at = perf_counter_ns()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with request_id_context(request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                self._log_completion(
                    scope=scope,
                    status_code=status_code or 500,
                    started_at=started_at,
                )
                raise
            else:
                self._log_completion(
                    scope=scope,
                    status_code=status_code or 500,
                    started_at=started_at,
                )

    @staticmethod
    def _log_completion(*, scope: Scope, status_code: int, started_at: int) -> None:
        route_object = scope.get("route")
        route = getattr(route_object, "path", _UNMATCHED_ROUTE)
        if not isinstance(route, str):
            route = _UNMATCHED_ROUTE

        if status_code < 400 and route in _SUCCESSFUL_HEALTH_ROUTES:
            return

        level = logging.ERROR if status_code >= 500 else logging.INFO
        duration_ms = max(0.0, (perf_counter_ns() - started_at) / 1_000_000)
        logger.log(
            level,
            "HTTP request completed",
            extra={
                "event": "http.request.completed",
                "request_id": get_request_id(),
                "method": scope["method"],
                "route": route,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 3),
            },
        )
