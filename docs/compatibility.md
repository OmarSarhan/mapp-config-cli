# Compatibility

The CLI and the MAPP configuration service are released separately. Safe
operation therefore depends on an explicit API contract rather than matching
repository commits.

## Initial compatibility line

| CLI release | Python | API version | Contract version | Status |
| --- | --- | --- | --- | --- |
| `0.1.x` | `3.11+` | `1.0` | `1.x` | Initial supported extraction |

The server reports its XYZ version, rules version, capabilities, instance ID,
workspace key, and current revision. The client does not import XYZ source or
encode a particular XYZ checkout.

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
- safe SVG assets;
- SQL-expression capabilities;
- XYZ-version-specific styles and rendering behavior;
- default/named/composite locale selection and XYZ-specific merge behavior;
- proposal validation, revision checks, application, reload, and visual tests.

Use `schema`, `rules`, `examples`, and capability endpoints at runtime. Do not
copy those rules into client code or assume every server exposes the same
optional capabilities.

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

Layer inspection requires the server's `layers effective` capability. A CLI
must fail closed when that capability is absent rather than reimplementing XYZ
locale composition or assuming `/api/layers` exists on an older `1.x` server.

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

The platform repository should publish or export a versioned OpenAPI contract.
This repository should pin a reviewed copy for automated contract tests. CI
should verify request methods, paths, bodies, response schemas, error details,
capabilities, exit-code mapping, revision conflicts, and instance mismatch
handling.

Compatibility is not established solely because authentication or one read
command succeeds.
