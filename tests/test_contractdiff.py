"""Tests for the contract diff engine: one fixture pair per classification rule,
plus the negative controls and the classification-independent behaviour.
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import miniyaml  # noqa: E402
from contractdiff import (  # noqa: E402
    BREAKING,
    COMPATIBLE,
    POTENTIALLY_BREAKING,
    DiffResult,
    Finding,
    diff_documents,
    resolve_pointer,
)

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

FIXTURES = os.path.join(ROOT, "fixtures")
RULES = os.path.join(FIXTURES, "rules")


def load(path, backend="miniyaml"):
    with open(path, "r", encoding="utf-8-sig") as handle:
        text = handle.read()
    if backend == "pyyaml":
        if yaml is None:  # pragma: no cover
            raise unittest.SkipTest("PyYAML is not installed")
        return yaml.safe_load(text)
    return miniyaml.load(text)


def load_pair(case, backend="miniyaml"):
    directory = os.path.join(RULES, case)
    return (
        load(os.path.join(directory, "old.yaml"), backend),
        load(os.path.join(directory, "new.yaml"), backend),
    )


def expectations(case):
    with open(os.path.join(RULES, case, "expect.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def fingerprint(result):
    """Multiset of (severity, kind) pairs, so over-reporting is visible."""
    return sorted("%s:%s" % (finding.severity, finding.kind) for finding in result.findings)


def rule_cases():
    return sorted(name for name in os.listdir(RULES) if os.path.isdir(os.path.join(RULES, name)))


def spec(paths=None, schemas=None, **extra):
    document = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1", "description": "test"},
        "paths": paths or {},
    }
    if schemas:
        document["components"] = {"schemas": schemas}
    document.update(extra)
    return document


def spec_with_ref(schemas, ref="#/components/schemas/Thing"):
    """A spec whose single operation response references one component schema."""
    paths = {"/things": {"get": {
        "operationId": "listThings",
        "summary": "An operation",
        "responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"$ref": ref}}}}},
    }}}
    return spec(paths, schemas)


def ok_operation(**extra):
    operation = {
        "operationId": "op",
        "summary": "An operation",
        "responses": {"200": {"description": "ok"}},
    }
    operation.update(extra)
    return operation


class RuleFixtureTests(unittest.TestCase):
    """One fixture pair per classification rule; expectations live beside them."""

    def test_rule_case_count(self):
        self.assertGreaterEqual(len(rule_cases()), 60, "expected at least 60 rule fixtures")

    def test_every_rule_fixture_matches_its_expectation(self):
        for case in rule_cases():
            expected = expectations(case)
            want = sorted("%s:%s" % (severity, kind) for severity, kind in expected["findings"])
            with self.subTest(case=case, backend="builtin"):
                old, new = load_pair(case, "miniyaml")
                self.assertEqual(fingerprint(diff_documents(old, new)), want)
            if yaml is not None:
                with self.subTest(case=case, backend="pyyaml"):
                    old, new = load_pair(case, "pyyaml")
                    self.assertEqual(fingerprint(diff_documents(old, new)), want)

    def test_expected_notes_are_present(self):
        for case in rule_cases():
            notes = expectations(case).get("notes_contain") or []
            if not notes:
                continue
            with self.subTest(case=case):
                old, new = load_pair(case)
                result = diff_documents(old, new)
                for fragment in notes:
                    self.assertTrue(
                        any(fragment in (finding.note or "") for finding in result.findings),
                        "%s: no finding carried a note containing %r" % (case, fragment),
                    )

    def test_expected_warnings_are_present(self):
        for case in rule_cases():
            warnings = expectations(case).get("warnings_contain") or []
            if not warnings:
                continue
            with self.subTest(case=case):
                old, new = load_pair(case)
                result = diff_documents(old, new)
                for fragment in warnings:
                    self.assertTrue(
                        any(fragment in warning for warning in result.warnings),
                        "%s: no warning contained %r (got %r)" % (case, fragment, result.warnings),
                    )

    def test_required_classifications(self):
        """The classifications the tool exists for, asserted directly by name."""
        required = {
            "endpoint-path-removed": (BREAKING, "endpoint-removed"),
            "endpoint-operation-removed": (BREAKING, "operation-removed"),
            "response-property-removed": (BREAKING, "response-property-removed"),
            "response-property-renamed": (BREAKING, "response-property-removed"),
            "request-property-required-added": (BREAKING, "request-property-required-added"),
            "response-property-became-required": (BREAKING, "property-became-required"),
            "request-property-became-required": (BREAKING, "property-became-required"),
            "enum-value-removed": (BREAKING, "enum-value-removed"),
            "enum-value-added": (COMPATIBLE, "enum-value-added"),
            "type-changed": (BREAKING, "type-changed"),
            "format-changed": (BREAKING, "format-changed"),
            "parameter-required-added": (BREAKING, "parameter-required-added"),
            "parameter-became-required": (BREAKING, "parameter-became-required"),
            "response-status-removed": (BREAKING, "response-status-removed"),
            "response-status-added": (COMPATIBLE, "response-status-added"),
            "nullable-removed": (BREAKING, "nullable-removed"),
            "endpoint-added": (COMPATIBLE, "endpoint-added"),
            "response-property-added-optional": (COMPATIBLE, "property-added"),
            "additional-properties-forbidden": (BREAKING, "additional-properties-forbidden"),
            "default-changed": (POTENTIALLY_BREAKING, "default-changed"),
            "security-scheme-removed": (BREAKING, "security-scheme-removed"),
            "security-scheme-changed": (BREAKING, "security-scheme-changed"),
            "parameter-location-changed": (BREAKING, "parameter-location-changed"),
        }
        for case, (severity, kind) in sorted(required.items()):
            with self.subTest(case=case):
                old, new = load_pair(case)
                pairs = fingerprint(diff_documents(old, new))
                self.assertIn("%s:%s" % (severity, kind), pairs)


class NegativeControlTests(unittest.TestCase):
    """The two controls that must never regress: no diff when nothing changed,
    and no breaking change when everything is additive."""

    def test_identical_documents_produce_zero_findings(self):
        variants = ["identical-a.yaml", "identical-b.yaml", "identical-c.json"]
        for backend in ("miniyaml", "pyyaml"):
            if backend == "pyyaml" and yaml is None:
                continue
            documents = {name: load(os.path.join(FIXTURES, name), backend) for name in variants}
            for left in variants:
                for right in variants:
                    with self.subTest(left=left, right=right, backend=backend):
                        result = diff_documents(documents[left], documents[right])
                        self.assertEqual(result.findings, [])
                        self.assertEqual(result.breaking, 0)
                        self.assertEqual(result.exit_code(BREAKING), 0)

    def test_a_document_against_itself_is_empty(self):
        document = load(os.path.join(FIXTURES, "petstore-v1.yaml"))
        self.assertEqual(diff_documents(document, document).findings, [])

    def test_purely_additive_change_has_no_breaking_changes(self):
        for backend in ("miniyaml", "pyyaml"):
            if backend == "pyyaml" and yaml is None:
                continue
            with self.subTest(backend=backend):
                old = load(os.path.join(FIXTURES, "additive-old.yaml"), backend)
                new = load(os.path.join(FIXTURES, "additive-new.yaml"), backend)
                result = diff_documents(old, new)
                self.assertEqual(result.breaking, 0)
                self.assertEqual(result.potentially_breaking, 0)
                self.assertGreaterEqual(result.compatible, 5)
                self.assertEqual(result.exit_code(BREAKING), 0)
                self.assertEqual(result.exit_code(POTENTIALLY_BREAKING), 0)

    def test_additive_case_only_uses_compatible_kinds(self):
        old = load(os.path.join(FIXTURES, "additive-old.yaml"))
        new = load(os.path.join(FIXTURES, "additive-new.yaml"))
        kinds = {finding.kind for finding in diff_documents(old, new).findings}
        self.assertTrue(kinds <= {"property-added", "response-property-added", "enum-value-added",
                                 "parameter-added-optional", "content-type-added", "response-status-added",
                                 "operation-added", "response-header-added", "endpoint-added"})


class ThresholdTests(unittest.TestCase):
    def test_exit_codes_follow_the_threshold(self):
        result = DiffResult(findings=[
            Finding(BREAKING, "k", "l", "m"),
            Finding(POTENTIALLY_BREAKING, "k", "l", "m"),
            Finding(COMPATIBLE, "k", "l", "m"),
        ])
        self.assertEqual(result.exit_code(BREAKING), 1)
        self.assertEqual(result.exit_code(POTENTIALLY_BREAKING), 1)
        self.assertEqual(result.exit_code("never"), 0)

    def test_potentially_breaking_only_passes_a_breaking_threshold(self):
        result = DiffResult(findings=[Finding(POTENTIALLY_BREAKING, "k", "l", "m")])
        self.assertEqual(result.exit_code(BREAKING), 0)
        self.assertEqual(result.exit_code(POTENTIALLY_BREAKING), 1)

    def test_compatible_changes_never_fail(self):
        result = DiffResult(findings=[Finding(COMPATIBLE, "k", "l", "m")])
        for threshold in ("breaking", "potentially-breaking", "never"):
            self.assertEqual(result.exit_code(threshold), 0)

    def test_summary_counts(self):
        result = DiffResult(findings=[
            Finding(BREAKING, "k", "l", "m"),
            Finding(BREAKING, "k2", "l", "m"),
            Finding(COMPATIBLE, "k3", "l", "m"),
        ])
        self.assertEqual(result.summary(), {
            "breaking": 2, "potentially_breaking": 0, "compatible": 1, "total": 3,
        })


class BasePathTests(unittest.TestCase):
    def setUp(self):
        self.old = spec({"/v1/pets": {"get": ok_operation()}})
        self.new = spec({"/pets": {"get": ok_operation()}})

    def test_prefix_difference_is_reported_without_base_path(self):
        result = diff_documents(self.old, self.new)
        self.assertEqual(fingerprint(result), ["breaking:endpoint-removed", "compatible:endpoint-added"])

    def test_base_path_strips_the_prefix(self):
        result = diff_documents(self.old, self.new, base_path="/v1")
        self.assertEqual(result.findings, [])

    def test_base_path_without_leading_slash_is_accepted(self):
        self.assertEqual(diff_documents(self.old, self.new, base_path="v1").findings, [])

    def test_unused_base_path_warns(self):
        result = diff_documents(self.old, self.new, base_path="/v9")
        self.assertTrue(any("matched no path" in warning for warning in result.warnings))

    def test_base_path_does_not_hide_real_changes(self):
        new = spec({"/pets": {"get": ok_operation(), "delete": ok_operation()}})
        result = diff_documents(self.old, new, base_path="/v1")
        self.assertEqual(fingerprint(result), ["compatible:operation-added"])


class BehaviourTests(unittest.TestCase):
    def test_shared_component_is_reported_once(self):
        schema = {"type": "object", "description": "d", "properties": {"a": {"type": "string"},
                                                                      "b": {"type": "string"}}}
        changed = {"type": "object", "description": "d", "properties": {"a": {"type": "string"}}}
        paths = {}
        for suffix in ("one", "two", "three"):
            paths["/%s" % suffix] = {"get": {
                "operationId": "get%s" % suffix,
                "summary": "s",
                "responses": {"200": {"description": "ok", "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}}},
            }}
        old = spec(paths, {"Thing": schema})
        new = spec(paths, {"Thing": changed})
        result = diff_documents(old, new)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].kind, "response-property-removed")
        self.assertEqual(result.findings[0].location, "components.schemas.Thing.properties.b")

    def test_recursive_schema_terminates_and_is_still_diffed(self):
        recursive_old = {
            "type": "object",
            "description": "A node.",
            "properties": {"children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}}},
        }
        recursive_new = {
            "type": "object",
            "description": "A node.",
            "properties": {
                "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
                "label": {"type": "string"},
            },
        }
        paths = {"/nodes": {"get": {
            "operationId": "listNodes", "summary": "s",
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}},
        }}}
        result = diff_documents(spec(paths, {"Node": recursive_old}), spec(paths, {"Node": recursive_new}))
        self.assertEqual(fingerprint(result), ["compatible:property-added"])

    def test_external_ref_is_reported_as_a_warning_not_a_finding(self):
        schema = {"type": "object", "description": "d",
                  "properties": {"detail": {"$ref": "common.yaml#/components/schemas/Error"}}}
        paths = {"/things": {"get": {
            "operationId": "listThings", "summary": "s",
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}}},
        }}}
        old = spec(paths, {"Thing": schema})
        new = spec(paths, {"Thing": json.loads(json.dumps(schema))})
        result = diff_documents(old, new)
        self.assertEqual(result.findings, [])
        self.assertTrue(any("external $ref" in warning for warning in result.warnings))
        self.assertTrue(any("common.yaml#/components/schemas/Error" in warning for warning in result.warnings))

    def test_dangling_ref_in_the_new_document_is_breaking(self):
        paths = {"/things": {"get": {
            "operationId": "listThings", "summary": "s",
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}}},
        }}}
        old = spec(paths, {"Thing": {"type": "object", "description": "d"}})
        new = spec(paths, {})
        result = diff_documents(old, new)
        self.assertEqual(fingerprint(result), ["breaking:schema-unresolvable"])

    def test_dangling_ref_in_the_old_document_warns_only(self):
        paths = {"/things": {"get": {
            "operationId": "listThings", "summary": "s",
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}}},
        }}}
        old = spec(paths, {})
        new = spec(paths, {"Thing": {"type": "object", "description": "d"}})
        result = diff_documents(old, new)
        self.assertEqual(result.findings, [])
        self.assertTrue(any("does not exist" in warning for warning in result.warnings))

    def test_openapi_31_null_type_arrays(self):
        old = spec_with_ref({"Thing": {"type": ["string", "null"], "description": "d"}})
        new = spec_with_ref({"Thing": {"type": "string", "description": "d"}})
        result = diff_documents(old, new)
        self.assertEqual(fingerprint(result), ["breaking:nullable-removed"])

    def test_openapi_31_null_type_added(self):
        old = spec_with_ref({"Thing": {"type": "string", "description": "d"}})
        new = spec_with_ref({"Thing": {"type": ["string", "null"], "description": "d"}})
        result = diff_documents(old, new)
        self.assertEqual(fingerprint(result), ["potentially-breaking:nullable-added"])

    def test_allof_members_are_merged_before_comparison(self):
        base = {"type": "object", "description": "d", "properties": {"id": {"type": "string"}}}
        # 'extra' exists only inside an allOf member: the merge has to find it,
        # otherwise every allOf-based document would diff as unchanged.
        extended = {"allOf": [{"$ref": "#/components/schemas/Base"},
                              {"type": "object", "properties": {"extra": {"type": "string"}}}]}
        old = spec_with_ref({"Base": base, "Thing": {"$ref": "#/components/schemas/Base"}})
        new = spec_with_ref({"Base": json.loads(json.dumps(base)), "Thing": extended})
        result = diff_documents(old, new)
        self.assertEqual(fingerprint(result), ["compatible:property-added"])

    def test_outer_schema_keywords_win_over_allof_members(self):
        base = {"type": "object", "description": "d", "properties": {"id": {"type": "string"}}}
        overridden = {
            "description": "d",
            "properties": {"id": {"type": "integer"}},
            "allOf": [{"$ref": "#/components/schemas/Base"}],
        }
        old = spec_with_ref({"Base": base, "Thing": {"$ref": "#/components/schemas/Base"}})
        new = spec_with_ref({"Base": json.loads(json.dumps(base)), "Thing": overridden})
        result = diff_documents(old, new)
        self.assertEqual(fingerprint(result), ["breaking:type-changed"])

    def test_status_code_keys_are_normalised(self):
        old = spec({"/things": {"get": {"operationId": "op", "summary": "s",
                                        "responses": {200: {"description": "ok"}}}}})
        new = spec({"/things": {"get": {"operationId": "op", "summary": "s",
                                        "responses": {"200": {"description": "ok"},
                                                      "404": {"description": "gone"}}}}})
        result = diff_documents(old, new)
        self.assertEqual(fingerprint(result), ["compatible:response-status-added"])

    def test_pointer_resolution(self):
        document = {"a": {"b": [{"c": "found"}]}}
        self.assertEqual(resolve_pointer(document, "#/a/b/0/c"), "found")
        self.assertEqual(resolve_pointer(document, "#"), document)
        self.assertIsNot(resolve_pointer(document, "#/a/missing"), "found")
        self.assertIsNot(resolve_pointer(document, "#/a/b/9"), "found")
        self.assertEqual(resolve_pointer({"a~b": {"c/d": 1}}, "#/a~0b/c~1d"), 1)

    def test_security_requirements_are_compared_per_operation(self):
        schemes = {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}
        old = spec({"/things": {"get": ok_operation(security=[{"apiKey": []}])}}, **{"components": {"schemas": {}, "securitySchemes": schemes}})
        new = spec({"/things": {"get": ok_operation(security=[])}} , **{"components": {"schemas": {}, "securitySchemes": schemes}})
        result = diff_documents(old, new)
        self.assertEqual(fingerprint(result), ["breaking:security-requirement-removed"])

    def test_findings_are_sorted_by_severity(self):
        old = load(os.path.join(FIXTURES, "petstore-v1.yaml"))
        new = load(os.path.join(FIXTURES, "petstore-v2.yaml"))
        severities = [finding.severity for finding in diff_documents(old, new).findings]
        self.assertEqual(severities, sorted(severities, key=lambda s: {BREAKING: 0, POTENTIALLY_BREAKING: 1, COMPATIBLE: 2}[s]))

    def test_identical_schema_rename_is_not_a_breaking_change(self):
        schema = {"type": "object", "description": "d", "properties": {"id": {"type": "string"}}}
        paths = {"/things": {"get": {
            "operationId": "listThings", "summary": "s",
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}}},
        }}}
        renamed_paths = json.loads(json.dumps(paths).replace("#/components/schemas/Thing", "#/components/schemas/Item"))
        old = spec(paths, {"Thing": schema})
        new = spec(renamed_paths, {"Item": json.loads(json.dumps(schema))})
        result = diff_documents(old, new)
        self.assertEqual(fingerprint(result), ["compatible:component-schema-renamed"])

    def test_components_that_is_not_a_mapping_is_tolerated(self):
        """Regression: a truthy non-mapping ``components`` used to raise AttributeError.

        ``components`` is optional and the diff engine guards every other
        optional object (``paths``, ``servers``, each component section) with
        an ``isinstance(..., dict)`` check.  The schema-rename comparison read
        through ``components`` without that guard, so a structurally wrong but
        perfectly parseable document like ``{"components": [1]}`` escaped as a
        traceback out of the CLI instead of being ignored.
        """
        for components in ([1], "components", 7, {}, [], None):
            with self.subTest(components=components):
                document = spec({}, None)
                document["components"] = components
                result = diff_documents(document, dict(document))
                self.assertEqual(result.findings, [])
                self.assertEqual(result.exit_code(BREAKING), 0)

    def test_components_schemas_that_is_not_a_mapping_is_tolerated(self):
        """The same guard, one level down: ``components.schemas`` as a list."""
        for schemas in ([1], "schemas", 7, {}, [], None):
            with self.subTest(schemas=schemas):
                document = spec({}, None)
                document["components"] = {"schemas": schemas}
                result = diff_documents(document, dict(document))
                self.assertEqual(result.findings, [])
                self.assertEqual(result.exit_code(BREAKING), 0)


class PetStoreIntegrationTests(unittest.TestCase):
    """The two fixtures used in the README, asserted as a regression net."""

    def setUp(self):
        self.result = diff_documents(
            load(os.path.join(FIXTURES, "petstore-v1.yaml")),
            load(os.path.join(FIXTURES, "petstore-v2.yaml")),
        )

    def test_summary_counts(self):
        self.assertEqual(self.result.summary(), {
            "breaking": 14, "potentially_breaking": 3, "compatible": 9, "total": 26,
        })

    def test_breaking_kinds(self):
        kinds = {finding.kind for finding in self.result.findings if finding.severity == BREAKING}
        self.assertEqual(kinds, {
            "operation-removed", "response-property-removed", "enum-value-removed", "format-changed",
            "property-became-required", "request-property-required-added", "additional-properties-forbidden",
            "nullable-removed", "security-scheme-changed", "parameter-location-changed",
            "parameter-became-required", "content-type-removed", "response-status-removed",
        })

    def test_potentially_breaking_kinds(self):
        kinds = {finding.kind for finding in self.result.findings
                 if finding.severity == POTENTIALLY_BREAKING}
        self.assertEqual(kinds, {"request-property-removed", "constraint-tightened", "operation-id-changed"})

    def test_compatible_kinds(self):
        kinds = {finding.kind for finding in self.result.findings if finding.severity == COMPATIBLE}
        self.assertEqual(kinds, {
            "property-added", "enum-value-added", "response-property-added", "parameter-added-optional",
            "response-status-added", "response-header-added", "endpoint-added",
        })

    def test_operation_counts(self):
        self.assertEqual(self.result.old_operation_count, 6)
        self.assertEqual(self.result.new_operation_count, 6)

    def test_exit_code_is_one(self):
        self.assertEqual(self.result.exit_code(BREAKING), 1)
        self.assertEqual(self.result.exit_code(POTENTIALLY_BREAKING), 1)
        self.assertEqual(self.result.exit_code("never"), 0)


if __name__ == "__main__":
    unittest.main()
