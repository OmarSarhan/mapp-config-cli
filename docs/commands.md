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
  COMMAND ...
```

`--version` prints the installed client version. For commands after
initialization, `--token-file` supplies a mode-`0600` token file for that
invocation instead of the stored credential. During `init`, the token is
copied into the CLI's private credential store. `--timeout` sets the request
timeout and defaults to 60 seconds.

## Connection and profiles

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

### `profiles remove`

Remove a profile and its locally stored credential:

```sh
config-cli profiles remove NAME
```

This does not revoke the remote token. Revoke it separately in the remote
dashboard.

### `describe`

Report client/server compatibility and the target currently bound to a
profile:

```sh
config-cli --profile production describe
```

The result includes the profile, endpoint, instance ID, workspace key, current
revision, API version, contract version, XYZ version, and compatibility
status. Run this before planning any change.

### `auth status`

Verify the credential and display the authenticated actor and reported scopes:

```sh
config-cli auth status
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
```

### `layers get`

Return one layer by workspace layer key:

```sh
config-cli layers get "Bus Stops"
config-cli layers get "Bus Stops" --locale en-GB
```

Layer keys, display names, and database relation names are different
identifiers. A missing layer is an error. Omitting `--locale` selects the
top-level default `locale`, even when named `locales` also exist. An explicit
name selects the effective named locale composed by XYZ's framework-specific
merge rules. If raw `workspace.locale` is absent, omitting the option or using
`--locale locale` selects XYZ's synthetic empty `{"layers": {}}` default; the
CLI never auto-selects a sole named alternative.

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

### `proposals create`

Create and validate a proposal against an exact workspace revision:

```sh
config-cli proposals create \
  --base-revision REVISION \
  [--set 'JSON_POINTER=JSON']... \
  [--unset JSON_POINTER]... \
  [--explanation TEXT]
```

`--base-revision` is required. At least one operation is required. Values after
`=` are parsed as strict JSON when possible; otherwise they are strings.
Quote the complete argument. JSON Pointer uses RFC 6901 escaping: `/` becomes
`~1` and `~` becomes `~0` inside a path segment. Other tilde escapes and
invalid array indices are rejected; the workspace root cannot be replaced or
deleted. In RFC 6901 the root pointer is the empty string; `/` instead
addresses an object member whose key is empty and is accepted.

Example:

```sh
config-cli proposals create \
  --base-revision 8b192e... \
  --set '/locale/layers/Bus Stops/display=false' \
  --explanation 'Hides Bus Stops initially without changing its data or style.'
```

Creating a proposal does not alter the live workspace.

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

Normal pre-approval tests render the current live workspace, not an unapplied
candidate. Treat them as baseline evidence unless the server explicitly
reports an isolated candidate-preview capability.

### `screenshot`

`screenshot` is a convenience alias for a visual test that returns its
screenshot artifacts:

```sh
config-cli screenshot --layer "Bus Stops" --locale en-GB
```

The same optional `--locale`, `--lng`, `--lat`, and `--zoom` controls are
available.

### `xyz reload`

An operator can explicitly request a standalone XYZ reload:

```sh
config-cli xyz reload --confirm
```

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

Automation should use the exit code for broad handling and the structured
error code/details for diagnosis. In particular, an apply timeout or server
failure uses code `5`, while a visual-plan, visual-test, or screenshot failure
uses code `6`.
