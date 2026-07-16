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
6. Create a proposal against the inspected revision. `--base-revision` is
   required:

   ```sh
   config-cli proposals create \
     --base-revision REVISION \
     --set '/path/to/property=JSON_VALUE' \
     --explanation 'Focused explanation of the requested change.'
   ```

7. Do not apply it. Present the proposal ID, target identity, base revision,
   explanation, focused JSON diff, validation results, warnings, SQL risks, and
   available visual evidence to the user.
8. Treat the original change request as intent, not approval. Wait for a
   separate, explicit approval of the reviewed proposal.
9. Only after approval, apply the exact proposal with:

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

10. Check `config-cli xyz status`, then run a `visual-test` for every changed
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

## Connecting

Create a full-access CLI token in the config dashboard under **Access & audit**.
The token is displayed once.

```sh
config-cli init https://config.example.com --profile production
config-cli profiles use production
config-cli auth status
```

For automation, put the token in a mode-`0600` file and use `--token-file` or
`CONFIG_CLI_TOKEN_FILE`. During `init`, that token is copied into the private
CLI credential store; on later commands it is a one-invocation override.
Never pass tokens as command arguments or include them in logs, proposals,
error reports, or screenshots. Never use `--allow-http` for a remote host.

## Natural-language style mapping

First determine the layer geometry and effective style. Layer keys, display
names, and tables are different identifiers.

- Filled point symbols (`dot`, `target`, `triangle`, `square`, `diamond`,
  `semiCircle`) use `style.default.icon.fillColor`.
- `circle` points use `style.default.icon.strokeColor`.
- `markerLetter` uses `style.default.icon.color`.
- `markerColor` has an outer `colorMarker` and inner `colorDot`; ask which part
  when the request is ambiguous.
- Custom SVG files generally cannot be recoloured by a workspace colour.
- Lines use `style.default.strokeColor`.
- An unqualified polygon colour means `style.default.fillColor`; an outline
  colour means `style.default.strokeColor`.
- Default, highlight, selected, hover, label, and theme styles are independent.
  Do not change more than the requested state.
- A theme or feature-driven style may override a simple default colour. Explain
  that limitation rather than claiming the change affects every feature.

Example: “make the bus stops blue”

```sh
config-cli layers get "Bus Stops"
config-cli proposals create \
  --base-revision REVISION \
  --set '/locale/layers/Bus Stops/style/default/icon/fillColor="#2563eb"' \
  --explanation 'Changes the default Bus Stops point fill from green to blue; highlight, size, visibility, data source, info fields, and other layers are preserved.'
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
# Hide a layer initially
config-cli proposals create \
  --base-revision REVISION \
  --set '/locale/layers/Recent Planning Applications/display=false'

# Increase a line width
config-cli proposals create \
  --base-revision REVISION \
  --set '/locale/layers/Definitive Paths/style/default/strokeWidth=4'

# Change workspace view
config-cli proposals create \
  --base-revision REVISION \
  --set '/locale/view/lng=-1.5491' \
  --set '/locale/view/lat=53.8008' \
  --set '/locale/view/z=12'

# Remove an optional property
config-cli proposals create \
  --base-revision REVISION \
  --unset '/locale/layers/Bus Stops/style/hover'
```

JSON Pointer escapes `/` as `~1` and `~` as `~0`. Quote paths containing
spaces. Values after `=` are parsed as JSON when possible, otherwise strings.

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

## Visual evidence and limitations

`visual-plan --layer` uses PostGIS geometry extent and map scale to choose a
view containing data. `visual-test` runs Chromium on the server and returns
authenticated artifact paths for its report and screenshots.

A passing visual test establishes that XYZ loaded, the named layer was present,
and a map canvas rendered. It is evidence, not a guarantee of cartographic
quality. Large/outlier-heavy datasets, external basemaps, theme-driven styles,
custom SVGs, and layers with unusual zoom rules may require a user-specified
view or manual screenshot review. A failed HTTP 422 visual result can still
contain its plan, report, and authenticated artifacts; preserve that evidence.
HTTP 429 means the bounded runner is busy; retry the read-only check only after
the contention clears.

Use `--lng LONGITUDE --lat LATITUDE --zoom ZOOM` on `visual-plan`,
`visual-test`, or `screenshot` when the automatic extent is misleading.
Omit `--locale` for the top-level default, even when named alternatives exist.
Use `--locale LOCALE` for a named effective locale. XYZ composes named locales
with framework-specific object and array merge rules, so inspect the effective
value but target only the raw override that owns the requested change. If raw
`workspace.locale` is absent, no option and `--locale locale` select XYZ's
synthetic empty default; never auto-select a sole named locale.

Normal pre-approval tests render the current live workspace, not the unapplied
candidate. Treat them as baseline evidence unless the server explicitly
reports an isolated candidate-preview capability. Normal proposals do not
alter the live workspace until approved.

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
