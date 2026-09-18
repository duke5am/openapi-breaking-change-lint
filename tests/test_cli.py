"""End-to-end tests for the command line interface: exit codes, output, options.

Every test calls ``main()`` in-process and captures stdout/stderr, so the exit
codes asserted here are exactly the ones a CI job would see.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from openapi_contract_diff import EXIT_BREAKING, EXIT_OK, EXIT_USAGE, main  # noqa: E402

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

FIXTURES = os.path.join(ROOT, "fixtures")
RULES = os.path.join(FIXTURES, "rules")
SCRATCH = os.path.join(ROOT, ".test-scratch")


def fixture(name):
    return os.path.join(FIXTURES, name)


def rule(name, side):
    return os.path.join(RULES, name, "%s.yaml" % side)


def run(*arguments):
    """Run the CLI in-process; return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(arguments))
    return code, out.getvalue(), err.getvalue()


class CliTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(SCRATCH, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def write_scratch(self, name, text):
        path = os.path.join(SCRATCH, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        self.assertTrue(os.path.exists(path), "scratch file was not written")
        return path


class ExitCodeTests(CliTestCase):
    def test_identical_documents_exit_zero(self):
        code, output, _err = run(fixture("identical-a.yaml"), fixture("identical-b.yaml"))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("No contract changes detected.", output)
        self.assertIn("RESULT: PASS", output)

    def test_breaking_changes_exit_one(self):
        code, output, _err = run(fixture("petstore-v1.yaml"), fixture("petstore-v2.yaml"))
        self.assertEqual(code, EXIT_BREAKING)
        self.assertIn("RESULT: FAIL", output)
        self.assertIn("BREAKING (14)", output)

    def test_additive_change_exits_zero(self):
        code, output, _err = run(fixture("additive-old.yaml"), fixture("additive-new.yaml"))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("breaking: 0", output)

    def test_never_threshold_always_exits_zero(self):
        code, output, _err = run(fixture("petstore-v1.yaml"), fixture("petstore-v2.yaml"), "--fail-on", "never")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("RESULT: PASS", output)

    def test_potentially_breaking_threshold(self):
        arguments = (rule("default-changed", "old"), rule("default-changed", "new"))
        code, _output, _err = run(*arguments)
        self.assertEqual(code, EXIT_OK)
        code, output, _err = run(*arguments, "--fail-on", "potentially-breaking")
        self.assertEqual(code, EXIT_BREAKING)
        self.assertIn("POTENTIALLY BREAKING (1)", output)

    def test_fail_on_lint(self):
        arguments = (fixture("identical-a.yaml"), fixture("lint-messy.yaml"),
                     "--fail-on", "never")
        code, _output, _err = run(*arguments)
        self.assertEqual(code, EXIT_OK)
        code, output, _err = run(*arguments, "--fail-on-lint")
        self.assertEqual(code, EXIT_BREAKING)
        self.assertIn("lint errors", output)


class UsageErrorTests(CliTestCase):
    def test_missing_file_exits_two(self):
        code, _output, error = run(fixture("does-not-exist.yaml"), fixture("identical-a.yaml"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no such file", error)

    def test_unparseable_document_exits_two(self):
        broken = self.write_scratch("broken.yaml", "openapi: 3.0.3\npaths:\n  /a: [unclosed\n")
        code, _output, error = run(broken, fixture("identical-a.yaml"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertTrue("could not be parsed" in error or "not valid YAML" in error, error)

    def test_empty_document_exits_two(self):
        empty = self.write_scratch("empty.yaml", "")
        code, _output, error = run(empty, fixture("identical-a.yaml"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("empty", error)

    def test_non_mapping_document_exits_two(self):
        listing = self.write_scratch("list.yaml", "- one\n- two\n")
        code, _output, error = run(listing, fixture("identical-a.yaml"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("expected a mapping", error)

    def test_unknown_threshold_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            run(fixture("identical-a.yaml"), fixture("identical-b.yaml"), "--fail-on", "sometimes")
        self.assertEqual(caught.exception.code, EXIT_USAGE)

    def test_missing_arguments_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            run(fixture("identical-a.yaml"))
        self.assertEqual(caught.exception.code, EXIT_USAGE)

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as caught:
            run("--version")
        self.assertEqual(caught.exception.code, EXIT_OK)

    def test_pyyaml_backend_without_pyyaml_is_reported(self):
        if yaml is not None:
            code, output, _err = run(fixture("identical-a.yaml"), fixture("identical-b.yaml"),
                                     "--yaml-backend", "pyyaml")
            self.assertEqual(code, EXIT_OK)
            self.assertIn("PyYAML", output)
        else:  # pragma: no cover - only on a machine without PyYAML
            code, _output, error = run(fixture("identical-a.yaml"), fixture("identical-b.yaml"),
                                       "--yaml-backend", "pyyaml")
            self.assertEqual(code, EXIT_USAGE)
            self.assertIn("PyYAML is not installed", error)


class OutputTests(CliTestCase):
    def test_json_output_structure(self):
        code, output, _err = run(fixture("petstore-v1.yaml"), fixture("petstore-v2.yaml"), "--json")
        self.assertEqual(code, EXIT_BREAKING)
        payload = json.loads(output)
        self.assertEqual(payload["tool"], "openapi-contract-diff")
        self.assertEqual(payload["exit_code"], EXIT_BREAKING)
        self.assertEqual(payload["threshold"], "breaking")
        self.assertEqual(payload["summary"]["breaking"], 14)
        self.assertEqual(payload["old"]["operations"], 6)
        self.assertTrue(payload["findings"])
        for finding in payload["findings"]:
            self.assertIn(finding["severity"], ("breaking", "potentially-breaking", "compatible"))
            self.assertTrue(finding["kind"])
            self.assertTrue(finding["location"])
            self.assertTrue(finding["message"])
        self.assertIn("lint", payload)
        self.assertIn("lint_summary", payload)

    def test_json_is_valid_for_the_clean_control(self):
        code, output, _err = run(fixture("identical-a.yaml"), fixture("identical-c.json"), "--json")
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["exit_code"], EXIT_OK)

    def test_no_lint_omits_the_lint_section(self):
        code, output, _err = run(fixture("petstore-v1.yaml"), fixture("petstore-v2.yaml"), "--json", "--no-lint")
        self.assertEqual(code, EXIT_BREAKING)
        payload = json.loads(output)
        self.assertNotIn("lint", payload)
        self.assertNotIn("lint_summary", payload)

    def test_text_output_shows_locations_and_notes(self):
        _code, output, _err = run(fixture("petstore-v1.yaml"), fixture("petstore-v2.yaml"))
        self.assertIn("components.schemas.Pet.properties.tag", output)
        self.assertIn("possibly renamed to 'label'", output)
        self.assertIn("parameter 'limit' moved from query to header", output)

    def test_lint_section_cites_guidance(self):
        _code, output, _err = run(fixture("identical-a.yaml"), fixture("lint-messy.yaml"))
        self.assertIn("DESIGN LINT", output)
        self.assertIn("guidance:", output)
        self.assertIn("Zalando", output)

    def test_warnings_are_printed(self):
        old = os.path.join(RULES, "external-ref-not-fetched", "old.yaml")
        new = os.path.join(RULES, "external-ref-not-fetched", "new.yaml")
        code, output, _err = run(old, new)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("WARNINGS", output)
        self.assertIn("external $ref", output)

    def test_report_names_the_reader_used(self):
        _code, output, _err = run(fixture("identical-a.yaml"), fixture("identical-b.yaml"))
        self.assertIn("read as", output)
        self.assertIn("OpenAPI 3.0.3", output)

    def test_builtin_backend_reads_json(self):
        code, output, _err = run(fixture("identical-a.yaml"), fixture("identical-c.json"),
                                 "--yaml-backend", "builtin")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("built-in subset reader", output)

    def test_builtin_backend_matches_the_default_backend(self):
        arguments = (fixture("petstore-v1.yaml"), fixture("petstore-v2.yaml"), "--json", "--no-lint")
        _code, default_output, _err = run(*arguments)
        _code, builtin_output, _err = run(*arguments, "--yaml-backend", "builtin")
        self.assertEqual(json.loads(default_output)["findings"], json.loads(builtin_output)["findings"])


class BasePathCliTests(CliTestCase):
    def setUp(self):
        self.old = self.write_scratch("based-old.yaml", (
            "openapi: 3.0.3\n"
            "info:\n  title: T\n  version: '1'\n"
            "paths:\n"
            "  /v2/pets:\n"
            "    get:\n"
            "      operationId: listPets\n"
            "      summary: List pets\n"
            "      parameters:\n"
            "        - name: limit\n"
            "          in: query\n"
            "          required: false\n"
            "          schema: {type: integer}\n"
            "      responses:\n"
            "        '200': {description: ok}\n"
            "        '400': {description: bad}\n"
        ))
        self.new = self.write_scratch("based-new.yaml", (
            "openapi: 3.0.3\n"
            "info:\n  title: T\n  version: '2'\n"
            "paths:\n"
            "  /pets:\n"
            "    get:\n"
            "      operationId: listPets\n"
            "      summary: List pets\n"
            "      parameters:\n"
            "        - name: limit\n"
            "          in: query\n"
            "          required: false\n"
            "          schema: {type: integer}\n"
            "      responses:\n"
            "        '200': {description: ok}\n"
            "        '400': {description: bad}\n"
        ))

    def test_without_base_path_the_prefix_looks_like_a_removal(self):
        code, output, _err = run(self.old, self.new, "--no-lint")
        self.assertEqual(code, EXIT_BREAKING)
        self.assertIn("endpoint-removed", output)

    def test_base_path_makes_the_documents_comparable(self):
        code, output, _err = run(self.old, self.new, "--base-path", "/v2", "--no-lint")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("No contract changes detected.", output)


if __name__ == "__main__":
    unittest.main()
