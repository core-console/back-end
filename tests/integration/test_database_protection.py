"""Pure tests for the protected PostgreSQL test-target guard."""

import socket

import pytest

from .conftest import (
    _canonical_address,
    _database_target,
    _database_targets_overlap,
    _validated_test_database_url,
)


def database_url(host: str, *, port: int = 5432, database: str = "core_console_test") -> str:
    """Build a URL without opening a connection."""

    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"postgresql+psycopg://user:password@{url_host}:{port}/{database}"


def install_address_map(
    monkeypatch: pytest.MonkeyPatch,
    address_map: dict[str, list[str]],
) -> None:
    """Make DNS resolution deterministic without touching the network."""

    def fake_getaddrinfo(
        host: str,
        *_args: object,
        **_kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 0),
            )
            for address in address_map[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_multi_address_dns_targets_overlap_when_any_address_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A possible shared address rejects otherwise non-equal DNS sets."""

    install_address_map(
        monkeypatch,
        {
            "first.example": ["192.0.2.10", "192.0.2.11"],
            "second.example": ["192.0.2.11", "192.0.2.12"],
        },
    )

    first = _database_target(database_url("first.example"))
    second = _database_target(database_url("second.example"))

    assert _database_targets_overlap(first, second)


def test_dns_and_direct_ip_aliases_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DNS name and its direct IP target are treated as possibly shared."""

    install_address_map(
        monkeypatch,
        {"database.example": ["192.0.2.20"], "192.0.2.20": ["192.0.2.20"]},
    )

    dns_target = _database_target(database_url("database.example"))
    ip_target = _database_target(database_url("192.0.2.20"))

    assert _database_targets_overlap(dns_target, ip_target)


def test_full_validator_rejects_overlapping_dns_development_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complete validator rejects partially overlapping DNS targets."""

    clear_database_environment(monkeypatch)
    install_address_map(
        monkeypatch,
        {
            "test.example": ["192.0.2.30", "192.0.2.31"],
            "development.example": ["192.0.2.31", "192.0.2.32"],
        },
    )
    monkeypatch.setenv("TEST_DATABASE_URL", database_url("test.example"))
    monkeypatch.setenv("DATABASE_URL", database_url("development.example"))

    with pytest.raises(pytest.fail.Exception, match="same PostgreSQL target"):
        _validated_test_database_url()


def test_full_validator_rejects_dns_test_against_direct_ip_development_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complete validator rejects a DNS test target and direct-IP alias."""

    clear_database_environment(monkeypatch)
    install_address_map(
        monkeypatch,
        {
            "test.example": ["192.0.2.40", "192.0.2.41"],
            "192.0.2.40": ["192.0.2.40"],
        },
    )
    monkeypatch.setenv("TEST_DATABASE_URL", database_url("test.example"))
    monkeypatch.setenv("DATABASE_URL", database_url("192.0.2.40"))

    with pytest.raises(pytest.fail.Exception, match="same PostgreSQL target"):
        _validated_test_database_url()


def test_ipv4_mapped_ipv6_is_normalized_to_ipv4() -> None:
    """Mapped IPv6 output cannot evade an IPv4 target comparison."""

    assert _canonical_address("::ffff:192.0.2.20") == "192.0.2.20"


@pytest.mark.parametrize("host", ("localhost", "127.0.0.1", "::1"))
def test_loopback_hosts_share_the_same_safe_target(host: str) -> None:
    """Named and literal loopback hosts remain equivalent."""

    assert _database_targets_overlap(
        _database_target(database_url("localhost")),
        _database_target(database_url(host)),
    )


def test_dns_alias_to_loopback_shares_the_safe_local_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DNS alias resolving to loopback cannot bypass local protection."""

    install_address_map(monkeypatch, {"local.example": ["127.0.0.1"]})

    assert _database_targets_overlap(
        _database_target(database_url("local.example")),
        _database_target(database_url("localhost")),
    )


def clear_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove database-related environment values for guard tests."""

    for variable in ("TEST_DATABASE_URL", "DATABASE_URL", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(variable, raising=False)


def test_missing_test_database_skips_outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer without a test database gets an explicit local skip."""

    clear_database_environment(monkeypatch)

    with pytest.raises(pytest.skip.Exception, match="TEST_DATABASE_URL"):
        _validated_test_database_url()


@pytest.mark.parametrize("ci_variable", ("CI", "GITHUB_ACTIONS"))
def test_missing_test_database_fails_closed_in_ci(
    monkeypatch: pytest.MonkeyPatch,
    ci_variable: str,
) -> None:
    """CI cannot silently omit the database required for integration coverage."""

    clear_database_environment(monkeypatch)
    monkeypatch.setenv(ci_variable, "true")

    with pytest.raises(pytest.fail.Exception, match="required in CI"):
        _validated_test_database_url()
