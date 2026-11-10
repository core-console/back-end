"""Scrub pytest reports before terminal, capture, or JUnit consumers see them."""

import warnings
from collections.abc import Callable, Generator
from dataclasses import fields, is_dataclass
from pathlib import Path
from pprint import PrettyPrinter
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from _pytest._code.code import ExceptionRepr
from _pytest._io import TerminalWriter, saferepr
from _pytest.assertion import pytest_assertrepr_compare as stock_assertrepr_compare
from _pytest.junitxml import xml_key

from scripts.database_diagnostics import DatabaseRedactor

if TYPE_CHECKING:
    from _typeshed import SupportsWrite


class ReportDiagnostics:
    def __init__(self, root: Path) -> None:
        # Settings reads .env in the invocation directory even when pytest
        # selects a different directory as its configuration root.
        self.redact = DatabaseRedactor(root, extra_dotenv_roots=(Path.cwd(),))
        self.aliases: dict[str, str] = {}

    def nodeid(self, nodeid: str) -> str:
        safe = self.redact(nodeid)
        if safe == nodeid:
            return nodeid
        # Distinct secret-bearing parameters must remain distinct in test proof.
        # Use a run-local sequence instead of hashing potentially weak credentials.
        return self.aliases.setdefault(nodeid, f"{safe} [item {len(self.aliases) + 1}]")

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(self, excrepr: ExceptionRepr) -> None:
        original = str(excrepr)
        safe = self.redact(original)
        if safe == original:
            return

        # Stock JUnit and terminal reporters render this object directly.
        # Scrub before the terminal reporter splits it into individual lines.
        def render(tw: TerminalWriter) -> None:
            tw.write(safe)

        excrepr.toterminal = render  # type: ignore[method-assign]

    @pytest.hookimpl(tryfirst=True)
    def pytest_warning_recorded(self, warning_message: warnings.WarningMessage) -> None:
        message = str(warning_message.message)
        safe = self.redact(message)
        if safe != message:
            # Preserve the warning category while sanitizing its original text
            # before the terminal summary splits lines and strips whitespace.
            warning_message.message = safe

    def _contains_sensitive(self, *values: object) -> bool:
        pending = list(values)
        seen: set[int] = set()
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                if self.redact(value) != value:
                    return True
            elif isinstance(value, (bytes, bytearray)):
                rendered = repr(value)
                if self.redact(rendered) != rendered:
                    return True
            elif isinstance(value, (dict, list, tuple, set, frozenset)) or (
                is_dataclass(value) and not isinstance(value, type)
            ):
                if id(value) in seen:
                    continue
                seen.add(id(value))
                if is_dataclass(value) and not isinstance(value, type):
                    pending.extend(getattr(value, field.name) for field in fields(value))
                elif isinstance(value, dict):
                    pending.extend(value.keys())
                    pending.extend(value.values())
                elif isinstance(value, (list, tuple, set, frozenset)):
                    pending.extend(value)
        return False

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_assertrepr_compare(
        self, config: pytest.Config, op: str, left: object, right: object
    ) -> Generator[None, list[list[str]], list[list[str]]]:
        safe_comparison = None
        if self._contains_sensitive(left, right):
            if isinstance(left, str) and isinstance(right, str):
                safe_left, safe_right = self.redact(left), self.redact(right)
                safe_comparison = stock_assertrepr_compare(config, op, safe_left, safe_right)
                if not safe_comparison:
                    safe_comparison = [f"{safe_left!r} {op} {safe_right!r}"]
            else:
                safe_comparison = [
                    f"{self.redact(repr(left))} {op} {self.redact(repr(right))}",
                    "Sensitive comparison details redacted.",
                ]

        explanations = yield
        if safe_comparison is not None:
            # Discard pytest's recursive diff, which may already have clipped
            # the context needed to recognize a credential.
            return [safe_comparison]

        # Pytest truncates these lines after returning from the comparison hook.
        safe_explanations = []
        for lines in explanations:
            text = "\n".join(lines)
            safe_text = self.redact(text)
            safe_explanations.append(lines if safe_text == text else safe_text.split("\n"))
        return safe_explanations


DIAGNOSTICS = pytest.StashKey[ReportDiagnostics]()
type MetadataRecorder = Callable[[str, object], None]


def pytest_configure(config: pytest.Config) -> None:
    diagnostics = ReportDiagnostics(config.rootpath)
    config.stash[DIAGNOSTICS] = diagnostics
    config.pluginmanager.register(diagnostics)
    _protect_repr(config, diagnostics.redact)


def _protect_repr(config: pytest.Config, redact: DatabaseRedactor) -> None:
    """Sanitize pytest's value representations before clipping or wrapping."""

    class RedactedSafeRepr(saferepr.SafeRepr):
        def __init__(self, maxsize: int | None, use_ascii: bool = False) -> None:
            super().__init__(None, use_ascii)
            self.limit = maxsize
            if maxsize is not None:
                self.maxstring = maxsize

        def _safe(self, rendered: str) -> str:
            safe = redact(rendered)
            return safe if self.limit is None else saferepr._ellipsize(safe, self.limit)

        def repr(self, x: object) -> str:
            return self._safe(super().repr(x))

        def repr_str(self, x: str, level: int) -> str:
            return super().repr_str(redact(x), level)

        def repr_instance(self, x: object, level: int) -> str:
            return self._safe(super().repr_instance(x, level))

    class RedactedPrettyPrinter(PrettyPrinter):
        def format(
            self, object: object, context: dict[int, int], maxlevels: int, level: int
        ) -> tuple[str, bool, bool]:
            rendered, readable, recursive = super().format(object, context, maxlevels, level)
            return redact(rendered), readable, recursive

        def _format(
            self,
            object: object,
            stream: SupportsWrite[str],
            indent: int,
            allowance: int,
            context: dict[int, int],
            level: int,
        ) -> None:
            if isinstance(object, str):
                object = redact(object)
            elif isinstance(object, (bytes, bytearray)):
                rendered = repr(object)
                safe = redact(rendered)
                if safe != rendered:
                    stream.write(safe)
                    return
            super()._format(object, stream, indent, allowance, context, level)

    patch = pytest.MonkeyPatch()
    patch.setattr(saferepr, "SafeRepr", RedactedSafeRepr)
    # Only pytest's safeformat uses this printer. Keep application pprint intact.
    patch.setattr(saferepr, "pprint", SimpleNamespace(pformat=RedactedPrettyPrinter().pformat))
    config.add_cleanup(patch.undo)


def _safe_metadata_recorder(
    request: pytest.FixtureRequest, record: MetadataRecorder, *, xml_attribute: bool = False
) -> MetadataRecorder:
    if request.config.stash.get(xml_key, None) is None:
        return record
    redact = request.config.stash[DIAGNOSTICS].redact

    def write(name: object, value: object) -> None:
        if not xml_attribute and not isinstance(name, str):
            # Preserve pytest's type check on testsuite property names.
            record(cast(str, name), value)
            return
        name = str(name)
        safe_name = redact(name)
        # Attribute names must remain XML names even when the credential was
        # the entire name. Property names are stored as XML attribute values.
        if xml_attribute and safe_name != name:
            safe_name = "redacted_attribute"
        record(safe_name, redact(str(value)))

    return write


@pytest.fixture(scope="session", name="record_testsuite_property")
def _record_testsuite_property(
    request: pytest.FixtureRequest, record_testsuite_property: MetadataRecorder
) -> MetadataRecorder:
    # The built-in fixture writes directly to JUnit without a TestReport.
    return _safe_metadata_recorder(request, record_testsuite_property)


@pytest.fixture(name="record_xml_attribute")
def _record_xml_attribute(
    request: pytest.FixtureRequest, record_xml_attribute: MetadataRecorder
) -> MetadataRecorder:
    return _safe_metadata_recorder(request, record_xml_attribute, xml_attribute=True)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    config = session.config
    redact = config.stash[DIAGNOSTICS].redact
    junit = config.stash.get(xml_key, None)
    if junit is not None:
        junit.suite_name = redact(junit.suite_name)
        if junit.prefix is not None:
            junit.prefix = redact(junit.prefix)
    terminal = config.pluginmanager.get_plugin("terminalreporter")
    if terminal is None:
        return
    # Verbose starts, collect-only listings and warning summaries go directly
    # through the terminal writer rather than through TestReport.
    writer = terminal._tw
    original_write = writer.write

    def write(value: str, *, flush: bool = False, **markup: bool) -> None:
        original_write(redact(value), flush=flush, **markup)

    def restore() -> None:
        writer.write = original_write

    writer.write = write
    config.add_cleanup(restore)


@pytest.hookimpl(tryfirst=True)
def pytest_itemcollected(item: pytest.Item) -> None:
    # Pytest exposes no nodeid setter. Scrub sensitive IDs before cacheprovider
    # or terminal listeners see them, leaving ordinary test IDs unchanged.
    item._nodeid = item.config.stash[DIAGNOSTICS].nodeid(item.nodeid)


def _redact_report(
    report: pytest.TestReport | pytest.CollectReport, diagnostics: ReportDiagnostics
) -> None:
    redact = diagnostics.redact
    report.nodeid = diagnostics.nodeid(report.nodeid)
    if isinstance(report.longrepr, tuple):
        path, line, reason = report.longrepr
        report.longrepr = (redact(path), line, redact(reason))
    elif report.longrepr is not None:
        report.longrepr = redact(str(report.longrepr))
    report.sections = [(redact(name), redact(text)) for name, text in report.sections]
    if isinstance(report, pytest.TestReport):
        path, location_line, name = report.location
        report.location = (redact(path), location_line, redact(name))
        report.keywords = {redact(key): value for key, value in report.keywords.items()}
        report.user_properties = [
            (redact(name), redact(str(value))) for name, value in report.user_properties
        ]
        if hasattr(report, "wasxfail"):
            report.wasxfail = redact(str(report.wasxfail))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item,
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    _redact_report(report, item.config.stash[DIAGNOSTICS])
    return report


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    report = yield
    _redact_report(report, collector.config.stash[DIAGNOSTICS])
    return report
