# openapi-contract-diff

A small, dependency-free CLI that diffs two OpenAPI 3.x documents and tells you, for every change,
whether it is **breaking**, **potentially breaking**, or **compatible** — then lints the new document
against published API design guidance. It is meant to sit in CI and fail a pull request that quietly
breaks the clients of your API.

```
$ python3 openapi_contract_diff.py fixtures/petstore-v1.yaml fixtures/petstore-v2.yaml
...
RESULT: FAIL (14 breaking change(s) at the 'breaking' threshold)
```

- **No dependencies.** Standard library only; `pip install` is never needed.
- **YAML and JSON**, with a built-in YAML reader for machines that have no PyYAML.
- **Local `$ref` resolution**, including `allOf` merging and recursive schemas.
- **Every lint rule cites the guidance it comes from** — no invented "best practices".
- **MIT licensed.** Tests included: 134 tests, ~2 380 assertions, one fixture pair per rule.

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Usage](#usage)
- [Exit codes](#exit-codes)
- [Example output](#example-output)
- [Classification rules](#classification-rules)
- [Design lint rules and where they come from](#design-lint-rules-and-where-they-come-from)
- [What this does not do](#what-this-does-not-do)
- [Tests](#tests)
- [Repository layout](#repository-layout)
- [Licence](#licence)

---

## What it does

Two halves, one command:

1. **Contract diff** — walks both documents from `paths` (through parameters, request bodies,
   responses, headers, media types and every reachable schema) and classifies each difference.
   Changes inside a named component schema are reported once, at the component, instead of once per
   operation that happens to reference it.
2. **Design lint** — checks the *new* document for the design problems that are documented in public
   style guides: verbs in paths, upper-case and underscored path segments, trailing slashes, missing
   or duplicated `operationId`s, undocumented error responses, missing descriptions, unpaginated list
   endpoints, and inconsistent pluralisation between sibling collections.

The diff decides the exit code. Lint findings are advisory unless you pass `--fail-on-lint`.

## Requirements

- Python 3.8 or newer. **Verified on CPython 3.13.5**; the source avoids anything newer than 3.8
  syntax, but no older interpreter was available to test on.
- PyYAML is optional. If `python3 -c "import yaml"` succeeds, PyYAML is used; otherwise the bundled
  `miniyaml` reader is used, and JSON input works either way.

Nothing to install. Keep the four `.py` files in one directory and run the entry point.

## Usage

```
python3 openapi_contract_diff.py old.yaml new.yaml [--json] [--fail-on breaking|potentially-breaking|never] [--base-path /v1]
```

| Option | Meaning |
| --- | --- |
| `--json` | Emit a machine-readable report instead of text. |
| `--fail-on LEVEL` | Lowest severity that makes the command exit 1. `breaking` (default), `potentially-breaking`, or `never` (report only). |
| `--base-path PREFIX` | Strip this prefix from every path in both documents before comparing, so a version bump in the URL does not look like a removed API (`--base-path /v1`). |
| `--no-lint` | Skip the design lint half. |
| `--fail-on-lint` | Also exit 1 when the lint reports an `error`-level issue (a rule grounded in an OpenAPI `MUST`). |
| `--yaml-backend auto\|pyyaml\|builtin` | Which YAML reader to use. `auto` (default) tries JSON, then PyYAML, then the built-in reader. |
| `--version` | Print the version. |

Typical CI use:

```bash
python3 openapi_contract_diff.py api/openapi-main.yaml api/openapi-pr.yaml --fail-on breaking
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Nothing at or above the `--fail-on` threshold (and no lint `error`s when `--fail-on-lint` is given). |
| `1` | Breaking changes found — or, with `--fail-on-lint`, lint errors. |
| `2` | Usage error: missing arguments, an unknown `--fail-on` value, an unreadable file, an empty document, a root that is not a mapping, or a document that could not be parsed. |

A parse or usage failure is always `2`, never `1`: a CI job must be able to tell "the API broke"
apart from "the tool could not read the input".

## Example output

Real output, from the two fixtures in this repository (`fixtures/petstore-v1.yaml` →
`fixtures/petstore-v2.yaml`). Nothing below is edited or abridged:

```
$ python3 openapi_contract_diff.py fixtures/petstore-v1.yaml fixtures/petstore-v2.yaml
openapi-contract-diff 1.0.0
  old: fixtures/petstore-v1.yaml  [OpenAPI 3.0.3, 6 operations, read as YAML via PyYAML 6.0.2]
  new: fixtures/petstore-v2.yaml  [OpenAPI 3.0.3, 6 operations, read as YAML via PyYAML 6.0.2]

BREAKING (14)
  x request-property-required-added  at components.schemas.NewPet (used as request).properties.chip-id
      new required request property 'chip-id' was added
      note: existing clients do not send it, so their requests are now invalid
  x property-became-required  at components.schemas.Pet.properties.photo-urls
      property 'photo-urls' became required (its name was added to the parent 'required' list)
      note: the server now guarantees the field is present; the tool still reports this as breaking because clients may have come to rely on its absence
  x enum-value-removed  at components.schemas.Pet.properties.status.enum
      enum values removed: ['sold']
      note: any client that sends or matches a removed value breaks
  x response-property-removed  at components.schemas.Pet.properties.tag
      response property 'tag' was removed
      note: possibly renamed to 'label'
  x format-changed  at components.schemas.Pet.properties.weight-grams.format
      format changed from 'int32' to 'int64'
  x nullable-removed  at components.schemas.PetPage.properties.next-page-token.nullable
      the value is no longer nullable
      note: servers or clients that sent or accepted null now fail
  x additional-properties-forbidden  at components.schemas.Toy.additionalProperties
      additionalProperties changed from true (anything was allowed) to false
      note: clients that send unknown properties are now invalid
  x security-scheme-changed  at components.securitySchemes.apiKey
      security scheme 'apiKey' changed (name: 'X-Api-Key' -> 'X-Tenant-Key')
      note: any change to how credentials are supplied requires client changes
  x parameter-location-changed  at paths./pets.get.parameters.header.limit
      parameter 'limit' moved from query to header
      note: the value now travels in a different part of the request, so clients must change
  x parameter-became-required  at paths./pets.get.parameters.query.page-token.required
      parameter 'page-token' became required
      note: requests that omitted it are now invalid
  x content-type-removed  at paths./pets.post.responses.201.content.application/xml
      media type 'application/xml' was removed
      note: clients that negotiated this media type can no longer use it
  x response-status-removed  at paths./pets.post.responses.409
      response status 409 was removed
      note: clients handling this status no longer receive it
  x operation-removed  at paths./pets/{petId}.delete
      DELETE /pets/{petId} was removed
  x response-status-removed  at paths./pets/{petId}.patch.responses.404
      response status 404 was removed
      note: clients handling this status no longer receive it

POTENTIALLY BREAKING (3)
  ~ request-property-removed  at components.schemas.NewPet (used as request).properties.tag
      request property 'tag' was removed
      note: clients that still send it may be rejected: common guidance is to fail unknown fields with 400 (Microsoft REST API Guidelines)
  ~ constraint-tightened  at components.schemas.Pet.properties.name.maxLength
      maxLength changed from None to 64
      note: the tool cannot know whether any client exceeds the old bound, so this is advisory
  ~ operation-id-changed  at paths./pets.get.operationId
      operationId changed from 'listPets' to 'getPets'
      note: the wire contract is unchanged, but generated clients and operationId-based tooling break

COMPATIBLE (9)
  + property-added  at components.schemas.NewPet (used as request).properties.label
      new optional property 'label' was added
  + property-added  at components.schemas.Pet.properties.color
      new optional property 'color' was added
  + property-added  at components.schemas.Pet.properties.label
      new optional property 'label' was added
  + enum-value-added  at components.schemas.Pet.properties.status.enum
      enum values added: ['reserved']
      note: additive, but clients whose switches are exhaustive over the old set may still need updating; server-side enums should declare that undocumented values can appear
  + response-property-added  at components.schemas.Pet.properties.updated-at
      new response property 'updated-at' was added and is always present
  + parameter-added-optional  at paths./pets.get.parameters.query.sort
      new optional parameter 'sort' (query) was added
  + response-status-added  at paths./pets.get.responses.429
      response status 429 was added
  + response-header-added  at paths./pets/{petId}/toys.get.responses.200.headers.X-Total-Count
      response header 'X-Total-Count' was added
  + endpoint-added  at paths./pets/{petId}/vaccinations
      path /pets/{petId}/vaccinations was added (methods: GET)

DESIGN LINT (1)
  warning response-no-error-documented  at paths./pets/{petId}.patch.responses
      only success status codes are documented (200); no 4xx/5xx or default response
      guidance: Zalando RESTful API Guidelines #151: 'MUST specify success and error responses'; OpenAPI 3.1 (Responses Object): 'documentation is expected to cover a successful operation response and any known errors'; Microsoft REST API Guidelines: 'DO document the service's top-level error code strings; they are part of the API contract.' A 'default' response counts as documenting errors - Microsoft: 'YOU SHOULD NOT document specific error status codes in your OpenAPI/Swagger spec unless the default response cannot properly describe the specific error response.'

SUMMARY
  breaking: 14
  potentially breaking: 3
  compatible: 9
  design lint: 1 (0 error, 1 warning, 0 info)
  fail-on threshold: breaking
RESULT: FAIL (14 breaking change(s) at the 'breaking' threshold)
```

`--json` gives the same information to a machine. A real (truncated) sample:

```json
{
  "tool": "openapi-contract-diff",
  "version": "1.0.0",
  "old": { "path": "fixtures/petstore-v1.yaml", "format": "YAML via PyYAML 6.0.2", "openapi": "3.0.3", "operations": 6 },
  "new": { "path": "fixtures/petstore-v2.yaml", "format": "YAML via PyYAML 6.0.2", "openapi": "3.0.3", "operations": 6 },
  "threshold": "breaking",
  "summary": { "breaking": 14, "potentially_breaking": 3, "compatible": 9, "total": 26 },
  "findings": [
    {
      "severity": "breaking",
      "kind": "request-property-required-added",
      "location": "components.schemas.NewPet (used as request).properties.chip-id",
      "message": "new required request property 'chip-id' was added",
      "new": "chip-id",
      "note": "existing clients do not send it, so their requests are now invalid"
    },
    {
      "severity": "breaking",
      "kind": "property-became-required",
      "location": "components.schemas.Pet.properties.photo-urls",
      "message": "property 'photo-urls' became required (its name was added to the parent 'required' list)",
      "old": false,
      "new": true,
      "note": "the server now guarantees the field is present; the tool still reports this as breaking because clients may have come to rely on its absence"
    }
  ],
  "warnings": [],
  "exit_code": 1,
  "result_line": "FAIL (14 breaking change(s) at the 'breaking' threshold)"
}
```

## Classification rules

This table **is** the contract of the tool. Read it as documented judgement, not as gospel: the
OpenAPI Specification describes how to write an API description, not which edits are safe for
existing clients, so every severity below is a decision this tool made and can be argued with.

### Breaking

| Change | `kind` | Why it breaks clients |
| --- | --- | --- |
| Endpoint (path) removed | `endpoint-removed` | Every client calling it fails. |
| Operation (method) removed | `operation-removed` | Same, for one method. |
| Response property removed or renamed | `response-property-removed` | The field is simply not there any more; a rename is indistinguishable from a removal on the wire. When a newly added property has the identical shape, the note says "possibly renamed to …". |
| Required request property added | `request-property-required-added` | Existing clients do not send it. |
| Property became required | `property-became-required` | Requests that omitted it are now invalid. Response-side requiredness is arguably compatible (the server now guarantees presence) but is still reported as breaking, because clients may have come to rely on its absence. |
| Enum value removed | `enum-value-removed` | Anything sending or matching that value breaks. |
| `type` changed (`string` → `integer`) | `type-changed` | The wire representation changes. |
| `format` changed or dropped | `format-changed` | `int32` → `int64` silently changes what clients must parse; dropping a format widens the accepted values, which is noted in the message. |
| New required parameter | `parameter-required-added` | Existing requests are now invalid. |
| Parameter became required | `parameter-became-required` | Same. |
| Required parameter removed | `parameter-removed` | Clients still send it; servers that reject unknown parameters now fail those calls. |
| Parameter moved (`query` → `header`) | `parameter-location-changed` | The value now travels in a different part of the request. |
| Response status removed | `response-status-removed` | Clients that handled it never see it again. |
| `nullable: true` → absent/false (or `type: [x,"null"]` → `type: x`) | `nullable-removed` | Null is no longer valid. |
| `additionalProperties` true → false | `additional-properties-forbidden` | Unknown properties are now rejected. |
| Media type removed from a request or response | `content-type-removed` | Clients that negotiated it can no longer use the service. |
| Response header removed | `response-header-removed` | Clients that read it (rate limits, pagination, ETags) break. |
| Security scheme removed or changed (`type`, `in`, `name`, `scheme`, `bearerFormat`, `flows`, `openIdConnectUrl`) | `security-scheme-removed`, `security-scheme-changed` | Credentials are supplied differently, so clients must change. |
| Authentication requirement added, or an alternative removed | `security-requirement-added`, `security-requirement-removed` | Clients authenticating the removed way, or anonymously, are rejected. |
| `enum` introduced on a request schema | `enum-constraint-added` | Values outside the enum are no longer accepted. |
| Required request body added / made required | `request-body-added-required`, `request-body-became-required` | Requests without a body are invalid. |
| `const` changed | `const-changed` | The only accepted value changed. |
| A `$ref` that no longer resolves | `schema-unresolvable` | The schema is defined nowhere; generated clients cannot be built. |
| A `oneOf`/`anyOf` variant removed or changed | `oneOf-variant-removed`, `anyOf-variant-removed` | Part of the accepted (or emitted) shape space disappears. |

### Potentially breaking

| Change | `kind` | Why it is not automatically breaking |
| --- | --- | --- |
| `default` changed, added or removed | `default-changed`, `default-added`, `default-removed` | Requests that omit the field behave differently — but defaults are frequently documentation-only, so the tool cannot know whether the server ever honoured them. |
| Optional parameter removed | `parameter-removed` | Clients that relied on it lose functionality, or get a `400` if unknown parameters are rejected. |
| Request property removed | `request-property-removed` | Clients that still send it may be rejected: common guidance (Microsoft REST API Guidelines) is to fail unknown fields with `400`. |
| Constraint tightened (`maxLength` lowered, `minimum` raised, `pattern` added, `uniqueItems` set, …) | `constraint-tightened`, `constraint-changed` | The tool cannot know whether any client actually exceeds the old bound. |
| `nullable` added (`false` → `true`) | `nullable-added` | Widening: clients must now handle a `null` they never saw before. |
| `enum` removed from a response schema | `enum-constraint-removed` | Clients may see values they never saw before. |
| `additionalProperties` changed from *anything allowed* to *a schema* | `additional-properties-constrained` | Values that used to be accepted unchecked may now be rejected. |
| `items` schema added to an array | `items-schema-added` | Elements are now constrained. |
| Parameter serialisation changed (`style`, `explode`, `allowReserved`) | `parameter-serialization-changed` | The same logical value is now encoded differently. |
| `operationId` changed | `operation-id-changed` | The wire contract is unchanged, but generated clients and `operationId`-based tooling break. |
| Server URLs changed | `servers-changed` | Clients hard-coded to the old host are affected; use `--base-path` for pure version-prefix moves. |
| Request body removed | `request-body-removed` | Clients that still send a body may be rejected or silently ignored. |

### Compatible

| Change | `kind` | Note |
| --- | --- | --- |
| Endpoint or operation added | `endpoint-added`, `operation-added` | Purely additive. |
| Optional property added (request or response) | `property-added`, `response-property-added` | A new required *response* property is also compatible: the server always sends it, and clients ignore unknown fields. |
| Enum value added | `enum-value-added` | Reported with the caveat that a client whose switch is exhaustive over the old set may still need updating, and that server-side enums should declare that undocumented values can appear. |
| New optional parameter | `parameter-added-optional` | Additive. |
| Response status added, media type added, response header added | `response-status-added`, `content-type-added`, `response-header-added` | Additive. |
| Property no longer required | `property-became-optional`, `parameter-became-optional` | Widening. |
| Constraint relaxed | `constraint-relaxed` | Widening. |
| `additionalProperties` false → true, or schema → unconstrained | `additional-properties-allowed`, `additional-properties-unconstrained` | Widening. |
| `items` schema removed | `items-schema-removed` | Elements are no longer constrained. |
| Operation deprecated | `operation-deprecated` | Still callable; plan a migration. |
| OpenAPI version changed (3.0 ↔ 3.1) | `spec-version-changed` | The wire contract is unchanged; tooling may need updating. |
| Component schema renamed with an identical shape | `component-schema-renamed` | `$ref` names are not part of the wire contract; only codegen and `$ref`-building tooling care. |

## Design lint rules and where they come from

Every rule below is grounded in a published source, quoted in the tool's own output as `guidance:`.
Severity `error` means the rule restates an OpenAPI `MUST` — a specification violation, not a taste
judgement. `warning` means a published style guide. `info` means "look at this", because the
heuristic can be wrong.

| Rule | Severity | Source |
| --- | --- | --- |
| `path-kebab-case` (upper-case, underscores) | warning | Zalando RESTful API Guidelines **#129**: "MUST use kebab-case for path segments", regex `^[a-z][a-z\-0-9]*$`. Microsoft REST API Guidelines: "DO use kebab-casing (preferred) or camel-casing for URL path segments." Google AIP-122 prefers `lowerCamelCase`, so this tool flags *upper case and underscores* rather than demanding one of the two conventions. |
| `path-trailing-slash` | warning | Zalando **#136**: "MUST use normalized paths without empty path segments and trailing slashes." |
| `path-verb-in-segment` | info | Google **AIP-136**: "The HTTP URI must use a `:` character followed by the custom verb", i.e. `/orders/{id}:cancel`, not `/orders/cancel`. Microsoft REST API Guidelines: "model resource state, not behavior." Reported as info because some nouns are also verbs (`/search`), and a `:verb` suffix is accepted silently. |
| `path-not-plural` | warning | Zalando **#134**: "MUST pluralize resource names." Google **AIP-122**: "Collection identifiers must be plural." Microsoft REST API Guidelines: a `resource-collection` is the "Name of the collection, unabbreviated, pluralized". |
| `inconsistent-pluralisation` | warning | Google **AIP-122** ("Collection identifiers must be plural") plus Microsoft REST API Guidelines: "DO focus heavily on clear & consistent naming." Fires when sibling collections under the same parent mix plural and singular names. |
| `operation-id-missing` | warning | OpenAPI 3.1 (Operation Object): `operationId` is optional, but "Tools and libraries MAY use the operationId to uniquely identify an operation, therefore, it is RECOMMENDED to follow common programming naming conventions." |
| `operation-id-duplicate` | error | OpenAPI 3.1 (Operation Object): "The id **MUST** be unique among all operations described in the API." |
| `operation-missing-responses` | error | OpenAPI 3.1 (Operation Object): `responses` is "**REQUIRED**. The list of possible responses." |
| `responses-empty` | error | OpenAPI 3.1 (Responses Object): "The Responses Object **MUST** contain at least one response code." |
| `no-success-response` | warning | OpenAPI 3.1 (Responses Object): "if only one response code is provided it SHOULD be the response for a successful operation call." |
| `response-description-missing` | error | OpenAPI 3.1 (Response Object): "`description` … **REQUIRED**. A description of the response." |
| `response-no-error-documented` | warning | Zalando **#151**: "MUST specify success and error responses." OpenAPI 3.1: "documentation is expected to cover a successful operation response and any known errors." Microsoft REST API Guidelines: "DO document the service's top-level error code strings; they are part of the API contract." A `default` response counts (Microsoft: "YOU SHOULD NOT document specific error status codes in your OpenAPI/Swagger spec unless the 'default' response cannot properly describe the specific error response"). |
| `operation-description-missing` | warning | Google **AIP-192** (Documentation): "public comments must be included over every component (service, method, message, field, enum, and enum value) … even in cases where the comment is terse". Not an OpenAPI requirement: `description` is optional in 3.1. Either `summary` or `description` satisfies this rule. |
| `schema-description-missing` | warning | Google **AIP-192**, as above, applied to named component schemas. |
| `list-endpoint-no-pagination` | warning | Google **AIP-158** (Pagination): "RPCs returning collections of data must provide pagination at the outset"; Zalando **#159**: "MUST support pagination"; Microsoft REST API Guidelines describe `maxpagesize`/`nextLink` for collections. AIP-158 notes the usual exemption for small, bounded collections — the tool cannot detect that, so treat the finding as a prompt. |
| `path-parameter-not-declared` | error | OpenAPI 3.1 (Parameter Object): "If `in` is `path`, the `name` field **MUST** correspond to a template expression occurring within the `path` field in the Paths Object." Reported both for a template with no declaration and for a declaration with no template. |
| `path-parameter-not-required` | error | OpenAPI 3.1 (Parameter Object): "If the parameter location is `path`, this property is **REQUIRED** and its value **MUST** be true." |

Sources: [Zalando RESTful API Guidelines](https://opensource.zalando.com/restful-api-guidelines/),
[Google API Improvement Proposals](https://google.aip.dev/) (AIP-122, AIP-136, AIP-158, AIP-192),
[Microsoft REST API Guidelines](https://github.com/microsoft/api-guidelines),
[OpenAPI Specification 3.1.0](https://spec.openapis.org/oas/v3.1.0.html).

## What this does not do

Honest limits, in roughly the order they are likely to bite:

- **YAML is not fully supported without PyYAML.** If PyYAML is importable it is used and the YAML
  support is complete. If it is not, the bundled `miniyaml` reader handles the subset that OpenAPI
  documents actually contain — block mappings and sequences, flow collections, quoted/plain scalars,
  literal (`|`) and folded (`>`) block scalars with chomping indicators, comments, anchors, aliases
  and merge keys, and a whole document written as one flow collection (which is what JSON looks
  like). It does **not** handle multi-line plain scalars, explicit `?` keys, complex keys, tags other
  than `!!str`/`!!int`/`!!float`/`!!bool`/`!!null`, directives, or several documents in one stream;
  it raises a line-numbered error instead of guessing. It also follows YAML 1.2, so `yes`/`no`/`on`/
  `off` stay strings where PyYAML (YAML 1.1) would make them booleans, and dates stay strings.
  **JSON input always works**, with or without PyYAML.
- **No external `$ref`s.** Local references (`#/components/...`, and any other same-document JSON
  pointer) are resolved, including `allOf` merging and recursive schemas. A `$ref` pointing at another
  file or at a URL is *not* fetched: the referencing node is treated as opaque, and each such ref is
  reported once in `warnings`. Split specifications must be bundled before diffing.
- **OpenAPI 3.x only.** Swagger/OpenAPI 2.0 (`swagger: "2.0"`) is not understood; if a document has a
  `swagger` key the tool says so in `warnings` and its results for that document are unreliable.
- **Classification is a judgement call, documented above — not a spec-mandated truth.** Two people
  can disagree about whether adding a required response property is compatible, and about whether an
  optional parameter removal is breaking. Where this tool made a conservative choice it says so in
  the finding's `note`, and `--fail-on potentially-breaking` lets you include the arguable cases in
  the gate. Nothing here is claimed to be universal.
- **The lint rules are style-guide rules, not law.** They are the ones that come from a published
  guide, and each finding carries its citation. A `warning` is a prompt to think, not a defect.
- **Not everything in an OpenAPI document is compared.** Ignored today: `callbacks`, `links`,
  `webhooks` (OpenAPI 3.1), `discriminator`, `xml`, `examples`/`example` payload changes, `tags`,
  `externalDocs`, `summary`/`description` text, and `deprecated` on schemas. `servers` is compared
  only as a whole, and only as a potentially-breaking change.
- **Unreferenced component schemas are not deeply diffed.** Only schemas reachable from an operation
  are compared structurally; a component that nothing references can be deleted or edited silently.
  Removing a component that *is* still referenced shows up as a breaking `schema-unresolvable`.
- **Changes inside a shared component are reported once, at the component** (with a
  `(used as request)` marker when the direction matters), not once per operation. If you need the
  per-operation blast radius, that is not this tool.
- **Heuristics in the lint half can be wrong.** "Looks like a verb", "looks like a collection" and
  "is not plural" are pattern matches; where they are unreliable the rule is `info` or the message
  says what it matched on. The pluralisation rule ignores a small allow-list of uncountable nouns and
  nouns that are also verbs.
- **No code generation, no semver advice, no changelog.** It tells you what changed and how it is
  classified; deciding the next version number is your call.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

- 134 tests, ~2 380 assertions, all passing.
- One fixture pair per classification rule under `fixtures/rules/` (68 cases), each with an
  `expect.json` that is asserted as an exact multiset, so *over*-reporting fails the suite too. Every
  case is run through both YAML readers.
- **Negative controls**: `fixtures/identical-{a,b,c}.{yaml,json}` describe the same API in three
  different syntaxes and must diff to exactly zero findings — across all 15 pairings and both
  readers; `fixtures/additive-{old,new}.yaml` is a purely additive change and must produce zero
  breaking and zero potentially-breaking findings.
- `tests/test_miniyaml.py` checks the built-in reader against PyYAML on every fixture in the tree and
  on hand-written edge cases (block scalar chomping, flow collections as a whole document, anchors,
  merge keys, YAML 1.2 booleans, and eight error cases).
- `tests/test_cli.py` runs the CLI in-process and asserts the real exit codes: 0, 1 and 2.

## Repository layout

```
openapi_contract_diff.py   CLI entry point, output rendering, exit codes
contractdiff.py            the diff engine and the classification rules
designlint.py              the design lint and its citations
miniyaml.py                the built-in YAML subset reader (no PyYAML needed)
fixtures/                  end-to-end fixtures, negative controls, lint fixtures
fixtures/rules/<case>/     one old.yaml + new.yaml + expect.json per classification rule
tests/                     unittest suite
```

## Licence

MIT. See [LICENSE](LICENSE).

Copyright (c) 2025 duke5am

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial
portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

→ **API Contract & Design Pack**: <!-- GUMROAD-LINK -->
