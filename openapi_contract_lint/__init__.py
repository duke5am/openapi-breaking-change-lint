"""openapi-breaking-change-lint: diff two OpenAPI 3.x documents and lint the new one.

:mod:`openapi_contract_lint.contractdiff` classifies every change as breaking,
potentially breaking or compatible; :mod:`openapi_contract_lint.designlint`
checks the new document against published API design guidance;
:mod:`openapi_contract_lint.cli` is the command line, and
:mod:`openapi_contract_lint.miniyaml` is the built-in YAML reader used when
PyYAML is not installed.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
