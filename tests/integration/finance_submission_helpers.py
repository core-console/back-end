"""Explicit protocol headers for Ledger-dependent integration setup."""

from uuid import UUID, uuid4


def submission_headers(owner_id: UUID, key: UUID | None = None) -> dict[str, str]:
    return {
        "Idempotency-Key": str(key or uuid4()),
        "Finance-Command-Version": "1",
        "Finance-Submission-Owner": str(owner_id),
    }
