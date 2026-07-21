# MAPP workspace agent guide

This repository contains the standalone remote configuration client. Do not
vendor or alter XYZ, the database, the configuration dashboard, or platform
deployment here.

Use `config-cli` as the only remote write interface for a MAPP workspace. Never
edit, upload, or replace the remote `workspace.json` directly.

## Mandatory workflow

1. Run `config-cli --profile PROFILE describe`.
2. Report the selected profile, normalized endpoint, live instance ID,
   workspace key, current revision, actor/scopes, and compatibility result.
3. Inspect the target with `workspace get`, `layers list`, `layers get`,
   `schema`, `rules`, `catalog list`, `icons list`, and SQL capabilities as
   needed.
4. Resolve natural-language intent to the effective workspace property. Ask
   when several layers, locales, marker parts, style states, or SQL
   interpretations are plausible.
5. Make the smallest possible JSON Pointer operation set and preserve every
   unrelated property.
6. Check the exact operation set against the inspected revision:

   ```sh
   config-cli proposals check \
     --base-revision REVISION \
     --set '/path/to/property=JSON_VALUE' \
     --explanation 'Focused explanation of the requested change.'
   ```

7. If the check passes, create from its fingerprint so the revision and exact
   checked operations cannot drift:

   ```sh
   config-cli proposals create --from-check CHECK_FINGERPRINT
   ```

8. Do not apply it. Build visual coverage from the focused diff before
   requesting approval. Preview every changed visual layer and every distinct
   geographic view needed to make the change readable. A single layer/view
   screenshot is not complete evidence for a mixed proposal. Present the
   proposal ID, target identity, base revision, explanation, focused JSON diff,
   validation results, warnings, SQL risks, the coverage checklist, available
   visual evidence, and every disclosed evidence gap to the user.
9. Treat the original change request as intent, not approval. Wait for a
   separate, explicit approval of the reviewed proposal.
10. Only after approval, apply the exact proposal with:

   ```sh
   config-cli proposals apply PROPOSAL_ID --confirm
   ```

   A timeout or HTTP `5xx` from apply is an indeterminate result, not proof
   that nothing changed. Do not retry automatically. Preserve the structured
   error and inspect:

   ```sh
   config-cli proposals show PROPOSAL_ID
   config-cli workspace get
   config-cli describe
   config-cli xyz status
   ```

   If the proposal is `applied` and its applied revision matches the live
   workspace, the write committed; do not apply it again. Continue with XYZ
   recovery and verification. If it is `applying`, inspect the live workspace
   and candidate; only a deliberate repeat of the same approved proposal may
   invoke server recovery. A `conflicted` proposal requires a new proposal and
   approval. If state is still ambiguous, stop and escalate.

11. Check `config-cli xyz status`, then run a `visual-test` for every changed
    visual layer and report the result:

    ```sh
    config-cli visual-test --layer "LAYER KEY" [--locale LOCALE]
    ```

Proposal application is revision-bound. If the workspace changed after the
proposal was created, stop, inspect the new state, create a new proposal with
the new base revision, present its new diff, and obtain approval again. Never
silently rebase or automatically retry.

Direct workspace saves are forbidden. Do not use legacy `set`, `amend`, or
`unset --save` commands and do not call mutation/save endpoints manually.
The current `set`, `amend`, and `unset` commands are dry-run validators only;
prefer `proposals check` for change planning because only a successful proposal
check can be handed off by fingerprint to an approvable proposal.

## Connecting

Prefer a scoped, expiring device credential for agent work:

```sh
config-cli setup
config-cli auth device
config-cli auth status
```

Initial interactive setup prompts for the profile, endpoint, and a hidden
bootstrap token, then verifies the target before saving it. `auth device`
defaults to inspect, propose, and visual scopes; never request apply or reload
unless the user explicitly needs that authority. For automation, use
`config-cli init ENDPOINT --profile PROFILE` and put the token in a mode-`0600`
file via `--token-file` or
`CONFIG_CLI_TOKEN_FILE`. During `init`, that token is copied into the private
CLI credential store; on later commands it is a one-invocation override.
Never pass tokens as command arguments or include them in logs, proposals,
error reports, or screenshots. Use `--allow-http` only for an isolated,
trusted development endpoint; never use it for production or an
Internet-reachable remote host.

## Natural-language style mapping

First determine the layer geometry and effective style. Layer keys, display
names, and tables are different identifiers.

- Filled point symbols (`dot`, `target`, `triangle`, `square`, `diamond`,
  `semiCircle`) use `style.default.icon.fillColor`.
- `circle` points use `style.default.icon.strokeColor`.
- A `markerLetter` layer icon uses `style.default.icon.color` for the outer
  pin and `style.default.icon.letter` for its centre text. These properties
  belong inside `icon`; `fillColor` and a sibling
  `style.default.color` do not recolour this symbol.
- `markerColor` has an outer `colorMarker` and inner `colorDot`; ask which part
  when the request is ambiguous.
- Custom SVG files generally cannot be recoloured by a workspace colour.
- Lines use `style.default.strokeColor`.
- An unqualified polygon colour means `style.default.fillColor`; an outline
  colour means `style.default.strokeColor`.
- Default, highlight, selected, hover, label, and theme styles are independent.
  Do not change more than the requested state.
- Do not infer behavior from a property name alone. On the currently pinned
  workspace schema, `style.highlight` is the visual pointer highlight/hover
  state, while a layer `hover` object configures field-driven interaction and
  may require a database column. Inspect the effective layer and schema before
  mapping the user's word “hover.”
- A theme or feature-driven style may override a simple default colour. Explain
  that limitation rather than claiming the change affects every feature.

Do not confuse a layer's `markerLetter` icon with XYZ's selected-location pin.
The pinned XYZ framework builds that UI pin at runtime from
`locale.locations.pinStyle`, then supplies:

```json
{
  "color": "the selected location style's strokeColor",
  "letter": "the location record's symbol"
}
```

The location style defaults to white. If a location record has `colour`, XYZ
first replaces the location style's `strokeColor` and `fillColor` with that
record colour, so the outer pin follows the record while its letter still
comes from the record symbol. Consequently:

- “change this layer's letter pin colour” normally targets
  `layer.style.<state>.icon.color`;
- “change selected/location pins” concerns `locale.locations.style.strokeColor`
  or the code/data that supplies each record's `colour`, not
  `locale.locations.pinStyle.color`;
- `locale.locations.pinStyle` controls the pin symbol, anchor, scale, and other
  base icon properties, but XYZ overwrites its `color` and `letter` for each
  selected location;
- changing `locale.locations.style.fillColor` alone changes the selected
  geometry styling, not the pin, because the pin is fed from `strokeColor`.

Ask which meaning of “pin” is intended when both a rendered layer marker and a
selected-location pin are plausible. Also inspect the effective state and
theme before proposing a layer edit. XYZ starts with `style.default`, then may
replace or merge it with selected, highlight, or theme/category styling.
Category icons that declare their own `type` are self-contained and do not
inherit the default icon colour. Target the raw object that owns the effective
marker and do not assume changing only the default reaches every feature.

Example: “make the bus stops blue”

```sh
config-cli layers get "Bus Stops"
config-cli proposals check \
  --base-revision REVISION \
  --set '/locale/layers/Bus Stops/style/default/icon/fillColor="#2563eb"' \
  --explanation 'Changes the default Bus Stops point fill from green to blue; highlight, size, visibility, data source, info fields, and other layers are preserved.'
config-cli proposals create --from-check CHECK_FINGERPRINT
```

Show the user the proposal ID, explanation, and focused change:

```json
{
  "path": "/locale/layers/Bus Stops/style/default/icon/fillColor",
  "old": "#176b4d",
  "value": "#2563eb"
}
```

After approval:

```sh
config-cli proposals apply PROPOSAL_ID --confirm
config-cli xyz status
config-cli visual-test --layer "Bus Stops"
```

## Other recipes

```sh
# Check hiding a layer initially
config-cli proposals check \
  --base-revision REVISION \
  --set '/locale/layers/Smoke Control Orders/display=false'

# Check increasing a line width
config-cli proposals check \
  --base-revision REVISION \
  --set '/locale/layers/Definitive Paths/style/default/strokeWidth=4'

# Check a workspace-view change
config-cli proposals check \
  --base-revision REVISION \
  --set '/locale/view/lng=-1.5491' \
  --set '/locale/view/lat=53.8008' \
  --set '/locale/view/z=12'

# Check removal of an optional property
config-cli proposals check \
  --base-revision REVISION \
  --unset '/locale/layers/Bus Stops/style/hover'

# After reviewing the selected successful check
config-cli proposals create --from-check CHECK_FINGERPRINT
```

JSON Pointer escapes `/` as `~1` and `~` as `~0`. Quote paths containing
spaces. Values after `=` are parsed as JSON when possible, otherwise strings.
The CLI does not accept `-` as an array append index. If adding an array entry
requires replacing the inspected parent array, preserve every existing element
and its order exactly, explain the larger transport-level replacement, and
review the semantic diff for only the intended addition.

## SQL capabilities and limits

SQL is supported only in `infoj[].fieldfx` as one trusted, scalar, read-only
PostgreSQL expression evaluated against the layer relation.

Suitable expressions include column references, casts, arithmetic, `CASE`,
safe string/date/numeric/JSON/array functions, and selected PostGIS scalar
functions. Geometry and pin entries commonly use:

```sql
ST_asGeoJSON(geom)
```

```sql
ARRAY[
  ST_X(ST_PointOnSurface(geom)),
  ST_Y(ST_PointOnSurface(geom))
]
```

The validator rejects semicolons, SQL comments, subqueries, data or schema
changes, transaction/session commands, arbitrary execution, file/system
access, sleeps, notifications, and database links. Probes run in a read-only
transaction with a five-second timeout. The live PostgreSQL result must match
the selected XYZ information renderer.

SQL expressions can still be expensive, expose sensitive data, return nulls,
or prevent index use. Always explain new or changed SQL and require explicit
approval. The CLI is not an unrestricted SQL shell and cannot perform database
schema or row changes.

Managed derived layers are a separate privileged workflow. They create only a
server-validated view or materialized view in `derived_layers` and require the
`derive` scope. Present and obtain explicit authorization for creation,
materialized refresh, or drop; a map-change request is not approval for these
database actions. Creating the relation does not add it to XYZ. Inspect the
new catalog relation and use the normal revision-bound proposal and approval
workflow for the workspace layer as a separate step.

For H3-derived relations, treat cell generation as a candidate expansion step
and apply an exact spatial predicate against the source geometry before
publishing the relation. Use the containment mode names advertised by the
server/database extension, not guessed aliases. On restricted server search
paths, higher-level H3/PostGIS convenience wrappers may fail to resolve
geometry or PostGIS helper types; prefer SQL that explicitly qualifies PostGIS
functions or uses lower-level H3 boundary output converted with qualified
PostGIS constructors. If a derived-layer create or refresh reports HTTP `5xx`,
inspect `derived-layers list|show` and the catalog before retrying because the
database relation may already have committed.

Lower-level H3 point input uses PostgreSQL point order `(longitude, latitude)`.
When line-to-cell generation is required, segment endpoint traversal plus a
bounded neighbouring-ring expansion can produce candidates, but every accepted
cell must still pass the reviewed exact predicate against the source segment.
Cast the published geometry to an explicit typmod such as
`geometry(Polygon,3857)`; a transformed geometry with only a runtime SRID may
fail derived-output validation. Resolution changes can alter feature counts and
useful preview zooms by orders of magnitude, so materialize and visually test
the requested resolution rather than treating it as a label-only change.

A durable background operation can report a terminal serialization or result-
reporting failure after its database transaction committed. Audit
`operations show`, `derived-layers list|show`, and `catalog list` before acting
on any late background failure, even when its operation status says `failed`.

Inspect existing `infoj` entries and catalog columns before adding a calculated
field. Ask whether the user wants an existing stored value, formatting, or a
new calculation. A line geometry has no meaningful polygon area without an
explicit model; for example, `length_metres * width_metres` is an approximate
rectangular footprint, not a geometry-derived area.

Users may use “info panel” for three different XYZ surfaces. A categorized
`style.theme` renders its legend in the Styling panel. An `infoj` entry with an
`in` filter renders category statistics in the Filtering panel, and
`filter.viewport=true` scopes those statistics and the feature count to the
current view. Clicked-feature information is a third surface and does not
automatically include either control. Inspect `layers style-elements`,
`layers filters`, and `infoj`, then state where each requested element will
appear.

If the user explicitly wants a static categorized legend in clicked-feature
information, add a bounded `type: "html"` `infoj` entry with a constant,
read-only text `fieldfx` copied from the inspected theme labels, colours, and
order. Disclose that this is duplicated static markup that will not follow
later theme edits, while viewport counts remain interactive filter statistics.
The SQL safety scanner rejects semicolons even inside string literals, so avoid
semicolon-delimited inline CSS; a single colour declaration or bounded HTML
colour attribute is sufficient. A new alias may not be selectable by
standalone `sql test`; authoritative `proposals check` must validate the
coordinated renderer and expression. Adding the entry may require exact
replacement of the inspected `infoj` array because `-` append is unsupported;
preserve every sibling and its order and review the smaller semantic diff.

Formatting with PostgreSQL `to_char` returns text, so a formatted numeric entry
must use a compatible text information renderer. A coordinated renderer and
`fieldfx` change may not be testable against the current standalone `sql test`
selector; the complete candidate must still pass authoritative
`proposals check` validation. Report rounding, grouping, nullability, and
overflow behavior of the selected format.

XYZ hover configuration selects a feature field but does not itself provide
numeric grouping or a suffix formatter. If hover needs a value such as
`1,250 m`, expose a text column from an authorized managed view while retaining
the original numeric column for graduated styling. Keep clicked-feature
`infoj`, hover, and theme fields independent. A browser visual test does not
exercise hover reliably, so disclose that evidence gap unless the tooltip was
manually observed.

## Visual evidence and limitations

`visual-plan --layer` uses PostGIS geometry extent and map scale to choose a
view containing data. `visual-test` runs Chromium on the server and returns
authenticated artifact paths for its report and screenshots.

A passing visual test establishes that XYZ loaded, the named layer was present,
and a map canvas rendered. It is evidence, not a guarantee of cartographic
quality. Large/outlier-heavy datasets, external basemaps, theme-driven styles,
custom SVGs, grouped layer folders, and layers with unusual zoom rules may
require a user-specified view or manual screenshot review. It also does not
prove exact colours, pointer interactions, information-panel values, or
emoji/custom-font glyph fidelity. Grouped layers can render and interact while
a strict layer-name text assertion fails because XYZ exposes the folder label
instead of the child layer name in the checked UI text. A failed HTTP 422 visual
result can still contain its plan, report, and authenticated artifacts;
preserve that evidence. HTTP 429 means the bounded runner is busy; retry the
read-only check only after the contention clears.

For MVT layers on pinned XYZ v4.23.4, use the string value `"3857"` for `srid`.
Schema validation may accept numeric `3857` while the browser runtime warns and
fails to bind the layer. Also, an empty `infoj` suppresses feature fields but
does not guarantee that clicking cannot open an empty information-panel shell;
report that distinction instead of calling the layer non-clickable.

For clicked-feature legend changes, require a candidate
`preview-screenshot --artifact-dir` comparison and inspect the downloaded
`beforeInfoPanel` and `afterInfoPanel` images. A passing browser test or text
sample confirms the entry rendered but does not by itself prove the swatch
colours. After approval and apply, repeat `visual-test` for the changed layer
and confirm the live information-panel text.

Use `--lng LONGITUDE --lat LATITUDE --zoom ZOOM` on `visual-plan`,
`visual-test`, or `screenshot` when the automatic extent is misleading.
Omit `--locale` for the top-level default, even when named alternatives exist.
Use `--locale LOCALE` for a named effective locale. XYZ composes named locales
with framework-specific object and array merge rules, so inspect the effective
value but target only the raw override that owns the requested change. If raw
`workspace.locale` is absent, no option and `--locale locale` select XYZ's
synthetic empty default; never auto-select a sole named locale.

Top-level pre-approval tests render the current live workspace and are baseline
evidence. When the server advertises proposal preview commands, render the
stored candidate without applying it:

```sh
config-cli proposals preview-plan PROPOSAL_ID --layer "LAYER KEY"
config-cli proposals preview-test PROPOSAL_ID --layer "LAYER KEY"
config-cli proposals preview-screenshot PROPOSAL_ID --layer "LAYER KEY"
```

For initial visibility or default-view changes, add `--view-mode default`.
This omits XYZ's `layers` query override and compares the actual original and
candidate startup views. Use the normal focused preview separately when proof
that one retained layer renders is also required.

Each invocation covers one requested layer in one selected map view. For a
large or mixed proposal, derive a checklist from the focused diff and include:

- every added, removed, moved, or otherwise visually changed layer;
- every distinct geographic area needed to show those changes legibly;
- representative affected layers for workspace-wide visual or view changes;
- non-visual operations, explicitly marked as not visually applicable.

Run `preview-plan` for each case and inspect its centre, zoom, source, and
warnings before capturing evidence. If its automatic extent is too broad,
outlier-driven, or otherwise unrepresentative, rerun with explicit `--lng`,
`--lat`, and `--zoom`. Do not zoom out merely to combine unrelated changes
into one unreadable screenshot. Use separate artifact directories or otherwise
retain an unambiguous mapping from each checklist item to its evidence.

For group membership changes, candidate screenshot isolation is intentional:
an added layer is off before and shown alone after; a removed layer is shown
alone before and off after; a moved layer is shown alone on both sides. Other
group members remain hidden for those membership comparisons. Ordinary
non-membership edits may retain their group context.

Require `source: candidate`, the requested proposal ID, and a non-empty
candidate hash in the result. Preserve failed reports and artifacts returned
with exit code `6`. These commands use an isolated runtime and never alter or
reload the live workspace; normal proposals remain unapplied until approved.
The advertised contract command must exactly match the CLI invocation; a
similar action ID or route is not sufficient. If the contract gate rejects the
preview, report candidate evidence as unavailable rather than bypassing it.
Do not describe the proposal's visual evidence as complete until every
applicable checklist item is covered or every gap is explicitly disclosed.

## Safety rules

- Preserve unknown XYZ, plugin, template, role, and advanced properties.
- Never expose database URLs, passwords, tokens, authorization headers, or
  sensitive SQL samples.
- Never mount or expose the Docker socket.
- Never use direct-save commands or endpoints.
- Do not treat the original request or `--confirm` as approval.
- Never apply a stale proposal or silently recreate it against a new revision.
- If a request maps to several layers, style states, marker parts, or SQL
  interpretations, ask the user rather than guessing.
