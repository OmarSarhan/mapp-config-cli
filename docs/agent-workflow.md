# Agent workflow

This workflow is mandatory when an AI agent uses `config-cli` to change a
remote MAPP workspace. The goal is to keep target selection, intent
resolution, approval, application, and verification as separate auditable
steps.

![Revision-bound proposal flow](images/proposal-flow.png)

The diagram summarizes the mandatory path below. Its preview step means
proposal-bound rendering of the pending candidate in isolated XYZ before
approval; its final verification step tests the live map after application.

## 1. Identify the target

Run:

```sh
config-cli --profile PROFILE doctor
config-cli --profile PROFILE describe
config-cli --profile PROFILE auth status
config-cli --profile PROFILE capabilities list
```

`doctor` checks local profile and credential-file safety, identity, contract
compatibility, authentication, scopes, workspace access, and advertised SQL
and visual capabilities without revealing the credential. Use it after
interactive setup and when troubleshooting; `describe` remains the source for
the exact target and revision used in the change workflow.

Report the selected profile, normalized endpoint, live instance ID, workspace
key, current revision, authenticated actor/scopes, and compatibility result.
Use the advertised action schemas as the current server truth. Do not infer an
unsupported action from client documentation, and do not use capability
discovery to bypass the named proposal commands.
Stop on an instance-ID mismatch, unsupported contract, authentication failure,
or unexpected endpoint.

Never infer that a familiar profile name still points to the expected server.

## 2. Inspect before proposing

Use the smallest set of read operations needed to understand the request:

```sh
config-cli workspace get
config-cli layers list
config-cli layers get "LAYER KEY"
config-cli schema --pointer "JSON POINTER"
config-cli rules
config-cli catalog list
config-cli icons list
config-cli sql capabilities
```

XYZ layer folders are not nested workspace objects. Layers with the same
non-empty `group` string render in one drawer. Use
`config-cli layers list --group "GROUP"` to inspect effective membership and a
focused `/locale/layers/LAYER/group` (or named-locale override) proposal
operation to add or move a layer. Unset the property to remove membership.

Inspect `layers style-elements "LAYER KEY"` before changing the interactive
Styling drawer. `style.elements` controls order and inclusion only; each
built-in element still needs its corresponding `style.<key>` configuration.
Preserve unknown/custom keys. Use `style.hidden`, not deletion of rendering
styles, when the request is only to suppress the drawer.

Inspect `layers filters "LAYER KEY"` before changing interactive filters. XYZ
derives them from `infoj` entries plus the layer `filter` object; preserve the
entry index, field, type, advanced options, and unknown extensions. Distinguish
interactive filters from `filter.default`, a fixed server-side restriction
that may carry trusted template SQL requiring explicit security review.

Resolve natural-language intent to the effective workspace property. Layer
keys, labels, database relations, locale names, style states, and theme-driven
overrides are independent concepts. Ask the user when the request could
reasonably select more than one target or interpretation.

The top-level `locale` is the default even when named `locales` exist. XYZ
composes the default into named alternatives with framework-specific merge
rules, including conditional array concatenation/replacement. Inspect the
effective layer, but target the raw property that owns the requested override;
do not manufacture a generic deep merge or copy an entire inherited layer.
If raw `workspace.locale` is absent, XYZ still selects a synthetic empty
default for no option or `--locale locale`; never infer that a sole named
locale is the default.

Preserve unknown XYZ, plugin, template, role, and advanced properties.

Inspect `infoj` before adding a feature-information field. A requested value
may already be displayed, may exist as an unused catalog column, or may require
a new calculated expression. Ask which interpretation is intended rather than
adding a duplicate. For line layers, “area” is ambiguous: line geometry itself
has no polygon area, while length multiplied by width is only an approximate
rectangular footprint.

## 3. Build the smallest operation set

Use only the JSON Pointer operations required for the request. Do not replace a
parent object when changing one nested property. Do not normalize, reorder, or
clean up unrelated configuration.

JSON Pointer escapes `/` as `~1` and `~` as `~0`. Quote every path containing
spaces or shell metacharacters. Keep the explanation specific about what
changes and what remains unchanged.

The CLI rejects `-` as an array append index. If the only supported operation
is replacement of an inspected parent array, preserve every existing element
and its order exactly. Present both the transport-level replacement and the
smaller semantic change so the reviewer can verify that no sibling entry
drifted.

## 4. Preflight and create a revision-bound proposal

Use the revision reported during inspection. Check the exact operation set
first without creating a proposal:

```sh
config-cli proposals check \
  --base-revision REVISION \
  --set '/path/to/property=JSON_VALUE' \
  --explanation 'Focused description of the requested change.'
```

Preflight uses the server's authoritative proposal validation. A successful
result reports `valid: true`, `proposalCreated: false`, the focused diff, and
review warnings; a rejected check reports blocking errors separately. Follow structured
`nextActions` by their IDs and arguments; do not parse human messages or copy
an SQL expression into logs. A successful check does not reserve the revision
or create an approvable record.

If preflight reports `valid: true` and `proposalCreated: false`, create from
its fingerprint so the checked revision and exact operations are reused:

```sh
config-cli proposals create --from-check CHECK_FINGERPRINT
```

Use repeated `--set` or `--unset` options only when the request genuinely
requires multiple changes. Never use legacy direct-save commands or call a
remote mutation endpoint outside the proposal API.

The named `set`, `amend`, and `unset` commands are read-only dry-run validators,
but they do not produce a checked fingerprint. Prefer `proposals check` for
agent change planning so the validated operations can be handed off exactly to
proposal creation.

Proposal creation validates the candidate but does not alter the live
workspace. It validates again, so a successful preflight does not override a
later revision conflict or validation failure.

## 5. Present the review packet

Before requesting approval, give the user:

- the profile, endpoint, instance ID, workspace key, and base revision;
- the proposal ID and explanation;
- a focused JSON diff containing paths, old values, and proposed values;
- every blocking error, review warning, and informational observation in
  separate groups;
- structured next actions that are relevant to remediation;
- a visual-coverage checklist derived from the focused diff, candidate evidence
  for each applicable item, and any limitations or uncovered items;
- SQL expressions, data exposure, cost, or nullability risks, if applicable.

Do not bury warnings in raw JSON. Do not claim that a baseline visual test
renders the proposed candidate.

The initial change request is not approval to apply. Approval must refer to the
reviewed proposal or be otherwise unambiguous after the review packet is
shown.

## 6. Apply only after explicit approval

After receiving explicit approval:

```sh
config-cli proposals apply PROPOSAL_ID --confirm
```

Confirm that the returned proposal ID and applied revision are the expected
ones. `--confirm` is required, but it is only a command-line safety guard; it
must never be used to manufacture approval.

If apply reports a revision conflict:

1. Do not retry the same proposal.
2. Run `describe` and re-inspect the affected properties.
3. Explain what changed remotely.
4. Create a new proposal against the new revision.
5. Present the new diff and obtain approval again.

Never silently rebase, merge, or recreate-and-apply in one step.

An apply timeout or HTTP `5xx` is indeterminate because the workspace may have
been saved and the proposal marked `applied` before XYZ reload confirmation
failed. Do not blindly retry the request. Preserve its structured error
details, then run:

```sh
config-cli proposals show PROPOSAL_ID
config-cli workspace get
config-cli describe
config-cli xyz status
```

Compare the proposal lifecycle status and applied revision with the live
workspace revision. If they match, the write committed: do not reapply it;
continue with XYZ recovery and visual verification. If the proposal remains
pending and the workspace revision is unchanged, investigate the failure
before an operator decides whether to retry. If it is `applying`, inspect the
candidate and live workspace; only a deliberate repeat of the same approved
proposal may invoke the server's recovery path. A `conflicted` proposal
requires a newly reviewed proposal. Stop and escalate if the state cannot be
reconciled.

## Candidate evidence before approval

When the contract advertises proposal preview commands, gather evidence from
the exact retained candidate without changing the live workspace:

```sh
config-cli proposals preview-plan PROPOSAL_ID --layer "LAYER KEY"
config-cli proposals preview-test PROPOSAL_ID --layer "LAYER KEY"
config-cli proposals preview-screenshot PROPOSAL_ID --layer "LAYER KEY"
```

One invocation represents one requested layer in one selected map view; it is
not proposal-wide visual coverage. For a large or mixed proposal, turn the
focused diff into a checklist covering every added, removed, moved, or
otherwise visually changed layer and every distinct geographic view needed to
show the result legibly. Include representative affected layers for
workspace-wide visual/view changes. Mark non-visual operations as not visually
applicable instead of silently omitting them.

Run `preview-plan` for every checklist case and inspect the returned centre,
zoom, source, and warnings. Then capture the candidate comparison into a
separate, clearly named artifact directory:

```sh
config-cli proposals preview-plan PROPOSAL_ID \
  --layer "Bus Stops" --lng -1.55 --lat 53.81 --zoom 12.5
config-cli proposals preview-screenshot PROPOSAL_ID \
  --layer "Bus Stops" --lng -1.55 --lat 53.81 --zoom 12.5 \
  --artifact-dir "./visual-evidence/PROPOSAL_ID-bus-stops"
```

Use explicit `--lng`, `--lat`, and `--zoom` when automatic framing is too
broad, outlier-driven, or unrepresentative. Prefer separate readable views to
one proposal-wide image zoomed too far out to verify. If the proposal affects
several distant areas, add a checklist case for each area.

Group membership comparisons deliberately isolate the affected layer: an
added layer is off before and shown alone after; a removed layer is shown alone
before and off after; a moved layer is shown alone on both sides. Other group
members remain hidden for these membership comparisons. Ordinary edits that do
not change membership may retain group context.

Report the returned candidate hash with the proposal ID. Exit code `6` still
preserves structured failed evidence and artifacts for review. Never describe
a candidate preview as applied or live.
The contract must advertise the exact command required by the CLI; a
similar-looking action ID or endpoint does not authorize bypassing the
client-side contract gate. Do not call the evidence complete until every
applicable checklist item is covered or each gap and its reason is disclosed.

## 7. Verify the live result

Check XYZ health and visually test every affected visual layer:

```sh
config-cli xyz status
config-cli visual-test --layer "LAYER KEY" [--locale LOCALE]
```

Review the result and authenticated artifact paths. Report failures, timeouts,
unexpected workspace fingerprints, missing layers, and screenshot concerns.
An HTTP 422 visual failure can still contain a useful plan, report, and
artifacts; preserve that evidence rather than reducing it to a generic error.
An HTTP 429 means the bounded visual runner is busy; report the contention and
retry the read-only check only after it clears.

A passing visual test establishes that XYZ loaded, the named layer was
present, and a map canvas rendered. It is evidence, not a guarantee of
cartographic quality. Large or outlier-heavy datasets, external basemaps,
theme-driven styles, custom SVGs, and unusual zoom rules may require a
user-specified view or manual screenshot review. A pass also does not prove
exact colours, pointer interactions, information-panel values, or
emoji/custom-font glyph fidelity; download and inspect screenshots when those
details matter.

When the automatic extent is misleading, provide a bounded explicit view:

```sh
config-cli visual-test --layer "LAYER KEY" \
  --locale LOCALE \
  --lng LONGITUDE --lat LATITUDE --zoom ZOOM
```

Omit `--locale` to use the top-level default, including when the workspace also
contains named alternatives. If the raw default is absent, this selects XYZ's
synthetic empty locale rather than a named alternative.

## Style mapping guidance

Determine layer geometry and effective style before selecting a property:

- Filled point symbols (`dot`, `target`, `triangle`, `square`, `diamond`, and
  `semiCircle`) use `style.default.icon.fillColor`.
- `circle` points use `style.default.icon.strokeColor`.
- A `markerLetter` layer icon uses `style.default.icon.color` for the outer
  pin and `style.default.icon.letter` for its centre text. These properties
  must be inside `icon`; `fillColor` does not recolor this symbol.
- `markerColor` has an outer `colorMarker` and inner `colorDot`; ask which part
  when the request is ambiguous.
- Custom SVG files generally cannot be recolored by a workspace color.
- Lines use `style.default.strokeColor`.
- An unqualified polygon color means `style.default.fillColor`; an outline
  color means `style.default.strokeColor`.
- Default, highlight, selected, hover, label, and theme styles are
  independent. Change only the requested state.
- Do not map the word “hover” from its spelling alone. On the pinned workspace
  schema, `style.highlight` supplies the visual pointer highlight, while a
  layer `hover` object is field-driven interaction configuration and may
  require a catalog column. Inspect schema and effective style first.
- Theme- or feature-driven styles may override a simple default. Explain that
  limitation rather than claiming the change affects every feature.

“Pin” is ambiguous in XYZ. A layer may render point features with a
`markerLetter` icon, but XYZ also uses `markerLetter` for selected-location UI
pins. For a selected location, XYZ clones `locale.locations.pinStyle` and then
sets the icon's `color` from the selected location style's `strokeColor` and
its `letter` from the location record's `symbol`. A record-level `colour`
first overrides that location style's stroke and fill colours. The location
style defaults to white.

This produces several non-obvious mappings:

- A layer marker colour normally belongs at
  `layer.style.<state>.icon.color`.
- A selected-location pin colour comes from
  `locale.locations.style.strokeColor`, or from the record `colour` supplied
  by the relevant location workflow.
- `locale.locations.pinStyle.color` is not an effective per-location control:
  XYZ overwrites it while constructing each selected-location pin.
- `locale.locations.style.fillColor` styles the selected geometry but does not
  directly feed the pin; the pin uses `strokeColor`.
- The selected-location letter comes from the record `symbol`, so a
  `locale.locations.pinStyle.letter` value is likewise overwritten.

Ask which pin the user means before creating a proposal when the layer and
selected-location interpretations are both plausible.

For layer markers, inspect the effective theme and state as well as the
default. XYZ begins with `style.default`, then selected, highlight, and
theme/category processing may replace or merge style data. A theme category
icon with its own `type` is treated as self-contained and does not inherit the
default icon colour. Keep `color` and `letter` inside each effective
`markerLetter` icon object; a colour placed beside `icon` is not an icon
property in the pinned framework.

This mapping is based on XYZ v4.23.4's
[`markerLetter` SVG symbol](https://github.com/GEOLYTIX/xyz/blob/a6f03c07dd7aaae2e9ab04087143ee0400e15cb9/lib/utils/svgSymbols.mjs#L157-L167)
and
[`listview` selected-location pin construction](https://github.com/GEOLYTIX/xyz/blob/a6f03c07dd7aaae2e9ab04087143ee0400e15cb9/lib/ui/locations/listview.mjs#L267-L279).
Recheck those implementation points when the deployed XYZ version changes.

Example:

```sh
config-cli layers get "Bus Stops"
config-cli proposals check \
  --base-revision REVISION \
  --set '/locale/layers/Bus Stops/style/default/icon/fillColor="#2563eb"' \
  --explanation 'Changes the default Bus Stops point fill from its current value to blue; highlight, size, visibility, data source, information fields, and other layers are preserved.'
config-cli proposals create --from-check CHECK_FINGERPRINT
```

## SQL changes

SQL is supported only in `infoj[].fieldfx` as one trusted, scalar, read-only
PostgreSQL expression evaluated against the layer relation. Inspect
`sql capabilities` and test the expression before proposing it.

When the target pointer is unambiguous, proposal preflight discovers the
effective locale, layer, information field, renderer, and expected result type
and returns a structured `sql.test` next action. Use those discovered arguments
to test the expression locally supplied by the operator; the server and CLI
must not repeat the expression in diagnostics or suggested commands. If
discovery is ambiguous, stop and resolve the layer, locale, or field rather
than guessing.

Even accepted expressions can be expensive, expose sensitive data, return
nulls, or prevent index use. Explain the expression and these risks in the
review packet. Never include database URLs, passwords, tokens, authorization
headers, or sensitive sample values in explanations or logs.

Formatting functions such as `to_char` return text and therefore require a
compatible text information renderer. Standalone `sql test` validates against
the current live entry selector; a coordinated change that adds `fieldfx` and
changes renderer type may need validation as one complete proposal candidate.
A standalone selector failure is not permission to skip SQL validation:
`proposals check` must still accept the exact candidate.

## Derived database layers

Use `derived-layers` when one rendered relation must combine or spatially
derive data from source relations. Ordinary views update with their sources;
materialized views require a confirmed refresh and suit expensive workloads.
The server fixes the output schema to `derived_layers`, validates declared
PostgreSQL dependencies and output geometry/ID properties, and remains
authoritative for SQL safety.

Creating or refreshing a derived relation is not a workspace proposal and
requires the separate `derive` scope. Do not infer approval for it from a
request to change the map. Present the definition, sources, mode, expected
cost, and refresh behavior, then obtain explicit authorization for the
database action. Adding the result to XYZ is a second operation: inspect it in
the catalog, create a revision-bound workspace proposal, present that diff,
and wait for its own approval before applying.

For H3-derived layers, keep candidate generation and final acceptance
separate. H3 polygon-to-cells functions can deliberately over-select when the
goal is "cells that touch" a source feature; follow that with an exact
`ST_Intersects` or other reviewed predicate against the original source
geometry so the derived table semantics are clear. Use the containment mode
names exposed by the installed H3 extension, such as `overlapping` when that
is the advertised value, rather than inferred aliases.

Restricted server search paths can expose extension-wrapper assumptions. If a
higher-level H3/PostGIS wrapper fails with missing `geometry`, `st_dump`, or
similar function/type resolution errors, rewrite the query to explicitly
qualify PostGIS functions or use lower-level H3 boundary output plus qualified
PostGIS constructors. If a derived-layer create, replace, or refresh returns a
timeout or HTTP `5xx`, inspect `derived-layers list`, `derived-layers show`,
and `catalog list` before retrying; the database action may have committed
even though response serialization or operation reporting failed.

## Non-negotiable safeguards

- Use `config-cli` as the only remote workspace write interface.
- Never edit or upload the remote `workspace.json` directly.
- Never use a direct-save command or endpoint.
- Never apply without separate explicit approval and `--confirm`.
- Never silently rebase a stale proposal.
- Never expose a token or other secret.
- Never mount or expose a remote Docker socket.
- Never assume ambiguous layer, marker, locale, style-state, or SQL intent.
