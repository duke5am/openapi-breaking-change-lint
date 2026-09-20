#!/usr/bin/env python3
"""openapi-contract-diff - a CI gate for OpenAPI 3.x contract changes.

This is the command-line layer of the ``openapi-breaking-change-lint``
package; the diff engine is :mod:`openapi_contract_lint.contractdiff` and the
design lint :mod:`openapi_contract_lint.designlint`.

Compares two OpenAPI documents and classifies every change as breaking,
potentially breaking or compatible, then optionally lints the new document
against published API design guidance.

Usage:
    python3 openapi_contract_diff.py old.yaml new.yaml [options]

Exit codes:
    0   no changes at or above the --fail-on threshold (and no lint errors
        when --fail-on-lint is given)
    1   breaking changes found
    2   usage error, unreadable file, or a document that could not be parsed
"""

from __future__ import annotations

import argparse
import json
import sys

from openapi_contract_lint import miniyaml
from openapi_contract_lint.contractdiff import (
    BREAKING,
    FAIL_ON_CHOICES,
    POTENTIALLY_BREAKING,
    COMPATIBLE,
    diff_documents,
)
from openapi_contract_lint.designlint import lint_document, lint_summary

__version__ = "1.0.0"

EXIT_OK = 0
EXIT_BREAKING = 1
EXIT_USAGE = 2

_MARKERS = {BREAKING: "x", POTENTIALLY_BREAKING: "~", COMPATIBLE: "+"}
_HEADINGS = {
    BREAKING: "BREAKING",
    POTENTIALLY_BREAKING: "POTENTIALLY BREAKING",
    COMPATIBLE: "COMPATIBLE",
}


class UsageError(Exception):
    """Anything that should exit with code 2."""


def _pyyaml():
    try:
        import yaml  # noqa: F401
    except Exception:
        return None
    return yaml


def load_document(path: str, backend: str = "auto"):
    """Load one OpenAPI document.  Returns ``(document, description)``."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise UsageError("cannot read %s: no such file" % path)
    except IsADirectoryError:
        raise UsageError("cannot read %s: it is a directory" % path)
    except OSError as error:
        raise UsageError("cannot read %s: %s" % (path, error))
    except UnicodeDecodeError as error:
        raise UsageError("cannot read %s: not UTF-8 text (%s)" % (path, error))

    if backend == "pyyaml":
        yaml = _pyyaml()
        if yaml is None:
            raise UsageError("--yaml-backend pyyaml was requested but PyYAML is not installed")
        return _parse_with_pyyaml(text, path, yaml)

    if backend == "builtin":
        return _parse_with_miniyaml(text, path)

    json_error = None
    try:
        return json.loads(text), "JSON"
    except ValueError as error:
        json_error = error

    yaml = _pyyaml()
    if yaml is not None:
        return _parse_with_pyyaml(text, path, yaml)
    try:
        return _parse_with_miniyaml(text, path)
    except UsageError as builtin_error:
        raise UsageError(
            "%s is not valid JSON (%s), and PyYAML is not installed so the built-in "
            "subset reader was used instead: %s" % (path, json_error, builtin_error)
        )


def _parse_with_pyyaml(text: str, path: str, yaml):
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise UsageError("%s is not valid YAML: %s" % (path, str(error).replace("\n", " ")))
    if document is None:
        raise UsageError("%s is empty" % path)
    if not isinstance(document, dict):
        raise UsageError("%s does not contain an OpenAPI document (root is %s, expected a mapping)"
                         % (path, type(document).__name__))
    return document, "YAML via PyYAML %s" % getattr(yaml, "__version__", "?")


def _parse_with_miniyaml(text: str, path: str):
    try:
        document = miniyaml.load(text)
    except miniyaml.YamlError as error:
        raise UsageError(
            "%s could not be parsed by the built-in YAML reader: %s. "
            "Install PyYAML for full YAML support, or convert the document to JSON (%s)"
            % (path, error, miniyaml.LIMITATIONS)
        )
    if document is None:
        raise UsageError("%s is empty" % path)
    if not isinstance(document, dict):
        raise UsageError("%s does not contain an OpenAPI document (root is %s, expected a mapping)"
                         % (path, type(document).__name__))
    return document, "YAML via the built-in subset reader (no PyYAML installed)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openapi-contract-diff",
        description=(
            "Diff two OpenAPI 3.x documents and classify every change as breaking, "
            "potentially breaking or compatible. Use it as a CI gate."
        ),
        epilog=(
            "exit codes: 0 = no changes at or above the threshold, 1 = breaking changes found, "
            "2 = usage or parse error"
        ),
    )
    parser.add_argument("old", help="the baseline (currently deployed) OpenAPI document")
    parser.add_argument("new", help="the candidate (proposed) OpenAPI document")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON report instead of text",
    )
    parser.add_argument(
        "--fail-on",
        choices=list(FAIL_ON_CHOICES),
        default=BREAKING,
        help="lowest severity that makes the command exit 1 (default: breaking)",
    )
    parser.add_argument(
        "--base-path",
        default=None,
        metavar="PREFIX",
        help="strip this prefix from every path before comparing, e.g. --base-path /v1",
    )
    parser.add_argument(
        "--yaml-backend",
        choices=("auto", "pyyaml", "builtin"),
        default="auto",
        help="which YAML reader to use (default: auto - JSON, then PyYAML, then the built-in reader)",
    )
    parser.add_argument(
        "--no-lint",
        dest="lint",
        action="store_false",
        help="skip the design lint half and report only the contract diff",
    )
    parser.add_argument(
        "--fail-on-lint",
        action="store_true",
        help="also exit 1 when the design lint reports an 'error' level issue",
    )
    parser.add_argument("--version", action="version", version="openapi-contract-diff %s" % __version__)
    return parser


def render_text(payload: dict) -> str:
    lines: list = []
    lines.append("openapi-contract-diff %s" % payload["version"])
    for side in ("old", "new"):
        info = payload[side]
        lines.append(
            "  %s: %s  [OpenAPI %s, %d operations, read as %s]"
            % (side, info["path"], info["openapi"] or "?", info["operations"], info["format"])
        )
    lines.append("")

    for severity in (BREAKING, POTENTIALLY_BREAKING, COMPATIBLE):
        findings = [f for f in payload["findings"] if f["severity"] == severity]
        if not findings:
            continue
        lines.append("%s (%d)" % (_HEADINGS[severity], len(findings)))
        for finding in findings:
            lines.append(
                "  %s %s  at %s" % (_MARKERS[severity], finding["kind"], finding["location"])
            )
            lines.append("      %s" % finding["message"])
            if finding.get("note"):
                lines.append("      note: %s" % finding["note"])
        lines.append("")

    if not payload["findings"]:
        lines.append("No contract changes detected.")
        lines.append("")

    if payload.get("lint") is not None:
        issues = payload["lint"]
        if issues:
            lines.append("DESIGN LINT (%d)" % len(issues))
            for issue in issues:
                lines.append(
                    "  %-7s %s  at %s" % (issue["severity"], issue["rule"], issue["location"])
                )
                lines.append("      %s" % issue["message"])
                if issue.get("guidance"):
                    lines.append("      guidance: %s" % issue["guidance"])
            lines.append("")
        else:
            lines.append("DESIGN LINT: no issues.")
            lines.append("")

    if payload["warnings"]:
        lines.append("WARNINGS")
        for warning in payload["warnings"]:
            lines.append("  ! %s" % warning)
        lines.append("")

    summary = payload["summary"]
    lines.append("SUMMARY")
    lines.append("  breaking: %d" % summary["breaking"])
    lines.append("  potentially breaking: %d" % summary["potentially_breaking"])
    lines.append("  compatible: %d" % summary["compatible"])
    if payload.get("lint_summary") is not None:
        lines.append(
            "  design lint: %d (%d error, %d warning, %d info)"
            % (
                payload["lint_summary"]["total"],
                payload["lint_summary"]["error"],
                payload["lint_summary"]["warning"],
                payload["lint_summary"]["info"],
            )
        )
    lines.append("  fail-on threshold: %s" % payload["threshold"])
    lines.append("RESULT: %s" % payload["result_line"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        old_document, old_format = load_document(args.old, args.yaml_backend)
        new_document, new_format = load_document(args.new, args.yaml_backend)
    except UsageError as error:
        sys.stderr.write("openapi-contract-diff: error: %s\n" % error)
        return EXIT_USAGE

    result = diff_documents(old_document, new_document, args.base_path)
    lint_issues = lint_document(new_document, args.base_path) if args.lint else None

    exit_code = result.exit_code(args.fail_on)
    if exit_code == 0 and args.fail_on_lint and lint_issues:
        if any(issue.severity == "error" for issue in lint_issues):
            exit_code = EXIT_BREAKING

    if exit_code == 0:
        result_line = "PASS (nothing at or above the '%s' threshold)" % args.fail_on
    else:
        if result.exit_code(args.fail_on) == EXIT_BREAKING:
            result_line = "FAIL (%d breaking change(s) at the '%s' threshold)" % (
                result.breaking,
                args.fail_on,
            )
        else:
            result_line = "FAIL (lint errors, --fail-on-lint)"

    payload = {
        "tool": "openapi-contract-diff",
        "version": __version__,
        "old": {
            "path": args.old,
            "format": old_format,
            "openapi": result.old_openapi,
            "operations": result.old_operation_count,
        },
        "new": {
            "path": args.new,
            "format": new_format,
            "openapi": result.new_openapi,
            "operations": result.new_operation_count,
        },
        "base_path": args.base_path,
        "threshold": args.fail_on,
        "summary": result.summary(),
        "findings": [finding.as_dict() for finding in result.findings],
        "warnings": list(result.warnings),
        "exit_code": exit_code,
        "result_line": result_line,
    }
    if lint_issues is not None:
        payload["lint"] = [issue.as_dict() for issue in lint_issues]
        payload["lint_summary"] = lint_summary(lint_issues)

    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=False, default=str) + "\n")
    else:
        sys.stdout.write(render_text(payload) + "\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
