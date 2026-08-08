# Compatibility

The CLI and the MAPP configuration service are released separately. Safe
operation therefore depends on an explicit API contract rather than matching
repository commits.

## Initial compatibility line

| CLI release | Python | API version | Contract version | Status |
| --- | --- | --- | --- | --- |
| `0.1.x` | `3.11+` | `1.x` | `1.x` | Initial supported extraction |

The server reports its XYZ version, rules version, capabilities, instance ID,
workspace key, and current revision. The client does not import XYZ source or
encode a particular XYZ checkout.

MAPP Platform API/contract `1.3` adds the hardened derived-layer error taxonomy,
reason-specific actions, operation-specific unchanged-state evidence, and
synchronous/background error parity. The CLI still verifies the connected
server at runtime; against an earlier compatible `1.x` server it preserves the
older response but cannot invent fields or distinctions that server did not
return.

MAPP Platform API/contract `1.4` advertises bounded pagination contract `1`.
For workspace proposals and growing semantic collections, this CLI sends a
page limit (100 by default, 1–100 accepted), retains only that response page,
and exposes the server's opaque `pagination.nextCursor`. Pass that value back
unchanged with `--cursor` to request the next page. A null cursor means the end
of the collection. `--limit` or `--cursor` fails closed when the server does
not advertise pagination; the one exception is semantic search's legacy
`--limit`, which remains compatible with earlier API-1.x servers.

The derived-profile list may also return a bounded `deliveryBlockers` repair
batch. A literal boolean `deliveryBlockersMore` means additional unmatched
archive repairs remain server-side; repair the displayed batch and refresh the
list. The CLI rejects malformed or unaccompanied backlog flags instead of
mistaking an incomplete administrative work queue for a complete result.

Release this contract-1.4-aware CLI before activating the platform's 1.1.0
legacy collection threshold. Then deploy the semantic service and matching
`config-ui` image together; that image owns both the gateway and its bundled
paginating dashboard and is not split into separate dashboard and API release
steps.

Derived-layer capabilities may include an additive top-level `queryPlanning`
version `1` object, and successful mutations may include the sibling
`derivedLayer.queryPlanningProbe`. These do not extend the closed legacy
`queryGuard` or `queryPlanProbe` objects, so older API-1.x clients can continue
to consume those existing shapes. This CLI validates the optional siblings
when present and adds its own bounded authoring guidance only for a recognized
over-limit `nested_loop_pair_work` failure. Server messages, actions, reasons,
and probes remain unchanged and authoritative.

Until the first stable CLI release, minor `0.x` releases may contain breaking
client-side changes. Review the changelog before upgrading.

## Compatibility checks

`config-cli setup` and `config-cli init` record:

- the normalized endpoint;
- the remote instance ID;
- the server contract version;
- whether non-loopback HTTP was explicitly permitted for a development profile.

`config-cli describe` compares stored identity with the live service and
reports client/server compatibility. A state-changing request must fail closed
when:

- the endpoint reports a different instance ID;
- the API or contract version is unsupported;
- the server contract does not advertise the command required by the
  invocation;
- the workspace revision differs from a proposal's base revision.

Do not override these checks to finish a change. Reinitialize a profile only
after independently confirming that the target replacement is intentional.

## Versioning policy

The CLI package follows semantic versioning:

- patch releases fix behavior without intentionally changing supported command
  syntax or contracts;
- minor releases add backward-compatible commands or add support for a new
  server contract;
- major releases may remove commands or drop a server contract.

During the pre-1.0 period, a minor release may be breaking and must call that
out in [CHANGELOG.md](../CHANGELOG.md).

The server's `contractVersion` governs request and response compatibility.
Workspace `rulesVersion` and `xyzVersion` describe server-owned behavior; they
do not need to match the CLI version.

API and contract versions must use one to three numeric components such as
`1`, `1.0`, or `1.0.2`, with only well-formed optional pre-release/build
suffixes. Empty components, leading-zero components, extra components, and
arbitrary suffixes are rejected rather than interpreted as a compatible major
version. Release builds also verify that package metadata and the runtime
`--version` value are identical.

## Server-owned behavior

The connected server is authoritative for:

- workspace schema and JSON Pointer locations;
- validation rules and remediation text;
- layer/database catalog metadata;
- semantic catalog definitions, revisions, derived-profile readiness,
  configured source exclusions and archive visibility, authorized
  source-relation discovery/synchronization, generation-context availability
  and caps, and curated proposal behavior;
- safe SVG assets;
- SQL-expression capabilities;
- managed derived-layer definition rules, ready source-profile requirements,
  durable execution, query-error classification/remediation, materialization
  estimate/actual-stage evidence, and server-resolved fixed map extents;
- plugin manifests, hashes, workspace usage, preview assertions, and catalogue
  fingerprint binding;
- XYZ-version-specific styles and rendering behavior;
- default/named/composite locale selection and XYZ-specific merge behavior;
- proposal validation, revision checks, application, reload, visual planning,
  interaction evidence, and artifact requirements.

Use `schema`, `rules`, `examples`, and capability endpoints at runtime. Do not
copy those rules into client code or assume every server exposes the same
optional capabilities.

`GET /api/contract` owns the exact CLI command set, while
`GET /api/capabilities` owns action IDs, risks, routes, input schemas,
conditional scopes, presentation hints, and operation kinds. These are
complementary checks: neither a familiar route nor a related action substitutes
for the exact command required by the installed CLI.

Use `plugins list`, `show`, `validate`, and `usage` for plugin behavior. The
server-owned response includes pinned built-ins and source-controlled external
manifests, hashes, schemas, preview checks, and a catalogue fingerprint.

The workspace schema is an advertised-capability contract, not merely a list
of properties the server can round-trip. A property under a typed `properties`
map means the server has audited it against its pinned XYZ commit. Unknown
contract properties are rejected with their exact path, not preserved or
silently removed. Open maps exist only where arbitrary keys are an audited
part of the feature; they are not general extension points.

For the MAPP Platform pinned to XYZ v4.23.4, the audited native additions cover
workspace/query templates, locale and layer template composition, recursive
key/value dictionaries, SVG templates, layer-panel gazetteer search, and the
plugins bundled in that exact XYZ commit plus compatible installed external
plugin manifests. The schema intentionally does not
advertise external/older keys such as `measure_distance`, `query_features`,
`posthog`, or `googleMaps`. Re-run `describe` and `schema` against each target;
another server or future pin may expose a different surface.

Capability matching is exact at the CLI command boundary. A server action ID
or route that resembles a command does not satisfy a differently named
contract command. For example, an advertised proposal visual-test action is
not sufficient if the selected client requires the explicit
`proposals preview-test` contract command. Treat `capability.missing` as an
unsupported operation and do not bypass the named client command.

Semantic command matching includes all three levels. Advertising `semantic
catalog export` does not authorize `semantic catalog search`, and advertising
`semantic catalog show` does not authorize `semantic catalog history`.
Advertising `semantic generate table` does not authorize `semantic generate field`;
advertising workspace `proposals check` does not satisfy `semantic proposals check`.
Advertising `semantic source relations` does not authorize the catalog-changing
`semantic source sync` command. Likewise, `semantic catalog show` is not
authority for `semantic catalog archive`, and `semantic source sync` is not
authority for the separate confirmed `semantic source archive-excluded`
lifecycle action.
Semantic reads and proposal responses are rejected when their required catalog
revision, object identity, asset version, or lifecycle state is malformed.
Generation responses are also rejected unless the exact target is preserved,
the reported metadata-only state and context options match the request,
proposal creation is false, and every operation is a valid curated annotation
for that target. A metadata-only request omits `contextOptions` for
compatibility with older servers; the CLI accepts both the legacy
metadata-only response and the newer explicit false/false context report.

Layer inspection requires the server's `layers effective` capability. A CLI
must fail closed when that capability is absent rather than reimplementing XYZ
locale composition or assuming `/api/layers` exists on an older `1.x` server.
Numeric distribution inspection additionally requires the exact
`layers statistics` command before using
`/api/layers/{layerKey}/statistics`. Inspect the current
`layers.statistics` schema explicitly with `capabilities show`; ordinary
command execution does not refetch action schemas after verifying the command
contract.
Platform dependency inspection requires the exact `dependencies list` or
`dependencies check` command before calling `/api/dependencies`; older
compatible servers can omit it, and the CLI must then report
`capability.missing` instead of guessing references from workspace JSON alone.
The area-weighted H3 planner likewise requires the exact
`derived-layers plan-area-weighted-h3` command. Inspect
`derived-layers.plan-area-weighted-h3` explicitly for its current request,
risk, route, and scope schema. Its successful response must remain non-mutating
and include a replayable create request plus resolved scope and preflight
evidence.

## Upgrade procedure

1. Read both client and server release notes.
2. Upgrade a non-production CLI installation.
3. Run `describe`, `auth status`, `schema`, and `rules`.
4. Exercise inspection and proposal creation against a test instance.
5. Confirm revision conflicts fail closed.
6. Confirm an approved proposal can be applied with `--confirm`.
7. Verify XYZ status and a representative visual test.
8. Retain the previous reviewed CLI artifact for rollback.

If a server is newer than the supported contract range, upgrade the CLI before
making changes. If the CLI is newer than the server, use a client release that
still lists that contract as supported.

## Cross-repository contract testing

The platform repository publishes
`contracts/api-compatibility-v1.4.json` as the machine-readable version and
pagination matrix. The separate private `mapp-explore` integration repository
checks out explicitly selected platform and CLI refs (their remote default
branches when omitted), verifies the artifact against the CLI's supported
majors, and runs the real CLI against the real configuration HTTP handler.
Repository-local tests continue to verify request methods, paths, bodies,
response schemas, error details, capabilities, exit-code mapping, revision
conflicts, and instance mismatch handling.

Compatibility is not established solely because authentication or one read
command succeeds.
