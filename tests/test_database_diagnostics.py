"""End-to-end credential redaction with synthetic failure output."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus
from xml.etree import ElementTree

import pytest
from scripts.agent_harness import ValidationCheck, validate_repository
from scripts.validation_evidence import write_new

from test_agent_harness import initialized_repository

ROOT = Path(__file__).resolve().parents[1]
USERS = ("probe_test_user", "probe_dev_user", "probe_file_user")
MARKER_PASSWORD_PARTS = ("probe-marker-prefix", "probe-marker-suffix")
URL_PASSWORD_PARTS = ("probe-url-prefix", "probe-url-suffix")
RAW_URL_PASSWORD_PARTS = ("52739017", "probe-raw-url-suffix")
RAW_URL_USER = "probe_raw_url_user"
MIXED_URL_PASSWORD_PARTS = ("64233", "probe-su%66fix", "probe-suffix")
MIXED_URL_USER = "synthetic_user"
MIXED_URL = (
    f"postgresql+psycopg://{MIXED_URL_USER}:{MIXED_URL_PASSWORD_PARTS[0]}"
    f"?{MIXED_URL_PASSWORD_PARTS[1]}@localhost:5432/guarded_db"
)
REPR_USER = 'probe"username'
REPR_PASSWORD_PARTS = ("probe-singlequote-prefix", "probe-singlequote-suffix")
REPR_PASSWORD = "'".join(REPR_PASSWORD_PARTS)
REPR_URL = (
    f"postgresql+psycopg://{quote(REPR_USER, safe='')}:{quote(REPR_PASSWORD, safe='')}"
    "@localhost:5432/repr_guarded_db"
)
ARG_PASSWORD_PARTS = ("probe-argument-prefix", "probe-argument-suffix")
ARG_PASSWORD = "'\\\" é\\extra ".join(ARG_PASSWORD_PARTS)
OVERLAP_PREFIX = "probe-overlap-prefix"
PASSWORDS = (
    "<redacted>?".join(MARKER_PASSWORD_PARTS),
    "?/#@ :\"'".join(URL_PASSWORD_PARTS),
    "probe-file@:/secret",
)
OVERLAPPING_PASSWORD = OVERLAP_PREFIX + MARKER_PASSWORD_PARTS[0] + "<redacted>"
TOKEN_PREFIX = "probe-db-token"
TOKEN = TOKEN_PREFIX + " @:/secret"
ENCODED_TOKEN = quote_plus(TOKEN).replace("secret", "se%63ret")
QUERY_USER = "probe_query_role"
LIBPQ_USER = "probe_libpq_role"
OVERLAP = "probe_overlapping_secret"
PAYLOAD = """
import json
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

from dotenv import dotenv_values
from sqlalchemy import make_url

def diagnose():
    dotenv = dotenv_values(".env")
    urls = [os.environ["TEST_DATABASE_URL"], os.environ["DATABASE_URL"],
            dotenv["DATABASE_URL"]]
    for url in urls:
        parsed = urlsplit(url)
        print("stdout-stage", url, unquote(parsed.username), unquote(parsed.password))
        print("stderr-stage", parsed.password, parsed.query, file=sys.stderr)
        print("decoded-url-stage", unquote(url), "encoded-url", url)
        print("repr-url-stage", repr(unquote(url)), file=sys.stderr)
        print("nested-repr-stage", json.dumps({"diagnostic": repr(unquote(url))}), file=sys.stderr)
        print("rendered-url-stage", make_url(url).render_as_string(hide_password=False))
        for user in (unquote(parsed.username), parse_qs(parsed.query)["user"][0]):
            print(f"role-stage pool_{user}_checkout", file=sys.stderr)
    password = unquote(urlsplit(urls[0]).password)
    overlap = os.environ["PGPASSWORD"] + password.split("<redacted>", 1)[1]
    print("overlap-stage", overlap, "role-stage pool_" + os.environ["PGUSER"] + "_checkout")
    print("role-stage pool_" + os.environ["PGUSER"] + "_checkout", file=sys.stderr)
    raw_url = dotenv["TEST_DATABASE_URL"]
    encoded_url = make_url(raw_url).render_as_string(hide_password=False)
    print("raw-url-stage", raw_url, "encoded-url", encoded_url)
    return urls[0]

def fail():
    try:
        raise ValueError("SQLSTATE 28P01 stage=connect " + diagnose())
    except ValueError as cause:
        raise RuntimeError("guarded_db checkout") from cause
"""
TEST_SOURCE = (
    PAYLOAD
    + """
import warnings
import pytest
from scripts.protected_pytest import OBSERVER

@pytest.fixture
def db_target():
    return os.environ["TEST_DATABASE_URL"]

def test_chained(db_target, pytestconfig, record_testsuite_property, record_xml_attribute):
    record_testsuite_property("target", db_target)
    record_testsuite_property("stage", "connect")
    record_xml_attribute("target", db_target)
    record_xml_attribute("sqlstate", "28P01")
    record_xml_attribute(make_url(db_target).username, "safe-metadata")
    warnings.warn("warning-stage " + db_target)
    observer = pytestconfig.stash.get(OBSERVER, None)
    if observer is not None:
        observer.postgres.append({"stage": "connected", "database": "probe_overlapping_secret"})
    fail()

def test_assertion(db_target):
    assert db_target == "safe expected route"

@pytest.mark.parametrize(
    "case", [None, None], ids=[
        urlsplit(os.environ["TEST_DATABASE_URL"]).password,
        urlsplit(os.environ["DATABASE_URL"]).password,
    ],
)
def test_report_id(case):
    assert case is not None
"""
)
REPORTER = """
import json
from pathlib import Path

def pytest_runtest_logreport(report):
    if report.failed:
        with Path("pytest-failures.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(report._to_json()) + "\\n")
"""


def diagnostic_environment(root: Path) -> dict[str, str]:
    def url(index: int) -> str:
        return (
            f"postgresql+psycopg://{USERS[index]}:{quote(PASSWORDS[index], safe='')}"
            f"@localhost:5432/guarded_db?user={QUERY_USER}&token={ENCODED_TOKEN}"
        )

    raw_password = "?".join(RAW_URL_PASSWORD_PARTS)
    raw_url = f"postgresql+psycopg://{RAW_URL_USER}:{raw_password}@localhost:5432/guarded_db"
    (root / ".env").write_text(
        f"DATABASE_URL={url(2)}\nTEST_DATABASE_URL={raw_url}\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTEST_")}
    return env | {
        "PGUSER": LIBPQ_USER,
        "PGPASSWORD": OVERLAPPING_PASSWORD,
        "PGSSLPASSWORD": OVERLAP,
        "TEST_DATABASE_URL": url(0),
        "DATABASE_URL": url(1),
        "PYTHONPATH": str(ROOT),
    }


def assert_no_credentials(output: str) -> None:
    for secret in (
        *USERS,
        *PASSWORDS,
        *MARKER_PASSWORD_PARTS,
        *URL_PASSWORD_PARTS,
        *RAW_URL_PASSWORD_PARTS,
        RAW_URL_USER,
        *MIXED_URL_PASSWORD_PARTS,
        MIXED_URL_USER,
        *REPR_PASSWORD_PARTS,
        REPR_USER,
        r"probe\"username",
        *ARG_PASSWORD_PARTS,
        OVERLAP_PREFIX,
        OVERLAPPING_PASSWORD,
        TOKEN_PREFIX,
        TOKEN,
        QUERY_USER,
        LIBPQ_USER,
        OVERLAP,
    ):
        for spelling in {secret, quote(secret, safe=""), quote(secret, safe="").lower()}:
            if spelling in output:
                pytest.fail("Synthetic database credential escaped the diagnostic boundary.")


def test_pytest_console_loads_diagnostics_without_pythonpath() -> None:
    executable = Path(sys.executable).with_name("pytest.exe" if os.name == "nt" else "pytest")
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() != "PYTHONPATH" and not key.upper().startswith("PYTEST_")
    }
    result = subprocess.run(
        (
            str(executable),
            "--disable-plugin-autoload",
            "-p",
            "anyio.pytest_plugin",
            "--trace-config",
            "--collect-only",
            "-q",
            "tests/test_health.py",
        ),
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "scripts.pytest_database_diagnostics" in result.stdout
    assert "test_liveness_does_not_require_database" in result.stdout


def test_pytest_nested_comparison_redacts_before_diffing(tmp_path: Path) -> None:
    suite = tmp_path / "tests"
    suite.mkdir()
    (suite / "conftest.py").write_text(
        (ROOT / "tests/conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (suite / "test_comparison.py").write_text(
        """\
import os
from dataclasses import dataclass, field

from sqlalchemy import make_url

@dataclass
class Credential:
    value: str

@dataclass
class HiddenCredential:
    value: str = field(repr=False)

def test_dataclass():
    actual = make_url(os.environ["DATABASE_URL"]).password
    expected = "shared" * 30 + "different safe value"
    assert Credential(actual) == Credential(expected), "stage=connect SQLSTATE 28P01 guarded_db"

def test_nested_containers():
    actual = make_url(os.environ["DATABASE_URL"]).password
    expected = "shared" * 30 + "different safe value"
    assert {"payload": [Credential(actual)]} == {"payload": [Credential(expected)]}, (
        "stage=connect SQLSTATE 28P01 guarded_db"
    )

def test_hidden_dataclass_field():
    actual = make_url(os.environ["DATABASE_URL"]).password
    expected = "shared" * 30 + "different safe value"
    assert HiddenCredential(actual) == HiddenCredential(expected), (
        "stage=connect SQLSTATE 28P01 guarded_db"
    )

def test_string():
    actual = make_url(os.environ["DATABASE_URL"]).password
    expected = "shared" * 30 + "different safe value"
    assert actual == expected, "stage=connect SQLSTATE 28P01 guarded_db"

def test_ordinary_comparison():
    assert {"color": "red"} == {"color": "blue"}, "ordinary comparison"
""",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
    suffix = "distinctive-synthetic-tail"
    password = "shared" * 30 + suffix
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PYTEST_", "PG"))
        and key.upper() not in {"DATABASE_URL", "TEST_DATABASE_URL"}
    }
    env.update(
        DATABASE_URL=f"postgresql+psycopg://db_user_probe:{password}@localhost:5432/guarded_db",
        PYTHONPATH=str(ROOT),
    )
    result = subprocess.run(
        (
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "--disable-plugin-autoload",
            "--junitxml=report.xml",
            "tests",
        ),
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    report = (tmp_path / "report.xml").read_text(encoding="utf-8")
    assert result.returncode == 1
    for output in (result.stdout + result.stderr, report):
        if password in output or suffix in output:
            pytest.fail("Synthetic password escaped a comparison into terminal or JUnit output.")
        assert "stage=connect SQLSTATE 28P01 guarded_db" in output

    cases = {
        case.get("name"): case.findtext("failure", default="")
        for case in ElementTree.fromstring(report).findall("testsuite/testcase")
    }
    for name in (
        "test_dataclass",
        "test_nested_containers",
        "test_hidden_dataclass_field",
        "test_string",
    ):
        assert "stage=connect SQLSTATE 28P01 guarded_db" in cases[name]
        if password in cases[name] or suffix in cases[name]:
            pytest.fail("Synthetic password escaped into a JUnit failure.")
    assert "<redacted>" in cases["test_dataclass"]
    assert "<redacted>" in cases["test_string"]
    assert "Differing items" in cases["test_ordinary_comparison"]


@pytest.mark.parametrize("protected", [False, True], ids=["terminal-junit", "protected-proof"])
def test_pytest_failure_output_excludes_database_credentials(
    tmp_path: Path, protected: bool
) -> None:
    suite = tmp_path / "tests"
    suite.mkdir()
    (suite / "conftest.py").write_text(
        (ROOT / "tests/conftest.py").read_text(encoding="utf-8") + REPORTER, encoding="utf-8"
    )
    (suite / "test_probe.py").write_text(TEST_SOURCE, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
    env = diagnostic_environment(tmp_path)
    command: tuple[str, ...]
    if protected:
        start = tmp_path / "started.json"
        write_new(start, {"executionId": "synthetic"})
        command = (
            "-c",
            "from pathlib import Path; from scripts.protected_pytest import main; "
            "raise SystemExit(main(Path('started.json')))",
        )
    else:
        command = (
            "-m",
            "pytest",
            "-v",
            "--tb=long",
            "--junitxml=pytest-report.xml",
            "--junitprefix",
            USERS[0],
            "-o",
            "junit_family=xunit1",
            "-o",
            f"junit_suite_name={USERS[0]}",
            "tests",
        )
    result = subprocess.run(
        (sys.executable, "-B", *command),
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    terminal = result.stdout + result.stderr
    assert_no_credentials(terminal)
    assert result.returncode == 1
    for detail in (
        "SQLSTATE 28P01",
        "RuntimeError",
        "ValueError",
        "guarded_db",
        "stdout-stage",
        "stderr-stage",
        "AssertionError",
        "warning-stage",
    ):
        assert detail in terminal
    report = tmp_path / ("pytest.json" if protected else "pytest-report.xml")
    failure_reports = (tmp_path / "pytest-failures.jsonl").read_text(encoding="utf-8")
    assert_no_credentials(failure_reports)
    assert "SQLSTATE 28P01" in failure_reports and "RuntimeError" in failure_reports
    report_text = report.read_text(encoding="utf-8")
    assert_no_credentials(report_text)
    if protected:
        reports = json.loads(report_text)["reports"]
        assert reports["tests/test_probe.py::test_chained"]["call"] == "failed"
        proof = json.loads(report_text)
        assert sum("test_report_id[<redacted>]" in node for node in reports) == 2
        assert len(set(proof["collected"])) == 4
        assert proof["postgresLifecycle"][0]["database"] == "<redacted>"
    else:
        assert "SQLSTATE 28P01" in report_text and "RuntimeError" in report_text
        suite_report = ElementTree.fromstring(report_text).find("testsuite")
        assert suite_report is not None
        route = "postgresql+psycopg://localhost:5432/guarded_db"
        properties = {
            entry.attrib["name"]: entry.attrib["value"]
            for entry in suite_report.findall("properties/property")
        }
        assert properties == {"target": route, "stage": "connect"}
        testcase = suite_report.find("testcase[@name='test_chained']")
        assert testcase is not None
        assert testcase.get("target") == route and testcase.get("sqlstate") == "28P01"
        assert testcase.get("redacted_attribute") == "safe-metadata"
        assert testcase.get("classname") == "<redacted>.tests.test_probe"
        assert suite_report.get("name") == "<redacted>"

    for path in (tmp_path / ".pytest_cache").rglob("*"):
        if path.is_file():
            assert_no_credentials(path.read_text(encoding="utf-8"))
    if not protected:
        collection = subprocess.run(
            (sys.executable, "-B", "-m", "pytest", "--collect-only", "-q", "tests"),
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert_no_credentials(collection.stdout + collection.stderr)
        assert collection.returncode == 0 and "4 tests collected" in collection.stdout


@pytest.mark.parametrize(
    "mode",
    [
        "report",
        "internal-error",
        "internal-full-trace",
        "exception-group",
        "internal-group",
        "group-cr",
        "group-line-separator",
        "assertion-diff",
        "showlocals",
        "verbose-locals",
        "warning",
    ],
)
def test_pytest_scrubs_cwd_settings_and_internal_errors(tmp_path: Path, mode: str) -> None:
    internal = mode.startswith("internal-")
    cwd = tmp_path / "nested"
    suite = tmp_path / "tests"
    cwd.mkdir()
    suite.mkdir()
    username = "probe_cwd_settings_user"
    fragments = ("probe-cwd-settings-prefix", "probe-cwd-settings-suffix")
    separator = {
        "showlocals": "x" * 280,
        "verbose-locals": " xx " * 120,
        "warning": "\u2028",
        "group-cr": "\r",
        "group-line-separator": "\u2028",
    }.get(mode, "\n")
    password = separator.join(fragments) + (" \t" if mode == "warning" else "")
    url = f"postgresql+psycopg://{username}:{quote(password)}@localhost:5432/guarded_cwd_db"
    (cwd / ".env").write_text(f"DATABASE_URL={url}\n", encoding="utf-8")
    config = tmp_path / "pyproject.toml"
    config.write_text("[tool.pytest]\n", encoding="utf-8")
    conftest = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    payload = """
import warnings
from pathlib import Path
from core_console.config import Settings
from sqlalchemy import make_url

def fail(config):
    assert config.rootpath != Path.cwd()
    settings = Settings(AUTH_MODE="development", DEV_IDENTITY_ISSUER="issuer",
                        DEV_IDENTITY_SUBJECT="subject")
    url = make_url(settings.database_url_value())
    password = url.password
    stage = f"stage=connect SQLSTATE 28P01 {url.database}"
"""
    if mode.endswith("group") or mode.startswith("group-"):
        payload += (
            '    error = ValueError(f"{stage} {url.username} {password}")\n'
            '    raise ExceptionGroup("db errors", [ExceptionGroup("inner", [error])])\n'
        )
    elif mode == "assertion-diff":
        payload += '    assert password == "safe expected password", stage\n'
    elif mode == "showlocals":
        payload += "    raise ValueError(stage)\n"
    elif mode == "verbose-locals":
        payload += '    details = {"credential": password}\n    raise ValueError(stage)\n'
    elif mode == "warning":
        payload += (
            '    warnings.warn_explicit("warning-stage " + password, UserWarning, '
            '"generated.py", 1)\n'
            "    raise ValueError(stage)\n"
        )
    else:
        payload += '    raise ValueError(f"{stage} {url.username} {password}")\n'
    if internal:
        conftest += payload + "\ndef pytest_collection_modifyitems(config):\n    fail(config)\n"
        test_module = "def test_probe(): pass\n"
    else:
        test_module = payload + "\ndef test_probe(pytestconfig):\n    fail(pytestconfig)\n"
    (suite / "conftest.py").write_text(conftest, encoding="utf-8")
    (suite / "test_probe.py").write_text(test_module, encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() != "DATABASE_URL" and not key.upper().startswith("PYTEST_")
    }
    options = {
        "internal-full-trace": ("--full-trace",),
        "showlocals": ("--showlocals",),
        "verbose-locals": ("--showlocals", "-vv"),
    }.get(mode, ())
    if mode.startswith("group-"):
        options = ("--color=yes", "--tb=native")
    result = subprocess.run(
        (
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-c",
            str(config),
            "--junitxml=report.xml",
            *options,
            str(suite),
        ),
        cwd=cwd,
        env=env | {"PYTHONPATH": str(ROOT)},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == (3 if internal else 1)
    report = (cwd / "report.xml").read_text(encoding="utf-8")
    for output in (result.stdout + result.stderr, report):
        if any(secret in output for secret in (username, *fragments)):
            pytest.fail("Synthetic dotenv credential escaped terminal or JUnit diagnostics.")
        error_type = "AssertionError" if mode == "assertion-diff" else "ValueError"
        assert error_type in output and "stage=connect SQLSTATE 28P01 guarded_cwd_db" in output
    if mode == "warning":
        assert "warning-stage <redacted>" in result.stdout
    assert ElementTree.fromstring(report).find("testsuite/testcase") is not None


def test_harness_failure_logs_and_receipt_exclude_database_credentials(tmp_path: Path) -> None:
    root, base = initialized_repository(tmp_path)
    # Environment is supplied explicitly to the Harness and is not inherited from this pytest run.
    env = diagnostic_environment(root)
    result = validate_repository(
        root,
        base,
        protected=False,
        environment=env,
        checks=(
            ValidationCheck("pytest", (sys.executable, "-c", PAYLOAD + "\nfail()", PASSWORDS[0])),
        ),
    )
    assert result.outcome == "FAIL"
    log = result.checks[0].log_path.read_text(encoding="utf-8")
    for path in (root / ".agent").rglob("*"):
        if path.is_file():
            assert_no_credentials(path.read_text(encoding="utf-8"))
    for detail in (
        "stdout-stage",
        "stderr-stage",
        "overlap-stage <redacted> role-stage pool_<redacted>_checkout",
        "role-stage pool_<redacted>_checkout",
        "SQLSTATE 28P01",
        "ValueError",
        "RuntimeError",
        "guarded_db",
    ):
        assert detail in log
    safe_route = "postgresql+psycopg://localhost:5432/guarded_db"
    assert log.count(f"decoded-url-stage {safe_route} encoded-url {safe_route}") == 3
    assert log.count(f"rendered-url-stage {safe_route}") == 3
    assert f"raw-url-stage {safe_route} encoded-url {safe_route}" in log


def test_harness_scrubs_query_even_when_credentials_match_url_delimiters(tmp_path: Path) -> None:
    root, base = initialized_repository(tmp_path)
    env = diagnostic_environment(root) | {"PGUSER": "/", "PGPASSWORD": "?"}
    query = "?token=probe@sample&key=synthetic_query_secret"
    urls = (
        f"postgresql://safe-host/safe_database{query}",
        f"postgresql://opaque-user:opaque-password@safe-host/safe_database{query}",
    )
    result = validate_repository(
        root,
        base,
        protected=False,
        environment=env,
        checks=(ValidationCheck("query", (sys.executable, "-c", f"print(*{urls!r}, sep='\\n')")),),
    )
    assert result.outcome == "PASS"
    log_path = result.checks[0].log_path
    for path in (log_path, result.receipt_path):
        output = path.read_text(encoding="utf-8")
        for fragment in (
            "probe@sample",
            "synthetic_query_secret",
            "opaque-user",
            "opaque-password",
        ):
            if fragment in output:
                pytest.fail("Synthetic URL credentials or query options escaped the Harness.")
    assert log_path.read_text(encoding="utf-8").count("safe_database") == 4


def test_ci_gate_sanitizes_subprocess_console_output(tmp_path: Path) -> None:
    env = diagnostic_environment(tmp_path)
    result = run_ci_gate(tmp_path, env, PAYLOAD + "\nfail()")
    output = result.stdout + result.stderr
    assert_no_credentials(output)
    assert result.returncode == 1
    for detail in ("==> pytest", "SQLSTATE 28P01", "RuntimeError", "stdout-stage", "stderr-stage"):
        assert detail in output


def run_ci_gate(root: Path, env: dict[str, str], payload: str) -> subprocess.CompletedProcess[str]:
    child = (
        "import sys; from scripts import validate; "
        "validate.CHECKS = (('pytest', (sys.executable, '-c', sys.argv[1])),); "
        "raise SystemExit(validate.main())"
    )
    return subprocess.run(
        (sys.executable, "-B", "-c", child, payload),
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )


REPRESENTATION_PAYLOAD = """
import json
import os
import sys
from urllib.parse import unquote

from core_console.config import Settings
from sqlalchemy import make_url

for key in ("DATABASE_URL", "TEST_DATABASE_URL"):
    raw = os.environ[key]
    settings = Settings(
        _env_file=None, AUTH_MODE="development", DEV_IDENTITY_ISSUER="issuer",
        DEV_IDENTITY_SUBJECT="subject", DATABASE_URL=raw,
    )
    assert settings.database_url_value() == raw
    decoded = unquote(raw)
    for output in (sys.stdout, sys.stderr):
        print("stage=connect ValueError SQLSTATE 28P01", file=output)
        print("url-stage", raw, decoded, repr(decoded), json.dumps(decoded), file=output)
        print("nested-stage", json.dumps({"diagnostic": repr(decoded)}),
              repr(ValueError(repr(decoded))), repr(json.dumps(decoded)), file=output)
    username = make_url(raw).username
    print(f"embedded-stage pool_{username}_checkout", file=sys.stderr)
password = os.environ["PGSSLPASSWORD"]
print("string-stage", ascii(password), repr("a diagnostic: " + password), file=sys.stderr)
print("bytes-stage", repr(password.encode()), file=sys.stderr)
for output in (sys.stdout, sys.stderr):
    print("adjacent-stage", os.environ["DATABASE_URL"] + "?unused=opaque,"
          + os.environ["TEST_DATABASE_URL"], file=output)
raise SystemExit(1)
"""


def test_credential_representations_at_subprocess_boundaries(tmp_path: Path) -> None:
    root, base = initialized_repository(tmp_path)
    env = diagnostic_environment(root) | {
        "DATABASE_URL": MIXED_URL,
        "TEST_DATABASE_URL": REPR_URL,
        "PGSSLPASSWORD": ARG_PASSWORD,
    }
    result = validate_repository(
        root,
        base,
        protected=False,
        environment=env,
        checks=(
            ValidationCheck(
                "representations", (sys.executable, "-c", REPRESENTATION_PAYLOAD, ARG_PASSWORD)
            ),
        ),
    )
    ci = run_ci_gate(root, env, REPRESENTATION_PAYLOAD)
    assert result.outcome == "FAIL" and ci.returncode == 1
    for path in (root / ".agent").rglob("*"):
        if path.is_file():
            assert_no_credentials(path.read_text(encoding="utf-8"))
    log = result.checks[0].log_path.read_text(encoding="utf-8")
    for output, expected_urls in ((log, 16), (ci.stdout, 8), (ci.stderr, 8)):
        assert_no_credentials(output)
        assert "stage=connect ValueError SQLSTATE 28P01" in output
        for database in ("guarded_db", "repr_guarded_db"):
            assert output.count(f"postgresql+psycopg://localhost:5432/{database}") == expected_urls
    assert "embedded-stage pool_<redacted>_checkout" in log
    assert "embedded-stage pool_<redacted>_checkout" in ci.stderr


@pytest.mark.parametrize(
    ("encoding", "letter"), [("cp936", "中"), ("cp1252", "é"), ("cp1252:replace", "中")]
)
def test_subprocess_output_in_a_configured_windows_encoding(
    tmp_path: Path, encoding: str, letter: str
) -> None:
    root, base = initialized_repository(tmp_path)
    fragments = ("native-user-left", "native-user-right", "native-pass-left", "native-pass-right")
    user = letter.join(fragments[:2])
    password = letter.join(fragments[2:])
    route = "localhost:5432/guarded_native_db"
    url = f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}@{route}"
    env = diagnostic_environment(root) | {"DATABASE_URL": url, "PYTHONIOENCODING": encoding}
    payload = """
import json
import os
import sys
from sqlalchemy import make_url

url = make_url(os.environ["DATABASE_URL"])
for output in (sys.stdout, sys.stderr):
    print("stage=connect ValueError SQLSTATE 28P01", file=output)
    print(f"pool_{url.username}_checkout", url.password, file=output)
    encoded = url.password.encode(sys.stdout.encoding, sys.stdout.errors)
    print("native-bytes-stage", repr(encoded), json.dumps({"error": repr(encoded)}), file=output)
    print(url.render_as_string(hide_password=False), file=output)
raise SystemExit(1)
"""
    result = validate_repository(
        root,
        base,
        protected=False,
        environment=env,
        checks=(ValidationCheck("native-output", (sys.executable, "-c", payload)),),
    )
    ci = run_ci_gate(root, env, payload)
    assert result.outcome == "FAIL" and ci.returncode == 1
    log = result.checks[0].log_path.read_text(encoding="utf-8")
    for output in (log, ci.stdout, ci.stderr):
        if any(fragment in output for fragment in fragments):
            pytest.fail("Synthetic database credential escaped the subprocess encoding boundary.")
        assert "pool_<redacted>_checkout <redacted>" in output
        assert "stage=connect ValueError SQLSTATE 28P01" in output
        assert route in output


def test_preflight_receipt_scrubs_database_identifier_that_matches_a_secret(tmp_path: Path) -> None:
    root, base = initialized_repository(tmp_path)
    env = diagnostic_environment(root)
    env["TEST_DATABASE_URL"] = env["TEST_DATABASE_URL"].replace("/guarded_db?", f"/{OVERLAP}?")
    result = validate_repository(root, base, protected=True, checks=(), environment=env)
    receipt_text = result.receipt_path.read_text(encoding="utf-8")
    assert_no_credentials(receipt_text)
    assert json.loads(receipt_text)["postgresTarget"]["database"] == "<redacted>"


def test_invalid_git_argument_does_not_echo_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = initialized_repository(tmp_path)
    secret_url = diagnostic_environment(root)["TEST_DATABASE_URL"]
    monkeypatch.setenv("TEST_DATABASE_URL", secret_url)
    with pytest.raises(RuntimeError) as failure:
        validate_repository(root, secret_url, protected=True)
    output = str(failure.value)
    assert_no_credentials(output)
    assert output.startswith("git ") and " failed: " in output
