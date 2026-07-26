"""Request identifier validation and asynchronous context propagation."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_STATE_ATTRIBUTE = "request_id"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z", flags=re.ASCII)
_current_request_id: ContextVar[str | None] = ContextVar(
    "core_console_request_id",
    default=None,
)


def select_request_id(candidate: str | None) -> str:
    """Use a safe inbound identifier or generate a new opaque identifier."""

    if candidate is not None and _VALID_REQUEST_ID.fullmatch(candidate) is not None:
        return candidate
    return uuid4().hex


def get_request_id() -> str | None:
    """Return the identifier bound to the current request, if one exists."""

    return _current_request_id.get()


@contextmanager
def request_id_context(request_id: str) -> Iterator[None]:
    """Bind one request identifier and reliably restore the previous context."""

    token = _current_request_id.set(request_id)
    try:
        yield
    finally:
        _current_request_id.reset(token)
