# Command reference

`config-cli` writes successful results as JSON so that humans and automation
can use the same interface. Errors are written to standard error as structured
JSON and the process exits non-zero. Secrets and authorization headers must
never appear in either stream.

Run `config-cli COMMAND --help` for the options supported by the installed
version. Global options are placed before the command:

```sh
config-cli \
  [--profile NAME] \
  [--token-file PATH] \
  [--timeout SECONDS] \
  [--output json|human] \
  [--input FILE|-] \
  [--extract JSON_PATH] \
  [--out FILE] \
  COMMAND ...
```

`--version` prints the installed client version. For commands after
initialization, `--token-file` supplies a mode-`0600` token file for that
invocation instead of the stored credential. During `init`, the token is
copied into the CLI's private credential store. `--timeout` sets the request
timeout and defaults to 60 seconds.
JSON remains the stable default. `--output human` provides a concise view for
`doctor`, `proposals check`, and `proposals create`; other commands retain JSON.
`--input` merges a bounded JSON object into supported request-producing
commands; conflicts with explicit flags fail closed. Use `-` for stdin.
Credential-like keys are rejected. `--extract` selects one scalar from the
final response, while `--out` writes it atomically with mode `0600`.

Input JSON is limited to 5 MiB and must be an object. A file input must be a
regular non-symlink file. Keys containing `authorization`, `credential`,
`password`, `secret`, or `token` are rejected recursively. Input merging occurs
after command-line parsing: it can add request fields, but it does not satisfy
required parser arguments such as `--base-revision`, `--set`, `--layer`, or
`--expression`.

Extraction accepts dot-separated object keys and numeric list indices, with an
optional `$` or `$.` prefix, for example `revision`,
`$.compatibility.compatible`, or `checks.0.id`. The selected value must be a
scalar; missing paths and object or array results are errors. `--out` creates
parent directories privately where possible and atomically replaces the
destination with a mode-`0600` file.

Specialized `--output human` rendering is currently provided for `doctor`,
`proposals check`, and `proposals create`. Other commands retain JSON output
even when `human` is selected.

`--input` is supported by `validate`, `sql test`, the dry-run mutation
commands, live visual commands, proposal check/create, and proposal candidate
preview commands. Other commands reject it with `input.unsupported`.

## Connection and profiles

### `setup`

Interactively configure the CLI:

```sh
config-cli setup [--force]
```

The wizard prompts for a profile name, configuration service URL, and CLI
token. Token input is hidden and is never returned in the JSON result. The
wizard verifies the public identity and authenticated contract before saving
the profile and token. It then verifies live workspace access and returns the
workspace key, revision, actor, scopes, versions, and compatibility under
`verification`. It requires a terminal; scripts and CI should use
`init` with `--token-file`. If final verification fails, the newly installed
profile is rolled back without overwriting concurrent changes. Replacing an
existing profile requires `--force`; interactive setup shows the old and new
endpoint/instance identities and asks again before committing.
Pressing Ctrl+C or closing terminal input stops setup without a traceback or a
partial profile write. The CLI emits `client.interrupted` or
`client.input_closed` and exits with code `130`.

### `init`

Bind a named local profile to a remote instance:

```sh
config-cli init ENDPOINT \
  [--profile NAME] \
  [--token-file PATH] \
  [--allow-http] \
  [--force]
```

`ENDPOINT` must be an absolute HTTPS origin without credentials, a query, or a
fragment. Plain HTTP is accepted automatically for loopback development
origins; a non-loopback development origin requires `--allow-http`. Never use
that option for production. Initialization records the instance ID and contract
version returned by the server. Replacing an existing profile requires
`--force`. Initialization stores the token in mode-`0600`
`credentials.json`; the source token file may then be removed if it was only
used for transfer.

### `profiles list`

List profile metadata and identify the active profile. Tokens are never
included.

```sh
config-cli profiles list
```

### `profiles use`

Select the default profile:

```sh
config-cli profiles use NAME
```

### `profiles show`

Show public profile metadata, active status, and credential availability:

```sh
config-cli profiles show NAME
```

Credential identifiers and values are never included.

### `profiles remove`

Remove a profile and its locally stored credential:

```sh
config-cli profiles remove NAME --confirm
```

This does not revoke the remote token. Revoke it separately in the remote
dashboard. Interactive use may omit `--confirm` and answer the explicit local
removal warning; scripts and CI must supply it.

### `describe`

Report client/server compatibility and the target currently bound to a
profile:

```sh
config-cli --profile production describe
```

The result includes the profile, endpoint, instance ID, workspace key, current
revision, API version, contract version, XYZ version, and compatibility
status. Run this before planning any change.

### `doctor`

Check local configuration safety and end-to-end readiness:

```sh
config-cli --profile production doctor
```

The result covers credential availability without revealing it, private state
permissions, target identity and compatibility, authentication/scopes,
workspace access, and advertised SQL and visual capabilities.
Failures include a machine-readable remediation action.

### `auth status`

Verify the credential and display the authenticated actor and reported scopes:

```sh
config-cli auth status
```

### `auth replace`

Verify and atomically replace the selected profile credential:

```sh
config-cli --profile production auth replace
config-cli --profile production auth replace --token-file PRIVATE_FILE
```

Interactive entry is hidden. Automation requires a mode-`0600` token file.
The old credential remains selected unless the new token passes identity and
contract verification and the profile has not changed concurrently.

### `auth device`

Replace the selected profile credential with a browser-approved scoped token:

```sh
config-cli auth device
config-cli auth device --scope inspect --scope propose --scope visual
config-cli auth device --no-browser
```

The default omits `apply` and `reload`. The device code expires after ten
minutes, the token after thirty days, and the token response is one-time. The
old credential remains selected until the new credential verifies the same
instance and compatible contract. Use `--no-browser` on a headless or remote
terminal to print the verification flow without attempting to open a local
browser.

### `capabilities list|show`

Discover server-advertised action schemas:

```sh
config-cli capabilities list
config-cli capabilities show proposals.check
```

Actions include stable IDs, risk classes, routes, input schemas, and operation
kinds. Named CLI commands remain the only write interface.

### `operations show|wait`

Inspect or wait for a durable operation:

```sh
config-cli operations show OPERATION_ID
config-cli operations wait OPERATION_ID --wait-timeout 120 --interval 1
```

Terminal states are `succeeded`, `failed`, and `indeterminate`. Never blindly
retry an indeterminate apply or reload. `operations wait` defaults to a
120-second wait timeout and a one-second polling interval.

### `completion`

Generate deterministic Bash, Zsh, or Fish completion:

```sh
config-cli completion bash
config-cli completion zsh
config-cli completion fish
```

## Server-authoritative guidance

### `schema`

Read the supported workspace schema, optionally at one JSON Pointer:

```sh
config-cli schema
config-cli schema --pointer '/properties/locale'
```

### `rules`

Read validation and safety rules, optionally restricted to a category:

```sh
config-cli rules
config-cli rules --category security
```

### `examples`

Read examples supplied by the connected server:

```sh
config-cli examples
```

### `explain-error`

Look up a server rule by its stable ID:

```sh
config-cli explain-error workspace.render
```

An unknown rule ID is an error rather than a successful `null` result.

## Inspection

### `workspace get`

Return the complete current workspace and its revision:

```sh
config-cli workspace get
```

Workspace output can contain operationally sensitive configuration. Minimize
where it is logged or retained.

### `layers list`

List layers in the effective locale:

```sh
config-cli layers list
config-cli layers list --locale en-GB
config-cli layers list --group "Transport"
```

XYZ creates a layer-list folder when layers share the same exact, non-empty
`group` property. `--group` filters the effective layers locally after the
server composes the selected locale; it does not change the workspace.

### `layers get`

Return one layer by workspace layer key:

```sh
config-cli layers get "Bus Stops"
config-cli layers get "Bus Stops" --locale en-GB
```

### `layers style-elements`

Inspect the effective XYZ Styling-panel configuration for one layer:

```sh
config-cli layers style-elements "Bus Stops 2"
config-cli layers style-elements "Bus Stops 2" --locale en-GB
```

The result separates the raw `configuredElements`, XYZ's
`effectiveElements`, and the built-in `renderedElements` whose corresponding
style properties exist. It also reports `panelHidden`. Unknown/custom element
keys are retained in the configured and effective arrays but are not claimed
as built-in rendered controls.

`style.elements` is an ordered UI allow-list, not the configuration for each
control. For example, `hover` renders only when `style.hover` also exists;
`hovers`, `labels`, and `themes` selectors generally require multiple choices.
The opacity slider requires an `opacitySlider` property as well as that key in
the element list.

Use the checked proposal workflow to change the array or panel visibility:

```sh
config-cli proposals check \
  --base-revision REVISION \
  --set '/locale/layers/Bus Stops 2/style/elements=["hover","opacitySlider"]' \
  --set '/locale/layers/Bus Stops 2/style/opacitySlider=true' \
  --explanation 'Shows the hover toggle followed by opacity in the Bus Stops 2 Styling panel.'
```

Set `/style/hidden=true` to suppress the complete interactive Styling panel
without removing default, highlight, hover, theme, or other rendering
configuration. Present the checked proposal and wait for separate approval
before applying it.

### `layers filters`

Inspect the effective interactive filters XYZ will offer for a layer:

```sh
config-cli layers filters "Bus Stops 2"
config-cli layers filters "Bus Stops 2" --locale en-GB
```

The report mirrors XYZ's `infoj` traversal and shows each effective field,
filter type, source (`entry`, `inferred`, `include`, or `includeAll`), panel
visibility, viewport behavior, include/exclude lists, and whether a fixed
default exists. It reports only the presence of `filter.default`, not its
potentially sensitive content.

Each reported filter includes `safe`. A `false` value means the filter comes
from a calculated `infoj[].fieldfx` alias. XYZ v4.23.4 can display that value
in clicked feature information, but its Filtering panel queries SQL and
numeric min/max statistics against the literal field name. Use a real source
column such as `resurface_cost`, or expose the calculation as a derived-layer
output column, before enabling an interactive filter.

Use checked proposals for changes:

```sh
config-cli proposals check \
  --base-revision REVISION \
  --set '/locale/layers/Bus Stops 2/infoj/3/filter={"type":"like","leading_wildcard":true}' \
  --set '/locale/layers/Bus Stops 2/filter/viewport=true' \
  --explanation 'Adds an interactive text filter for the inspected Town entry and scopes generated filter statistics to the map viewport.'
```

The `infoj` index must come from the inspected live layer. A revision change
requires a new inspection and proposal. `filter.default` is a fixed
server-side restriction and may contain trusted template SQL in upstream XYZ;
do not change one without explicit query and data-access review.

For categorized symbology, distinguish three XYZ surfaces that users may call
an “info panel”:

- `style.theme` renders the category colour legend in the Styling panel;
- an `infoj` entry with `filter.type="in"` supplies category statistics in the
  Filtering panel, and layer `filter.viewport=true` scopes those statistics
  and the feature count to the current map view;
- clicked-feature information comes from `infoj` renderers and does not
  automatically embed the Styling-panel legend or Filtering-panel statistics.

When a user explicitly wants a fixed category key in clicked-feature
information, add a `type="html"` entry whose `fieldfx` is a constant,
read-only PostgreSQL text expression. Copy the labels and colours from the
inspected categorized theme so the static key does not drift at proposal
creation time. For example:

```json
{
  "type": "html",
  "title": "Registration year legend",
  "field": "registration_period_legend",
  "fieldfx": "'<div><div><font color=\"#b2182b\">■</font> Before 1970</div><div><font color=\"#2166ac\">■</font> 2015 onward</div></div>'::text",
  "display": true
}
```

This entry is static: it repeats for each selected feature and its labels,
colours, and order do not update automatically when `style.theme` changes.
Viewport category counts remain interactive filter statistics rather than
values inside this HTML legend.

The SQL safety scanner rejects semicolons even when they occur inside an SQL
string, so avoid semicolon-delimited inline CSS in constant HTML expressions.
A single `color` declaration or a bounded HTML `color` attribute is sufficient
for a swatch. The new alias may not exist in the live `infoj` array, so
standalone `sql test` can report that the selected entry does not exist.
Treat that as a selector limitation and require the complete coordinated
candidate to pass authoritative `proposals check` render validation.

Because the CLI does not accept `-` as an array append index, adding the HTML
entry may require replacing the inspected `infoj` parent array. Preserve every
existing entry and its order exactly, add the `in` filter to the inspected
category entry, and review the semantic diff separately from the
transport-level array replacement.

Layer keys, display names, and database relation names are different
identifiers. A missing layer is an error. Omitting `--locale` selects the
top-level default `locale`, even when named `locales` also exist. An explicit
name selects the effective named locale composed by XYZ's framework-specific
merge rules. If raw `workspace.locale` is absent, omitting the option or using
`--locale locale` selects XYZ's synthetic empty `{"layers": {}}` default; the
CLI never auto-selects a sole named alternative.

To add or move a layer into an XYZ folder, inspect the layer and current
revision, then use the normal checked, revision-bound proposal workflow:

```sh
config-cli layers get "Bus Stops"
config-cli proposals check \
  --base-revision REVISION \
  --set '/locale/layers/Bus Stops/group="Transport"' \
  --explanation 'Moves Bus Stops into the Transport layer folder without changing its data or styling.'
config-cli proposals create --from-check CHECK_FINGERPRINT
```

Present the proposal and wait for separate approval before applying it. Remove
the optional property with `--unset '/locale/layers/Bus Stops/group'` to move
the layer back to the ungrouped list. For named locales, target the raw
`/locales/LOCALE/layers/...` override that should own the value; do not flatten
the effective locale.

### `catalog list`

List server-approved database relations, geometry fields, identifiers, and
other catalog metadata:

```sh
config-cli catalog list
```

### `icons list`

List safe custom SVG assets exposed by the server:

```sh
config-cli icons list
```

## Validation and SQL

### `validate`

Validate the live workspace or a local JSON candidate without saving:

```sh
config-cli validate
config-cli validate --file candidate.json
```

Validation is read-only. It may perform bounded database probes through the
server.

### `sql capabilities`

Describe the server's restricted calculated-field SQL subset:

```sh
config-cli sql capabilities
```

### `sql test`

Test one trusted, scalar, read-only expression against a layer:

```sh
config-cli sql test \
  --layer "Planning Applications" \
  --expression 'upper(status)' \
  --type text \
  --field display_status \
  --locale en-GB
```

The CLI is not a general SQL shell. SQL tests cannot perform database or schema
changes.

`--type` defaults to `text` and `--field` defaults to `calculated_value`.
Servers may use the type and field to select the feature-information renderer
being tested. When a proposal simultaneously adds `fieldfx` and changes an
existing renderer from numeric to text, the current live entry may not be
selectable through standalone `sql test`; the exact complete candidate must
still pass `proposals check`. A standalone selector error does not establish
that an expression is valid or invalid by itself.

## Managed derived layers

The server can expose one dependency-checked `SELECT` as a view or
materialized view in its fixed `derived_layers` schema:

```sh
config-cli derived-layers capabilities
config-cli derived-layers list
config-cli derived-layers show paths_h3_r9
```

Put the query in a file so shell parsing cannot change it:

```sh
config-cli derived-layers create paths_h3_r9 \
  --kind view \
  --query-file paths-h3-r9.sql \
  --source leeds.definitive_paths \
  --id-column h3_id \
  --geometry-column geom_3857
```

Use `--kind materialized` for a stored result. Refresh and drop are explicit
confirmed database actions:

```sh
config-cli derived-layers refresh paths_h3_r9 --confirm
config-cli derived-layers drop paths_h3_r9 --confirm
```

For a refresh already known to be slow, opt into durable background execution:

```sh
config-cli derived-layers refresh paths_h3_r9 --confirm --background
```

Atomically replace a definition or convert its kind:

```sh
config-cli derived-layers replace paths_h3_r9 \
  --kind materialized \
  --query-file paths-h3-r9.sql \
  --source leeds.definitive_paths \
  --id-column h3_id \
  --geometry-column geom_3857 \
  --confirm
```

The server builds and validates a temporary relation before swapping it in one
transaction. Replacement is refused for PostgreSQL dependent objects. Drop is
refused for either PostgreSQL dependents or live dashboard workspace
references. Structured errors include `dependents`, `workspaceReferences`,
and `dropped: false`. External clients that only issue reads cannot be
discovered from PostgreSQL catalog dependencies.

These mutations require the separately granted `derive` scope. Creation does
not add an XYZ workspace layer. Inspect the refreshed catalog, then use the
normal revision-bound proposal workflow to add the new relation. The CLI
forwards definitions to the server and never receives database credentials.

H3-derived relations should make their spatial semantics explicit. For
"touches any source feature" workflows, use H3 cell generation to find
candidates and then filter the generated cell polygons with a reviewed exact
predicate, such as `ST_Intersects`, against the original geometry. Use the H3
containment strings accepted by the connected database extension; do not guess
short aliases.

Some H3/PostGIS convenience wrappers assume a broader PostgreSQL search path
than the derived-layer runner provides. Errors such as unresolved `geometry`
types or `st_dump` functions can indicate wrapper resolution rather than a bad
spatial idea. Prefer explicitly qualified PostGIS calls or lower-level H3 WKB
boundary functions when that happens. A derived-layer mutation that returns a
timeout or HTTP `5xx` is indeterminate: inspect `derived-layers list`,
`derived-layers show NAME`, and `catalog list` before recreating, replacing,
or dropping anything.

Create, replace, and refresh are synchronous by default; use that path first
for ordinary views and jobs expected to finish promptly. For a known slow
materialized job, add `--background`; the CLI then requests a durable server
operation and polls it automatically. Background mode accepts
`--wait-timeout` (default 1860 seconds) and `--interval` (default one second).
Reaching the local wait timeout does not cancel database work; continue with
the operation ID from the structured error using
`config-cli operations wait OPERATION_ID`.

Do not blindly resend a synchronous request after a client timeout or HTTP
`5xx`: it may have committed. Inspect `derived-layers list`,
`derived-layers show NAME`, and `catalog list` first. Use `--background` for a
subsequent deliberate attempt only after confirming the original did not
commit.

The same audit is required when a background operation reaches `failed` after
doing substantial work. A serialization or result-reporting error can occur
after the relation was committed. Compare `operations show OPERATION_ID`,
`derived-layers list`, `derived-layers show NAME`, and the catalog; do not
recreate a relation that those reads confirm already exists.

Lower-level H3 point functions take PostgreSQL points as
`(longitude, latitude)`. A line workflow may traverse cells between each
segment's endpoint cells and expand candidates by a bounded grid ring, but it
must still filter candidates with exact `ST_Intersects` against the generating
segment. Cast output explicitly, for example
`ST_Transform(...,3857)::geometry(Polygon,3857)`, so geometry type and SRID are
part of the relation contract. Resolution changes should be re-materialized
and re-previewed: feature count, cell size, rendering cost, and useful map zoom
can all change substantially.

`style.hover` displays a selected feature field; it does not format numbers or
append units. To show `1,250 m`, expose a text field from an authorized managed
view while preserving the numeric length field used by a graduated theme. For
example, use `to_char(round(length_metres)::bigint,
'FM999,999,999,990') || ' m'`, with an explicit null branch when nulls must stay
null. The standard visual runner validates that the layer renders but does not
reliably trigger hover, so tooltip formatting remains a disclosed evidence gap
unless manually observed.

For MVT configuration on XYZ v4.23.4, use `"3857"` rather than numeric `3857`
for `srid`; the schema accepts both, but the browser runtime may warn and fail
to bind the numeric form. An empty `infoj` removes feature fields but can still
leave an empty information-panel shell on click, so it is not sufficient proof
that a layer is completely non-clickable.

## Dry-run mutation validation

`set`, `amend`, and `unset` send a candidate to the server with saving
disabled. They can be used to inspect validation behavior, but they never
change the live workspace and do not replace the proposal workflow:

```sh
config-cli set --set '/locale/view/z=12'
config-cli amend \
  --set '/locale/view/lng=-1.5491' \
  --set '/locale/view/lat=53.8008'
config-cli unset '/locale/layers/Bus Stops/style/hover'
```

These commands have no `--save` option. The server must explicitly return
`saved: false`; the CLI fails closed if that field is missing or true rather
than overwriting an unexpected response to make it appear safe.

## Proposals

Proposals are the only supported way to change a workspace.

### `proposals check`

Validate and preview operations without creating a proposal or changing the
live workspace:

```sh
config-cli proposals check \
  --base-revision REVISION \
  [--set 'JSON_POINTER=JSON']... \
  [--unset JSON_POINTER]... \
  [--explanation TEXT]
```

It uses the same authoritative candidate validation as proposal creation and
returns the focused diff, categorized warnings/information,
`proposalCreated: false`, and a machine-readable `proposal.create` next action.
The returned check fingerprint and exact operations are cached in private local
state for checked handoff.

### `proposals create`

Create and validate a proposal against an exact workspace revision:

```sh
config-cli proposals create \
  --base-revision REVISION \
  [--set 'JSON_POINTER=JSON']... \
  [--unset JSON_POINTER]... \
  [--explanation TEXT]
```

To create from the exact operations and revision returned by a prior check:

```sh
config-cli proposals create \
  --from-check CHECK_FINGERPRINT \
  [--explanation TEXT]
```

The CLI loads the target-bound mode-`0600` cache and the server recomputes the
fingerprint. `--from-check` cannot be combined with operations or
`--base-revision`.

Exactly one of `--base-revision` or `--from-check` is required. At least one
operation is required for direct creation. Values after
`=` are parsed as strict JSON when possible; otherwise they are strings.
Quote the complete argument. JSON Pointer uses RFC 6901 escaping: `/` becomes
`~1` and `~` becomes `~0` inside a path segment. Other tilde escapes and
invalid array indices are rejected; the workspace root cannot be replaced or
deleted. In RFC 6901 the root pointer is the empty string; `/` instead
addresses an object member whose key is empty and is accepted.

Although RFC 6902 uses `-` for array append in add operations, this CLI's
`set` operation does not accept `-` as an array index. To add an item where no
narrower supported pointer exists, inspect the current array and replace it
with an exact copy plus the intended item. Review the result as a
transport-level array replacement and verify that the semantic diff contains
only the requested addition.

Example:

```sh
config-cli proposals create \
  --base-revision 8b192e... \
  --set '/locale/layers/Bus Stops/display=false' \
  --explanation 'Hides Bus Stops initially without changing its data or style.'
```

Creating a proposal does not alter the live workspace.

If candidate validation fails, the CLI exits with code `3` and reports
`proposal.validation_failed`. The structured details retain the rejected JSON
Pointer and, when supplied by the server, the rule ID plus expected and actual
types. Failures involving `infoj[].fieldfx` also include a structured `sql
test` remediation with the layer key. The SQL expression itself is
intentionally omitted from that suggestion so it is not copied into logs.

### `proposals list`

List proposal summaries:

```sh
config-cli proposals list
```

### `proposals show`

Read the proposal record, including its retained candidate and current
lifecycle status:

```sh
config-cli proposals show PROPOSAL_ID
```

Review its status, original revision, operations, focused diff, explanation,
warnings, and validation evidence before approval.

### `proposals apply`

Apply one previously approved proposal:

```sh
config-cli proposals apply PROPOSAL_ID --confirm
```

Both the proposal ID and `--confirm` are required. The confirmation flag is a
local guard; it does not replace an approver's explicit decision. If the live
revision no longer matches the proposal's original revision, apply fails with
a conflict. Inspect the new state and create a new proposal rather than
rebasing the old one.

An apply timeout or HTTP `5xx` is an indeterminate outcome. The server may have
saved the workspace and marked the proposal `applied` before XYZ reload
confirmation timed out. The CLI preserves the server's structured response
details and exits with code `5`; it does not retry. Before any retry, inspect:

```sh
config-cli proposals show PROPOSAL_ID
config-cli workspace get
config-cli describe
config-cli xyz status
```

If the proposal status is `applied` and its applied revision matches the live
workspace revision, do not apply it again. Treat the write as committed and
continue with XYZ recovery and visual verification. If the proposal is still
pending and the workspace revision is unchanged, investigate before an
operator decides whether to retry. An `applying` status means the server was
interrupted during its recoverable transition; inspect the live revision and
candidate before an operator deliberately repeats the exact approved apply.
A `conflicted` proposal must be replaced with a newly reviewed proposal. Stop
if the state remains ambiguous.

### `proposals decline`

Close a pending proposal without applying it:

```sh
config-cli proposals decline PROPOSAL_ID --confirm [--reason TEXT]
```

Declining is an explicit state change, so `--confirm` is required. Proposal
records are retained for auditability.

## Health and visual evidence

### `xyz status`

Read the remote XYZ reload/health state:

```sh
config-cli xyz status
```

### `visual-plan`

Ask the server to select a map view containing data for a layer:

```sh
config-cli visual-plan --layer "Bus Stops"
config-cli visual-plan --layer "Bus Stops" \
  --locale en-GB \
  --lng -1.55 --lat 53.81 --zoom 12.5
```

`--lng` and `--lat` must be supplied together. `--zoom` may be supplied with
or without an explicit centre. Longitude, latitude, and zoom are validated
before the request. `--locale` selects a named workspace locale; omit it to use
the top-level default, including when named alternatives exist. A missing raw
default resolves to XYZ's synthetic empty locale, not a sole named alternative.

### `visual-test`

Run the server-side browser check for a layer:

```sh
config-cli visual-test --layer "Bus Stops"
config-cli visual-test --layer "Bus Stops" \
  --locale en-GB \
  --lng -1.55 --lat 53.81 --zoom 12.5
config-cli visual-test --layer "Bus Stops" \
  --artifact-dir ./visual-evidence
```

Run it after applying a proposal for every changed visual layer. A passing test
proves that XYZ loaded, the named layer was present, and a canvas rendered. It
does not guarantee cartographic quality; review the returned screenshots when
the change is visually significant.

A browser-validation failure exits with code `6`, but its structured error can
still contain the selected plan, failed report, and authenticated artifact
paths from the server's HTTP 422 response. Preserve and review that evidence.
If the bounded browser runner is already full, the server returns HTTP 429 with
the plan; the CLI also exits with code `6`, and the read-only visual request
may be retried after the reported contention clears.

Use `--artifact-dir` to fetch returned authenticated artifacts into a local
directory. The JSON response then includes `localArtifacts`, keyed like the
server's `visual.artifacts` object, so before/after screenshots can be opened
directly from the agent workspace.

A passing test checks HTTP success, canvas presence, the requested layer, and
browser errors. It does not prove exact colour appearance, pointer interaction,
information-panel output, emoji/custom-font glyph fidelity, or general
cartographic quality. Grouped layers may render and interact successfully while
a strict layer-name text assertion fails because the visible drawer text is the
folder label rather than the child layer name. Use downloaded screenshots,
returned reports, and manual interaction when those details are acceptance
criteria.

Normal pre-approval tests render the current live workspace, not an unapplied
candidate. Treat them as baseline evidence unless the server explicitly
reports an isolated candidate-preview capability.

### Proposal candidate previews

Render a pending proposal's retained candidate in an isolated runtime:

```sh
config-cli proposals preview-plan PROPOSAL_ID --layer "Bus Stops"
config-cli proposals preview-test PROPOSAL_ID --layer "Bus Stops"
config-cli proposals preview-screenshot PROPOSAL_ID --layer "Bus Stops"
config-cli proposals preview-screenshot PROPOSAL_ID --layer "Bus Stops" \
  --view-mode default
config-cli proposals preview-screenshot PROPOSAL_ID \
  --layer "Definitive Paths" \
  --panel filtering \
  --expect-panel-text "Resurface cost"
config-cli proposals preview-screenshot PROPOSAL_ID \
  --layer "Definitive Paths" \
  --panel styling \
  --expect-panel-text "0–500 m"
config-cli proposals preview-test PROPOSAL_ID --layer "Bus Stops" \
  --artifact-dir ./visual-evidence
```

All three accept the same optional `--locale`, `--lng`, `--lat`, `--zoom`, and
`--artifact-dir` controls as the top-level visual commands. The CLI requires
the response to report `source: candidate`, the requested proposal ID, and a
non-empty candidate hash. A mismatched or live-workspace response is rejected.
The server contract must also advertise the exact command name required by the
invocation. A related capability action or matching route is insufficient; do
not bypass a `capability.missing` result.

Candidate preview is read-only: it neither applies the proposal nor reloads or
changes the live workspace. A failed test or screenshot exits with code `6`
while preserving the returned plan, report, and authenticated artifact paths.
Conflicted, declined, superseded, corrupt, or stale candidates are rejected by
the server rather than rendered.

`preview-screenshot` accepts repeated `--panel filtering` and `--panel styling`
options. The server expands the requested layer, including its containing
folder when XYZ exposes a group label instead of the child layer name, opens
the requested drawer in both original and candidate renders, and returns
dedicated artifacts such as `beforeFilteringPanel`, `afterFilteringPanel`,
`beforeStylingPanel`, and `afterStylingPanel`. Use repeated
`--expect-panel-text` values to require specific filter labels, numeric bound
labels, legend titles, class labels, or other control text to be present. If
XYZ cannot open the requested drawer, the command exits as a visual evidence
failure while preserving the page/map/report artifacts that were produced.

The default `--view-mode focus` supplies an XYZ `layers` query parameter so the
requested layer (and relevant group context) is visible in the evidence. Use
`--view-mode default` for initial-visibility changes. That mode omits the
`layers` query parameter from both the original and candidate URLs, producing
one comparison of the actual before and after startup views. Since the
requested layer may correctly be hidden on either side, default-view evidence
checks page health, canvas rendering, and browser errors without requiring its
drawer label to be present.

Each preview invocation covers one requested layer in one selected map view.
It must not be presented as complete visual evidence for a large or mixed
proposal. Build a coverage checklist from the focused diff:

- preview every added, removed, moved, or otherwise visually changed layer;
- add separate cases for distinct geographic areas that cannot share a
  readable view;
- choose representative affected layers for workspace-wide visual/view
  changes;
- mark non-visual operations as not visually applicable.

Run `preview-plan` for each case before capturing the screenshot. Inspect its
centre, zoom, source, and warnings. When automatic framing is misleading,
provide `--lng`, `--lat`, and `--zoom`; do not force unrelated changes into one
unreadable, zoomed-out image. Use a separate `--artifact-dir` per checklist
case so each result can be traced to its layer and view.

For group membership changes, the server's comparison isolates the affected
layer: add is off before and alone after, remove is alone before and off after,
and move is alone on both sides. Other group members are hidden for those
comparisons. A normal layer edit that does not change group membership may
retain group context.

### `screenshot`

`screenshot` is a convenience alias for a visual test that returns its
screenshot artifacts:

```sh
config-cli screenshot --layer "Bus Stops" --locale en-GB
config-cli screenshot --layer "Bus Stops" \
  --artifact-dir ./visual-evidence
```

The same optional `--locale`, `--lng`, `--lat`, `--zoom`, and `--artifact-dir`
controls are available.

### `xyz reload`

An operator can explicitly request a standalone XYZ reload:

```sh
config-cli reload-xyz --confirm
```

`reload-xyz` is the top-level alias for `xyz reload`; the existing nested form
remains available and performs exactly the same confirmed request:

```sh
config-cli xyz reload --confirm
```

On the current MAPP Platform, the endpoint derives the live workspace
fingerprint and waits for the supervisor to report TCP readiness with that
fingerprint. This confirms process/file readiness, not database-backed
rendering or cartographic quality.

This is outside the normal agent change workflow because proposal application
requests the associated reload on supported servers.

## No direct-write interface

Legacy `--save` options are not part of the client. Dry-run mutation commands
always send `save: false`. Do not call mutation or workspace-save endpoints
manually to bypass proposals.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Success |
| `2` | Invalid command or local input |
| `3` | Remote validation failure |
| `4` | Target identity, contract, revision, or proposal conflict |
| `5` | Connectivity or protocol failure |
| `6` | Visual verification failure |
| `7` | Authentication or authorization failure |
| `130` | Command interrupted or interactive input closed |

Automation should use the exit code for broad handling and the structured
error code/details for diagnosis. In particular, an apply timeout or server
failure uses code `5`, while a live or proposal-bound visual failure uses code
`6`.
