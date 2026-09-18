"""Tests for the built-in YAML subset reader, including a cross-check against PyYAML.

Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import miniyaml  # noqa: E402

try:  # PyYAML is optional at runtime, but if it is here we should agree with it
    import yaml
except Exception:  # pragma: no cover - depends on the environment
    yaml = None

FIXTURES = os.path.join(ROOT, "fixtures")


def normalise(value):
    """Recursively force mapping keys to strings so the two readers can be compared."""
    if isinstance(value, dict):
        return {str(key): normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalise(item) for item in value]
    return value


def iter_document_files():
    for dirpath, dirnames, filenames in os.walk(FIXTURES):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith((".yaml", ".yml", ".json")):
                yield os.path.join(dirpath, name)


class ScalarTests(unittest.TestCase):
    def test_empty_and_null_scalars(self):
        self.assertIsNone(miniyaml.load("key:\n")["key"])
        self.assertIsNone(miniyaml.load("key: ~")["key"])
        self.assertIsNone(miniyaml.load("key: null")["key"])
        self.assertIsNone(miniyaml.load("key: NULL")["key"])
        self.assertIsNone(miniyaml.load("key:")["key"])

    def test_booleans_use_the_yaml_1_2_core_schema(self):
        self.assertIs(miniyaml.load("a: true")["a"], True)
        self.assertIs(miniyaml.load("a: False")["a"], False)
        self.assertIs(miniyaml.load("a: TRUE")["a"], True)
        # YAML 1.1 (and therefore PyYAML) would make these booleans; YAML 1.2 does not.
        self.assertEqual(miniyaml.load("a: yes")["a"], "yes")
        self.assertEqual(miniyaml.load("a: on")["a"], "on")

    def test_numbers(self):
        self.assertEqual(miniyaml.load("a: 42")["a"], 42)
        self.assertEqual(miniyaml.load("a: -7")["a"], -7)
        self.assertEqual(miniyaml.load("a: 3.5")["a"], 3.5)
        self.assertEqual(miniyaml.load("a: 1e3")["a"], 1000.0)
        # Leading zeros and versions stay strings: they are identifiers, not numbers.
        self.assertEqual(miniyaml.load("a: 007")["a"], "007")
        self.assertEqual(miniyaml.load("a: 3.0.3")["a"], "3.0.3")
        self.assertEqual(miniyaml.load("a: 1.0.0")["a"], "1.0.0")

    def test_quoted_scalars(self):
        self.assertEqual(miniyaml.load("a: 'it''s'")["a"], "it's")
        self.assertEqual(miniyaml.load('a: "line\\nbreak"')["a"], "line\nbreak")
        self.assertEqual(miniyaml.load('a: "\\u00e9"')["a"], "\u00e9")
        self.assertEqual(miniyaml.load('a: "say \\"hi\\""')["a"], 'say "hi"')

    def test_comments(self):
        document = miniyaml.load(
            "# leading comment\n"
            "a: 1  # trailing comment\n"
            "b: '# not a comment'\n"
            "c: http://example.test/path#fragment\n"
            "d: value#notacomment\n"
        )
        self.assertEqual(document["a"], 1)
        self.assertEqual(document["b"], "# not a comment")
        self.assertEqual(document["c"], "http://example.test/path#fragment")
        self.assertEqual(document["d"], "value#notacomment")

    def test_keys_are_always_strings(self):
        document = miniyaml.load("200: ok\ntrue: yes-please\n")
        self.assertIn("200", document)
        self.assertEqual(document["200"], "ok")
        self.assertIn("true", document)


class StructureTests(unittest.TestCase):
    def test_nested_mappings_and_sequences(self):
        document = miniyaml.load(
            "openapi: 3.0.3\n"
            "paths:\n"
            "  /pets:\n"
            "    get:\n"
            "      tags:\n"
            "        - pets\n"
            "        - store\n"
        )
        self.assertEqual(document["paths"]["/pets"]["get"]["tags"], ["pets", "store"])

    def test_sequence_at_the_same_indent_as_its_key(self):
        document = miniyaml.load("items:\n- 1\n- 2\n")
        self.assertEqual(document["items"], [1, 2])

    def test_sequence_of_mappings(self):
        document = miniyaml.load(
            "parameters:\n"
            "  - name: limit\n"
            "    in: query\n"
            "    schema:\n"
            "      type: integer\n"
            "  - name: offset\n"
            "    in: query\n"
        )
        self.assertEqual(len(document["parameters"]), 2)
        self.assertEqual(document["parameters"][0]["schema"]["type"], "integer")
        self.assertEqual(document["parameters"][1]["in"], "query")

    def test_sequence_item_that_is_a_scalar(self):
        document = miniyaml.load("values:\n  - plain\n  - 12\n  - [1, 2]\n  - {a: 1}\n")
        self.assertEqual(document["values"], ["plain", 12, [1, 2], {"a": 1}])

    def test_flow_collections(self):
        document = miniyaml.load(
            "a: [1, 2, {b: [3, 4], c: 'x, y'}]\n"
            "d: {e: f, g: {h: true}}\n"
            "i: [1, 2, ]\n"
            "j: {'200': ok}\n"
        )
        self.assertEqual(document["a"], [1, 2, {"b": [3, 4], "c": "x, y"}])
        self.assertEqual(document["d"], {"e": "f", "g": {"h": True}})
        self.assertEqual(document["i"], [1, 2])
        self.assertEqual(document["j"], {"200": "ok"})

    def test_multi_line_flow_collection(self):
        document = miniyaml.load("a: [1,\n    2,\n    3]\n")
        self.assertEqual(document["a"], [1, 2, 3])

    def test_document_root_may_be_a_flow_collection(self):
        # This is what a JSON file looks like to this reader.
        text = json.dumps({"openapi": "3.0.3", "paths": {"/pets": {"get": {}}}}, indent=2)
        self.assertEqual(miniyaml.load(text)["paths"]["/pets"]["get"], {})

    def test_literal_block_scalar(self):
        document = miniyaml.load("description: |\n  first\n  second\n\nnext: 1\n")
        self.assertEqual(document["description"], "first\nsecond\n")
        self.assertEqual(document["next"], 1)

    def test_block_scalar_chomping(self):
        # These expectations are PyYAML's, verified character for character.
        self.assertEqual(miniyaml.load("a: |-\n  x\n")["a"], "x")
        self.assertEqual(miniyaml.load("a: |\n  x\n")["a"], "x\n")
        self.assertEqual(miniyaml.load("a: |+\n  x\n")["a"], "x\n")
        self.assertEqual(miniyaml.load("a: |+\n  x\n\n")["a"], "x\n\n")
        self.assertEqual(miniyaml.load("a: |\n  x\n\n  y\n")["a"], "x\n\ny\n")
        self.assertEqual(miniyaml.load("a: |2\n    x\n")["a"], "  x\n")

    def test_folded_block_scalar(self):
        document = miniyaml.load("a: >-\n  one\n  two\n\n  three\n")
        self.assertEqual(document["a"], "one two\nthree")

    def test_anchors_aliases_and_merge_keys(self):
        document = miniyaml.load(
            "base: &base\n"
            "  type: object\n"
            "  description: shared\n"
            "copy: *base\n"
            "extended:\n"
            "  <<: *base\n"
            "  extra: 1\n"
        )
        self.assertEqual(document["copy"], {"type": "object", "description": "shared"})
        self.assertEqual(document["extended"]["type"], "object")
        self.assertEqual(document["extended"]["extra"], 1)

    def test_explicit_tags(self):
        self.assertEqual(miniyaml.load("a: !!str 5")["a"], "5")
        self.assertEqual(miniyaml.load("a: !!int '5'")["a"], 5)

    def test_document_markers_are_skipped(self):
        document = miniyaml.load("---\na: 1\n...\n")
        self.assertEqual(document, {"a": 1})


class ErrorTests(unittest.TestCase):
    def test_tab_indentation_is_rejected(self):
        with self.assertRaises(miniyaml.YamlError):
            miniyaml.load("a:\n\tb: 1\n")

    def test_unquoted_colon_without_space_is_rejected(self):
        with self.assertRaises(miniyaml.YamlError):
            miniyaml.load("key:value\n")

    def test_unterminated_quote_is_rejected(self):
        with self.assertRaises(miniyaml.YamlError):
            miniyaml.load('a: "unterminated\n')

    def test_unknown_alias_is_rejected(self):
        with self.assertRaises(miniyaml.YamlError):
            miniyaml.load("a: *nope\n")

    def test_unbalanced_flow_collection_is_rejected(self):
        with self.assertRaises(miniyaml.YamlError):
            miniyaml.load("a: [1, 2\n")

    def test_bad_indentation_is_rejected(self):
        with self.assertRaises(miniyaml.YamlError):
            miniyaml.load("a: 1\n    b: 2\n")

    def test_document_must_start_at_column_zero(self):
        with self.assertRaises(miniyaml.YamlError):
            miniyaml.load("  a: 1\n")

    def test_error_carries_a_line_number(self):
        try:
            miniyaml.load("a: 1\nb: *missing\n")
        except miniyaml.YamlError as error:
            self.assertEqual(error.line, 2)
        else:  # pragma: no cover
            self.fail("expected a YamlError")


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class PyYamlAgreementTests(unittest.TestCase):
    """Every shipped fixture must parse the same way in both readers."""

    def test_all_fixture_documents_agree(self):
        checked = 0
        for path in iter_document_files():
            with open(path, "r", encoding="utf-8-sig") as handle:
                text = handle.read()
            with self.subTest(fixture=os.path.relpath(path, ROOT)):
                self.assertEqual(normalise(miniyaml.load(text)), normalise(yaml.safe_load(text)))
            checked += 1
        self.assertGreaterEqual(checked, 70, "expected the whole fixture tree to be exercised")

    def test_rule_fixture_count(self):
        rule_root = os.path.join(FIXTURES, "rules")
        cases = sorted(name for name in os.listdir(rule_root) if os.path.isdir(os.path.join(rule_root, name)))
        self.assertGreaterEqual(len(cases), 50)
        for case in cases:
            for filename in ("old.yaml", "new.yaml", "expect.json"):
                self.assertTrue(
                    os.path.exists(os.path.join(rule_root, case, filename)),
                    "%s is missing %s" % (case, filename),
                )


class AgreementTests(unittest.TestCase):
    """The cross-checks that do not need PyYAML."""

    def test_agreement_test_is_meaningful(self):
        # Guards the skip: if PyYAML disappears, the suite still asserts something
        # about the fixtures it ships.
        self.assertTrue(callable(miniyaml.load))
        paths = list(iter_document_files())
        self.assertGreaterEqual(len(paths), 70)


if __name__ == "__main__":
    unittest.main()
