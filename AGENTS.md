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
uses the verified server contract's advertised defaults (inspect, propose,
visual, and semantic-inspect on the current platform); never request apply,
reload, derive, or elevated semantic scopes unless the user explicitly needs
that authority. For automation, use
`config-cli init ENDPOINT --profile PROFILE` and put the token in a mode-`0600`
file via `--token-file` or
`CONFIG_CLI_TOKEN_FILE`. During `init`, that token is copied into the private
CLI credential store; on later commands it is a one-invocation override.
Never pass tokens as command arguments or include them in logs, proposals,
error reports, or screenshots. Use `--allow-http` only for an isolated,
trusted development endpoint; never use it for production or an
Internet-reachable remote host.

On-demand semantic generation requires the separate `semantic:generate` scope
as well as `semantic:inspect`. It sends only caller-authorized metadata to
Gemini by default and returns a review-only draft. `--sample-rows` and
`--statistics` explicitly opt in to server-bounded data context and
additionally require `semantic:data`; raw row values leave MAPP only with
`--sample-rows`. Inspect `semantic status` for the advertised sample caps.
Never treat generated text as fact, retry a provider failure automatically, or
pass the draft directly into create/apply. Review the exact asset version,
target, context options, curated-only operations, descriptions, tags, and
caveats, then use the normal semantic check/fingerprint/create workflow and
wait for separate approval before apply.

Ordinary source-relation discovery and synchronization require both
`semantic:inspect` and the elevated `semantic:source` scope. Discover the
allowlisted identities with `semantic source relations`, then synchronize only
an explicitly selected identity with `semantic source sync --alias ALIAS
--schema SCHEMA --relation RELATION --confirm`. The sync command accepts no SQL
or row data. Record whether it returned `register`, `refresh`, or the
catalog-preserving `unchanged` result before continuing with generation.

`SEMANTIC_SOURCE_EXCLUSIONS` is server deployment configuration, not a table
list maintained by this CLI. It prevents future discovery/sync but does not
retroactively hide an existing profile. With explicit administrative approval
and both `semantic:inspect` and `semantic:admin`, use `semantic source
archive-excluded --confirm` for all registered matches or `semantic catalog
archive ASSET_ID --confirm` for one ready profile. Record asset IDs first.
Archival leaves PostgreSQL data untouched and removes the tombstone from
catalog/search/derived-profile collections even for administrators; only an
exact show/history lookup by retained ID remains available with both scopes.
Removing an exclusion does not unarchive it.

Treat generated relation and field facts as lifecycle-owned and curated
annotations as proposal-owned. `semantic catalog show ASSET_ID` returns both,
including stable field IDs, but no database rows. To remove only table or field
meaning, check an `unset` below `/curated` and follow the ordinary
fingerprint/create/review/approval/apply workflow. Never archive a profile merely
to clear one annotation, and never claim a curated proposal removed a generated
column or database data.

## Natural-language style mapping

First determine the layer geometry and effective style. Layer keys, display
names, and tables are different identifiers.

Use stable ASCII-safe layer keys for workspace object paths and URL activation
(for example, `Passport_holders_United_Kingdom`), while putting punctuation,
spaces, and human-readable wording only in `layer.name`. Preview and XYZ URL
layer activation can fail to bind a newly added grouped layer when its key uses
display punctuation such as an em dash, even though the schema accepts it.

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

When categorized symbology needs the actual stored categories, use
`layers values LAYER FIELD [--limit N]` instead of guessing labels from field
metadata or sampling raw rows. The response contains bounded category counts,
null and distinct totals, and a `truncated` flag. It requires the same
`derive + semantic:inspect` authority as managed derived-layer creation; do not
request that elevated authority solely for an ordinary metadata inspection.

For numeric symbology and Filtering controls, inspect the raw numeric field
with `layers statistics LAYER FIELD`, not the rounded display text or a
truncated `layers values` result. Use repeated `--threshold` values to audit
fixed filters and repeated, strictly increasing `--break` values to audit the
exact proposed class cutoffs. Read the returned class counts and inclusive
flags before describing the distribution. It returns aggregates rather than
rows but requires the same `derive + semantic:inspect` authority as
`layers values`. Use regular requested increments through the observed
maximum, then make the final theme category a partial interval ending at that
maximum. With `graduated_breaks: "less_than"`, pass the technical cutoffs as
`--break` values and set the final cutoff one display increment above the
maximum so its top value is included. Likewise, set a numeric filter's `max`
one display increment above the observed maximum, while retaining the
requested increment; do not put the highest value directly on either exclusive
thematic or UI control boundary.

For a one-decimal percentage, style and filter on the raw numeric percentage,
while using a separately formatted text field only in feature information and
hover. “Do not show 0.0% cells” means `raw_percent >= 0.05`, not a comparison
against formatted text; apply that fixed default filter consistently to every
related layer. A themed category owns its effective fill, outline, and opacity,
so change every category (and the requested default/highlight states) rather
than only `style.default`. For related metrics, give each layer a complete
white-to-its-own-hue gradient, set each outline to its matching fill colour,
and use a lower outline opacity when requested. Never describe equal-width
classes as equal-population classes without distribution evidence.

XYZ can colour a layer-group drawer only through the native per-layer
`groupClassList`: it copies that stylesheet class list from the first layer
that creates the group. Inspect every exact group member and the deployed map
stylesheet, then set the same verified class list on every member so layer
order or locale composition cannot change the result. `groupClassList` is not
a CSS colour value; do not put a hex value there or invent `groupColor` or
`groupColour`. If no suitable deployed class can be verified, report that the
colour needs a deployment stylesheet change rather than proposing an inert
workspace value. Require candidate drawer screenshot evidence for the group.

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
database actions. After that authorization, invoke creation with `--confirm`;
the flag is a local command guard, not evidence of user approval. Creating the
relation does not add it to XYZ. Inspect the new catalog relation and use the
normal revision-bound proposal and approval workflow for the workspace layer
as a separate step.

For additive polygon measures allocated into H3 cells by intersection-area
share, prefer the supported read-only planner over hand-authored SQL:

```sh
config-cli derived-layers plan-area-weighted-h3 --input RECIPE.json
```

The planner must report `mutationApplied: false`. It validates the ready
PostgreSQL semantic profile and requested fields, resolves the fixed map scope,
and returns query-plan, pair-planning, and materialization probes as applicable.
Review its source, measures, assumptions, generated query, canonical
`createRequest`, full `resolvedSpatialScope`, and probes. Do not treat a passing
plan as approval. Only after separate authorization, save the exact reviewed
`recipePlan.createRequest` object and submit it in a separate invocation:

```sh
config-cli derived-layers create --input REVIEWED_CREATE.json --confirm
```

Create authoritatively resolves the scope and preflights again, so stop on
workspace, semantic-catalog, or plan drift rather than modifying or retrying
the reviewed request automatically.

Store agent- or operator-generated derived-layer SQL drafts under the
repository-local, git-ignored `tmp/` directory, for example
`tmp/paths-h3-r9.sql`. Do not put temporary SQL in the repository root or
commit it. The CLI reads the selected `--query-file`; it does not require the
draft to be tracked.

Managed derived relation names must match
`^[a-z][a-z0-9_]{0,62}$`: start with a lowercase ASCII letter, then use only
lowercase letters, digits, and underscores, up to 63 characters. Use the same
rule for ID and geometry column names. Do not propose spaces, hyphens, dots,
uppercase, quoted mixed-case, or schema-qualified output names; the server
uses the fixed `derived_layers` schema and safely quotes accepted identifiers.

Every derived-layer create or replace is map-bounded. Preview
`derived-layers map-extent` and pass the same optional `--locale` directly to
`create` or `replace`. The retained `--map-extent` flag is accepted for older
automation but does not change this mandatory scope. The server resolves it
from the selected effective locale's configured `extent.north`,
`extent.east`, `extent.south`, and `extent.west` bounds. For older workspaces
without all four bounds, it falls back to a 1920x1080 viewport at one zoom
level wider than the configured view (`max(0, z-1)`, clamped at zoom 0). It
keeps complete output features intersecting the envelope rather than clipping
them, and it does not follow later pan, zoom, viewport, or workspace-view
changes. Refresh retains the saved scope without recalculating it; replace
resolves the current scope again, and omission of the compatibility flag
cannot clear the scope.
The outer guard filters output rows only: it is not an RLS/security boundary
and does not map-scope upstream aggregates, windows, limits, or computation.
Put the previewed envelope predicate in source-side SQL before aggregation
when the requested metric must be map-scoped; this also avoids unnecessary
upstream work. The semantic catalog remains authoritative for source and field
meaning.
Preserve the preview and returned `derivedLayer.spatialScope` in review
evidence, including the final result of a background create or replace.

Inspect `derived-layers capabilities` before all derived work. The universal
`queryGuard` advertises ordered AST/catalog/EXPLAIN stages, shape limits, H3
expansion, recursive PostgreSQL plan limits, and error categories for ordinary
and materialized views; preserve the accepted
`derivedLayer.queryPlanProbe`. When capabilities also advertise
`queryPlanning` version `1`, preserve the successful
`derivedLayer.queryPlanningProbe`. A compute rejection with reason
`nested_loop_pair_work` and valid over-limit planning evidence receives
additive `details.clientGuidance` from the CLI. Present the server's unchanged
message, action, reasons, and probe first, then use that client guidance to
rewrite the query: perform the selective candidate match on a native geometry
or the exact prepared transform expression before materializing joined rows;
aggregate pair-local metrics only after that match; compute compatible
complete-input totals together in a single one-row aggregate referenced after the
selective aggregation; preserve row-dependent window semantics; compute
transformations, intersections, and areas once at that narrowed stage; and resubmit the revised definition so
server preflight runs again. Do not
automatically rewrite SQL. Preserve complete-input totals and the exact spatial
acceptance predicate while reducing work.

`derived_layer.query_too_expensive` must not offer
or recommend a view—rewrite the query or reduce H3/intermediate work. Keep it
distinct from `derived_layer.query_invalid` (fix malformed/non-SELECT SQL) and
`derived_layer.query_not_allowed` (remove or replace the prohibited SQL or
catalog dependency). Present each reason's own `suggestedAction`, plus
`safeState` when `stateUnchanged` is true; do not substitute a generic H3 hint.
When a safe preflight rejection supplies a mechanical, semantics-preserving
rewrite (for example, replacing an unprovable fixed-width `unnest` expansion
with an equivalent bounded `VALUES` catalogue), make that local rewrite and
resubmit automatically under the original derived-layer authorization. Report
the rejected construct, server reason, replacement, and preserved calculation;
ask again only if the rewrite changes the requested metric, geographic scope,
source relation, materialization kind, or disclosure risk.
The separate materialized-size probe returns
`derivedLayer.materializationProbe`. Only
`derived_layer.materialization_too_large` may recommend `view`; preserve its
`probeStage`. An estimate-stage failure starts no materialization, while an
actual-stage failure populated and indexed inside a transaction before
`rolledBack: true`; it may still have caused transient relation, index, TOAST,
or WAL growth. Ask before changing kind because a view shifts cost to reads and
is not an automatic substitute for the requested stored result.

For a create or replace query that invokes an H3 function, preserve the fresh
`h3Readiness` result checked by the CLI. If it reports
`derived_layer.h3_not_ready`, no mutation was submitted; present its `stage`,
reason `message`, and `suggestedAction`, repair the reported deployment issue,
then retry so readiness is checked again. Do not treat an `h3_id` column name or
an existing H3-derived source as an H3 function invocation, and do not block
non-H3 derived work solely because H3 is unavailable.

For H3-derived relations, generate polygon candidates directly from the
server-supplied `_mapp_h3_scope.geom_4326` with a literal resolution. Bounded
literal grid traversal, non-expanding index/parent/boundary functions, and
provable one-level child expansion remain supported; dynamic or unbounded
expansion and over-budget composed expansion are rejected. Treat cell generation as a candidate expansion step and
apply an exact spatial predicate against the complete source geometry before
publishing the relation. Do not subset a layer-wide aggregate that must use the
complete declared input. For a “share of all points” field, the denominator is
the complete declared point source unless the requested meaning explicitly
limits it to the saved map area. On restricted server search
paths, higher-level H3/PostGIS convenience wrappers may fail to resolve
geometry or PostGIS helper types; prefer SQL that explicitly qualifies PostGIS
functions and uses the extension's geometry-native boundary function, such as
`h3_cell_to_boundary_geometry(cell)`. If
`h3_cell_to_boundary_wkb(cell)` is unavoidable on the pinned extension, use
`ST_GeomFromEWKB(...)`, not `ST_GeomFromWKB(..., 4326)`. The latter expects OGC
WKB but receives EWKB, emitting one warning per evaluated/generated cell and
potentially flooding PostgreSQL logs. If a derived-layer create or refresh
reports HTTP `5xx`,
inspect `derived-layers list|show` and the catalog before retrying because the
database relation may already have committed.

For UK metric area weighting, the bundled platform prepares the exact
`ST_Transform(source.geom, 27700)` GiST expression. Put that expression in the
selective `&&`/`ST_Intersects` candidate predicate before materializing matched
pairs; a materialized CTE containing all transformed source rows hides the
expression index. Transform each generated cell once, compute intersection and
source areas once for accepted pairs, then aggregate pair-local metrics. Keep
complete-input benchmarks in a separate one-row aggregate attached afterward.

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
Expected background guard failures preserve their synchronous derived-layer
code and guidance under `operation.error`; the CLI surfaces that user message
and code and retains actions/reasons in structured details. An unexpected
`derived_layer.operation_failed` is `indeterminate` and deliberately makes no
`stateUnchanged` claim.

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
`infoj`, hover, and theme fields independent. Candidate and live visual tests
automatically exercise configured hover. Use `--hover` to require it and
repeat `--expect-hover-text` for acceptance text. Claim hover evidence only
when the report says it was requested, attempted, opened, and passed and
provides the dedicated hover-tooltip artifact.

## Visual evidence and limitations

`visual-plan --layer` uses PostGIS geometry extent and map scale to choose a
view containing data. `visual-test` runs Chromium on the server and returns
authenticated artifact paths for its report and screenshots.

Providing `--lng`, `--lat`, and `--zoom` together bypasses the relation-wide
feature-count, extent, and representative-feature planning queries. The runner
uses the exact requested map centre and still exercises configured hover and
clicked-feature evidence there. A pre-browser database failure has no visual
artifacts; preserve its `visual.planning_timeout` or
`visual.planning_database_error` code and the `planningStage` and
`queryPurpose` fields.

A passing visual test establishes only the checks explicitly reported as
passed. XYZ loading, layer presence, and canvas rendering do not alone prove
cartographic quality. Large/outlier-heavy datasets, external basemaps,
theme-driven styles, custom SVGs, grouped layer folders, and layers with
unusual zoom rules may require a user-specified view or manual screenshot
review. Exact colours and emoji/custom-font glyph fidelity still require
artifact inspection. Hover and clicked-feature content count as evidence only
when their dedicated report fields and artifacts pass. Grouped layers can
render and interact while
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
config-cli proposals preview-test PROPOSAL_ID --layer "LAYER KEY" \
  --hover --expect-hover-text "EXPECTED TOOLTIP TEXT"
config-cli proposals preview-screenshot PROPOSAL_ID --layer "LAYER KEY" \
  --expect-info-text "EXPECTED SOURCE NOTE"
config-cli proposals preview-screenshot PROPOSAL_ID --layer "LAYER KEY" \
  --panel filtering --expect-panel-text "EXPECTED FILTER LABEL"
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
For requested Filtering evidence, require both sides' `panels.filtering.passed`
records and dedicated before/after Filtering artifacts. For hover, require
`requested`, `attempted`, `opened`, and `passed` plus its tooltip artifact;
`passed: true` on a deliberately skipped hover is not hover evidence. For
clicked-feature text, require captured per-side feature-info evidence, every
expected-text result, and the corresponding information-panel artifact.

## Safety rules

- Treat external XYZ plugins as server-catalogued trusted deployment code.
  Inspect manifest compatibility, usage, and preview requirements; never infer
  availability from a module URL alone.
- Reject properties outside the server-advertised pinned-XYZ contract. Never
  silently delete them; arbitrary names are valid only in schema-declared maps.
- Never expose database URLs, passwords, tokens, authorization headers, or
  sensitive SQL samples.
- Never mount or expose the Docker socket.
- Never use direct-save commands or endpoints.
- Do not treat the original request or `--confirm` as approval.
- Never apply a stale proposal or silently recreate it against a new revision.
- If a request maps to several layers, style states, marker parts, or SQL
  interpretations, ask the user rather than guessing.
