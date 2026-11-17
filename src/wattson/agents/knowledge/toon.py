"""A small TOON decoder for the answers Grimoire writes (mcp/output.ts with @toon-format/toon 4.1.1).

TOON (Token-Oriented Object Notation, spec v4.1) is JSON written for an AI to read. Grimoire uses it
for its list answers. What this decoder reads:
- a list of records as a table: a header "[N]{field,...}:" and then one row per record, the values
  separated by commas ("[N|]" or "[N<tab>]" name another separator);
- a list of records as blocks: "[N]:" and then one "- name: value" block per record, its other
  fields on the lines below, one level deeper (Grimoire does this when a tag holds a space);
- inline lists of plain values: "tags[2]: a,b" ("tags[0]:" is an empty list);
- plain "name: value" records and nested records (two spaces per level);
- "[]" for no items;
- values: "a quoted string" with \\ \" \n \r \t \\uXXXX, true, false, null, numbers, and plain text.

Not read (Grimoire never writes them): nested field groups in a header ("temp{min,max}"), keyed
tables ("[N:]"), lists of lists. Anything that does not fit raises ToonError: the caller then reads
the answer as plain text, it never guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

INDENT = 2
_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:e[+-]?\d+)?$", re.IGNORECASE)
_INTEGER = re.compile(r"^-?(?:0|[1-9]\d*)$")
_LITERALS: dict[str, Any] = {"true": True, "false": False, "null": None}
_BRACKET = re.compile(r"^\[(?P<n>0|[1-9]\d*)(?P<delim>[|\t])?\]")


class ToonError(ValueError):
    """The text is not TOON this decoder can read."""


@dataclass(frozen=True)
class _Line:
    number: int
    depth: int
    content: str


@dataclass(frozen=True)
class _Header:
    count: int
    delimiter: str
    fields: list[str] | None
    rest: str  # what follows the colon, spaces trimmed


def decode(text: str) -> Any:
    """The JSON value that a TOON text stands for."""
    lines = _lines(text)
    if not lines:
        raise ToonError("the text is empty")
    first = lines[0]
    if first.depth != 0:
        raise ToonError("the first line is indented")
    parser = _Parser(lines)
    if first.content == "[]" and len(lines) == 1:
        return []
    if first.content.startswith("["):
        header = _parse_header(first.content, 1)
        parser.index = 1
        value: Any = parser.array(header, 0)
    elif len(lines) == 1 and _find_unquoted(first.content, ":") < 0:
        return _primitive(first.content, 1)
    else:
        value = parser.record(0)
    if parser.index != len(lines):
        raise ToonError(f"line {lines[parser.index].number}: not expected here")
    return value


def _lines(text: str) -> list[_Line]:
    out = []
    for number, raw in enumerate(text.split("\n"), start=1):
        raw = raw.removesuffix("\r")
        if not raw.strip(" \t"):
            continue
        content = raw.lstrip(" ")
        indent = len(raw) - len(content)
        if content.startswith("\t") or indent % INDENT:
            raise ToonError(f"line {number}: the indentation is not a multiple of {INDENT} spaces")
        out.append(_Line(number, indent // INDENT, content.rstrip(" ")))
    return out


class _Parser:
    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.index = 0

    def _peek(self) -> _Line | None:
        return self.lines[self.index] if self.index < len(self.lines) else None

    def record(self, depth: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while (line := self._peek()) is not None and line.depth >= depth:
            if line.depth > depth:
                raise ToonError(f"line {line.number}: indented too deep")
            if line.content == "-" or line.content.startswith("- "):
                raise ToonError(f"line {line.number}: a list item outside a list")
            self.index += 1
            name, value = self.field(line.content, depth, line.number)
            out[name] = value
        return out

    def field(self, content: str, depth: int, number: int) -> tuple[str, Any]:
        name, at = _parse_key(content, number)
        rest = content[at:]
        if rest.startswith("["):
            return name, self.array(_parse_header(rest, number), depth)
        if not rest.startswith(":"):
            raise ToonError(f"line {number}: a colon is missing after {name!r}")
        value = rest[1:].strip(" ")
        if value:
            return name, _primitive(value, number)
        nested = self._peek()
        if nested is not None and nested.depth > depth:
            return name, self.record(depth + 1)
        return name, {}

    def array(self, header: _Header, depth: int) -> list[Any]:
        if header.fields is not None:
            if header.rest:
                raise ToonError("a table header has values after its colon")
            rows = [self._row(header, depth + 1) for _ in range(header.count)]
            self._no_more_at(depth + 1)
            return rows
        if header.rest:
            values = [_primitive(v, 0) for v in _split(header.rest, header.delimiter)]
            if len(values) != header.count:
                raise ToonError(f"{header.count} values announced, {len(values)} found")
            return values
        items = [self._item(depth + 1) for _ in range(header.count)]
        self._no_more_at(depth + 1)
        return items

    def _no_more_at(self, depth: int) -> None:
        line = self._peek()
        if line is not None and line.depth >= depth:
            raise ToonError(f"line {line.number}: more items than the header announced")

    def _row(self, header: _Header, depth: int) -> dict[str, Any]:
        line = self._peek()
        if line is None or line.depth != depth:
            raise ToonError("fewer rows than the header announced")
        self.index += 1
        values = _split(line.content, header.delimiter)
        assert header.fields is not None
        if len(values) != len(header.fields):
            raise ToonError(f"line {line.number}: {len(values)} values for {len(header.fields)} fields")
        return {name: _primitive(v, line.number) for name, v in zip(header.fields, values, strict=True)}

    def _item(self, depth: int) -> Any:
        line = self._peek()
        if line is None or line.depth != depth or not (line.content == "-" or line.content.startswith("- ")):
            raise ToonError("fewer list items than the header announced")
        self.index += 1
        content = line.content[2:].strip(" ") if line.content != "-" else ""
        if not content:
            return {}
        if content.startswith("["):
            return self.array(_parse_header(content, line.number), depth + 1)
        if _find_unquoted(content, ":") < 0:
            return _primitive(content, line.number)
        # A record: its first field is on the "- " line, the others one level below the item.
        name, value = self.field(content, depth + 1, line.number)
        out = {name: value}
        for other, other_value in self.record(depth + 1).items():
            out[other] = other_value
        return out


def _parse_header(text: str, number: int) -> _Header:
    found = _BRACKET.match(text)
    if found is None:
        raise ToonError(f"line {number}: not an array header")
    delimiter = found.group("delim") or ","
    rest = text[found.end() :]
    fields: list[str] | None = None
    if rest.startswith("{"):
        close = _find_unquoted(rest, "}")
        if close < 0 or "{" in rest[1:close]:
            raise ToonError(f"line {number}: nested field groups are not supported")
        fields = [_key_text(f, number) for f in _split(rest[1:close], delimiter)]
        rest = rest[close + 1 :]
    if not rest.startswith(":"):
        raise ToonError(f"line {number}: the array header has no colon")
    return _Header(int(found.group("n")), delimiter, fields, rest[1:].strip(" "))


def _parse_key(content: str, number: int) -> tuple[str, int]:
    if content.startswith('"'):
        close = _closing_quote(content, 0)
        if close < 0:
            raise ToonError(f"line {number}: a quoted key is not closed")
        return _unescape(content[1:close], number), close + 1
    for at, char in enumerate(content):
        if char in ":[":
            name = content[:at].strip(" ")
            if not name:
                raise ToonError(f"line {number}: a key is missing")
            return name, at
    raise ToonError(f"line {number}: a colon is missing")


def _key_text(token: str, number: int) -> str:
    token = token.strip(" ")
    if token.startswith('"'):
        if _closing_quote(token, 0) != len(token) - 1:
            raise ToonError(f"line {number}: a quoted field name is broken")
        return _unescape(token[1:-1], number)
    if not token:
        raise ToonError(f"line {number}: an empty field name")
    return token


def _primitive(token: str, number: int) -> Any:
    token = token.strip(" ")
    if not token:
        return ""
    if token.startswith('"'):
        close = _closing_quote(token, 0)
        if close != len(token) - 1:
            raise ToonError(f"line {number}: a quoted string is broken")
        return _unescape(token[1:close], number)
    if token in _LITERALS:
        return _LITERALS[token]
    if _NUMBER.match(token):
        if _INTEGER.match(token):
            return int(token)
        return float(token)
    return token


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}


def _unescape(text: str, number: int) -> str:
    if "\\" not in text:
        return text
    out = []
    i = 0
    while i < len(text):
        char = text[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        if i + 1 >= len(text):
            raise ToonError(f"line {number}: a backslash at the end of a string")
        nxt = text[i + 1]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
        elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[i + 2 : i + 6]):
            code = int(text[i + 2 : i + 6], 16)
            if 0xD800 <= code <= 0xDFFF:
                raise ToonError(f"line {number}: a lone surrogate")
            out.append(chr(code))
            i += 6
        else:
            raise ToonError(f"line {number}: an unknown escape \\{nxt}")
    return "".join(out)


def _closing_quote(text: str, start: int) -> int:
    i = start + 1
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return -1


def _find_unquoted(text: str, char: str) -> int:
    quoted = False
    i = 0
    while i < len(text):
        c = text[i]
        if quoted and c == "\\" and i + 1 < len(text):
            i += 2
            continue
        if c == '"':
            quoted = not quoted
        elif c == char and not quoted:
            return i
        i += 1
    return -1


def _split(text: str, delimiter: str) -> list[str]:
    values: list[str] = []
    buf: list[str] = []
    quoted = False
    i = 0
    while i < len(text):
        c = text[i]
        if quoted and c == "\\" and i + 1 < len(text):
            buf.append(text[i : i + 2])
            i += 2
            continue
        if c == '"':
            quoted = not quoted
            buf.append(c)
        elif c == delimiter and not quoted:
            values.append("".join(buf).strip(" "))
            buf = []
        else:
            buf.append(c)
        i += 1
    if buf or values:
        values.append("".join(buf).strip(" "))
    return values
