"""Tests for the design lint, one test per rule, plus the clean-document control.

Each rule is anchored to published guidance; a meta-test asserts no rule can
ship without a citation.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import miniyaml  # noqa: E402
from designlint import (  # noqa: E402
    GUIDANCE,
    RULE_SEVERITY,
    ERROR,
    WARNING,
    lint_document,
    lint_summary,
)

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

FIXTURES = os.path.join(ROOT, "fixtures")


def load(name, backend="miniyaml"):
    path = os.path.join(FIXTURES, name)
    with open(path, "r", encoding="utf-8-sig") as handle:
        text = handle.read()
    if backend == "pyyaml":
        if yaml is None:  # pragma: no cover
            raise unittest.SkipTest("PyYAML is not installed")
        return yaml.safe_load(text)
    return miniyaml.load(text)


def document(paths=None, schemas=None):
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1", "description": "test"},
        "paths": paths or {},
    }
    if schemas is not None:
        doc["components"] = {"schemas": schemas}
    return doc


def good_operation(**extra):
    operation = {
        "operationId": "op",
        "summary": "An operation",
        "responses": {
            "200": {"description": "ok"},
            "400": {"description": "bad request"},
        },
    }
    operation.update(extra)
    return operation


def rules_of(doc):
    return {issue.rule for issue in lint_document(doc)}


def collection_get(operation_id):
    """A GET that plainly reads a collection: a 200 whose schema is an array."""
    return {
        "operationId": operation_id,
        "summary": "Lists things",
        "responses": {
            "200": {"description": "A list.", "content": {"application/json": {
                "schema": {"type": "array", "items": {"type": "string"}}}}},
            "400": {"description": "bad request"},
        },
    }


class FixtureLintTests(unittest.TestCase):
    def test_clean_document_has_no_issues(self):
        for backend in ("miniyaml", "pyyaml"):
            if backend == "pyyaml" and yaml is None:
                continue
            with self.subTest(backend=backend):
                issues = lint_document(load("lint-clean.yaml", backend))
                self.assertEqual([issue.as_dict() for issue in issues], [])

    def test_messy_document_reports_every_expected_rule(self):
        expected = {
            "path-trailing-slash",
            "path-kebab-case",
            "path-verb-in-segment",
            "operation-id-missing",
            "operation-id-duplicate",
            "responses-empty",
            "response-description-missing",
            "response-no-error-documented",
            "no-success-response",
            "list-endpoint-no-pagination",
            "path-not-plural",
            "inconsistent-pluralisation",
            "path-parameter-not-declared",
            "path-parameter-not-required",
            "operation-description-missing",
            "schema-description-missing",
        }
        for backend in ("miniyaml", "pyyaml"):
            if backend == "pyyaml" and yaml is None:
                continue
            with self.subTest(backend=backend):
                issues = lint_document(load("lint-messy.yaml", backend))
                found = {issue.rule for issue in issues}
                self.assertEqual(expected - found, set(), "rules that should have fired")
                self.assertGreaterEqual(len(issues), 20)

    def test_messy_document_summary(self):
        summary = lint_summary(lint_document(load("lint-messy.yaml")))
        self.assertGreaterEqual(summary["error"], 4)
        self.assertGreaterEqual(summary["warning"], 12)
        self.assertEqual(summary["total"], summary["error"] + summary["warning"] + summary["info"])

    def test_lint_is_stable_across_backends(self):
        if yaml is None:  # pragma: no cover
            self.skipTest("PyYAML is not installed")
        builtin = [issue.as_dict() for issue in lint_document(load("lint-messy.yaml", "miniyaml"))]
        external = [issue.as_dict() for issue in lint_document(load("lint-messy.yaml", "pyyaml"))]
        self.assertEqual(builtin, external)


class PathRuleTests(unittest.TestCase):
    def test_trailing_slash(self):
        self.assertIn("path-trailing-slash", rules_of(document({"/pets/": {"get": good_operation()}})))
        self.assertNotIn("path-trailing-slash", rules_of(document({"/pets": {"get": good_operation()}})))

    def test_upper_case_segment(self):
        self.assertIn("path-kebab-case", rules_of(document({"/PetStore": {"get": good_operation()}})))

    def test_underscore_segment(self):
        self.assertIn("path-kebab-case", rules_of(document({"/pet_store": {"get": good_operation()}})))

    def test_kebab_case_is_accepted(self):
        self.assertNotIn("path-kebab-case", rules_of(document({"/pet-store": {"get": good_operation()}})))

    def test_verb_in_path_is_info_not_error(self):
        issues = lint_document(document({"/invoices/create": {"post": good_operation()}}))
        verbs = [issue for issue in issues if issue.rule == "path-verb-in-segment"]
        self.assertEqual(len(verbs), 1)
        self.assertEqual(verbs[0].severity, "info")
        self.assertIn("AIP-136", verbs[0].guidance)

    def test_custom_verb_suffix_is_accepted(self):
        self.assertNotIn(
            "path-verb-in-segment",
            rules_of(document({"/invoices/{invoiceId}:cancel": {"post": good_operation()}})),
        )

    def test_path_parameter_must_be_declared(self):
        path = {"/pets/{petId}": {"get": good_operation()}}
        self.assertIn("path-parameter-not-declared", rules_of(document(path)))

    def test_declared_path_parameter_must_appear_in_the_template(self):
        path = {"/pets": {"get": good_operation(parameters=[
            {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}])}}
        self.assertIn("path-parameter-not-declared", rules_of(document(path)))

    def test_path_parameter_must_be_required(self):
        path = {"/pets/{petId}": {"get": good_operation(parameters=[
            {"name": "petId", "in": "path", "schema": {"type": "string"}}])}}
        issues = lint_document(document(path))
        self.assertIn("path-parameter-not-required", issues[0].rule)
        self.assertEqual(issues[0].severity, ERROR)

    def test_declared_and_required_path_parameter_is_accepted(self):
        path = {"/pets/{petId}": {"get": good_operation(parameters=[
            {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}])}}
        self.assertEqual(rules_of(document(path)), set())

    def test_singular_collection_is_reported(self):
        path = {"/widget/{widgetId}": {"get": good_operation(parameters=[
            {"name": "widgetId", "in": "path", "required": True, "schema": {"type": "string"}}])}}
        self.assertIn("path-not-plural", rules_of(document(path)))

    def test_plural_collection_is_accepted(self):
        path = {"/widgets/{widgetId}": {"get": good_operation(parameters=[
            {"name": "widgetId", "in": "path", "required": True, "schema": {"type": "string"}}])}}
        self.assertNotIn("path-not-plural", rules_of(document(path)))

    def test_sibling_collections_must_agree_on_pluralisation(self):
        paths = {
            "/widget": {"get": collection_get("listWidget")},
            "/gadgets": {"get": collection_get("listGadgets")},
        }
        found = rules_of(document(paths))
        self.assertIn("inconsistent-pluralisation", found)
        self.assertIn("path-not-plural", found)

    def test_consistent_siblings_are_accepted(self):
        paths = {
            "/widgets": {"get": collection_get("listWidgets")},
            "/gadgets": {"get": collection_get("listGadgets")},
        }
        found = rules_of(document(paths))
        self.assertNotIn("inconsistent-pluralisation", found)
        self.assertNotIn("path-not-plural", found)


class OperationRuleTests(unittest.TestCase):
    def test_missing_operation_id(self):
        self.assertIn("operation-id-missing", rules_of(document({"/pets": {"get": {
            "summary": "s", "responses": {"200": {"description": "ok"}, "400": {"description": "bad"}}}}})))

    def test_duplicate_operation_id_is_an_error(self):
        paths = {
            "/pets": {"get": good_operation(operationId="list")},
            "/toys": {"get": good_operation(operationId="list")},
        }
        issues = [issue for issue in lint_document(document(paths)) if issue.rule == "operation-id-duplicate"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, ERROR)
        self.assertIn("MUST be unique", issues[0].guidance)

    def test_missing_description(self):
        operation = {"operationId": "op", "responses": {"200": {"description": "ok"},
                                                        "400": {"description": "bad"}}}
        self.assertIn("operation-description-missing", rules_of(document({"/pets": {"get": operation}})))

    def test_summary_counts_as_documentation(self):
        operation = {"operationId": "op", "summary": "Does a thing",
                     "responses": {"200": {"description": "ok"}, "400": {"description": "bad"}}}
        self.assertNotIn("operation-description-missing", rules_of(document({"/pets": {"get": operation}})))

    def test_missing_responses_object_is_an_error(self):
        operation = {"operationId": "op", "summary": "s"}
        issues = lint_document(document({"/pets": {"get": operation}}))
        self.assertIn("operation-missing-responses", {issue.rule for issue in issues})
        self.assertEqual(
            [issue.severity for issue in issues if issue.rule == "operation-missing-responses"], [ERROR]
        )

    def test_empty_responses_object_is_an_error(self):
        operation = {"operationId": "op", "summary": "s", "responses": {}}
        issues = lint_document(document({"/pets": {"get": operation}}))
        self.assertIn("responses-empty", {issue.rule for issue in issues})

    def test_only_one_non_success_response(self):
        operation = {"operationId": "op", "summary": "s", "responses": {"404": {"description": "gone"}}}
        self.assertIn("no-success-response", rules_of(document({"/pets": {"get": operation}})))

    def test_undocumented_errors(self):
        operation = {"operationId": "op", "summary": "s", "responses": {"200": {"description": "ok"}}}
        self.assertIn("response-no-error-documented", rules_of(document({"/pets": {"get": operation}})))

    def test_a_4xx_response_satisfies_the_error_rule(self):
        operation = {"operationId": "op", "summary": "s",
                     "responses": {"200": {"description": "ok"}, "400": {"description": "bad"}}}
        self.assertNotIn("response-no-error-documented", rules_of(document({"/pets": {"get": operation}})))

    def test_a_default_response_satisfies_the_error_rule(self):
        operation = {"operationId": "op", "summary": "s",
                     "responses": {"200": {"description": "ok"}, "default": {"description": "errors"}}}
        self.assertNotIn("response-no-error-documented", rules_of(document({"/pets": {"get": operation}})))

    def test_response_description_is_required_by_the_spec(self):
        operation = {"operationId": "op", "summary": "s",
                     "responses": {"200": {"description": "ok"}, "400": {}}}
        issues = [issue for issue in lint_document(document({"/pets": {"get": operation}}))
                  if issue.rule == "response-description-missing"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, ERROR)
        self.assertIn("REQUIRED", issues[0].guidance)

    def test_schema_description_is_reported(self):
        document_with_schema = document({"/pets": {"get": good_operation()}}, {"Pet": {"type": "object"}})
        self.assertIn("schema-description-missing", rules_of(document_with_schema))

    def test_described_schema_is_accepted(self):
        doc = document({"/pets": {"get": good_operation()}},
                       {"Pet": {"type": "object", "description": "A pet."}})
        self.assertNotIn("schema-description-missing", rules_of(doc))


class PaginationRuleTests(unittest.TestCase):
    def test_array_response_without_pagination(self):
        operation = good_operation(responses={
            "200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": "array", "items": {"type": "string"}}}}},
            "400": {"description": "bad"},
        })
        self.assertIn("list-endpoint-no-pagination", rules_of(document({"/pets": {"get": operation}})))

    def test_paginated_collection_is_accepted(self):
        operation = good_operation(
            parameters=[{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer"}}],
            responses={
                "200": {"description": "ok", "content": {"application/json": {
                    "schema": {"type": "array", "items": {"type": "string"}}}}},
                "400": {"description": "bad"},
            },
        )
        self.assertNotIn("list-endpoint-no-pagination", rules_of(document({"/pets": {"get": operation}})))

    def test_paged_object_response_without_pagination(self):
        operation = good_operation(responses={
            "200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": "object", "properties": {
                    "items": {"type": "array", "items": {"type": "string"}}}}}}},
            "400": {"description": "bad"},
        })
        self.assertIn("list-endpoint-no-pagination", rules_of(document({"/pets": {"get": operation}})))

    def test_single_resource_is_not_a_collection(self):
        operation = good_operation(responses={
            "200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": "object", "properties": {
                    "photos": {"type": "array", "items": {"type": "string"}}}}}}},
            "400": {"description": "bad"},
        })
        paths = {"/pets/{petId}": {"get": dict(operation, parameters=[
            {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}])}}
        self.assertNotIn("list-endpoint-no-pagination", rules_of(document(paths)))


class GuidelineMetaTests(unittest.TestCase):
    """No rule may exist without a citation, and no citation without a rule."""

    def test_every_rule_declares_a_severity(self):
        for rule in GUIDANCE:
            self.assertIn(rule, RULE_SEVERITY, "%s has guidance but no severity" % rule)

    def test_every_severity_has_guidance(self):
        for rule in RULE_SEVERITY:
            self.assertIn(rule, GUIDANCE, "%s has a severity but no citation" % rule)
            self.assertTrue(GUIDANCE[rule].strip())

    def test_severities_are_known_values(self):
        for rule, severity in RULE_SEVERITY.items():
            self.assertIn(severity, (ERROR, WARNING, "info"), rule)

    def test_citations_name_a_published_source(self):
        sources = ("Zalando", "Google AIP", "Microsoft REST API Guidelines", "OpenAPI 3.")
        for rule, text in GUIDANCE.items():
            with self.subTest(rule=rule):
                self.assertTrue(any(source in text for source in sources),
                                "%s cites no published source" % rule)

    def test_every_issue_carries_its_citation(self):
        for issue in lint_document(load("lint-messy.yaml")):
            with self.subTest(rule=issue.rule):
                self.assertEqual(issue.guidance, GUIDANCE[issue.rule])
                self.assertTrue(issue.guidance)

    def test_spec_violations_are_errors_and_style_rules_are_not(self):
        # Rules grounded in an OpenAPI MUST are errors; style-guide rules are warnings.
        for rule in ("operation-id-duplicate", "response-description-missing", "responses-empty",
                     "operation-missing-responses", "path-parameter-not-declared",
                     "path-parameter-not-required"):
            self.assertEqual(RULE_SEVERITY[rule], ERROR, rule)
        for rule in ("path-trailing-slash", "path-kebab-case", "path-not-plural",
                     "list-endpoint-no-pagination", "response-no-error-documented"):
            self.assertEqual(RULE_SEVERITY[rule], WARNING, rule)


if __name__ == "__main__":
    unittest.main()
