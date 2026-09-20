#!/usr/bin/env python3
"""openapi-contract-diff - a CI gate for OpenAPI 3.x contract changes.

This wrapper exists so `python3 openapi_contract_diff.py old.yaml new.yaml`
keeps working from a clone. The same CLI is installed as the
`openapi-breaking-change-lint` console script; the implementation lives in
`openapi_contract_lint/cli.py` so that the installed package and the checkout
are the same code, not two versions of it.

Usage:
    python3 openapi_contract_diff.py old.yaml new.yaml [options]

Exit codes:
    0   no changes at or above the --fail-on threshold (and no lint errors
        when --fail-on-lint is given)
    1   breaking changes found
    2   usage error, unreadable file, or a document that could not be parsed
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openapi_contract_lint.cli import *  # noqa: E402,F401,F403
from openapi_contract_lint.cli import (  # noqa: E402
    EXIT_BREAKING,
    EXIT_OK,
    EXIT_USAGE,
    UsageError,
    build_parser,
    load_document,
    main,
    render_text,
)

__all__ = [
    "EXIT_BREAKING",
    "EXIT_OK",
    "EXIT_USAGE",
    "UsageError",
    "build_parser",
    "load_document",
    "main",
    "render_text",
]

if __name__ == "__main__":
    sys.exit(main())
