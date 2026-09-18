"""Design lint for OpenAPI 3.x documents - the "is this a well-designed API?" half.

Every rule below carries the published guidance it comes from.  The OpenAPI
Specification itself only mandates a handful of these (marked ``error``);
most are style-guide rules from Google's API Improvement Proposals (AIP),
the Zalando RESTful API Guidelines and the Microsoft REST API Guidelines.

Rules marked ``error`` are checked against a MUST in the OpenAPI 3.1
Specification.  Rules marked ``warning`` come from a published style guide.
Rules marked ``info`` are heuristics: they are worth a look, not a verdict.
The exact citation is attached to every issue in the ``guidance`` field, so
output can be audited instead of trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from contractdiff import RefResolver, as_list, is_mapping as _is_dict

ERROR = "error"
WARNING = "warning"
INFO = "info"
LINT_SEVERITIES = (ERROR, WARNING, INFO)
LINT_RANK = {INFO: 0, WARNING: 1, ERROR: 2}

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

#: Parameter names that are accepted as "this collection is paginated".
PAGINATION_PARAMETERS = frozenset(
    name.lower()
    for name in (
        "page",
        "pageSize",
        "page_size",
        "perPage",
        "per_page",
        "pageToken",
        "page_token",
        "nextPageToken",
        "limit",
        "offset",
        "skip",
        "cursor",
        "after",
        "before",
        "starting_after",
        "ending_before",
        "continuationToken",
        "maxpagesize",
        "maxPageSize",
        "top",
        "$top",
        "$skip",
        "size",
        "count",
        "from",
    )
)

#: Field names that mark an object as a page of a collection.
COLLECTION_FIELD_NAMES = frozenset(
    """
    items data results entries values records elements rows nodes members edges
    list content objects hits docs documents children
    """.split()
)

#: Unambiguous CRUD/action words that should not be a bare path segment.
_VERB_WORDS = frozenset(
    """
    get set add remove create update delete list fetch retrieve find save
    insert modify edit unset assign unassign enable disable activate deactivate
    approve reject execute compute calculate generate register unregister
    signup signin signout login logout
    cancel archive restore publish unpublish submit send move copy merge close
    lock unlock retry resume suspend pause start stop complete confirm verify
    transfer revoke grant rotate rename apply refresh reset clear
    """.split()
)

#: Nouns that look like verbs (or are uncountable/singular by convention) and
#: must not be reported by the verb rule or the pluralisation rule.
_NOUN_ALLOWLIST = frozenset(
    """
    search order orders export import report reports download upload request requests
    check process run batch query filter sort statistics data info metadata media
    health status statuses me self config settings metrics series species news audit
    feedback content security billing admin analytics monitoring access sync
    calendar schedule inventory payment payments invoice invoices shipping tracking
    """.split()
)

#: Camel-case verb prefixes such as ``getPets`` or ``createOrder``.
_VERB_PREFIX_RE = re.compile(r"^(get|set|add|remove|create|update|delete|list|fetch|find)[A-Z_0-9]")

_VERSION_SEGMENT_RE = re.compile(r"^v[0-9]+(?:[a-z0-9]*)?$", re.I)
_TEMPLATE_RE = re.compile(r"\{([^}]*)\}")

GUIDANCE = {
    "path-trailing-slash": (
        "Zalando RESTful API Guidelines #136: 'MUST use normalized paths without empty path "
        "segments and trailing slashes' (https://opensource.zalando.com/restful-api-guidelines/)"
    ),
    "path-kebab-case": (
        "Zalando RESTful API Guidelines #129: 'MUST use kebab-case for path segments', regex "
        "^[a-z][a-z\\-0-9]*$; Microsoft REST API Guidelines: 'DO use kebab-casing (preferred) or "
        "camel-casing for URL path segments'"
    ),
    "path-verb-in-segment": (
        "Google AIP-136 (Custom methods): 'The HTTP URI must use a : character followed by the "
        "custom verb', i.e. /orders/{id}:cancel rather than /orders/cancel; Microsoft REST API "
        "Guidelines: 'model resource state, not behavior'. Heuristic: some nouns are also verbs, "
        "so this is reported as info, not as a defect."
    ),
    "operation-id-missing": (
        "OpenAPI 3.1 (Operation Object): operationId is optional, but 'Tools and libraries MAY use "
        "the operationId to uniquely identify an operation, therefore, it is RECOMMENDED to follow "
        "common programming naming conventions'"
    ),
    "operation-id-duplicate": (
        "OpenAPI 3.1 (Operation Object): 'Unique string used to identify the operation. The id "
        "MUST be unique among all operations described in the API.'"
    ),
    "operation-missing-responses": (
        "OpenAPI 3.1 (Operation Object): 'responses | Responses Object | REQUIRED. The list of "
        "possible responses.'"
    ),
    "responses-empty": (
        "OpenAPI 3.1 (Responses Object): 'The Responses Object MUST contain at least one response code'"
    ),
    "no-success-response": (
        "OpenAPI 3.1 (Responses Object): 'if only one response code is provided it SHOULD be the "
        "response for a successful operation call'"
    ),
    "response-no-error-documented": (
        "Zalando RESTful API Guidelines #151: 'MUST specify success and error responses'; OpenAPI "
        "3.1 (Responses Object): 'documentation is expected to cover a successful operation "
        "response and any known errors'; Microsoft REST API Guidelines: 'DO document the service's "
        "top-level error code strings; they are part of the API contract.' A 'default' response "
        "counts as documenting errors - Microsoft: 'YOU SHOULD NOT document specific error status "
        "codes in your OpenAPI/Swagger spec unless the default response cannot properly describe "
        "the specific error response.'"
    ),
    "response-description-missing": (
        "OpenAPI 3.1 (Response Object): 'description | string | REQUIRED. A description of the response.'"
    ),
    "operation-description-missing": (
        "Google AIP-192 (Documentation): 'public comments must be included over every component "
        "(service, method, message, field, enum, and enum value)... This is important even in cases "
        "where the comment is terse and uninteresting, as numerous tools read these comments and use "
        "them.' Not an OpenAPI requirement: description is optional in OAS 3.1."
    ),
    "schema-description-missing": (
        "Google AIP-192 (Documentation): every message and field must carry a public comment; "
        "OpenAPI 3.1 makes Schema Object description optional, so this is a documentation-quality "
        "rule, not a spec violation."
    ),
    "list-endpoint-no-pagination": (
        "Google AIP-158 (Pagination): 'RPCs returning collections of data must provide pagination at "
        "the outset'; Zalando RESTful API Guidelines #159: 'MUST support pagination'; Microsoft REST "
        "API Guidelines describe maxpagesize/nextLink for collection responses. Exemptions exist - "
        "AIP-158 notes that small, bounded collections are the usual exception."
    ),
    "path-not-plural": (
        "Zalando RESTful API Guidelines #134: 'MUST pluralize resource names'; Google AIP-122: "
        "'Collection identifiers must be plural.'; Microsoft REST API Guidelines: resource-collection "
        "is 'Name of the collection, unabbreviated, pluralized'"
    ),
    "inconsistent-pluralisation": (
        "Google AIP-122: 'Collection identifiers must be plural.' plus Microsoft REST API "
        "Guidelines: 'DO focus heavily on clear & consistent naming'"
    ),
    "path-parameter-not-declared": (
        "OpenAPI 3.1 (Parameter Object): 'If in is \"path\", the name field MUST correspond to a "
        "template expression occurring within the path field in the Paths Object.'"
    ),
    "path-parameter-not-required": (
        "OpenAPI 3.1 (Parameter Object): 'If the parameter location is \"path\", this property is "
        "REQUIRED and its value MUST be true.'"
    ),
}

RULE_SEVERITY = {
    "path-trailing-slash": WARNING,
    "path-kebab-case": WARNING,
    "path-verb-in-segment": INFO,
    "operation-id-missing": WARNING,
    "operation-id-duplicate": ERROR,
    "operation-missing-responses": ERROR,
    "responses-empty": ERROR,
    "no-success-response": WARNING,
    "response-no-error-documented": WARNING,
    "response-description-missing": ERROR,
    "operation-description-missing": WARNING,
    "schema-description-missing": WARNING,
    "list-endpoint-no-pagination": WARNING,
    "path-not-plural": WARNING,
    "inconsistent-pluralisation": WARNING,
    "path-parameter-not-declared": ERROR,
    "path-parameter-not-required": ERROR,
}


@dataclass
class LintIssue:
    rule: str
    severity: str
    location: str
    message: str
    guidance: str = ""

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "location": self.location,
            "message": self.message,
            "guidance": self.guidance,
        }


def _looks_plural(name: str) -> bool:
    if name in _NOUN_ALLOWLIST:
        return False
    if name.endswith("data"):
        return True
    return name.endswith("s") and not name.endswith("ss")


def _looks_singular(name: str) -> bool:
    if name in _NOUN_ALLOWLIST:
        return False
    if _VERSION_SEGMENT_RE.match(name):
        return False
    return not _looks_plural(name)


class DesignLinter:
    def __init__(self, document: dict, base_path: str | None = None):
        self.document = document if isinstance(document, dict) else {}
        self.resolver = RefResolver(self.document, "document")
        self.base_path = (base_path or "").strip()
        if self.base_path and not self.base_path.startswith("/"):
            self.base_path = "/" + self.base_path
        self.base_path = self.base_path.rstrip("/")
        self.issues: list = []

    def _add(self, rule: str, location: str, message: str) -> None:
        self.issues.append(
            LintIssue(rule, RULE_SEVERITY.get(rule, WARNING), location, message, GUIDANCE.get(rule, ""))
        )

    # -- helpers ----------------------------------------------------------
    def _paths(self) -> dict:
        paths = self.document.get("paths")
        return paths if _is_dict(paths) else {}

    def _analysis_path(self, path: str) -> str:
        if self.base_path and (path == self.base_path or path.startswith(self.base_path + "/")):
            stripped = path[len(self.base_path) :]
            return stripped or "/"
        return path

    def _operations(self):
        for raw_path, item in sorted(self._paths().items()):
            resolved, _ref = self.resolver.deref(item)
            if not _is_dict(resolved):
                continue
            for method in HTTP_METHODS:
                operation = resolved.get(method)
                if _is_dict(operation):
                    yield str(raw_path), method, operation, resolved

    # -- rules ------------------------------------------------------------
    def run(self) -> list:
        self._lint_operation_ids()
        for raw_path, method, operation, item in self._operations():
            base = "paths.%s.%s" % (raw_path, method)
            self._lint_operation_description(base, operation)
            self._lint_responses(base, operation)
            self._lint_pagination(raw_path, base, method, operation, item)
        self._lint_paths()
        self._lint_path_parameters()
        self._lint_component_descriptions()
        self.issues.sort(key=lambda issue: (-LINT_RANK.get(issue.severity, 0), issue.location, issue.rule))
        return self.issues

    def _lint_operation_ids(self) -> None:
        seen: dict = {}
        for raw_path, method, operation, _item in self._operations():
            location = "paths.%s.%s" % (raw_path, method)
            operation_id = operation.get("operationId")
            if operation_id is None:
                self._add(
                    "operation-id-missing",
                    location,
                    "%s %s has no operationId" % (method.upper(), raw_path),
                )
                continue
            seen.setdefault(str(operation_id), []).append(location)
        for operation_id, locations in sorted(seen.items()):
            if len(locations) > 1:
                self._add(
                    "operation-id-duplicate",
                    "operationId(%s)" % operation_id,
                    "operationId %r is used by %d operations: %s"
                    % (operation_id, len(locations), ", ".join(sorted(locations))),
                )

    def _lint_operation_description(self, base: str, operation: dict) -> None:
        has_text = any(
            isinstance(operation.get(key), str) and operation.get(key).strip() for key in ("summary", "description")
        )
        if not has_text:
            self._add(
                "operation-description-missing",
                base,
                "the operation has neither summary nor description",
            )

    def _lint_responses(self, base: str, operation: dict) -> None:
        responses = operation.get("responses")
        if responses is None:
            self._add("operation-missing-responses", base, "the operation has no responses object")
            return
        if not _is_dict(responses):
            return
        resolved, _ref = self.resolver.deref(responses)
        if not _is_dict(resolved) or not resolved:
            self._add("responses-empty", base, "the responses object contains no response codes")
            return
        lookup = {str(code): value for code, value in resolved.items()}
        codes = sorted(lookup)
        has_error = any(
            code == "default" or re.match(r"^[45][0-9]{2}$", code) or re.match(r"^[45]XX$", code) for code in codes
        )
        if not has_error:
            self._add(
                "response-no-error-documented",
                base + ".responses",
                "only success status codes are documented (%s); no 4xx/5xx or default response"
                % ", ".join(sorted(codes)),
            )
        if len(codes) == 1 and not re.match(r"^2[0-9]{2}$", codes[0]):
            self._add(
                "no-success-response",
                base + ".responses",
                "the only documented response is %s" % codes[0],
            )
        for code in codes:
            response, _ref = self.resolver.deref(lookup.get(code))
            if not _is_dict(response):
                continue
            if not (isinstance(response.get("description"), str) and response.get("description").strip()):
                self._add(
                    "response-description-missing",
                    "%s.responses.%s" % (base, code),
                    "response %s has no description" % code,
                )

    def _returns_collection(self, operation: dict) -> bool:
        responses = operation.get("responses")
        if not _is_dict(responses):
            return False
        for code, response in responses.items():
            if not re.match(r"^2[0-9]{2}$", str(code)):
                continue
            resolved, _ref = self.resolver.deref(response)
            content = resolved.get("content") if _is_dict(resolved) else None
            if not _is_dict(content):
                continue
            for _media_type, media in content.items():
                media, _ref = self.resolver.deref(media)
                schema, _ref = self.resolver.deref(media.get("schema")) if _is_dict(media) else (None, None)
                if not _is_dict(schema):
                    continue
                if self._is_array(schema):
                    return True
                properties = schema.get("properties")
                if _is_dict(properties):
                    for name, prop in properties.items():
                        prop, _ref = self.resolver.deref(prop)
                        # Only a plausibly-named array field: a single resource
                        # with an array field (Pet.photo-urls) is not a collection.
                        if str(name).lower() in COLLECTION_FIELD_NAMES and _is_dict(prop) and self._is_array(prop):
                            return True
        return False

    @staticmethod
    def _is_array(schema: dict) -> bool:
        schema_type = schema.get("type")
        if schema_type == "array":
            return True
        return isinstance(schema_type, list) and "array" in schema_type

    def _lint_pagination(self, raw_path: str, base: str, method: str, operation: dict, item: dict) -> None:
        if method != "get":
            return
        if not self._is_collection_read(raw_path, operation):
            return
        names = set()
        for source in (as_list(item.get("parameters")), as_list(operation.get("parameters"))):
            for raw in source:
                parameter, _ref = self.resolver.deref(raw)
                if _is_dict(parameter) and parameter.get("name") is not None:
                    names.add(str(parameter["name"]).lower())
        if names & PAGINATION_PARAMETERS:
            return
        self._add(
            "list-endpoint-no-pagination",
            base,
            "GET %s looks like a collection endpoint but declares no pagination parameter "
            "(looked for %s, ...)" % (raw_path, ", ".join(sorted(PAGINATION_PARAMETERS)[:6])),
        )

    def _is_collection_read(self, raw_path: str, operation: dict) -> bool:
        """True when a GET on this path plausibly reads a collection."""
        segments = self._path_segments(raw_path)
        if not segments:
            return False
        last = segments[-1]
        if _TEMPLATE_RE.fullmatch(last) or last.startswith(":"):
            return self._returns_collection(operation)
        if self._returns_collection(operation):
            return True
        return not self._is_verb(last)

    @staticmethod
    def _is_verb(segment: str) -> bool:
        lowered = segment.lower()
        return (lowered in _VERB_WORDS or bool(_VERB_PREFIX_RE.match(segment))) and lowered not in _NOUN_ALLOWLIST

    def _path_segments(self, raw_path: str) -> list:
        return [segment for segment in self._analysis_path(raw_path).split("/") if segment]

    def _lint_paths(self) -> None:
        collections: dict = {}
        for raw_path in sorted(self._paths()):
            location = "paths.%s" % raw_path
            analysis = self._analysis_path(raw_path)
            if len(analysis) > 1 and analysis.endswith("/"):
                self._add("path-trailing-slash", location, "the path ends with a trailing slash")
            segments = self._path_segments(raw_path)
            for index, segment in enumerate(segments):
                if _TEMPLATE_RE.fullmatch(segment):
                    continue
                if segment.startswith(":"):
                    continue  # AIP-136 custom verb suffix, e.g. /orders/{id}:cancel
                if segment != segment.lower():
                    self._add(
                        "path-kebab-case",
                        location,
                        "path segment %r contains upper-case characters" % segment,
                    )
                if "_" in segment:
                    self._add(
                        "path-kebab-case",
                        location,
                        "path segment %r uses underscores; kebab-case uses hyphens" % segment,
                    )
                if self._is_verb(segment):
                    self._add(
                        "path-verb-in-segment",
                        location,
                        "path segment %r looks like a verb" % segment,
                    )
                if self._collection_at(segments, index):
                    self._check_collection_name(location, segment, tuple(segments[:index]), collections)
            for parent, name, reason in self._implicit_collections(raw_path):
                self._check_collection_name(location, name, parent, collections, reason)

        for parent, names in sorted(collections.items()):
            plural = {name for name in names if _looks_plural(name)}
            singular = {name for name in names if _looks_singular(name)}
            if plural and singular:
                prefix = "/" + "/".join(parent)
                self._add(
                    "inconsistent-pluralisation",
                    "paths%s" % (prefix if prefix != "/" else ""),
                    "sibling collections under %s mix plural %s with singular %s"
                    % (prefix or "/", sorted(plural), sorted(singular)),
                )

    @staticmethod
    def _collection_at(segments: list, index: int) -> bool:
        """A segment directly followed by a resource-id template is a collection."""
        if index + 1 >= len(segments):
            return False
        return bool(_TEMPLATE_RE.fullmatch(segments[index + 1]))

    def _implicit_collections(self, raw_path: str) -> list:
        """Collections named by the final segment: reads that return arrays, or creates."""
        segments = self._path_segments(raw_path)
        if not segments:
            return []
        last = segments[-1]
        if _TEMPLATE_RE.fullmatch(last) or last.startswith(":") or self._is_verb(last):
            return []
        if _VERSION_SEGMENT_RE.match(last) or last.lower() in _NOUN_ALLOWLIST:
            return []
        out = []
        for method in ("get", "post"):
            operation = None
            item, _ref = self.resolver.deref(self._paths().get(raw_path))
            if _is_dict(item):
                operation = item.get(method)
                if not _is_dict(operation):
                    operation = None
            if operation is None:
                continue
            if method == "get" and self._returns_collection(operation):
                out.append((tuple(segments[:-1]), last, "a GET on it returns an array"))
            elif method == "post" and _is_dict(operation.get("requestBody")):
                out.append((tuple(segments[:-1]), last, "a POST on it takes a request body (create)"))
        return out

    def _check_collection_name(self, location: str, segment: str, parent: tuple, collections: dict, reason=None) -> None:
        lowered = segment.lower()
        # Record every collection name (plural or not) so that sibling paths can
        # be compared for consistency further down.
        collections.setdefault(parent, set()).add(lowered)
        if not _looks_singular(lowered):
            return
        detail = " (%s)" % reason if reason else " (it is followed by a resource id)"
        self._add(
            "path-not-plural",
            location,
            "collection segment %r is not plural%s" % (segment, detail),
        )

    def _lint_path_parameters(self) -> None:
        for raw_path in sorted(self._paths()):
            item, _ref = self.resolver.deref(self._paths()[raw_path])
            if not _is_dict(item):
                continue
            location = "paths.%s" % raw_path
            declared = {}
            for method in HTTP_METHODS:
                operation = item.get(method)
                if not _is_dict(operation):
                    continue
                for source in (as_list(item.get("parameters")), as_list(operation.get("parameters"))):
                    for raw in source:
                        parameter, _ref = self.resolver.deref(raw)
                        if _is_dict(parameter) and str(parameter.get("in")) == "path":
                            declared[str(parameter.get("name"))] = parameter
            templates = set(_TEMPLATE_RE.findall(self._analysis_path(raw_path)))
            for name in sorted(templates):
                if name not in declared:
                    self._add(
                        "path-parameter-not-declared",
                        location,
                        "path template {%s} has no matching path parameter definition" % name,
                    )
            for name, parameter in sorted(declared.items()):
                if name not in templates:
                    self._add(
                        "path-parameter-not-declared",
                        location,
                        "path parameter %r is declared but does not appear in the path template" % name,
                    )
                elif parameter.get("required") is not True:
                    self._add(
                        "path-parameter-not-required",
                        location,
                        "path parameter %r must be declared with required: true" % name,
                    )

    def _lint_component_descriptions(self) -> None:
        components = self.document.get("components")
        schemas = components.get("schemas") if _is_dict(components) else None
        if not _is_dict(schemas):
            return
        for name, schema in sorted(schemas.items()):
            resolved, _ref = self.resolver.deref(schema)
            if not _is_dict(resolved):
                continue
            description = resolved.get("description")
            if not (isinstance(description, str) and description.strip()):
                self._add(
                    "schema-description-missing",
                    "components.schemas.%s" % name,
                    "component schema %r has no description" % name,
                )


def lint_document(document: dict, base_path: str | None = None) -> list:
    """Return the list of :class:`LintIssue` for one parsed OpenAPI document."""
    return DesignLinter(document, base_path).run()


def lint_summary(issues: list) -> dict:
    return {
        "error": sum(1 for issue in issues if issue.severity == ERROR),
        "warning": sum(1 for issue in issues if issue.severity == WARNING),
        "info": sum(1 for issue in issues if issue.severity == INFO),
        "total": len(issues),
    }
