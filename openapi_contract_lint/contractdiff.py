"""Contract diff engine for ``openapi-contract-diff``.

Compares two OpenAPI 3.x documents and classifies every detected change as
``breaking``, ``potentially-breaking`` or ``compatible``.

The classification is a documented judgement call, not a truth mandated by the
OpenAPI Specification: the specification describes how to *write* an API
description, not which edits are safe for existing clients.  Every rule, its
severity and the reasoning behind it is listed in the README table.

Local ``$ref`` values (``#/components/...`` and any other same-document JSON
pointer) are resolved transparently.  ``$ref`` values that point at another
file or a URL are *not* fetched: the referencing node is treated as opaque and
each such ref is reported once in ``DiffResult.warnings``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BREAKING = "breaking"
POTENTIALLY_BREAKING = "potentially-breaking"
COMPATIBLE = "compatible"

SEVERITIES = (BREAKING, POTENTIALLY_BREAKING, COMPATIBLE)
SEVERITY_RANK = {COMPATIBLE: 0, POTENTIALLY_BREAKING: 1, BREAKING: 2}

#: Threshold values accepted by ``--fail-on``.
FAIL_ON_CHOICES = (BREAKING, POTENTIALLY_BREAKING, "never")

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

#: Keys of a Security Scheme Object that form the security contract.
_SECURITY_SCHEME_KEYS = ("type", "scheme", "in", "name", "bearerFormat", "openIdConnectUrl", "flows")

#: Constraint keywords and the direction that makes them stricter.
_TIGHTEN_UP = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
_TIGHTEN_DOWN = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")
_CONSTRAINTS = _TIGHTEN_UP + _TIGHTEN_DOWN + ("pattern", "multipleOf", "uniqueItems")

_MISSING = object()


@dataclass
class Finding:
    """One detected difference between the two documents."""

    severity: str
    kind: str
    location: str
    message: str
    old: object = None
    new: object = None
    note: str | None = None

    def as_dict(self) -> dict:
        data = {
            "severity": self.severity,
            "kind": self.kind,
            "location": self.location,
            "message": self.message,
        }
        if self.old is not None:
            data["old"] = self.old
        if self.new is not None:
            data["new"] = self.new
        if self.note:
            data["note"] = self.note
        return data


@dataclass
class DiffResult:
    findings: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    old_openapi: str = ""
    new_openapi: str = ""
    old_operation_count: int = 0
    new_operation_count: int = 0

    def count(self, severity: str) -> int:
        return sum(1 for finding in self.findings if finding.severity == severity)

    @property
    def breaking(self) -> int:
        return self.count(BREAKING)

    @property
    def potentially_breaking(self) -> int:
        return self.count(POTENTIALLY_BREAKING)

    @property
    def compatible(self) -> int:
        return self.count(COMPATIBLE)

    def summary(self) -> dict:
        return {
            "breaking": self.breaking,
            "potentially_breaking": self.potentially_breaking,
            "compatible": self.compatible,
            "total": len(self.findings),
        }

    def exit_code(self, fail_on: str) -> int:
        """0 when nothing at or above ``fail_on`` was found, else 1."""
        if fail_on == "never":
            return 0
        if fail_on == POTENTIALLY_BREAKING:
            return 1 if (self.breaking or self.potentially_breaking) else 0
        return 1 if self.breaking else 0


# --------------------------------------------------------------------------
# reference resolution
# --------------------------------------------------------------------------
def resolve_pointer(document, pointer: str):
    """Resolve a same-document JSON pointer such as ``#/components/schemas/Pet``."""
    if not pointer.startswith("#"):
        return _MISSING
    fragment = pointer[1:]
    if fragment in ("", "/"):
        return document
    if not fragment.startswith("/"):
        return _MISSING
    node = document
    for raw in fragment[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return _MISSING
            node = node[token]
        elif isinstance(node, list):
            if not token.isdigit() or int(token) >= len(node):
                return _MISSING
            node = node[int(token)]
        else:
            return _MISSING
    return node


class _Resolver:
    """Follows local ``$ref`` chains and records external refs it cannot follow."""

    def __init__(self, document, label: str):
        self.document = document if isinstance(document, dict) else {}
        self.label = label
        self.external_refs: set = set()

    def deref(self, node):
        """Return ``(resolved_node, ref_name)``.

        ``resolved_node`` is ``None`` when a local reference cannot be
        resolved, and is the untouched node (still carrying ``$ref``) when the
        reference points outside the document.
        """
        name = None
        seen: set = set()
        while isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if not ref.startswith("#"):
                self.external_refs.add(ref)
                return node, ref
            if ref in seen:
                return {}, name
            seen.add(ref)
            target = resolve_pointer(self.document, ref)
            if target is _MISSING:
                return None, ref
            name = ref
            node = target
        return node, name


def _is_dict(value) -> bool:
    return isinstance(value, dict)


def _safe_list(value) -> list:
    return value if isinstance(value, list) else []


def _render_set(values) -> str:
    return "{" + ", ".join(sorted(str(v) for v in values)) + "}"


def _deep_equal(left, right) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(_deep_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(_deep_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


class ContractDiffer:
    """Compares two parsed OpenAPI documents."""

    def __init__(self, old: dict, new: dict, base_path: str | None = None):
        self.old = old if isinstance(old, dict) else {}
        self.new = new if isinstance(new, dict) else {}
        self.base_path = self._normalise_base_path(base_path)
        self.old_res = _Resolver(self.old, "old")
        self.new_res = _Resolver(self.new, "new")
        self.result = DiffResult()
        self._stack: set = set()

    # -- small helpers ----------------------------------------------------
    @staticmethod
    def _normalise_base_path(base_path: str | None) -> str:
        if not base_path:
            return ""
        value = str(base_path).strip()
        if not value or value == "/":
            return ""
        if not value.startswith("/"):
            value = "/" + value
        return value.rstrip("/")

    def _add(self, severity, kind, location, message, old=None, new=None, note=None):
        self.result.findings.append(Finding(severity, kind, location, message, old, new, note))

    def _warn(self, message: str) -> None:
        if message not in self.result.warnings:
            self.result.warnings.append(message)

    def _strip_base(self, path: str) -> str:
        if not self.base_path:
            return path
        if path == self.base_path:
            return "/"
        if path.startswith(self.base_path + "/"):
            return path[len(self.base_path) :]
        return path

    # -- entry point ------------------------------------------------------
    def run(self) -> DiffResult:
        self.result.old_openapi = self._spec_version(self.old)
        self.result.new_openapi = self._spec_version(self.new)
        self._compare_spec_meta()
        self._compare_servers()
        self._compare_security_schemes()
        old_paths = self._path_map(self.old, "old")
        new_paths = self._path_map(self.new, "new")
        self._note_base_path_usage(old_paths, new_paths)
        self._compare_paths(old_paths, new_paths)
        self._compare_component_schemas()
        for resolver in (self.old_res, self.new_res):
            for ref in sorted(resolver.external_refs):
                self._warn(
                    "external $ref %r in the %s document was not fetched; "
                    "the referencing node was treated as opaque" % (ref, resolver.label)
                )
        self.result.findings.sort(key=lambda f: (-SEVERITY_RANK.get(f.severity, 0), f.location, f.kind))
        deduped: list = []
        seen: set = set()
        for finding in self.result.findings:
            key = (finding.severity, finding.kind, finding.location, finding.message)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(finding)
        self.result.findings = deduped
        return self.result

    @staticmethod
    def _spec_version(document: dict) -> str:
        value = document.get("openapi")
        if value is None and "swagger" in document:
            value = document.get("swagger")
        return str(value) if value is not None else ""

    def _compare_spec_meta(self) -> None:
        if "swagger" in self.old or "swagger" in self.new:
            self._warn(
                "an OpenAPI 2.0 (Swagger) document was detected; this tool only understands "
                "OpenAPI 3.x, so results for it are unreliable"
            )
        if self.result.old_openapi != self.result.new_openapi:
            self._add(
                COMPATIBLE,
                "spec-version-changed",
                "openapi",
                "OpenAPI version changed from %r to %r"
                % (self.result.old_openapi, self.result.new_openapi),
                self.result.old_openapi,
                self.result.new_openapi,
                "the wire contract is unchanged, but tooling built for one version may need updates",
            )

    def _compare_servers(self) -> None:
        old_urls = [str(s.get("url")) for s in _safe_list(self.old.get("servers")) if _is_dict(s) and s.get("url")]
        new_urls = [str(s.get("url")) for s in _safe_list(self.new.get("servers")) if _is_dict(s) and s.get("url")]
        if sorted(set(old_urls)) != sorted(set(new_urls)):
            self._add(
                POTENTIALLY_BREAKING,
                "servers-changed",
                "servers",
                "server URLs changed from %s to %s" % (old_urls or "[]", new_urls or "[]"),
                old_urls,
                new_urls,
                "use --base-path to compare documents whose only difference is a version prefix",
            )

    def _compare_security_schemes(self) -> None:
        old_schemes = self._component_map(self.old, "securitySchemes", self.old_res)
        new_schemes = self._component_map(self.new, "securitySchemes", self.new_res)
        for name in sorted(old_schemes):
            location = "components.securitySchemes.%s" % name
            if name not in new_schemes:
                self._add(
                    BREAKING,
                    "security-scheme-removed",
                    location,
                    "security scheme %r was removed" % name,
                    name,
                    None,
                    "clients authenticating with this scheme can no longer call the API",
                )
                continue
            old_scheme = self.old_res.deref(old_schemes[name])[0] or {}
            new_scheme = self.new_res.deref(new_schemes[name])[0] or {}
            changed = [
                key
                for key in _SECURITY_SCHEME_KEYS
                if not _deep_equal(old_scheme.get(key, None), new_scheme.get(key, None))
            ]
            if changed:
                details = ", ".join(
                    "%s: %r -> %r" % (key, old_scheme.get(key), new_scheme.get(key)) for key in changed
                )
                self._add(
                    BREAKING,
                    "security-scheme-changed",
                    location,
                    "security scheme %r changed (%s)" % (name, details),
                    {k: old_scheme.get(k) for k in changed},
                    {k: new_scheme.get(k) for k in changed},
                    "any change to how credentials are supplied requires client changes",
                )

    def _component_map(self, document: dict, key: str, resolver: _Resolver) -> dict:
        components = document.get("components")
        if not _is_dict(components):
            return {}
        section = components.get(key)
        if not _is_dict(section):
            return {}
        out = {}
        for name, value in section.items():
            resolved, _ref = resolver.deref(value)
            out[str(name)] = resolved if resolved is not None else {}
        return out

    def _path_map(self, document: dict, label: str) -> dict:
        paths = document.get("paths")
        if not _is_dict(paths):
            self._warn("the %s document has no usable 'paths' object" % label)
            return {}
        resolver = self.old_res if label == "old" else self.new_res
        out = {}
        for raw_path, item in paths.items():
            path = self._strip_base(str(raw_path))
            resolved, _ref = resolver.deref(item)
            if path in out:
                self._warn(
                    "the %s document has two paths that collapse to %r after --base-path stripping; "
                    "the last one wins" % (label, path)
                )
            out[path] = resolved if _is_dict(resolved) else {}
        return out

    def _note_base_path_usage(self, old_paths: dict, new_paths: dict) -> None:
        if not self.base_path:
            return
        for label, paths in (("old", old_paths), ("new", new_paths)):
            if paths and not any(str(p).startswith("/") and p != "/" for p in paths):
                continue
            if not paths:
                continue
            raw_stripped = [
                p
                for p in paths
                if p == self.base_path or str(p).startswith(self.base_path + "/")
            ]
            if not raw_stripped:
                self._warn(
                    "--base-path %r matched no path in the %s document; no prefix was stripped"
                    % (self.base_path, label)
                )

    # -- paths / operations ----------------------------------------------
    def _compare_paths(self, old_paths: dict, new_paths: dict) -> None:
        self.result.old_operation_count = self._count_operations(old_paths)
        self.result.new_operation_count = self._count_operations(new_paths)
        for path in sorted(old_paths):
            methods = sorted(m for m in HTTP_METHODS if _is_dict(old_paths[path].get(m)))
            if path not in new_paths:
                self._add(
                    BREAKING,
                    "endpoint-removed",
                    "paths.%s" % path,
                    "path %s was removed (methods: %s)" % (path, ", ".join(m.upper() for m in methods) or "none"),
                    path,
                    None,
                    "every client calling this path breaks, whatever method it used",
                )
        for path in sorted(new_paths):
            if path not in old_paths:
                methods = sorted(m for m in HTTP_METHODS if _is_dict(new_paths[path].get(m)))
                self._add(
                    COMPATIBLE,
                    "endpoint-added",
                    "paths.%s" % path,
                    "path %s was added (methods: %s)" % (path, ", ".join(m.upper() for m in methods) or "none"),
                    None,
                    path,
                )
                continue
            old_item, new_item = old_paths[path], new_paths[path]
            for method in HTTP_METHODS:
                old_op = old_item.get(method)
                new_op = new_item.get(method)
                if _is_dict(old_op) and not _is_dict(new_op):
                    self._add(
                        BREAKING,
                        "operation-removed",
                        "paths.%s.%s" % (path, method),
                        "%s %s was removed" % (method.upper(), path),
                        "%s %s" % (method.upper(), path),
                        None,
                    )
                elif _is_dict(new_op) and not _is_dict(old_op):
                    self._add(
                        COMPATIBLE,
                        "operation-added",
                        "paths.%s.%s" % (path, method),
                        "%s %s was added" % (method.upper(), path),
                        None,
                        "%s %s" % (method.upper(), path),
                    )
                elif _is_dict(old_op) and _is_dict(new_op):
                    self._compare_operation(path, method, old_item, new_item, old_op, new_op)

    @staticmethod
    def _count_operations(paths: dict) -> int:
        return sum(1 for item in paths.values() if _is_dict(item) for m in HTTP_METHODS if _is_dict(item.get(m)))

    def _compare_operation(self, path, method, old_item, new_item, old_op, new_op) -> None:
        base = "paths.%s.%s" % (path, method)
        old_id, new_id = old_op.get("operationId"), new_op.get("operationId")
        if isinstance(old_id, str) and isinstance(new_id, str) and old_id != new_id:
            self._add(
                POTENTIALLY_BREAKING,
                "operation-id-changed",
                base + ".operationId",
                "operationId changed from %r to %r" % (old_id, new_id),
                old_id,
                new_id,
                "the wire contract is unchanged, but generated clients and operationId-based tooling break",
            )
        if not old_op.get("deprecated") and new_op.get("deprecated"):
            self._add(
                COMPATIBLE,
                "operation-deprecated",
                base + ".deprecated",
                "the operation is now marked deprecated",
                None,
                True,
                "still callable; plan a migration",
            )
        old_params = self._effective_parameters(old_item, old_op, self.old_res)
        new_params = self._effective_parameters(new_item, new_op, self.new_res)
        self._compare_parameters(base, path, method, old_params, new_params)
        self._compare_request_body(base, old_op, new_op)
        self._compare_responses(base, old_op.get("responses"), new_op.get("responses"))
        self._compare_operation_security(base, old_op, new_op)

    def _effective_parameters(self, item: dict, operation: dict, resolver: _Resolver) -> dict:
        out = {}
        for source in (_safe_list(item.get("parameters")), _safe_list(operation.get("parameters"))):
            for raw in source:
                resolved, _ref = resolver.deref(raw)
                if not _is_dict(resolved):
                    continue
                key = (str(resolved.get("in")), str(resolved.get("name")))
                out[key] = resolved
        return out

    def _compare_parameters(self, base, path, method, old_params, new_params) -> None:
        old_by_name = {}
        new_by_name = {}
        for (where, name), param in old_params.items():
            old_by_name.setdefault(name, []).append((where, param))
        for (where, name), param in new_params.items():
            new_by_name.setdefault(name, []).append((where, param))

        relocated = set()
        for name in sorted(set(old_by_name) & set(new_by_name)):
            old_wheres = {w for w, _ in old_by_name[name]}
            new_wheres = {w for w, _ in new_by_name[name]}
            if not (old_wheres & new_wheres):
                old_where = sorted(old_wheres)[0]
                new_where = sorted(new_wheres)[0]
                relocated.add(name)
                self._add(
                    BREAKING,
                    "parameter-location-changed",
                    "%s.parameters.%s.%s" % (base, new_where, name),
                    "parameter %r moved from %s to %s" % (name, old_where, new_where),
                    old_where,
                    new_where,
                    "the value now travels in a different part of the request, so clients must change",
                )

        for (where, name), param in sorted(old_params.items()):
            if name in relocated or (where, name) in new_params:
                continue
            relation = "%s.%s" % (where, name)
            if param.get("required"):
                self._add(
                    BREAKING,
                    "parameter-removed",
                    "%s.parameters.%s" % (base, relation),
                    "required parameter %r (%s) was removed" % (name, where),
                    relation,
                    None,
                    "clients still send it; servers that reject unknown parameters now fail those calls",
                )
            else:
                self._add(
                    POTENTIALLY_BREAKING,
                    "parameter-removed",
                    "%s.parameters.%s" % (base, relation),
                    "optional parameter %r (%s) was removed" % (name, where),
                    relation,
                    None,
                    "clients that relied on it lose functionality, or get a 400 if unknown parameters are rejected",
                )

        for (where, name), param in sorted(new_params.items()):
            if name in relocated or (where, name) in old_params:
                continue
            relation = "%s.%s" % (where, name)
            if param.get("required"):
                self._add(
                    BREAKING,
                    "parameter-required-added",
                    "%s.parameters.%s" % (base, relation),
                    "new required parameter %r (%s) was added" % (name, where),
                    None,
                    relation,
                    "existing clients do not send it, so their requests are now invalid",
                )
            else:
                self._add(
                    COMPATIBLE,
                    "parameter-added-optional",
                    "%s.parameters.%s" % (base, relation),
                    "new optional parameter %r (%s) was added" % (name, where),
                    None,
                    relation,
                )

        for (where, name), old_param in sorted(old_params.items()):
            new_param = new_params.get((where, name))
            if new_param is None:
                continue
            self._compare_parameter(base, where, name, old_param, new_param)

    def _compare_parameter(self, base, where, name, old_param, new_param) -> None:
        location = "%s.parameters.%s.%s" % (base, where, name)
        old_required = bool(old_param.get("required"))
        new_required = bool(new_param.get("required"))
        if old_required != new_required:
            if new_required:
                self._add(
                    BREAKING,
                    "parameter-became-required",
                    location + ".required",
                    "parameter %r became required" % name,
                    False,
                    True,
                    "requests that omitted it are now invalid",
                )
            else:
                self._add(
                    COMPATIBLE,
                    "parameter-became-optional",
                    location + ".required",
                    "parameter %r is no longer required" % name,
                    True,
                    False,
                )
        for key in ("style", "explode", "allowReserved"):
            old_value = old_param.get(key)
            new_value = new_param.get(key)
            if old_value != new_value:
                self._add(
                    POTENTIALLY_BREAKING,
                    "parameter-serialization-changed",
                    "%s.%s" % (location, key),
                    "parameter %r %s changed from %r to %r" % (name, key, old_value, new_value),
                    old_value,
                    new_value,
                    "the same logical value is now encoded differently on the wire",
                )
        self._compare_schema(
            old_param.get("schema"),
            new_param.get("schema"),
            location + ".schema",
            "request",
        )

    def _compare_request_body(self, base, old_op, new_op) -> None:
        location = base + ".requestBody"
        old_body, _old_ref = self.old_res.deref(old_op.get("requestBody"))
        new_body, _new_ref = self.new_res.deref(new_op.get("requestBody"))
        old_body = old_body if _is_dict(old_body) else None
        new_body = new_body if _is_dict(new_body) else None
        if old_body is None and new_body is None:
            return
        if old_body is None and new_body is not None:
            if new_body.get("required"):
                self._add(
                    BREAKING,
                    "request-body-added-required",
                    location,
                    "a required request body was added",
                    None,
                    True,
                    "existing clients send no body and are now invalid",
                )
            else:
                self._add(COMPATIBLE, "request-body-added-optional", location, "an optional request body was added")
            self._compare_content(location + ".content", old_body, new_body, "request")
            return
        if new_body is None:
            self._add(
                POTENTIALLY_BREAKING,
                "request-body-removed",
                location,
                "the request body was removed",
                True,
                None,
                "clients that still send a body may be rejected or silently ignored",
            )
            return
        old_required = bool(old_body.get("required"))
        new_required = bool(new_body.get("required"))
        if old_required != new_required:
            if new_required:
                self._add(
                    BREAKING,
                    "request-body-became-required",
                    location + ".required",
                    "the request body became required",
                    False,
                    True,
                )
            else:
                self._add(
                    COMPATIBLE,
                    "request-body-became-optional",
                    location + ".required",
                    "the request body is no longer required",
                    True,
                    False,
                )
        self._compare_content(location + ".content", old_body, new_body, "request")

    def _compare_content(self, location, old_holder, new_holder, context) -> None:
        old_content = old_holder.get("content") if _is_dict(old_holder) else None
        new_content = new_holder.get("content") if _is_dict(new_holder) else None
        old_content = old_content if _is_dict(old_content) else {}
        new_content = new_content if _is_dict(new_content) else {}
        resolver_pair = (self.old_res, self.new_res)
        for media_type in sorted(old_content):
            if media_type not in new_content:
                self._add(
                    BREAKING,
                    "content-type-removed",
                    "%s.%s" % (location, media_type),
                    "media type %r was removed" % media_type,
                    media_type,
                    None,
                    "clients that negotiated this media type can no longer use it",
                )
        for media_type in sorted(new_content):
            if media_type not in old_content:
                self._add(
                    COMPATIBLE,
                    "content-type-added",
                    "%s.%s" % (location, media_type),
                    "media type %r was added" % media_type,
                    None,
                    media_type,
                )
        for media_type in sorted(set(old_content) & set(new_content)):
            old_media, _r1 = resolver_pair[0].deref(old_content[media_type])
            new_media, _r2 = resolver_pair[1].deref(new_content[media_type])
            base = "%s.%s.schema" % (location, media_type)
            if not _is_dict(old_media) or not _is_dict(new_media):
                continue
            self._compare_schema(old_media.get("schema"), new_media.get("schema"), base, context)

    def _compare_responses(self, base, old_responses, new_responses) -> None:
        old_responses = old_responses if _is_dict(old_responses) else {}
        new_responses = new_responses if _is_dict(new_responses) else {}
        old_keys = {str(k) for k in old_responses}
        new_keys = {str(k) for k in new_responses}
        old_lookup = {str(k): v for k, v in old_responses.items()}
        new_lookup = {str(k): v for k, v in new_responses.items()}
        for status in sorted(old_keys - new_keys):
            self._add(
                BREAKING,
                "response-status-removed",
                "%s.responses.%s" % (base, status),
                "response status %s was removed" % status,
                status,
                None,
                "clients handling this status no longer receive it",
            )
        for status in sorted(new_keys - old_keys):
            self._add(
                COMPATIBLE,
                "response-status-added",
                "%s.responses.%s" % (base, status),
                "response status %s was added" % status,
                None,
                status,
            )
        for status in sorted(old_keys & new_keys):
            location = "%s.responses.%s" % (base, status)
            old_response, _r1 = self.old_res.deref(old_lookup[status])
            new_response, _r2 = self.new_res.deref(new_lookup[status])
            if not _is_dict(old_response) or not _is_dict(new_response):
                continue
            old_headers = old_response.get("headers") if _is_dict(old_response.get("headers")) else {}
            new_headers = new_response.get("headers") if _is_dict(new_response.get("headers")) else {}
            for header in sorted({str(h) for h in old_headers} - {str(h) for h in new_headers}):
                self._add(
                    BREAKING,
                    "response-header-removed",
                    "%s.headers.%s" % (location, header),
                    "response header %r was removed" % header,
                    header,
                    None,
                    "clients that read this header (rate limits, pagination, ETags) break",
                )
            for header in sorted({str(h) for h in new_headers} - {str(h) for h in old_headers}):
                self._add(
                    COMPATIBLE,
                    "response-header-added",
                    "%s.headers.%s" % (location, header),
                    "response header %r was added" % header,
                    None,
                    header,
                )
            self._compare_content(location + ".content", old_response, new_response, "response")

    def _compare_operation_security(self, base, old_op, new_op) -> None:
        old_security = self._effective_security(self.old, old_op)
        new_security = self._effective_security(self.new, new_op)
        old_sets = self._security_sets(old_security)
        new_sets = self._security_sets(new_security)
        if old_sets is None and new_sets is None:
            return
        location = base + ".security"
        if old_sets is None:
            if new_sets and frozenset() not in new_sets:
                self._add(
                    BREAKING,
                    "security-requirement-added",
                    location,
                    "the operation now requires authentication (%s)"
                    % ", ".join(sorted("+".join(sorted(s)) or "anonymous" for s in new_sets)),
                    None,
                    sorted(sorted(s) for s in new_sets),
                    "unauthenticated clients are now rejected",
                )
            return
        if new_sets is None:
            self._add(
                COMPATIBLE,
                "security-requirement-removed",
                location,
                "authentication is no longer required",
                sorted(sorted(s) for s in old_sets),
                None,
            )
            return
        for requirement in sorted(old_sets - new_sets, key=lambda s: sorted(s)):
            label = "+".join(sorted(requirement)) or "anonymous"
            self._add(
                BREAKING,
                "security-requirement-removed",
                location,
                "the %s authentication option is no longer accepted" % label,
                label,
                None,
                "clients authenticating this way are now rejected",
            )
        for requirement in sorted(new_sets - old_sets, key=lambda s: sorted(s)):
            label = "+".join(sorted(requirement)) or "anonymous"
            self._add(
                COMPATIBLE,
                "security-requirement-added",
                location,
                "a new %s authentication option was added" % label,
                None,
                label,
            )

    @staticmethod
    def _effective_security(document: dict, operation: dict):
        if "security" in operation:
            return operation.get("security")
        if "security" in document:
            return document.get("security")
        return None

    @staticmethod
    def _security_sets(security):
        if security is None or not isinstance(security, list):
            return None
        out = set()
        for requirement in security:
            if isinstance(requirement, dict):
                out.add(frozenset(str(k) for k in requirement))
        return out

    # -- schemas ----------------------------------------------------------
    def _flatten(self, schema, resolver: _Resolver, seen=None) -> dict:
        """Resolve ``$ref`` and merge ``allOf`` members into one schema object."""
        if not _is_dict(schema):
            return {}
        seen = set() if seen is None else seen
        out = {key: value for key, value in schema.items() if key != "allOf"}
        for raw in _safe_list(schema.get("allOf")):
            member, ref = resolver.deref(raw)
            if not _is_dict(member) or (ref and ref in seen):
                continue
            merged = self._flatten(member, resolver, seen | {ref} if ref else seen)
            for key, value in merged.items():
                if key == "properties" and _is_dict(value):
                    combined = dict(value)
                    combined.update(out.get("properties") or {})
                    out["properties"] = combined
                elif key == "required" and isinstance(value, list):
                    required = list(value)
                    for name in out.get("required") or []:
                        if name not in required:
                            required.append(name)
                    out["required"] = required
                elif key not in out:
                    out[key] = value
        return out

    @staticmethod
    def _types(schema: dict) -> set:
        value = schema.get("type")
        if isinstance(value, str):
            return {value}
        if isinstance(value, list):
            return {str(item) for item in value}
        return set()

    @classmethod
    def _nullable(cls, schema: dict) -> bool:
        if schema.get("nullable") is True:
            return True
        return "null" in cls._types(schema)

    def _compare_schema(self, old_raw, new_raw, location, context) -> None:
        old_schema, old_ref = self.old_res.deref(old_raw)
        new_schema, new_ref = self.new_res.deref(new_raw)
        if new_schema is None:
            self._add(
                BREAKING,
                "schema-unresolvable",
                location,
                "the new document references %r, which does not exist" % (new_ref or new_raw),
                None,
                new_ref,
                "a dangling $ref means the schema is no longer defined anywhere",
            )
            return
        if old_schema is None:
            self._warn(
                "the old document references %r, which does not exist; that part of the diff was skipped"
                % (old_ref or old_raw)
            )
            return
        if not _is_dict(old_schema) or not _is_dict(new_schema):
            return
        if old_ref and new_ref:
            pair = (old_ref, new_ref)
            if pair in self._stack:
                return
            self._stack.add(pair)
            try:
                self._compare_schema_bodies(
                    old_schema, new_schema, self._canonical_location(location, old_ref, context), context
                )
            finally:
                self._stack.discard(pair)
            return
        self._compare_schema_bodies(old_schema, new_schema, location, context)

    @staticmethod
    def _canonical_location(location: str, old_ref: str, context: str) -> str:
        """Report changes inside a named component once, at the component.

        A component schema can be referenced from many operations; without this
        every affected operation would repeat the same finding.  The direction
        of use is kept in the location because the same change is breaking on a
        request schema and only potentially breaking on a response schema.
        """
        marker = "#/components/schemas/"
        if not old_ref.startswith(marker):
            return location
        name = old_ref[len(marker) :]
        suffix = "" if context in ("response", "component") else " (used as request)"
        return "components.schemas.%s%s" % (name, suffix)

    def _compare_schema_bodies(self, old_schema, new_schema, location, context) -> None:
        old_schema = self._flatten(old_schema, self.old_res)
        new_schema = self._flatten(new_schema, self.new_res)

        old_types = self._types(old_schema) - {"null"}
        new_types = self._types(new_schema) - {"null"}
        if old_types != new_types:
            if not old_types:
                severity, note = POTENTIALLY_BREAKING, "the old schema did not restrict the type"
            elif not new_types:
                severity, note = COMPATIBLE, "the new schema no longer restricts the type"
            else:
                severity, note = BREAKING, None
            self._add(
                severity,
                "type-changed",
                location + ".type",
                "type changed from %s to %s" % (_render_set(old_types) if old_types else "any",
                                                _render_set(new_types) if new_types else "any"),
                sorted(old_types),
                sorted(new_types),
                note,
            )

        old_format, new_format = old_schema.get("format"), new_schema.get("format")
        if old_format != new_format:
            note = None
            if new_format is None:
                note = "dropping a format widens the accepted values; clients that validated against it may still break"
            elif old_format is None:
                note = "adding a format narrows the accepted values"
            self._add(
                BREAKING,
                "format-changed",
                location + ".format",
                "format changed from %r to %r" % (old_format, new_format),
                old_format,
                new_format,
                note,
            )

        old_nullable, new_nullable = self._nullable(old_schema), self._nullable(new_schema)
        if old_nullable and not new_nullable:
            self._add(
                BREAKING,
                "nullable-removed",
                location + ".nullable",
                "the value is no longer nullable",
                True,
                False,
                "servers or clients that sent or accepted null now fail",
            )
        elif new_nullable and not old_nullable:
            self._add(
                POTENTIALLY_BREAKING,
                "nullable-added",
                location + ".nullable",
                "the value may now be null",
                False,
                True,
                "the type was widened, so clients must now handle a null they never saw before",
            )

        self._compare_const_schema(old_schema, new_schema, location, context)
        self._compare_enum(old_schema, new_schema, location, context)
        self._compare_default(old_schema, new_schema, location)
        self._compare_constraint_keywords(old_schema, new_schema, location)
        self._compare_properties(old_schema, new_schema, location, context)
        self._compare_additional_properties(old_schema, new_schema, location, context)
        self._compare_items(old_schema, new_schema, location, context)
        self._compare_composition(old_schema, new_schema, location, context)

    def _compare_const_schema(self, old_schema, new_schema, location, context) -> None:
        if "const" not in old_schema and "const" not in new_schema:
            return
        old_const, new_const = old_schema.get("const", _MISSING), new_schema.get("const", _MISSING)
        if old_const is not _MISSING and new_const is not _MISSING and old_const == new_const:
            return
        self._add(
            BREAKING,
            "const-changed",
            location + ".const",
            "const changed from %r to %r"
            % (None if old_const is _MISSING else old_const, None if new_const is _MISSING else new_const),
            None if old_const is _MISSING else old_const,
            None if new_const is _MISSING else new_const,
        )

    def _compare_enum(self, old_schema, new_schema, location, context) -> None:
        old_enum = old_schema.get("enum")
        new_enum = new_schema.get("enum")
        old_is_list, new_is_list = isinstance(old_enum, list), isinstance(new_enum, list)
        if not old_is_list and not new_is_list:
            return
        if old_is_list and new_is_list:
            removed = [value for value in old_enum if value not in new_enum]
            added = [value for value in new_enum if value not in old_enum]
            if removed:
                self._add(
                    BREAKING,
                    "enum-value-removed",
                    location + ".enum",
                    "enum values removed: %r" % (removed,),
                    removed,
                    None,
                    "any client that sends or matches a removed value breaks",
                )
            if added:
                self._add(
                    COMPATIBLE,
                    "enum-value-added",
                    location + ".enum",
                    "enum values added: %r" % (added,),
                    None,
                    added,
                    "additive, but clients whose switches are exhaustive over the old set may still need updating; "
                    "server-side enums should declare that undocumented values can appear",
                )
            return
        if new_is_list:
            severity = BREAKING if context == "request" else COMPATIBLE
            self._add(
                severity,
                "enum-constraint-added",
                location + ".enum",
                "an enum was introduced: %r" % (new_enum,),
                None,
                new_enum,
                "values outside the enum are no longer valid"
                if context == "request"
                else "the server now promises a closed set of values",
            )
        else:
            severity = COMPATIBLE if context == "request" else POTENTIALLY_BREAKING
            self._add(
                severity,
                "enum-constraint-removed",
                location + ".enum",
                "the enum restriction was removed (was %r)" % (old_enum,),
                old_enum,
                None,
                "the set of values is now open, so clients may see values they never saw before"
                if context != "request"
                else None,
            )

    def _compare_default(self, old_schema, new_schema, location) -> None:
        old_has, new_has = "default" in old_schema, "default" in new_schema
        if not old_has and not new_has:
            return
        old_default, new_default = old_schema.get("default"), new_schema.get("default")
        if old_has and not new_has:
            self._add(
                POTENTIALLY_BREAKING,
                "default-removed",
                location + ".default",
                "the default value %r was removed" % (old_default,),
                old_default,
                None,
                "omitting the field now has an unspecified effect",
            )
        elif new_has and not old_has:
            self._add(
                POTENTIALLY_BREAKING,
                "default-added",
                location + ".default",
                "a default value %r was added" % (new_default,),
                None,
                new_default,
                "requests that omit the field now behave differently from before",
            )
        elif old_default != new_default:
            self._add(
                POTENTIALLY_BREAKING,
                "default-changed",
                location + ".default",
                "default changed from %r to %r" % (old_default, new_default),
                old_default,
                new_default,
                "clients that omit the field silently get different behaviour; "
                "defaults are often documentation-only, so this is not counted as breaking",
            )

    def _compare_constraint_keywords(self, old_schema, new_schema, location) -> None:
        for keyword in _CONSTRAINTS:
            old_has, new_has = keyword in old_schema, keyword in new_schema
            if not old_has and not new_has:
                continue
            old_value, new_value = old_schema.get(keyword), new_schema.get(keyword)
            if old_has and new_has and old_value == new_value:
                continue
            direction = self._constraint_direction(keyword, old_value, new_value, old_has, new_has)
            if direction == "tightened":
                severity, kind = POTENTIALLY_BREAKING, "constraint-tightened"
            elif direction == "relaxed":
                severity, kind = COMPATIBLE, "constraint-relaxed"
            else:
                severity, kind = POTENTIALLY_BREAKING, "constraint-changed"
            self._add(
                severity,
                kind,
                "%s.%s" % (location, keyword),
                "%s changed from %r to %r" % (keyword, old_value, new_value),
                old_value,
                new_value,
                "the tool cannot know whether any client exceeds the old bound, so this is advisory",
            )

    @staticmethod
    def _constraint_direction(keyword, old_value, new_value, old_has, new_has) -> str:
        if keyword == "uniqueItems":
            if new_has and new_value is True and (not old_has or old_value is False):
                return "tightened"
            return "relaxed"
        if not old_has:
            return "tightened"
        if not new_has:
            return "relaxed"
        if keyword in ("pattern", "multipleOf"):
            return "changed"
        if not isinstance(old_value, (int, float)) or not isinstance(new_value, (int, float)):
            return "changed"
        if keyword in _TIGHTEN_UP:
            return "tightened" if new_value > old_value else "relaxed"
        return "tightened" if new_value < old_value else "relaxed"

    def _compare_properties(self, old_schema, new_schema, location, context) -> None:
        old_props = old_schema.get("properties") if _is_dict(old_schema.get("properties")) else {}
        new_props = new_schema.get("properties") if _is_dict(new_schema.get("properties")) else {}
        old_required = {str(name) for name in _safe_list(old_schema.get("required"))}
        new_required = {str(name) for name in _safe_list(new_schema.get("required"))}

        removed = [name for name in old_props if name not in new_props]
        added = [name for name in new_props if name not in old_props]
        added_pairs = [(name, new_props[name]) for name in sorted(added)]

        for name in sorted(removed):
            child = "%s.properties.%s" % (location, name)
            if context == "request":
                self._add(
                    POTENTIALLY_BREAKING,
                    "request-property-removed",
                    child,
                    "request property %r was removed" % name,
                    name,
                    None,
                    "clients that still send it may be rejected: common guidance is to fail unknown fields "
                    "with 400 (Microsoft REST API Guidelines)",
                )
            else:
                note = None
                hint = self._rename_hint(old_props[name], added_pairs)
                if hint:
                    note = "possibly renamed to %r" % hint
                self._add(
                    BREAKING,
                    "response-property-removed",
                    child,
                    "response property %r was removed" % name,
                    name,
                    None,
                    note or "clients reading this field get nothing; a rename is indistinguishable from a removal",
                )

        for name in sorted(added):
            child = "%s.properties.%s" % (location, name)
            if name in new_required:
                if context == "request":
                    self._add(
                        BREAKING,
                        "request-property-required-added",
                        child,
                        "new required request property %r was added" % name,
                        None,
                        name,
                        "existing clients do not send it, so their requests are now invalid",
                    )
                else:
                    self._add(
                        COMPATIBLE,
                        "response-property-added",
                        child,
                        "new response property %r was added and is always present" % name,
                        None,
                        name,
                    )
            else:
                self._add(
                    COMPATIBLE,
                    "property-added",
                    child,
                    "new optional property %r was added" % name,
                    None,
                    name,
                )

        for name in sorted(set(old_props) & set(new_props)):
            child = "%s.properties.%s" % (location, name)
            was_required, is_required = name in old_required, name in new_required
            if was_required != is_required:
                if is_required:
                    note = "requests that omitted it are now invalid"
                    if context != "request":
                        note = (
                            "the server now guarantees the field is present; the tool still reports this as "
                            "breaking because clients may have come to rely on its absence"
                        )
                    self._add(
                        BREAKING,
                        "property-became-required",
                        child,
                        "property %r became required (its name was added to the parent 'required' list)" % name,
                        False,
                        True,
                        note,
                    )
                else:
                    self._add(
                        COMPATIBLE,
                        "property-became-optional",
                        child,
                        "property %r is no longer required" % name,
                        True,
                        False,
                    )
            self._compare_schema(old_props[name], new_props[name], child, context)

    def _rename_hint(self, old_property, candidates) -> str | None:
        """Return the name of an added property whose shape matches a removed one."""
        old_flat = self._flatten(self.old_res.deref(old_property)[0] or {}, self.old_res)
        if not old_flat:
            return None
        for name, candidate in candidates:
            new_flat = self._flatten(self.new_res.deref(candidate)[0] or {}, self.new_res)
            if new_flat and _deep_equal(old_flat, new_flat):
                return name
        return None

    def _compare_additional_properties(self, old_schema, new_schema, location, context) -> None:
        old_kind, old_value = self._additional_properties(old_schema)
        new_kind, new_value = self._additional_properties(new_schema)
        if old_kind == new_kind and _deep_equal(old_value, new_value):
            return
        child = location + ".additionalProperties"
        if new_kind == "forbidden":
            self._add(
                BREAKING,
                "additional-properties-forbidden",
                child,
                "additionalProperties changed from %s to false"
                % ("true (anything was allowed)" if old_kind == "allowed" else "a schema"),
                old_kind if old_kind != "schema" else "schema",
                False,
                "clients that send unknown properties are now invalid",
            )
            return
        if old_kind == "forbidden":
            self._add(
                COMPATIBLE,
                "additional-properties-allowed",
                child,
                "additionalProperties changed from false to %s" % new_kind,
                False,
                new_kind,
            )
            return
        if new_kind == "schema" and old_kind == "allowed":
            self._add(
                POTENTIALLY_BREAKING,
                "additional-properties-constrained",
                child,
                "additionalProperties now must match a schema",
                "allowed",
                "schema",
                "values that used to be accepted unchecked may now be rejected",
            )
            return
        if old_kind == "schema" and new_kind == "allowed":
            self._add(
                COMPATIBLE,
                "additional-properties-unconstrained",
                child,
                "additionalProperties is no longer constrained by a schema",
                "schema",
                "allowed",
            )
            return
        if old_kind == "schema" and new_kind == "schema":
            self._compare_schema(old_value, new_value, child, context)

    @staticmethod
    def _additional_properties(schema: dict):
        value = schema.get("additionalProperties", True)
        if isinstance(value, dict):
            return "schema", value
        if value is False:
            return "forbidden", None
        return "allowed", None

    def _compare_items(self, old_schema, new_schema, location, context) -> None:
        old_has, new_has = "items" in old_schema, "items" in new_schema
        if not old_has and not new_has:
            return
        child = location + ".items"
        if old_has and not new_has:
            self._add(
                COMPATIBLE,
                "items-schema-removed",
                child,
                "the items schema was removed",
                True,
                None,
                "array elements are no longer constrained",
            )
            return
        if new_has and not old_has:
            self._add(
                POTENTIALLY_BREAKING,
                "items-schema-added",
                child,
                "an items schema was added",
                None,
                True,
                "array elements are now constrained",
            )
            return
        self._compare_schema(old_schema.get("items"), new_schema.get("items"), child, context)

    def _compare_composition(self, old_schema, new_schema, location, context) -> None:
        for keyword in ("oneOf", "anyOf"):
            old_list = old_schema.get(keyword)
            new_list = new_schema.get(keyword)
            old_is, new_is = isinstance(old_list, list), isinstance(new_list, list)
            if not old_is and not new_is:
                continue
            child = "%s.%s" % (location, keyword)
            if old_is and not new_is:
                self._add(
                    COMPATIBLE,
                    "composition-removed",
                    child,
                    "%s was removed" % keyword,
                    len(old_list),
                    None,
                )
                continue
            if new_is and not old_is:
                self._add(
                    POTENTIALLY_BREAKING,
                    "composition-added",
                    child,
                    "%s was added (%d variants)" % (keyword, len(new_list)),
                    None,
                    len(new_list),
                )
                continue
            old_variants = [self._flatten(self.old_res.deref(v)[0] or {}, self.old_res) for v in old_list]
            new_variants = [self._flatten(self.new_res.deref(v)[0] or {}, self.new_res) for v in new_list]
            removed = [v for v in old_variants if not any(_deep_equal(v, n) for n in new_variants)]
            added = [n for n in new_variants if not any(_deep_equal(n, o) for o in old_variants)]
            if removed:
                self._add(
                    BREAKING,
                    "%s-variant-removed" % keyword,
                    child,
                    "%d of %d %s variants were removed or changed" % (len(removed), len(old_variants), keyword),
                    len(old_variants),
                    len(new_variants),
                )
            if added:
                self._add(
                    COMPATIBLE,
                    "%s-variant-added" % keyword,
                    child,
                    "%d new %s variant(s) were added" % (len(added), keyword),
                    len(old_variants),
                    len(new_variants),
                )

    def _compare_component_schemas(self) -> None:
        # Both 'components' and 'components.schemas' are optional, and a
        # structurally wrong document can put anything in either slot.  Guard
        # both levels before reading through them: assuming 'components' was a
        # mapping made a document like {"components": []} raise AttributeError
        # out of the CLI as a traceback instead of a clean exit 2.
        old_components = self.old.get("components")
        new_components = self.new.get("components")
        if not _is_dict(old_components) or not _is_dict(new_components):
            return
        old_schemas = old_components.get("schemas") or {}
        new_schemas = new_components.get("schemas") or {}
        if not _is_dict(old_schemas) or not _is_dict(new_schemas):
            return
        removed = [str(k) for k in old_schemas if str(k) not in {str(k2) for k2 in new_schemas}]
        added = [str(k) for k in new_schemas if str(k) not in {str(k2) for k2 in old_schemas}]
        if not removed or not added:
            return
        lookup_old = {str(k): v for k, v in old_schemas.items()}
        lookup_new = {str(k): v for k, v in new_schemas.items()}
        for old_name in sorted(removed):
            old_flat = self._flatten(self.old_res.deref(lookup_old[old_name])[0] or {}, self.old_res)
            for new_name in sorted(added):
                new_flat = self._flatten(self.new_res.deref(lookup_new[new_name])[0] or {}, self.new_res)
                if old_flat and new_flat and _deep_equal(old_flat, new_flat):
                    self._add(
                        COMPATIBLE,
                        "component-schema-renamed",
                        "components.schemas.%s" % old_name,
                        "component schema %r looks renamed to %r (identical shape)" % (old_name, new_name),
                        old_name,
                        new_name,
                        "the wire contract is unchanged, so only $ref-building tooling and generated clients care",
                    )
                    break


def diff_documents(old: dict, new: dict, base_path: str | None = None) -> DiffResult:
    """Compare two parsed OpenAPI documents and return a :class:`DiffResult`."""
    return ContractDiffer(old, new, base_path).run()


# Public aliases: the linter and the tests use these helpers too.
RefResolver = _Resolver
is_mapping = _is_dict
as_list = _safe_list
