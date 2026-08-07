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
copied into the CLI's private credential store. Token files must be regular,
non-symlink files no larger than 64 KiB. `--timeout` sets the request timeout
and defaults to 60 seconds.
JSON remains the stable default. `--output human` provides a concise view for
`doctor`, `proposals check`, `proposals create`, and semantic table/field
generation; other commands retain JSON.
`--input` merges a bounded JSON object into supported request-producing
commands; conflicts with explicit flags fail closed. Use `-` for stdin.
Credential-like keys are rejected. `--extract` selects one scalar from the
final response, while `--out` writes it atomically with mode `0600` on
supported POSIX hosts. Run the CLI under WSL on Windows; native Windows
operational commands fail before local-state access or a remote request.

Input JSON is limited to 5 MiB and must be an object. A file input must be a
regular non-symlink file. Keys containing `authorization`, `credential`,
`password`, `secret`, or `token` are rejected recursively. Input merging occurs
after command-line parsing: it can add request fields, but it does not satisfy
required parser arguments such as `--base-revision`, `--set`, `--layer`, or
`--expression`. For `semantic proposals check`, a supplied `operations` array
may replace `--set`/`--unset`; `--asset-id` and `--base-version` remain
required.

The same regular-file, no-symlink, and 5 MiB local-read boundary applies to
`validate --file` candidates and derived-layer `--query-file` SQL.
Do not pass `/dev/stdin` or a process-substitution path as `--query-file`:
they are rejected as non-regular or symlinked files. When an automation must
generate a derived query in memory, use the documented global `--input -` JSON
object with `query` and `sources` instead.

Extraction accepts dot-separated object keys and numeric list indices, with an
optional `$` or `$.` prefix, for example `revision`,
`$.compatibility.compatible`, or `checks.0.id`. The selected value must be a
scalar; missing paths and object or array results are errors. `--out` creates
parent directories privately where possible and atomically replaces the
destination with a mode-`0600` file.

Specialized `--output human` rendering is currently provided for `doctor`,
`proposals check`, `proposals create`, `semantic generate table`, and
`semantic generate field`. Other commands retain JSON output even when
`human` is selected.

`--input` is supported by `validate`, `sql test`, the dry-run mutation
commands, live visual commands, workspace proposal check/create, semantic
proposal check, and proposal candidate preview commands. Other commands reject
it with `input.unsupported`.

## Runtime command and API map

The connected platform, not this document, is authoritative. The CLI reads the
exact command set from `GET /api/contract` and action schemas from
`GET /api/capabilities`. It fails with `capability.missing` when the required
command or compatibility marker is absent; a related action ID or a known URL
is not permission to bypass that gate.

| CLI family | Server API | Advertised action | Scope |
| --- | --- | --- | --- |
| `setup`, `init`, `auth replace` | Public identity plus authenticated contract/connect; setup finishes with `describe` | Client-side bootstrap/profile operation | A valid token can initialize/replace; setup's final workspace check needs `inspect` |
| `profiles *`, `completion` | None | Local-only command | None |
| `doctor` | Identity/contract/auth plus conditional workspace/semantic reads | Client-side diagnostic; intentionally not capability-gated | Depends on checks supported by the target and granted to the credential |
| `describe`, `schema`, `rules`, `examples`, `explain-error` | Identity, contract/connect, workspace, and guidance reads | Command-advertised read | `inspect` after unauthenticated public identity |
| `capabilities list\|show` | `/api/capabilities` | Returns `actions[]` | Any authenticated credential, including a semantic-only token |
| `plugins list\|show\|validate\|usage` | `/api/plugins` | Command-advertised read | `inspect` |
| `workspace get`, `layers list\|get\|style-elements\|filters`, `catalog list`, `icons list`, `sql capabilities` | Corresponding authenticated GET routes | Command-advertised reads; layer commands require the `layers effective` compatibility marker | `inspect` |
| `validate` | `/api/validate` | Command-advertised non-saving validation | Legacy `full` or administrator session |
| `set`, `unset`, `amend` | `/api/mutate` with `save: false` | Command-advertised dry run | Legacy `full` or administrator session |
| `sql test` | `/api/sql/test` | Command-advertised bounded probe | Legacy `full` or administrator session |
| `derived-layers capabilities\|list\|show\|map-extent` | `/api/derived-layers/*` GET routes | `derived-layers.map-extent` for the preview | `inspect` |
| `derived-layers create\|refresh\|replace\|drop` | Managed derived-layer POST routes | `derived-layers.create`, `derived-layers.refresh`, `derived-layers.replace`, `derived-layers.drop` | `derive`; create/replace also need `semantic:inspect` |
| `proposals check\|create` | Workspace proposal routes | `proposals.check`, `proposals.create` | `propose` |
| `proposals list\|show` | Workspace proposal GET routes | Command-advertised reads | `inspect` |
| `proposals decline` | Workspace proposal decline route | Command-advertised lifecycle operation | `propose` |
| `proposals apply` | Workspace proposal apply route | `proposals.apply` | `apply` |
| `visual-plan`, `visual-test`, `screenshot` | `/api/visual-plan`, `/api/visual-test` | `visual.plan`, `visual.test`, `visual.screenshot` | `visual` |
| `proposals preview-plan\|preview-test\|preview-screenshot` | Proposal-bound visual routes | `proposals.preview-plan`, `proposals.preview-test`, `proposals.preview-screenshot` | `visual` |
| `xyz status\|reload` (`reload-xyz` alias) | `/api/xyz/status`, `/api/xyz/reload` | `xyz.reload` for reload | `inspect` or `reload` |
| `operations show\|wait` | `/api/operations/{operationId}` | Originating operation kind | Same `visual`, `apply`, `reload`, or `derive` scope |
| `operations cancel` | `/api/operations/{operationId}/cancel` | Background derived-layer create, replace, or refresh | `derive`; `--confirm` required |
| `auth status\|device` | Auth identity and device start/poll routes | Command-advertised auth flow | Any authenticated credential for status; device verifies the current target before unauthenticated start/poll |
| `semantic *` | `/api/semantic/*` | Matching `semantic.*` action | See [Semantic metadata](#semantic-metadata) |

`/api/auth/login`, `/api/auth/logout`, and the password, token,
device-approval, and audit routes under `/api/admin/*` are dashboard-only
administrator-session surfaces, not CLI commands. Neither are direct workspace
saves or private semantic-service, browser-runner, preview, and XYZ
reload-channel interfaces. Dry-run `set`, `unset`, and `amend` use only the
configuration API's non-saving validation path; remote writes use proposals.

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
revision, semantic readiness and catalog revision when advertised, API
version, contract version, XYZ version, and compatibility status. Run this
before planning any change.

### `doctor`

Check local configuration safety and end-to-end readiness:

```sh
config-cli --profile production doctor
```

The result covers credential availability without revealing it, private state
permissions, target identity and compatibility, authentication/scopes,
workspace access, semantic readiness, and advertised SQL, visual, and semantic
capabilities.
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
config-cli auth device --scope derive
config-cli auth device --scope semantic:inspect
config-cli auth device --scope semantic:inspect --scope semantic:source
config-cli auth device --scope semantic:inspect --scope semantic:generate
config-cli auth device --scope semantic:inspect --scope semantic:generate \
  --scope semantic:data
config-cli auth device --no-browser
```

Without an explicit `--scope`, the CLI requests the server contract's
advertised safe defaults. The current platform advertises `inspect`,
`propose`, `visual`, and `semantic:inspect`; a legacy 1.0 server without
semantic support falls back to `inspect`, `propose`, and `visual`. Malformed,
unsupported, unknown, or elevated advertised defaults are rejected.
Workspace apply/reload, `derive`, and the elevated `semantic:source`,
`semantic:propose`, `semantic:generate`, `semantic:data`, `semantic:apply`, and
`semantic:admin` scopes remain explicit grants. Semantic generation also
requires `semantic:inspect`; it is
not included in the safe default device authority because it sends authorized
metadata to an external model. The
device code expires after ten minutes, the token after thirty days, and the
token response is one-time. The start response, issued credential record, and
authenticated identity must each report exactly the requested scope set. The
old credential remains selected unless all checks verify the same instance,
compatible contract, and authority. Use `--no-browser` on a headless or remote
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

### `operations show|wait|cancel`

Inspect or wait for a durable operation:

```sh
config-cli operations show OPERATION_ID
config-cli operations wait OPERATION_ID --wait-timeout 120 --interval 1
config-cli operations cancel OPERATION_ID --confirm --wait-timeout 120 --interval 1
```

Terminal states are `succeeded`, `failed`, `cancelled`, and `indeterminate`.
Never blindly retry an indeterminate apply or reload. Background derived-layer
create, replace, and refresh jobs are durable operations and require `derive`
to inspect or cancel. Cancellation first reports nonterminal `cancelling`; the
CLI returns success only after the server reports `cancelled`, proving the
database transaction rolled back. If commit already won the race, cancellation
fails and the original terminal result remains authoritative. A late
derived-layer reporting failure may follow a committed database transaction,
so reconcile the operation, managed-layer registry, and catalog before
retrying. Wait and cancel default to a 120-second wait timeout and a one-second
polling interval. A lost poll or local wait timeout returns
`indeterminate: true`, `failurePhase: "operation-polling"`, the operation ID,
and reconciliation commands with automatic retry disabled.

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
config-cli schema --pointer '/$defs/templateDefinition'
config-cli schema --pointer '/$defs/locale/properties'
config-cli schema --pointer '/$defs/layer/properties/gazetteer'
```

Treat entries explicitly present under the returned `properties` map as the
server's audited pinned-XYZ capabilities. Unknown contract properties are
rejected rather than preserved or silently removed. Open maps represent
audited arbitrary-name features, not general extension support. Before proposing a
template, plugin, gazetteer, or other advanced change, inspect the focused
definition and then inspect the raw target value with `workspace get` or
`layers get`.

On the currently pinned platform, gazetteer configuration is a layer property,
not a locale property. A live template `src` descriptor is validated without
executing it; XYZ fetches it on first use in a reloaded generation. Proposal
evidence must therefore include post-apply reload status and an appropriate
functional or visual test rather than treating schema validation as proof that
the source loaded or its SQL/module ran.

### Advanced workspace setup

Advanced configuration is optional: an absent `templates` object or an empty
advanced locale object means it has not been configured. It is not an API
read error. Place named query/composition definitions in top-level
`templates`; use `locale.keyvalue_dictionary` or `layer.keyvalue_dictionary`
for recursive replacements; configure a gazetteer at `layer.gazetteer`; and
inspect `plugins list`, `plugins show KEY`, and `plugins usage KEY` before
configuring a bundled plugin. The dashboard’s **Advanced workspace
configuration** guide provides the same mapping.

### `rules`

Read validation and safety rules, optionally restricted to a category:

```sh
config-cli rules
config-cli rules --category security
```

### `plugins list|show|validate|usage`

Inspect the connected server's pinned XYZ plugin registry and runtime rules:

```sh
config-cli plugins list
config-cli plugins show feature_info
config-cli plugins show viewport-layer-count
config-cli plugins validate
config-cli plugins usage viewport-layer-count
```

The response covers dynamic sources, registration, synchronous/parallel
locale dispatch, non-awaited layer dispatch, failure behavior, prerequisites,
and security. External entries also include their manifest schema, hashes,
XYZ range, catalogue fingerprint, usage, and declarative preview checks.
`validate` fails when the catalogue or current workspace usage is invalid.
Dynamic modules execute trusted browser JavaScript; preview evidence verifies
registration and declared observable behavior even though XYZ itself tolerates
an import failure.

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

Single-field categorized themes use `style.theme.field`. Multi-field
categorized point icons use `style.theme.fields` and each category's own
`field`; do not set both `field` and `fields` on the same theme. The multi-field
form composes an array of point icons, so use it only where the target layer
renders as point geometry. A revision-bound proposal can set the complete theme:

```sh
config-cli proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/style/theme={"type":"categorized","title":"Bus stop status markers","fields":["status","priority"],"categories":[{"field":"status","value":"open","label":"Open","style":{"icon":{"type":"dot","fillColor":"#176b4d"}}},{"field":"priority","value":"high","label":"High priority","style":{"icon":{"type":"triangle","fillColor":"#f8961e"}}}]}' \
  --set '/locale/layers/Bus Stops/style/elements=["theme"]' \
  --explanation 'Composes Bus Stops point icons from status and priority without setting a top-level categorized field.'
```

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
config-cli derived-layers map-extent
config-cli derived-layers map-extent --locale Leeds
```

Choose a PostgreSQL-safe managed relation name before writing the query. It
must start with `a-z`, then contain only lowercase `a-z`, `0-9`, or `_`, and
must be no longer than 63 characters:
`^[a-z][a-z0-9_]{0,62}$`. The `--id-column` and `--geometry-column` values use
the same rule. Prefer a name such as `road_lengths_h3_r9`; do not use spaces,
hyphens, dots, uppercase, or quoted mixed-case names. The server always uses
the fixed `derived_layers` schema and safely quotes these identifiers.

For agent-driven work, start with `semantic catalog search` and
`semantic catalog show` rather than guessing a relation, field, geometry, or
unit. Every declared `--source` must already have a ready PostgreSQL semantic
profile, so an unprofiled source is rejected by the server. If no suitable
profile exists, discover only authorized candidates with `semantic source
relations`, then explicitly register the selected relation with `semantic
source sync ... --confirm` and inspect the resulting generated profile before
writing the derived SQL. This applies to table and view relations only;
PostgreSQL, PostGIS, and H3 functions used inside the query do not need
semantic profiles.

Put the query in a file so shell parsing cannot change it. Store generated SQL
drafts in the repository-local, git-ignored `tmp/` directory rather than the
repository root, and do not commit them:

```sh
config-cli derived-layers create paths_h3_r9 \
  --kind view \
  --query-file tmp/paths-h3-r9.sql \
  --source leeds.definitive_paths \
  --id-column h3_id \
  --geometry-column geom_3857 \
  --confirm
```

Create, replace, refresh, and drop all require `--confirm`. This is a local
command guard that records deliberate invocation; it is not evidence of the
separate user authorization required for the database action.

Every create or replace uses a fixed, bounded spatial scope around the selected
locale's configured map centre. The server uses a 1920x1080 planning viewport
at one zoom level wider than the configured view (`max(0, z-1)`, clamped at
zoom 0) and wraps the submitted query with an exact intersection filter.
`--map-extent` remains accepted for compatibility with older automation but
does not change this mandatory behavior:

```sh
config-cli derived-layers map-extent --locale Leeds
config-cli derived-layers create paths_h3_r9 \
  --kind materialized \
  --query-file tmp/paths-h3-r9.sql \
  --source leeds.definitive_paths \
  --id-column h3_id \
  --geometry-column geom_3857 \
  --locale Leeds \
  --confirm
```

Preview the resolved envelope before the mutation. The filter selects complete
output features whose geometry intersects the envelope; it does not clip their
geometry. The saved envelope does not follow later map pans, zooms, viewport
sizes, or workspace view changes. Use `derived-layers replace ... --locale ...
--confirm` to resolve and save a new scope. Ordinary views still track
source-row changes within that fixed scope; materialized views update on
refresh; refresh does not recalculate the envelope. Replace always submits a
new server-resolved scope, and omitting the compatibility flag cannot clear it.

The automatic wrapper filters final output rows only. It is not an RLS or
security boundary, and it does not make upstream aggregates, window values,
limits, or computation map-scoped. When a metric itself must describe the map
area, apply the previewed envelope inside the source-side SQL before
aggregation. Doing this as early as practical also avoids an otherwise
unbounded source calculation. Semantic catalog profiles remain authoritative
for relation and field meaning; the spatial option is not permission to guess
either. The preview and returned `derivedLayer.spatialScope` contain the
server-resolved plan; retain that object in an agent's review/evidence packet.
Background create or replace returns and validates the same resolved plan when
the durable operation completes.

A per-cell point count and its denominator can intentionally have different
scopes. For “portion of all points in each cell,” count intersections per cell
but calculate the denominator from the complete declared point relation. Use a
map-filtered denominator only when “all” explicitly means all points within the
saved map area; neither the wrapper nor a size probe samples that denominator.

`derived-layers capabilities` advertises `queryGuard` for every layer kind.
On API/contract 1.3 it includes ordered AST/catalog/EXPLAIN `stages`,
`shapeLimits`, plan `limits`, H3 bounds, and the stable `errorCategories`
mapping; the CLI validates that closed shape while still accepting the earlier
compatible 1.x guard shape.
Newer compatible servers can advertise the separate, optional
`queryPlanning` version `1` contract. It bounds estimated nested-loop pair rows
without changing the existing `queryGuard` or `queryPlanProbe` shapes. A
successful mutation then preserves `derivedLayer.queryPlanningProbe` alongside
`derivedLayer.queryPlanProbe`. Both optional objects use closed versioned
schemas; earlier servers that omit them remain compatible.

When the server advertises `h3Readiness`, the CLI also validates its closed
catalog-and-execution result and its consistency with `h3Available`. A failed
result includes one bounded stage-specific reason and `suggestedAction`.
Create and replace inspect this fresh readiness immediately before submitting a
query that invokes an H3 function; an unavailable result stops locally and no
mutation request is sent. Queries that merely read an `h3_id` column or an
existing H3-derived relation do not require H3 function readiness. Earlier
servers without `h3Readiness` remain compatible; their boolean `h3Available`,
when present, is still authoritative.
Before any create or replacement, and before a materialized refresh, the server
runs non-writing PostgreSQL `EXPLAIN` over the exact scoped query and recursively
checks total cost, final and intermediate rows, intermediate bytes, join
expansion, plan size/depth, planned workers, and recursion. It first rejects
obviously explosive SQL shapes, sleep/session/advisory-lock functions, and
unprovable H3 or generated-row expansion.
Successful mutations return `derivedLayer.queryPlanProbe`; preserve it in the
review packet. Guard failures use distinct codes:

| Code | Status/category | What the user should correct |
| --- | --- | --- |
| `derived_layer.query_invalid` | HTTP 400 / `invalid` | Fix malformed SQL or submit exactly one parseable `SELECT` statement. |
| `derived_layer.query_not_allowed` | HTTP 422 / `policy` | Remove or replace the prohibited SQL/catalog dependency, following each reason's `suggestedAction`. |
| `derived_layer.query_too_expensive` | HTTP 409 / `compute` | Reduce SQL/H3 expansion, join fan-out, recursion, or intermediate planner work. |

All three block both kinds and have no `recommendedKind`; never offer a view as
an escape. The structured response includes `operation`, reason-specific
actions, and, when known, `stateUnchanged: true` plus an operation-specific
`safeState`. Present those fields instead of a generic cost or H3 message, and
keep `technicalDetail` in optional diagnostics only. The closed
`failurePhase` vocabulary distinguishes `preflight`, proven
`database-transaction` rollback, uncertain `transaction-rollback` or
`transaction-commit`, post-commit `result-reporting`, client-side
`request-response` and `operation-polling`, and startup `service-recovery`.
Only preflight and proven rollback may include unchanged-state fields.
For `derived_layer.database_error`, that optional object is limited to bounded
`sqlstate` and primary `message` fields; it never contains the SQL, PostgreSQL
context, detail, or hint.
`derived_layer.database_contention` instead reports a proven-safe HTTP `409`
conflict. Its `contentionScope` distinguishes the global `derived-mutation`
admission lock from a `postgresql-lock` outside that admission boundary, and
`retryable: true` means only that the same reviewed request may be retried
manually after the blocker clears. It never authorizes an automatic retry.
A `derived_layer.source_mismatch` response separately exposes
`declaredSources`, `resolvedSources`, `missingSources`, and `extraSources`; make
those two lists match instead of treating the failure as query cost.

When a compute failure contains reason `nested_loop_pair_work` and a valid
over-limit `queryPlanningProbe`, the CLI adds `details.clientGuidance` without
altering any server field. The guidance is an authoring aid, not a SQL rewrite:
perform the selective candidate match on a native geometry or the exact
prepared transform expression before materializing joined rows; aggregate
pair-local metrics after that match; calculate compatible complete-input
global totals together in a single one-row aggregate referenced after selective
aggregation while preserving row-dependent window semantics; calculate
transformations, intersections, and areas once at that narrowed stage rather
than relying on an inline CTE alias; then resubmit the revised
definition so the server reruns preflight before mutation. Do not reduce pair
work by sampling or map-filtering totals whose intended meaning covers the
complete declared input, and retain the reviewed exact spatial predicate after
candidate generation. Unknown,
malformed, or within-limit planning evidence is preserved as received but does
not receive client guidance. The same behavior applies to synchronous errors
and durable failures surfaced from `--background` or `operations wait`.

Capabilities separately advertise `materializationGuard`. For a materialized
create, view-to-materialized replacement, or refresh, the same plan estimates
storage from planned rows and row width, adds the advertised row overhead and
safety multiplier, and blocks estimates above `maxEstimatedBytes` (currently
1 GiB). This is a planner estimate, not a row sample or a guarantee of final
on-disk size. Successful materialized mutations also return
`derivedLayer.materializationProbe` for review.

An oversized stored result returns `derived_layer.materialization_too_large`,
`blocked: true`, `recommendedKind: "view"`, and the closed `probe`. With
`probeStage: "estimate"`, no materialized DDL or refresh started. With
`probeStage: "actual"`, population and indexing happened inside the transaction
before the measured result exceeded the limit; `rolledBack: true` confirms the
transaction was rolled back, but not that it avoided transient table, index,
TOAST, or WAL growth. Preserve the operation-specific `safeState` and ask
before changing the requested kind. An ordinary view stores no result rows and
is the offered fallback only after the computation probe passes; it moves query
cost to reads and is not an automatic equivalent. For refresh, the corrective
choice is to convert the existing layer or reduce its output, not create a
duplicate layer.

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
  --query-file tmp/paths-h3-r9.sql \
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
Every response preserves the server's `semanticProfile` readiness record.
Do not propose a new workspace reference until its status is `ready`; inspect
other states with the semantic commands below. `repair` is not a general state
transition: it only requeues the unchanged retained event for a
`repair_required` profile. Correct deterministic failures before requeueing or
the same rejection will recur.

H3-derived relations should make their spatial semantics explicit. Generate
polygon candidates directly from the server-supplied
`_mapp_h3_scope.geom_4326` using a literal resolution. The server estimates the
resulting scope cells and rejects unsafe or dynamic expansion; it also requires
literal bounded distances for grid disk/ring traversal. Non-expanding H3 index,
parent, and boundary operations and provable one-level child expansion remain
available for legitimate aggregation. Arbitrary child targets, uncompact, and
grid-path expansion are not admitted. Polygon cells, disk/ring maxima, and
one-level children are multiplied conservatively and must remain within the
advertised 10,000,000 combined-cell bound. The general PostgreSQL plan budget
still applies.

For "touches any source feature" workflows, use those map-bounded cells as
candidates and then filter or aggregate their generated polygons with a
reviewed exact predicate, such as `ST_Intersects`, against the complete original
relation. Do not sample source rows when a layer-level average, sum, window, or
other value must use the complete declared input.

When a complete-input aggregate is needed beside spatially filtered results,
place it in a separate one-row CTE but do not attach it with `CROSS JOIN` or
`JOIN ... ON TRUE`. The derived-layer guard rejects those constructs as
Cartesian joins even for a one-row CTE. Reference the aggregate with scalar
subqueries in the final projection instead, such as
`(SELECT national_total FROM national_totals)`, while retaining the bounded
spatial predicate for the candidate join.

Some H3/PostGIS convenience wrappers assume a broader PostgreSQL search path
than the derived-layer runner provides. Errors such as unresolved `geometry`
types or `st_dump` functions can indicate wrapper resolution rather than a bad
spatial idea. Prefer explicitly qualified PostGIS calls and the extension's
geometry-native `h3_cell_to_boundary_geometry(cell)` function. If
`h3_cell_to_boundary_wkb(cell)` is unavoidable on the pinned extension, pass
its EWKB result to `ST_GeomFromEWKB(...)`, not
`ST_GeomFromWKB(..., 4326)`. That mismatch emits one warning per
evaluated/generated cell and can flood PostgreSQL logs. A derived-layer
mutation that times out or returns an unclassified or malformed HTTP `5xx` is
indeterminate with
`failurePhase: "request-response"`: inspect `derived-layers list`,
`derived-layers show NAME`, and `catalog list` before recreating, replacing,
or dropping anything. A coherent structured server failure retains its
server-provided phase and unchanged or indeterminate state.

Create, replace, and refresh are synchronous by default; use that path first
for ordinary views and jobs expected to finish promptly. For a known slow
materialized job, add `--background`; the CLI then requests a durable server
operation and polls it automatically. Background mode accepts
`--wait-timeout` (default 1860 seconds) and `--interval` (default one second).
Reaching the local wait timeout does not cancel database work; continue with
the operation ID from the structured error using
`config-cli operations wait OPERATION_ID`. This error uses
`failurePhase: "operation-polling"` and disables automatic retry.

When a durable query or size guard rejects the work, the server stores the same
structured envelope under `operation.error`. The CLI uses its safe
`userMessage` and `derived_layer.*` code as the primary error and preserves its
`suggestedAction`, reasons, probe, and state fields under `details`. An
unexpected `derived_layer.operation_failed` at preflight or after proven
rollback can retain an unchanged-state claim. Commit, rollback-finalization,
and reporting failures are `indeterminate`; inspect the operation, managed
layer, and catalog before retrying.

Do not blindly resend a synchronous request after a client timeout or an
unclassified/malformed HTTP `5xx`: it may have committed. Inspect
`derived-layers list`,
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
null. Live and candidate visual tests automatically exercise configured hover.
Use `--hover` to require it and repeated `--expect-hover-text` assertions for
the formatted value; count it as evidence only when the report says hover was
attempted, opened, and passed and includes the dedicated tooltip artifact.

For MVT configuration on XYZ v4.23.4, use `"3857"` rather than numeric `3857`
for `srid`; the schema accepts both, but the browser runtime may warn and fail
to bind the numeric form. An empty `infoj` removes feature fields but can still
leave an empty information-panel shell on click, so it is not sufficient proof
that a layer is completely non-clickable.

## Semantic metadata

Semantic reads require `semantic:inspect` and return the authoritative
`catalogRevision`:

```sh
config-cli semantic status
config-cli semantic catalog export
config-cli semantic catalog search "bus stops" --limit 20
config-cli semantic catalog show ASSET_ID
config-cli semantic catalog history ASSET_ID
config-cli semantic catalog archive ASSET_ID --confirm
config-cli semantic derived-profiles list
config-cli semantic derived-profiles show DERIVED_LAYER_NAME
```

`catalog show` returns the complete generated profile for that asset, including
its binding, relation facts, ordered fields, types, and stable field IDs, plus
the separately curated table annotation and field annotations. Search results
are discovery hints; use `catalog show` and its canonical asset ID, version,
provenance, and revision as evidence. These semantic commands consume API JSON;
they do not connect to PostgreSQL or return table rows. `catalog list` is a
separate workspace-configuration catalog and is not a substitute for reviewed
semantic meaning.

The semantic command-to-contract mapping is:

| CLI command | Capability action | API route | Required narrow scopes |
| --- | --- | --- | --- |
| `semantic status` | `semantic.status` | `GET /api/semantic/status` | `semantic:inspect` |
| `semantic catalog export` | `semantic.catalog.export` | `GET /api/semantic/catalog` | `semantic:inspect` |
| `semantic catalog search` | `semantic.catalog.search` | `GET /api/semantic/catalog/search` | `semantic:inspect` |
| `semantic catalog show` | `semantic.catalog.show` | `GET /api/semantic/catalog/objects/{assetId}` | `semantic:inspect`; add `semantic:admin` for an exact archived ID |
| `semantic catalog history` | `semantic.catalog.history` | `GET /api/semantic/catalog/objects/{assetId}/history` | `semantic:inspect`; add `semantic:admin` for an exact archived ID |
| `semantic catalog archive` | `semantic.catalog.archive` | `POST /api/semantic/catalog/objects/{assetId}/archive` | `semantic:inspect` + `semantic:admin` |
| `semantic source relations` | `semantic.source.relations` | `GET /api/semantic/source/relations` | `semantic:inspect` + `semantic:source` |
| `semantic source sync` | `semantic.source.sync` | `POST /api/semantic/source/sync` | `semantic:inspect` + `semantic:source` |
| `semantic source archive-excluded` | `semantic.source.archive-excluded` | `POST /api/semantic/source/archive-excluded` | `semantic:inspect` + `semantic:admin` |
| `semantic generate table\|field` | `semantic.generate` | `POST /api/semantic/generate` | `semantic:inspect` + `semantic:generate`; add `semantic:data` for either context flag |
| `semantic derived-profiles list\|show` | `semantic.derived-profiles.list\|show` | `GET /api/semantic/derived-profiles[/{name}]` | `semantic:inspect`; `semantic:admin` adds delivery diagnostics |
| `semantic derived-profiles repair` | `semantic.derived-profiles.repair` | `POST /api/semantic/derived-profiles/{name}/repair` | `semantic:admin` (`semantic:inspect` is additionally needed to discover/inspect the profile) |
| `semantic proposals list\|show` | `semantic.proposals.list\|show` | `GET /api/semantic/proposals[/{proposalId}]` | `semantic:inspect` |
| `semantic proposals check\|create\|decline` | matching `semantic.proposals.*` action | matching semantic proposal `POST` route | `semantic:propose` |
| `semantic proposals apply` | `semantic.proposals.apply` | `POST /api/semantic/proposals/{proposalId}/apply` | `semantic:apply` |

The exact action and CLI command must be advertised by the connected server;
a similar route is not a compatibility substitute. A legacy `full` token or
dashboard administrator is handled by the server separately from these narrow
scope combinations. `schemaVersion` identifies the semantic data shape, while
`catalogRevision` identifies a whole-catalog snapshot; neither is the asset
`version` used as a proposal's `baseVersion`.

Tokens with both `semantic:inspect` and the separate `semantic:source` scope
can discover source relations and explicitly synchronize one relation into
generated semantic metadata:

```sh
config-cli semantic source relations
config-cli semantic source sync \
  --alias DATABASE_ALIAS \
  --schema SCHEMA \
  --relation RELATION \
  --confirm
config-cli semantic source archive-excluded --confirm
```

Discovery returns the relation kind and deterministic asset ID for configured
database relations, but no columns or rows. Internal underscore-prefixed
relations are never returned or accepted as semantic sources. Operators can
also configure named exclusions with `SEMANTIC_SOURCE_EXCLUSIONS`, using the
same `ALIAS:schema.relation` or `ALIAS:schema.*` selector form as the source
allowlist. This is deployment configuration rather than a hard-coded table
list; the bundled platform example uses
`MAPP:leeds.census_datasets`. It blocks future discovery and synchronization
but does not retroactively hide a profile registered before the setting
changed.
Synchronization sends exactly the
alias/schema/relation identity to the server, which performs the authoritative
schema inspection; it does not accept SQL or upload row data. The operation
registers a missing source asset or refreshes its generated schema facts while
preserving curated meaning. If the schema definition digest has not changed,
the server returns `unchanged` and does not advance the catalog. Review the
returned operation, canonical asset, version, and catalog revision before
using `semantic generate`. The local
`--confirm` guard is required because synchronization changes the semantic
catalog; it is not sent as an extra API property.

`archive-excluded` requires `semantic:inspect` and `semantic:admin`. It
archives every ready source asset matching the server's configured
`SEMANTIC_SOURCE_EXCLUSIONS`, retaining immutable history while removing it
from normal semantic discovery. `semantic catalog archive ASSET_ID --confirm`
uses the same scopes to archive one selected ready profile. Neither action
changes the database relation or its rows. Retain the asset IDs before
archiving: catalog export, search, and derived-profile collections omit
archived assets even for administrators, while an exact `catalog show` or
`catalog history` remains available by ID only with both scopes. Ordinary
exact reads return `404`, and removing an exclusion does not unarchive the
tombstone.

An explicitly authorized caller can ask Gemini for a review-only draft for an
entire table or one stable field ID. Generation is metadata-only unless one of
the optional data-context flags is supplied:

```sh
config-cli semantic generate table ASSET_ID
config-cli semantic generate field ASSET_ID FIELD_ID
config-cli semantic generate table ASSET_ID --sample-rows
config-cli semantic generate field ASSET_ID FIELD_ID --statistics
config-cli semantic generate table ASSET_ID --sample-rows --statistics
```

Generation requires both `semantic:inspect` and `semantic:generate`. The server
sends only semantic/schema metadata the caller is already allowed to inspect
when neither flag is present. `--sample-rows` explicitly allows raw values from
a bounded 5% sample to leave MAPP for Gemini. `--statistics` allows relevant
server-calculated table or target-column statistics to be sent, without
including raw sampled values unless `--sample-rows` is also present. Either
option additionally requires `semantic:data`. The current percentage, maximum
rows, maximum serialized bytes, availability, and required scope are owned by
the server and advertised by `semantic status`; the CLI does not silently
increase those limits. On the current platform the sample caps are 100 rows,
96 KiB, 20 eligible table columns, and 512 characters per serialized value;
geometry and binary values are omitted. Field statistics use at most 1,000
rows selected from a 5% sample. The status response remains authoritative for
another deployment. The server never exposes its Gemini key or raw database
credentials. A field must belong to the selected asset and an archived or
hidden asset remains unavailable according to the caller's normal catalog
permissions.

The response binds `draft.assetId` and `draft.baseVersion`, identifies the
exact table/field target and Gemini model, and contains curated-only
`draft.operations`. `generation.metadataOnly` is true only when neither
optional context was selected; `generation.contextOptions` reports the exact
`sampleRows` and `statistics` booleans used. `generation.proposalCreated`
remains false. The CLI makes one generation
request and does not automatically retry provider failures. It never checks,
creates, or applies a proposal. Table and field drafts contain one to four
individual `set` operations for `displayName`, `description`, `tags`, and
`caveats`; values already identical to curated metadata are omitted. A fully
identical result returns `semantic.generation_no_change` instead of an empty
draft. Review the text, tags, caveats, exact JSON Pointer paths, reported
context options, and existing curated values before copying `draft.operations`
and `draft.explanation` into an explicit check:

In the dashboard, up to ten stable field IDs can be selected together. The
dashboard makes one field-scoped generation request for each selected ID and
submits those requests concurrently, reports completed/total progress, and
preserves selected field order in the combined non-overlapping operations. It
publishes no partial draft if any request fails and still creates no proposal
automatically. One CLI invocation targets one table or one field; the CLI does
not silently fan out a batch.

```sh
config-cli semantic proposals check \
  --asset-id ASSET_ID \
  --base-version DRAFT_BASE_VERSION \
  --input REVIEWED_OPERATIONS_JSON
```

`REVIEWED_OPERATIONS_JSON` is an object containing only the reviewed
`operations` array and optional `explanation`; do not pass the complete
generation response as proposal input. Continue with `proposals create` only
from the successful check fingerprint, then wait for separate approval before
apply. If the asset version changed, discard the draft and generate or inspect
again rather than silently rebasing it.

Users with `derive` cause the generated profile to be registered as part of
the managed derived-layer lifecycle, but that does not grant general semantic
editing. After investigating and correcting the delivery failure, a
`semantic:admin` caller may requeue a retained `repair_required` event:

```sh
config-cli semantic derived-profiles repair DERIVED_LAYER_NAME --confirm
```

The command requeues the exact retained event; it does not regenerate or edit
its payload. A deterministic validation or payload failure must be corrected
first, otherwise the same failure will recur.
For administrators, `derived-profiles list` also returns
`deliveryBlockers`. This includes retained archive failures whose derived
relation has already been dropped, so they have no ordinary derived-profile
row. Use the blocker `name` with the same repair command; a requeued archive
reports `pending_archive`.

Curated metadata changes use their own checked proposal domain:

```sh
config-cli semantic proposals check \
  --asset-id ASSET_ID \
  --base-version ASSET_VERSION \
  --set '/curated/description="Reviewed business meaning"' \
  --explanation 'Clarifies the meaning of this derived asset.'
config-cli semantic proposals create --from-check CHECK_FINGERPRINT
config-cli semantic proposals list
config-cli semantic proposals show SEMANTIC_PROPOSAL_ID
config-cli semantic proposals apply SEMANTIC_PROPOSAL_ID --confirm
config-cli semantic proposals decline SEMANTIC_PROPOSAL_ID \
  --reason 'Superseded' --confirm
```

To remove only reviewed semantic wording while keeping the generated profile
and database data, use a focused `--unset`, for example:

```sh
config-cli semantic proposals check \
  --asset-id ASSET_ID \
  --base-version ASSET_VERSION \
  --unset '/curated/description' \
  --explanation 'Removes only the saved table description.'
config-cli semantic proposals check \
  --asset-id ASSET_ID \
  --base-version ASSET_VERSION \
  --unset '/curated/fields/FIELD_ID' \
  --explanation 'Removes only this field annotation.'
```

Review and create only one intended check. A narrower field property can be
unset below `/curated/fields/FIELD_ID/...`. JSON Pointer-escape `/` as `~1`
and `~` as `~0` in an ID or property. These operations cannot remove generated
relation or column facts; a trusted source refresh owns those facts. Use the
administrator archive command only when the whole semantic profile should
leave normal discovery.

Only `/curated` JSON Pointers are accepted locally; generated bindings,
columns, types, and provenance remain lifecycle-owned. JSON input accepts one
to 100 closed operation objects: `set` has exactly `op`, `path`, and `value`;
`unset` has exactly `op` and `path`. Paths use strict RFC 6901 escaping, cannot
repeat, and cannot contain empty keys. A root `set` requires an object and the
root cannot be unset. Semantic and workspace check fingerprints are namespaced
and cannot be reused across domains. Creation and application require
`semantic:propose` and `semantic:apply` respectively. The original request is
not approval to apply. A timeout, HTTP `5xx`, or malformed/inconsistent
successful apply response is indeterminate: do not retry automatically;
inspect the proposal and asset version first.

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
Supplying longitude, latitude, and zoom together also bypasses database-wide
feature-count, extent, and representative-feature planning. The browser uses
the exact map centre, so hover or clicked-feature evidence still proves whether
the requested location contains the expected feature.

### `visual-test`

Run the server-side browser check for a layer:

```sh
config-cli visual-test --layer "Bus Stops"
config-cli visual-test --layer "Bus Stops" \
  --locale en-GB \
  --lng -1.55 --lat 53.81 --zoom 12.5
config-cli visual-test --layer "Bus Stops" \
  --artifact-dir ./visual-evidence
config-cli visual-test --layer "Bus Stops" \
  --hover --expect-hover-text "Stop name"
config-cli visual-test --layer "Bus Stops" \
  --expect-info-text "Data source: approved survey"
```

Run it after applying a proposal for every changed visual layer. A passing test
proves that XYZ loaded, the named layer was present, and a canvas rendered. It
does not guarantee cartographic quality; review the returned screenshots when
the change is visually significant.

`visual-test` and `screenshot` submit durable background operations and poll
them automatically. Use `--wait-timeout` and `--interval` to bound that local
wait. If it expires, the error retains the operation ID and the server work is
not cancelled; continue with `operations wait OPERATION_ID` instead of starting
a second Chromium run.

A browser-validation failure exits with code `6`, but its structured error can
still contain the selected plan, failed report, and authenticated artifact
paths from the server's HTTP 422 response. Preserve and review that evidence.
If the bounded browser runner is already full, the server returns HTTP 429 with
the plan; the CLI also exits with code `6`, and the read-only visual request
may be retried after the reported contention clears.

A database failure before the browser starts also exits with code `6`, but has
no report or artifacts. Inspect the preserved `code`, `planningStage`, and
`queryPurpose` fields: `visual.planning_timeout` identifies a timed-out
automatic planning query, while `visual.planning_database_error` identifies
another read-only planning failure. The server does not return raw SQL or
driver details. Retry with a complete `--lng`, `--lat`, and `--zoom` view when
the automatic feature-count/extent path is unnecessary.

Use `--artifact-dir` to fetch returned authenticated artifacts into a local
directory. The JSON response then includes `localArtifacts`, keyed like the
server's `visual.artifacts` object, so its report and screenshots can be opened
directly from the agent workspace. An explicit export requires a non-empty map
with at most 16 artifacts. Each artifact response is limited to 20 MiB and the
complete response to 64 MiB. Successful responses are fully downloaded and
validated before any artifact is written, so a size or transport failure
cannot leave a partial successful evidence set. These limits are part of the
client compatibility boundary; servers must stay within them or negotiate a
future contract revision.

Local artifact export is supported on POSIX hosts. Run the CLI under WSL on
Windows; native Windows operational execution is rejected before the server is
contacted because the supported Python filesystem APIs do not provide the
descriptor-relative traversal required by the client safety contract.

Configured hover is exercised automatically. `--hover` requires it,
`--no-hover` suppresses it, and repeated `--expect-hover-text` values assert
tooltip content. Repeated `--expect-info-text` values assert captured
clicked-feature information. A requested interaction counts as evidence only
when its dedicated report fields pass and its cropped artifact is present.

A passing test checks HTTP success, canvas presence, the requested layer, and
browser errors. It does not prove exact colour appearance, emoji/custom-font
glyph fidelity, or general cartographic quality. Hover and clicked-feature
content are evidence only when their dedicated checks and cropped artifacts
pass. Grouped layers may render and interact successfully while
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
config-cli proposals preview-test PROPOSAL_ID --layer "Bus Stops" \
  --hover --expect-hover-text "Stop name"
config-cli proposals preview-screenshot PROPOSAL_ID --layer "Bus Stops" \
  --expect-info-text "Data source: approved survey"
```

All three accept the same optional `--locale`, `--lng`, `--lat`, `--zoom`, and
`--artifact-dir` controls as the top-level visual commands. The CLI requires
the response to report `source: candidate`, the requested proposal ID, and a
non-empty candidate hash. A mismatched or live-workspace response is rejected.
The server contract must also advertise the exact command name required by the
invocation. A related capability action or matching route is insufficient; do
not bypass a `capability.missing` result.

`preview-test` and `preview-screenshot` use the same durable polling behavior
and accept `--wait-timeout` and `--interval`. `preview-plan` remains synchronous
because it does not start Chromium.

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
The CLI additionally requires each requested panel's `passed` record and
dedicated before/after artifact, so an overall page screenshot cannot be
mistaken for panel evidence.

Configured hover is exercised automatically. `--hover` makes it mandatory,
`--no-hover` suppresses the automatic check, and repeated
`--expect-hover-text` values require tooltip content. Hover counts as evidence
only when it was requested, attempted, opened, passed, and produced the
dedicated tooltip artifact; the runner's skipped-hover state is not evidence.

Use repeated `--expect-info-text` values for clicked-feature labels or a static
source note. For a changed `infoj`, the server captures the applicable
original/candidate side independently—including candidate-only capture for a
new layer—and requires the expected text and cropped information-panel
artifact. The CLI exits with code `6` and identifies `missingEvidence` if a
nominally successful response omits any requested proof.

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
