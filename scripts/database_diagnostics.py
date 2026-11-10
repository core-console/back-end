"""Redact database credentials at diagnostic output boundaries."""

import json
import locale
import os
import re
from bisect import bisect_right
from codecs import lookup
from collections.abc import Callable, Iterable, Iterator, Mapping
from itertools import pairwise
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlsplit

_REDACTED = "<redacted>"
_MASKED_CHAR = "\0"
_URL_SCHEME = re.compile(r"postgres(?:ql)?(?:\+\w+)?://", re.IGNORECASE)
_URL_TARGET = re.compile(r"""[^\s<>"']+""")
_BYTE_URL_TARGET = re.compile(_URL_TARGET.pattern, re.ASCII)
_AUTHORITY_END = re.compile(r"[/#?]")
_OPTIONS_START = re.compile(r"[?#]")
# Match the complete set of line boundaries used by Python's traceback renderer.
_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_LITERAL_TOKENS = re.compile(rf"(%[0-9a-fA-F]{{2}}|{_LINE_BREAK.pattern})")
_ESCAPE = re.compile(
    r"""\\(?:u[dD][89a-bA-B][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}"""
    r"""|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|[\\'"abfnrtv/])"""
)
_CONTROL_ESCAPES = dict(zip("abfnrtv", "\a\b\f\n\r\t\v", strict=True))
_SECRET_OPTIONS = frozenset({"user", "password", "sslpassword", "token", "access_token", "sslkey"})
_URL_KEYS = ("database_url", "test_database_url")

type Span = tuple[int, int]


def _byte_reprs(value: bytes) -> set[str]:
    return {repr(value)[2:-1], repr(b'"' + value)[3:-1]} if value else set()


def _spellings(value: str) -> set[str]:
    if not value:
        return set()
    # repr() can choose double quotes for an isolated credential and single
    # quotes for the containing URL or diagnostic. A leading double quote
    # forces Python's single-quoted spelling without changing the payload.
    return {
        value,
        quote(value, safe=""),
        quote_plus(value, safe=""),
        repr(value)[1:-1],
        repr('"' + value)[2:-1],
        ascii(value)[1:-1],
        ascii('"' + value)[2:-1],
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
    } | _byte_reprs(value.encode("utf-8"))


def _literal(value: str) -> str:
    # Percent escapes are case insensitive, whereas decoded credentials are not.
    parts: list[str] = []
    for index, part in enumerate(_LITERAL_TOKENS.split(value)):
        if index % 2 == 0:
            parts.append(re.escape(part))
        elif _LINE_BREAK.fullmatch(part):
            # Pipes can translate newlines. Tracebacks and assertion diffs
            # insert indentation and margin markers on continuation lines.
            line_break = r"\r{0,2}\n" if part == "\r\n" else r"\r?\n"
            if part not in {"\n", "\r\n"}:
                line_break = f"(?:{re.escape(part)}|{line_break})"
            line_break += r"(?:[ \t]*[E|+?\-](?=[ \t]))*[ \t]*"
            parts.append(line_break)
        else:
            parts.append(f"(?i:{re.escape(part)})")
    return "".join(parts)


def _url_spellings(raw: str) -> set[str]:
    from sqlalchemy import make_url
    from sqlalchemy.exc import ArgumentError

    # Settings passes the original string to SQLAlchemy. Protect percent signs
    # for a second parse to retain the exact encoded credential spelling with
    # the same URL grammar. In particular, '?' can be inside the password.
    try:
        parsed = make_url(raw)
        encoded = make_url(raw.replace("%", "%25"))
    except ArgumentError, ValueError:
        return _unparseable_url_spellings(raw)

    # Match credential spans in visible URLs so that the safe route survives.
    # Keep whole-string spellings only when the URL scanner cannot see them.
    spellings = {value for value in _spellings(raw) if _URL_SCHEME.match(value) is None}
    for value in (parsed.username, parsed.password, encoded.username, encoded.password):
        if value:
            spellings.update(_spellings(value))
            # SQLAlchemy URL rendering also leaves spaces and plus signs literal.
            spellings.add(quote(value, safe=" +"))
    for option, values in parsed.normalized_query.items():
        if option.lower() in _SECRET_OPTIONS:
            for value in values:
                spellings.update(_spellings(value))

    # Locate query options after the exact userinfo SQLAlchemy recognized.
    # Preserve plus signs as well as percent escapes in isolated raw query output.
    authority_start = len(encoded.drivername) + len("://")
    if encoded.username is not None:
        authority_start += len(encoded.username) + 1  # username and @
    if encoded.password is not None:
        authority_start += len(encoded.password) + 1  # password and colon
    query = raw[authority_start:].partition("?")[2]
    for option, value in parse_qsl(query.replace("%", "%25").replace("+", "%2B")):
        if unquote_plus(option).lower() in _SECRET_OPTIONS:
            spellings.update(_spellings(value))
            spellings.update(_spellings(unquote(value)))
    return spellings


def _unparseable_url_spellings(raw: str) -> set[str]:
    # Invalid configurations can still reach validation errors. Protect the
    # complete value and any recognizable components on that error path.
    spellings = _spellings(raw) | _spellings(unquote(raw))
    try:
        uri = urlsplit(raw)
    except ValueError:
        return spellings
    for value in (uri.username or "", uri.password or ""):
        spellings.update(_spellings(value))
        spellings.update(_spellings(unquote(value)))
    for option, value in parse_qsl(uri.query):
        if option.lower() in _SECRET_OPTIONS:
            spellings.update(_spellings(value))
    return spellings


def _merged_spans(spans: Iterable[Span]) -> list[Span]:
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _replace_spans(value: str, spans: Iterable[Span], replacement: Callable[[Span], str]) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.extend((value[cursor:start], replacement((start, end))))
        cursor = end
    parts.append(value[cursor:])
    return "".join(parts)


def _unescape(value: str) -> tuple[str, list[int], list[int]]:
    """Decode one Python or JSON escape layer and record original offset deltas."""
    boundaries = [0]
    deltas = [0]

    def replace(match: re.Match[str]) -> str:
        escape = match[0][1:]
        if len(escape) == 1:
            character = _CONTROL_ESCAPES.get(escape, escape)
        else:
            # A JSON surrogate pair encodes one character.
            if len(escape) == 11:
                high, low = int(escape[1:5], 16), int(escape[7:], 16)
                codepoint = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
            else:
                codepoint = int(escape[1:], 16)
            if codepoint > 0x10FFFF:
                return match[0]
            character = chr(codepoint)
        deltas.append(deltas[-1] + len(escape))
        boundaries.append(match.end() - deltas[-1])
        return character

    return _ESCAPE.sub(replace, value), boundaries, deltas


def _secret_spans(value: str, secrets: tuple[re.Pattern[str], ...]) -> Iterator[Span]:
    layers: list[tuple[list[int], list[int]]] = []
    while True:
        for pattern in secrets:
            for match in pattern.finditer(value):
                start, end = match.span(1)
                for boundaries, deltas in reversed(layers):
                    start += deltas[bisect_right(boundaries, start) - 1]
                    end += deltas[bisect_right(boundaries, end) - 1]
                yield start, end
        value, boundaries, deltas = _unescape(value)
        if len(boundaries) == 1:
            return
        layers.append((boundaries, deltas))


def _byte_spans(value: str, spans: Iterable[Span], encoding: str) -> Iterator[Span]:
    previous = offset = 0
    for start, end in _merged_spans(spans):
        offset += len(value[previous:start].encode(encoding, errors="surrogateescape"))
        size = len(value[start:end].encode(encoding, errors="surrogateescape"))
        yield offset, offset + size
        previous = end
        offset += size


def _authority_end(value: str, start: int, end: int) -> int:
    boundary = _AUTHORITY_END.search(value, start, end)
    return boundary.start() if boundary else end


def _userinfo_end(value: str, masked: str, start: int, end: int) -> int:
    raw_end = _authority_end(value, start, end)
    raw_separator = value.rfind("@", start, raw_end)
    if raw_separator >= 0 and masked[raw_separator] == "@":
        return raw_separator + 1
    # A decoded credential can contain an apparent authority delimiter. Only
    # cross it if the rest of the userinfo is covered by credentials or their
    # colon separator. An @ in ordinary query text cannot extend the authority.
    separator = value.rfind("@", start, _authority_end(masked, start, end))
    if separator >= raw_end and masked[raw_end:separator].strip(_MASKED_CHAR + ":"):
        return raw_separator + 1
    return separator + 1


def _url_removals(value: str, masked: str, target_pattern: re.Pattern[str]) -> Iterator[Span]:
    schemes = [
        match.span()
        for match in _URL_SCHEME.finditer(value)
        if masked[match.start() : match.end()].strip(_MASKED_CHAR)
    ]
    schemes.append((len(value), len(value)))
    for (_, start), (limit, _) in pairwise(schemes):
        if limit < len(value) and value[limit - 1] in ",;":
            limit -= 1
        target = target_pattern.match(masked, start, limit)
        if target is None:
            continue
        end = target.end()
        userinfo_end = _userinfo_end(value, masked, start, end)
        if userinfo_end:
            yield start, userinfo_end
        options = _OPTIONS_START.search(value, userinfo_end or start, end)
        if options:
            yield options.start(), end


def _scrub(value: str, spans: Iterable[Span], target_pattern: re.Pattern[str]) -> str:
    secrets = _merged_spans(spans)
    masked = _replace_spans(value, secrets, lambda span: _MASKED_CHAR * (span[1] - span[0]))
    removals = set(_merged_spans(_url_removals(value, masked, target_pattern)))
    # Render from the original offsets once, including any secret that crosses
    # a structural removal boundary. Pure URL removals leave the safe route.
    spans = _merged_spans((*secrets, *removals))
    return _replace_spans(value, spans, lambda span: "" if span in removals else _REDACTED)


def _capture_encodings(environment: Mapping[str, str]) -> tuple[str, ...]:
    configured = environment.get("pythonioencoding", "").partition(":")[0]
    encodings: dict[str, None] = {}
    for encoding in (configured, "utf-8", locale.getencoding()):
        if encoding:
            try:
                normalized = lookup(encoding).name
                # Exclude transform codecs such as base64, which cannot decode text.
                "".encode(normalized)
            except LookupError:
                continue
            encodings[normalized] = None
    return tuple(encodings)


def _capture_spellings(literals: set[str], encodings: tuple[str, ...], errors: str) -> set[str]:
    spellings: set[str] = set()
    for encoding in encodings:
        if encoding == "utf-8" and errors == "strict":
            continue
        for literal in literals:
            try:
                encoded = literal.encode(encoding, errors=errors)
                rendered = encoded.decode(encoding, errors="replace")
            except UnicodeError, LookupError:
                continue
            spellings.update(_byte_reprs(encoded))
            if rendered != literal:
                spellings.update(_spellings(rendered))
    return spellings


class DatabaseRedactor:
    """Use both environment and dotenv sources, including overridden values."""

    def __init__(
        self,
        root: Path,
        environment: Mapping[str, str] | None = None,
        *,
        extra_dotenv_roots: tuple[Path, ...] = (),
    ) -> None:
        from pydantic_settings import DotEnvSettingsSource

        from core_console.config import Settings

        environment = {
            key.lower(): value
            for key, value in (os.environ if environment is None else environment).items()
        }
        self._encodings = _capture_encodings(environment)
        dotenv_sources = (
            DotEnvSettingsSource(Settings, env_file=directory / ".env").env_vars
            for directory in dict.fromkeys((root, *extra_dotenv_roots))
        )
        literals = {_REDACTED}
        for source in (environment, *dotenv_sources):
            for key in ("pguser", "pgpassword", "pgsslpassword"):
                literals.update(_spellings(source.get(key) or ""))
            for key in _URL_KEYS:
                raw = source.get(key)
                if raw:
                    literals.update(_url_spellings(raw))
        output_errors = environment.get("pythonioencoding", "").partition(":")[2] or "strict"
        literals.update(_capture_spellings(literals, self._encodings, output_errors))
        # A zero-width match finds the longest fixed-length spelling at each
        # starting position. Check multiline spellings independently because
        # continuation prefixes can change their matched length.
        ordered = sorted(literals, key=lambda value: (-len(value), value))
        alternatives = "|".join(
            _literal(value) for value in ordered if not _LINE_BREAK.search(value)
        )
        multiline = (_literal(value) for value in ordered if _LINE_BREAK.search(value))
        self._secrets = tuple(
            re.compile(f"(?=({pattern}))") for pattern in (alternatives, *multiline)
        )

    def __call__(self, value: str) -> str:
        return _scrub(value, _secret_spans(value, self._secrets), _URL_TARGET)

    def subprocess_text(self, value: bytes) -> str:
        preferred = self._encodings[0]
        if "A".encode(preferred) != b"A" or len(self._encodings) == 1 or value.isascii():
            return self(value.decode(preferred, errors="replace"))
        # Inspect every plausible text decoding before choosing the display
        # encoding. A valid UTF-8 decode can hide a native code-page credential.
        spans: list[Span] = []
        display = None
        for encoding in self._encodings:
            try:
                decoded = value.decode(encoding)
            except UnicodeDecodeError:
                decoded = value.decode(encoding, errors="surrogateescape")
            else:
                if display is None:
                    display = encoding
            spans.extend(_byte_spans(decoded, _secret_spans(decoded, self._secrets), encoding))
        # Latin-1 maps one code point to each original byte. Scrub the union
        # of byte spans and ASCII URL syntax before decoding for display.
        safe = _scrub(value.decode("latin-1"), spans, _BYTE_URL_TARGET)
        return safe.encode("latin-1").decode(display or preferred, errors="replace")

    def bytes(self, value: bytes) -> bytes:
        return self.subprocess_text(value).encode("utf-8")
