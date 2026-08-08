# MAPP Config CLI

`config-cli` is the standalone, JSON-first command-line client for a remote
MAPP configuration service. It lets a human or an automation agent inspect an
XYZ workspace and its semantic catalog, create revision-bound configuration
proposals, and apply an approved proposal without needing shell or filesystem
access to the server.

This repository contains only the client. The XYZ application, PostgreSQL
database, configuration dashboard, validation rules, browser runner, and
deployment configuration belong in the separate
[MAPP Platform](https://github.com/OmarSarhan/mapp-platform) repository.

This directory is repository-ready source, not proof of a
history-preserving Git split. Before publishing it, repeat the extraction from
the canonical clone, retain relevant history and tags, and scan the complete
history for credentials and generated state.

## Safety model

The supported mutation workflow is deliberately narrow:

1. Inspect the selected remote instance and its current workspace revision.
2. Check the smallest possible operation set against that exact revision, then
   create the proposal from the returned fingerprint.
3. Present the proposal ID, explanation, focused diff, warnings, and available
   evidence to the approver.
4. Apply only after a separate, explicit approval.
5. Verify XYZ health and visually test each affected layer.

There are no direct workspace-save commands. A proposal cannot be silently
rebased if the remote workspace changes. Applying a proposal requires both its
ID and `--confirm`.

Top-level visual commands render the current live workspace and provide
baseline evidence. Proposal-bound `preview-plan`, `preview-test`, and
`preview-screenshot` commands render an integrity-checked pending candidate in
an isolated runtime; they never apply it or change the live workspace.
Each candidate preview is scoped to one layer and one map view. Large or mixed
proposals therefore require a diff-derived coverage checklist and separate
readable previews for every changed visual layer and distinct geographic view;
one zoomed-out screenshot must not be presented as complete evidence.

The CLI preserves structured partial outcomes. A failed visual check can still
include its plan, report, and authenticated artifact paths. An apply request
can also report that the proposal committed before XYZ reload confirmation
timed out; inspect proposal, workspace, and reload state before retrying.

See [Agent workflow](docs/agent-workflow.md) for the complete operational
procedure.

## Installation

Python 3.11 or newer is required.

For an isolated installation from a checked-out repository:

```sh
python -m pip install --user pipx
python -m pipx ensurepath
pipx install .
```

For development:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

See [Installation](docs/installation.md) for upgrades, token files, and
non-interactive environments.

## Quick start

Create a CLI token in the remote configuration dashboard, then run the guided
setup. It asks for the profile name, service URL, and token; token entry is
hidden:

```sh
config-cli setup
config-cli auth device
```

Setup verifies the remote service before saving the profile, verifies live
workspace access afterward, and rolls the local change back if that final
check fails. It writes the token only to the CLI's private mode-`0600`
credential store and never echoes it. For automation, use `config-cli
init` with a private token file instead; see [Installation](docs/installation.md).
`auth device` replaces that bootstrap credential with a browser-approved,
thirty-day agent token using the verified server contract's safe advertised
defaults. The current platform defaults to inspect, propose, visual, and
semantic-inspect work; workspace apply, reload, derived-layer lifecycle
authority, and elevated semantic changes remain separate grants. After `auth
status` confirms the replacement, revoke the bootstrap token in the remote
dashboard.

Agents can inspect deployed action schemas with `config-cli capabilities
list`, correlate responses through `meta.requestId`, and inspect durable
visual, reload, apply, and background derived-layer outcomes with `config-cli
operations show|wait|cancel`. The server contract's exact command list and
`/api/capabilities` action schemas are complementary runtime authorities; the
CLI fails closed when either required declaration is absent instead of calling
a familiar route directly.
Global `--input`, `--extract`, and `--out` options support JSON file/stdin
workflows without fragile shell quoting.

Inspect the bound instance:

```sh
config-cli --profile production describe
config-cli --profile production workspace get
config-cli --profile production layers get "Bus Stops"
config-cli --profile production layers values "Bus Stops" town --limit 100
config-cli --profile production layers statistics "Areas" percentage \
  --threshold 0.05 --break 10 --break 20
config-cli --profile production derived-layers plan-area-weighted-h3 \
  --input tmp/population-h3-recipe.json
config-cli --profile production semantic catalog search "bus stops"
config-cli --profile production semantic catalog show ASSET_ID
config-cli --profile production semantic catalog history ASSET_ID
config-cli --profile production semantic source relations
config-cli --profile production semantic source sync \
  --alias DATABASE_ALIAS --schema SCHEMA --relation RELATION --confirm
config-cli --profile production semantic generate field ASSET_ID FIELD_ID
config-cli --profile production semantic generate table ASSET_ID \
  --sample-rows --statistics
```

Use the revision returned by `describe` or `workspace get` to check the exact
operations, then create from the returned fingerprint:

```sh
config-cli --profile production proposals check \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/style/default/icon/fillColor="#2563eb"' \
  --explanation 'Changes only the default Bus Stops point fill to blue.'
config-cli --profile production proposals create \
  --from-check CHECK_FINGERPRINT
```

Review the returned proposal. After an approver explicitly accepts that
proposal ID:

```sh
config-cli --profile production proposals apply PROPOSAL_ID --confirm
config-cli --profile production xyz status
config-cli --profile production visual-test --layer "Bus Stops"
```

The original request to make a change is not approval to apply its proposal.
Semantic metadata uses the same separation: check curated-only operations,
create from the returned fingerprint, review the focused diff, and run
`semantic proposals apply ... --confirm` only after explicit approval.
Gemini generation is metadata-only by default and never performs any of those
proposal steps. `--sample-rows` and `--statistics` are separate explicit
opt-ins for server-bounded data context; both additionally require the
`semantic:data` scope. On the current platform, sampling selects from 5% of the
relation and is capped at 100 rows, 96 KiB, 20 eligible table columns, and 512
characters per value; field statistics aggregate at most 1,000 sampled rows.
Always inspect `semantic status` because the connected server advertises the
authoritative availability and caps.

`semantic catalog show` returns generated relation/field facts and their stable
IDs together with separately curated table and field meaning; it never returns
database rows. Start derived-layer planning from that semantic profile and let
it overrule agent guesses. If the needed relation profile is absent, use the
separately scoped `semantic source relations` and confirmed `semantic source
sync` fallback; PostgreSQL/PostGIS/H3 functions are not relations and need no
profile of their own.

Administrators can archive a single semantic profile without changing its
database relation:

```sh
config-cli --profile production semantic catalog archive ASSET_ID --confirm
config-cli --profile production semantic source archive-excluded --confirm
```

Source exclusions are deployment configuration, not a hard-coded CLI list,
and affect future discovery/sync; the bundled platform currently excludes
`MAPP:leeds.census_datasets`. The second command explicitly archives
already-registered matches. Archived assets are omitted from catalog/search
collections even for administrators, while exact show/history remains
available by a retained ID with `semantic:inspect + semantic:admin`. To remove
only curated wording, use a checked `/curated/...` `unset` proposal instead;
generated metadata and database data remain untouched.
For an explicitly approved standalone XYZ reload, use
`config-cli --profile production reload-xyz --confirm`; the existing
`xyz reload --confirm` spelling remains available. This is an
operator/recovery action, not an extra step after a successful proposal apply,
which already requests and waits for the associated reload.

## Documentation

- [Installation](docs/installation.md)
- [Command reference](docs/commands.md)
- [Advanced workspace setup](docs/commands.md#advanced-workspace-setup)
- [Agent workflow](docs/agent-workflow.md)
- [Security](docs/security.md)
- [Compatibility](docs/compatibility.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

The live server remains authoritative for workspace structure and
XYZ-version-specific behavior. Use `config-cli schema`, `config-cli rules`,
`config-cli plugins list`, `config-cli plugins show KEY`,
`config-cli plugins validate`, `config-cli plugins usage [KEY]`,
and `config-cli examples` instead of encoding those rules in the client.

Omitting `--locale` selects the top-level XYZ `locale`, including when named
`locales` also exist. A named selection uses the server's effective XYZ
composition, including its framework-specific object and array merge rules;
the client must not invent a generic deep merge. If raw `workspace.locale` is
absent, both an omitted option and `--locale locale` select XYZ's synthetic
empty default; a sole named alternative is never selected automatically.

## Repository layout

```text
.
├── src/mapp_config_cli/   # installable client package
├── tests/                 # unit, contract, and local integration tests
├── docs/                  # operator and agent documentation
├── pyproject.toml         # package metadata and console entry point
└── .github/workflows/     # continuous integration
```

## Development

For isolated development, open this repository directory (not its parent split
workspace) in a separate VS Code window and choose **Dev Containers: Reopen in
Container**. The container installs this package in editable mode and reaches a
locally running platform at `http://config.localhost:3000`. It has no platform
source/state mounts, shared Docker network, or Docker socket; HTTP is the only
integration boundary. The `MAPP_PLATFORM_URL` environment variable is provided
as a convenience, but profiles remain the CLI's authoritative endpoint setup.
At container start, the development hook probes the native Docker gateway and
`host.docker.internal` against the configured platform port, then maps
`config.localhost` to the first reachable IPv4 address. If the platform has not
started yet, it records the native gateway so the route becomes usable once
the platform is running. This preserves the `config.localhost` Host header
required by Caddy while avoiding stale or unreachable Docker Desktop gateway
addresses.

Start the platform in its own dev container first, then initialize a local
profile from this container:

```sh
config-cli init "$MAPP_PLATFORM_URL" --profile local
```

Run the local quality checks before proposing a change:

```sh
python -m unittest discover -s tests -v
python -m mypy src
python -m build
python -m twine check dist/*
```

Client changes must preserve structured errors, stable exit codes, secret
redaction, instance binding, contract checks, revision binding, and the
explicit approval boundary.

## Licensing status

No license has been selected for this repository. The repository owner must
choose and add an appropriate `LICENSE` file before distributing the CLI or
accepting public contributions. Until then, no permission to copy, modify, or
redistribute this software is granted.
