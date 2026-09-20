"""miniyaml - a deliberately small YAML subset reader for OpenAPI documents.

Standard library only.  ``openapi-contract-diff`` uses this module when PyYAML
is not installed (see the README section "YAML support" for the exact list of
what is and is not supported).

Design goals, in order:

1. Never guess.  Anything outside the supported subset raises :class:`YamlError`
   with a line number instead of silently producing a wrong document.
2. Cover the YAML that OpenAPI 3.x documents actually contain: block mappings,
   block sequences, flow collections, quoted and plain scalars, literal/folded
   block scalars, comments, anchors/aliases, and merge keys.
3. Stay small enough to audit in one sitting.

Deliberate divergences from PyYAML (documented, not accidental):

* YAML 1.2 core-schema booleans only: ``true``/``false`` in their common
  spellings.  ``yes``/``no``/``on``/``off`` are strings here - PyYAML (YAML 1.1)
  turns them into booleans, which is a well-known source of bugs.
* Dates and timestamps stay strings; PyYAML converts them to ``datetime``.
* Mapping keys are always returned as strings.  PyYAML returns ``int`` for a
  key such as ``200:``; OpenAPI keys are strings, so the coercion is safer, and
  the differ normalises both shapes anyway.
"""

from __future__ import annotations

import copy
import re

__all__ = ["YamlError", "load", "load_file", "is_available", "LIMITATIONS"]

#: Human-readable summary of what this reader does not support.
LIMITATIONS = (
    "much of YAML is unsupported: multi-line plain scalars, '?' explicit keys, "
    "complex keys, tags other than !!str/!!int/!!float/!!bool/!!null, "
    "directives, multiple documents in one stream, and explicit '---' blocks "
    "with per-document content"
)

_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][-+]?[0-9]+)?$")
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}

_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}


class YamlError(ValueError):
    """Raised for any input outside the supported YAML subset."""

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        if line is not None:
            message = "%s (line %d)" % (message, line)
        super().__init__(message)


def is_available() -> bool:
    """True: this module is importable.  Kept for symmetry with the PyYAML probe."""
    return True


# --------------------------------------------------------------------------
# scalar helpers
# --------------------------------------------------------------------------
def _read_quoted(text: str, index: int, line: int) -> tuple[str, int]:
    """Read a quoted scalar starting at ``text[index]``; return (value, next)."""
    quote = text[index]
    i = index + 1
    out: list[str] = []
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), i + 1
            out.append(ch)
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= len(text):
                raise YamlError("unterminated escape sequence in double-quoted scalar", line)
            esc = text[i + 1]
            if esc in _ESCAPES:
                out.append(_ESCAPES[esc])
                i += 2
                continue
            if esc in _HEX_ESCAPES:
                width = _HEX_ESCAPES[esc]
                digits = text[i + 2 : i + 2 + width]
                if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]{%d}" % width, digits):
                    raise YamlError("invalid \\%s escape in double-quoted scalar" % esc, line)
                out.append(chr(int(digits, 16)))
                i += 2 + width
                continue
            raise YamlError("unsupported escape sequence \\%s" % esc, line)
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise YamlError("unterminated quoted scalar", line)


def _strip_comment(text: str) -> str:
    """Drop a trailing ``#`` comment, ignoring ``#`` inside quotes."""
    out: list[str] = []
    i = 0
    quote = None
    while i < len(text):
        ch = text[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if quote == '"':
            out.append(ch)
            if ch == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _plain_scalar(text: str):
    """Resolve a plain (unquoted) scalar using the YAML 1.2 core schema."""
    token = text.strip()
    if token == "" or token == "~" or token.lower() == "null":
        return None
    if token in ("true", "True", "TRUE"):
        return True
    if token in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(token):
        digits = token.lstrip("+-")
        if not (len(digits) > 1 and digits[0] == "0"):
            try:
                return int(token)
            except ValueError:  # pragma: no cover - defensive
                pass
        return token
    if _FLOAT_RE.match(token):
        try:
            return float(token)
        except ValueError:  # pragma: no cover - defensive
            pass
    return token


def _key_str(key) -> str:
    if isinstance(key, str):
        return key
    if key is True:
        return "true"
    if key is False:
        return "false"
    if key is None:
        return ""
    return str(key)


def _split_key(text: str, line: int) -> tuple[str, str]:
    """Split ``key: value`` into raw (key, value) text."""
    if text and text[0] in "\"'":
        value, index = _read_quoted(text, 0, line)
        rest = text[index:].lstrip()
        if not rest.startswith(":"):
            raise YamlError("expected ':' after quoted mapping key", line)
        return str(value), rest[1:].strip()
    i = 0
    while i < len(text):
        if text[i] == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
            return text[:i].strip(), text[i + 1 :].strip()
        i += 1
    raise YamlError("expected 'key: value', got %r" % text[:48], line)


def _flow_balance(text: str) -> int:
    depth = 0
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth


class _FlowParser:
    """Recursive-descent parser for YAML flow collections (``[...]`` / ``{...}``)."""

    def __init__(self, text: str, line: int):
        self.text = text
        self.line = line
        self.i = 0

    def parse(self):
        value = self.value()
        self.skip_ws()
        if self.i < len(self.text):
            raise YamlError(
                "unexpected trailing characters in flow collection: %r" % self.text[self.i : self.i + 24],
                self.line,
            )
        return value

    def skip_ws(self) -> None:
        while self.i < len(self.text) and self.text[self.i] in " \t":
            self.i += 1

    def peek(self) -> str:
        self.skip_ws()
        return self.text[self.i] if self.i < len(self.text) else ""

    def value(self):
        ch = self.peek()
        if ch == "[":
            return self.sequence()
        if ch == "{":
            return self.mapping()
        if ch in "\"'":
            value, index = _read_quoted(self.text, self.i, self.line)
            self.i = index
            return value
        return _plain_scalar(self.raw_plain())

    def raw_plain(self) -> str:
        start = self.i
        depth = 0
        while self.i < len(self.text):
            ch = self.text[self.i]
            if depth == 0 and ch in ",]}":
                break
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            self.i += 1
        return self.text[start : self.i].strip()

    def sequence(self) -> list:
        self.i += 1  # consume '['
        out: list = []
        if self.peek() == "]":
            self.i += 1
            return out
        while True:
            out.append(self.value())
            ch = self.peek()
            if ch == ",":
                self.i += 1
                if self.peek() == "]":
                    self.i += 1
                    return out
                continue
            if ch == "]":
                self.i += 1
                return out
            raise YamlError("expected ',' or ']' in flow sequence", self.line)

    def mapping(self) -> dict:
        self.i += 1  # consume '{'
        out: dict = {}
        if self.peek() == "}":
            self.i += 1
            return out
        while True:
            if self.peek() in "\"'":
                key, index = _read_quoted(self.text, self.i, self.line)
                self.i = index
            else:
                key = _plain_scalar(self.raw_key())
            self.skip_ws()
            if self.i >= len(self.text) or self.text[self.i] != ":":
                raise YamlError("expected ':' in flow mapping", self.line)
            self.i += 1
            out[_key_str(key)] = self.value()
            ch = self.peek()
            if ch == ",":
                self.i += 1
                if self.peek() == "}":
                    self.i += 1
                    return out
                continue
            if ch == "}":
                self.i += 1
                return out
            raise YamlError("expected ',' or '}' in flow mapping", self.line)

    def raw_key(self) -> str:
        start = self.i
        while self.i < len(self.text) and self.text[self.i] not in ":,}":
            self.i += 1
        token = self.text[start : self.i].strip()
        if token == "":
            raise YamlError("empty key in flow mapping", self.line)
        return token


def _fold(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        if line == "":
            out.append("\n")
            continue
        if out and not out[-1].endswith("\n"):
            out.append(" ")
        out.append(line)
    return "".join(out)


class _Parser:
    def __init__(self, text: str):
        self.raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # ``split`` leaves a trailing empty string when the text ends with a
        # newline; that artifact must not be mistaken for a blank content line.
        self._ends_with_newline = self.raw and self.raw[-1] == ""
        self.i = 0
        self.anchors: dict = {}

    # -- line scanning ----------------------------------------------------
    def _peek(self):
        """Return (indent, content, next_index, lineno) of the next real line."""
        j = self.i
        while j < len(self.raw):
            line = self.raw[j]
            stripped = line.strip()
            if stripped == "" or stripped.startswith("#") or stripped in ("---", "...") or stripped.startswith("%"):
                j += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent < len(line) and line[indent] == "\t":
                raise YamlError("tab characters may not be used for indentation", j + 1)
            return indent, line[indent:].rstrip(), j + 1, j + 1
        return None

    # -- node parsing -----------------------------------------------------
    def parse_document(self):
        token = self._peek()
        if token is None:
            return None
        indent, content, nxt, lineno = token
        if indent != 0:
            raise YamlError("document must start at indentation 0", lineno)
        if content[:1] in ("[", "{"):
            # A whole document written as a flow collection: JSON input lands
            # here, and JSON is what many OpenAPI documents are stored as.
            self.i = nxt
            value = _FlowParser(self._gather_flow(content, lineno), lineno).parse()
            leftover = self._peek()
            if leftover is not None:
                raise YamlError("unexpected content after the document ended", leftover[3])
            return value
        value = self._parse_node(0)
        leftover = self._peek()
        if leftover is not None:
            raise YamlError("unexpected content after the document ended", leftover[3])
        return value

    def _parse_node(self, indent: int):
        token = self._peek()
        if token is None:
            return None
        ind, content, _next, lineno = token
        if ind < indent:
            return None
        if content == "-" or content.startswith("- "):
            return self._parse_sequence(ind)
        return self._parse_mapping(ind)

    def _parse_sequence(self, indent: int) -> list:
        out: list = []
        while True:
            token = self._peek()
            if token is None:
                break
            ind, content, nxt, lineno = token
            if ind < indent:
                break
            if ind > indent:
                raise YamlError("bad indentation in sequence", lineno)
            if not (content == "-" or content.startswith("- ")):
                break
            after = content[1:]
            column = indent + 1 + (len(after) - len(after.lstrip(" ")))
            item_text = after.strip()
            self.i = nxt
            if item_text == "":
                nested = self._peek()
                if nested is not None and nested[0] > indent:
                    out.append(self._parse_node(nested[0]))
                else:
                    out.append(None)
                continue
            if self._looks_like_entry(item_text):
                out.append(self._parse_mapping(column, seed=(item_text, lineno)))
            else:
                out.append(self._parse_value(item_text, indent, lineno, "-"))
        return out

    @staticmethod
    def _looks_like_entry(text: str) -> bool:
        if text[0] in "\"'":
            try:
                _value, index = _read_quoted(text, 0, 0)
            except YamlError:
                return False
            return text[index:].lstrip().startswith(":")
        if text[0] in "[{":
            # A flow collection is a value, even though it contains ':'.
            return False
        i = 0
        while i < len(text):
            if text[i] == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
                return i > 0
            i += 1
        return False

    def _parse_mapping(self, indent: int, seed=None) -> dict:
        result: dict = {}
        if seed is not None:
            self._apply_entry(result, seed[0], indent, seed[1])
        while True:
            token = self._peek()
            if token is None:
                break
            ind, content, nxt, lineno = token
            if ind < indent:
                break
            if ind > indent:
                raise YamlError("bad indentation in mapping", lineno)
            if content == "-" or content.startswith("- "):
                break
            self.i = nxt
            self._apply_entry(result, content, indent, lineno)
        return result

    def _apply_entry(self, result: dict, text: str, indent: int, lineno: int) -> None:
        key_text, rest = _split_key(text, lineno)
        key = _key_str(key_text)
        if key == "<<":
            merged = self._parse_value(rest, indent, lineno, key)
            candidates = merged if isinstance(merged, list) else [merged]
            for candidate in candidates:
                if isinstance(candidate, dict):
                    for mkey, mvalue in candidate.items():
                        result.setdefault(mkey, mvalue)
            return
        result[key] = self._parse_value(rest, indent, lineno, key)

    def _parse_value(self, rest: str, indent: int, lineno: int, key: str):
        text = _strip_comment(rest).strip() if rest else ""
        if text == "":
            return self._parse_block_value(indent)
        first = text[0]
        if first in "|>":
            return self._read_block_scalar(text, indent, lineno)
        if first == "&":
            parts = text.split(None, 1)
            name = parts[0][1:]
            if not name:
                raise YamlError("empty anchor name", lineno)
            value = self._parse_block_value(indent) if len(parts) == 1 else self._parse_value(parts[1], indent, lineno, key)
            self.anchors[name] = value
            return value
        if first == "*":
            name = text[1:].strip()
            if name not in self.anchors:
                raise YamlError("unknown alias *%s" % name, lineno)
            return copy.deepcopy(self.anchors[name])
        if first == "!":
            parts = text.split(None, 1)
            tag = parts[0]
            value = self._parse_block_value(indent) if len(parts) == 1 else self._parse_value(parts[1], indent, lineno, key)
            return self._apply_tag(tag, value, lineno)
        if first in "[{":
            return _FlowParser(self._gather_flow(text, lineno), lineno).parse()
        return _scalar(text, lineno)

    @staticmethod
    def _apply_tag(tag: str, value, lineno: int):
        if tag == "!!str":
            return "" if value is None else str(value)
        if tag == "!!int":
            return int(value)
        if tag == "!!float":
            return float(value)
        if tag == "!!bool":
            return str(value).strip().lower() in ("true", "yes", "on", "1")
        if tag == "!!null":
            return None
        raise YamlError("unsupported YAML tag %s" % tag, lineno)

    def _gather_flow(self, text: str, lineno: int) -> str:
        buffer = text
        guard = 0
        while _flow_balance(buffer) > 0:
            guard += 1
            if guard > 1000 or self.i >= len(self.raw):
                raise YamlError("unterminated flow collection", lineno)
            nxt = self.raw[self.i]
            self.i += 1
            buffer = buffer + " " + _strip_comment(nxt).strip()
        return buffer

    def _parse_block_value(self, indent: int):
        token = self._peek()
        if token is None:
            return None
        ind, content, _nxt, _lineno = token
        if ind > indent:
            return self._parse_node(ind)
        if ind == indent and (content == "-" or content.startswith("- ")):
            return self._parse_sequence(ind)
        return None

    def _read_block_scalar(self, header: str, indent: int, lineno: int) -> str:
        style = header[0]
        rest = header[1:].strip()
        chomp = "clip"
        explicit = None
        for ch in rest:
            if ch == "-":
                chomp = "strip"
            elif ch == "+":
                chomp = "keep"
            elif ch.isdigit():
                explicit = int(ch)
            elif ch in " \t":
                continue
            else:
                raise YamlError("invalid block scalar header %r" % header, lineno)
        lines: list[str] = []
        j = self.i
        content_indent = indent + explicit if explicit is not None else None
        while j < len(self.raw):
            line = self.raw[j]
            if line.strip() == "":
                if j == len(self.raw) - 1 and self._ends_with_newline:
                    break  # the trailing '' produced by splitting on '\n'
                lines.append("")
                j += 1
                continue
            ind = len(line) - len(line.lstrip(" "))
            if ind <= indent:
                break
            if content_indent is None:
                content_indent = ind
            if ind < content_indent:
                break
            lines.append(line[content_indent:])
            j += 1
        # Whether the last content line was terminated by a line break, which
        # decides how much trailing whitespace the chomping indicator keeps.
        terminated = j < len(self.raw) or self._ends_with_newline
        while lines and lines[-1] == "" and chomp != "keep":
            lines.pop()
        self.i = j
        if not lines:
            return ""
        body = "\n".join(lines) if style == "|" else _fold(lines)
        if chomp == "strip":
            return body.rstrip("\n")
        return body + "\n" if terminated else body


def _scalar(text: str, lineno: int):
    if text and text[0] in "\"'":
        value, index = _read_quoted(text, 0, lineno)
        tail = text[index:].strip()
        if tail:
            raise YamlError("unexpected content after quoted scalar: %r" % tail[:24], lineno)
        return value
    return _plain_scalar(text)


def load(text: str):
    """Parse one YAML document from ``text``.  Raises :class:`YamlError`."""
    if not isinstance(text, str):
        raise YamlError("expected str input, got %s" % type(text).__name__)
    return _Parser(text).parse_document()


def load_file(path: str):
    """Parse one YAML document from ``path`` (UTF-8, BOM tolerant)."""
    with open(path, "r", encoding="utf-8-sig") as handle:
        return load(handle.read())


safe_load = load
