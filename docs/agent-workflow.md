# Agent workflow

This workflow is mandatory when an AI agent uses `config-cli` to change a
remote MAPP workspace. The goal is to keep target selection, intent
resolution, approval, application, and verification as separate auditable
steps.

## 1. Identify the target

Run:

```sh
config-cli --profile PROFILE describe
config-cli --profile PROFILE auth status
```

Report the selected profile, normalized endpoint, live instance ID, workspace
key, current revision, authenticated actor/scopes, and compatibility result.
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

## 3. Build the smallest operation set

Use only the JSON Pointer operations required for the request. Do not replace a
parent object when changing one nested property. Do not normalize, reorder, or
clean up unrelated configuration.

JSON Pointer escapes `/` as `~1` and `~` as `~0`. Quote every path containing
spaces or shell metacharacters. Keep the explanation specific about what
changes and what remains unchanged.

## 4. Create a revision-bound proposal

Use the revision reported during inspection:

```sh
config-cli proposals create \
  --base-revision REVISION \
  --set '/path/to/property=JSON_VALUE' \
  --explanation 'Focused description of the requested change.'
```

Use repeated `--set` or `--unset` options only when the request genuinely
requires multiple changes. Never use legacy direct-save commands or call a
remote mutation endpoint outside the proposal API.

Proposal creation validates the candidate but does not alter the live
workspace.

## 5. Present the review packet

Before requesting approval, give the user:

- the profile, endpoint, instance ID, workspace key, and base revision;
- the proposal ID and explanation;
- a focused JSON diff containing paths, old values, and proposed values;
- validation results and every warning;
- any relevant baseline visual evidence and its limitations;
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
user-specified view or manual screenshot review.

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
- `markerLetter` uses `style.default.icon.color`.
- `markerColor` has an outer `colorMarker` and inner `colorDot`; ask which part
  when the request is ambiguous.
- Custom SVG files generally cannot be recolored by a workspace color.
- Lines use `style.default.strokeColor`.
- An unqualified polygon color means `style.default.fillColor`; an outline
  color means `style.default.strokeColor`.
- Default, highlight, selected, hover, label, and theme styles are
  independent. Change only the requested state.
- Theme- or feature-driven styles may override a simple default. Explain that
  limitation rather than claiming the change affects every feature.

Example:

```sh
config-cli layers get "Bus Stops"
config-cli proposals create \
  --base-revision REVISION \
  --set '/locale/layers/Bus Stops/style/default/icon/fillColor="#2563eb"' \
  --explanation 'Changes the default Bus Stops point fill from its current value to blue; highlight, size, visibility, data source, information fields, and other layers are preserved.'
```

## SQL changes

SQL is supported only in `infoj[].fieldfx` as one trusted, scalar, read-only
PostgreSQL expression evaluated against the layer relation. Inspect
`sql capabilities` and test the expression before proposing it.

Even accepted expressions can be expensive, expose sensitive data, return
nulls, or prevent index use. Explain the expression and these risks in the
review packet. Never include database URLs, passwords, tokens, authorization
headers, or sensitive sample values in explanations or logs.

## Non-negotiable safeguards

- Use `config-cli` as the only remote workspace write interface.
- Never edit or upload the remote `workspace.json` directly.
- Never use a direct-save command or endpoint.
- Never apply without separate explicit approval and `--confirm`.
- Never silently rebase a stale proposal.
- Never expose a token or other secret.
- Never mount or expose a remote Docker socket.
- Never assume ambiguous layer, marker, locale, style-state, or SQL intent.
