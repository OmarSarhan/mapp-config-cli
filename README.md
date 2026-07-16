# MAPP Config CLI

`config-cli` is the standalone, JSON-first command-line client for a remote
MAPP configuration service. It lets a human or an automation agent inspect an
XYZ workspace, create revision-bound configuration proposals, and apply an
approved proposal without needing shell or filesystem access to the server.

This repository contains only the client. The XYZ application, PostgreSQL
database, configuration dashboard, validation rules, browser runner, and
deployment configuration belong in the separate MAPP platform repository.

This directory is repository-ready source, not proof of a
history-preserving Git split. Before publishing it, repeat the extraction from
the canonical clone, retain relevant history and tags, and scan the complete
history for credentials and generated state.

## Safety model

The supported mutation workflow is deliberately narrow:

1. Inspect the selected remote instance and its current workspace revision.
2. Create the smallest possible proposal against that exact revision.
3. Present the proposal ID, explanation, focused diff, warnings, and available
   evidence to the approver.
4. Apply only after a separate, explicit approval.
5. Verify XYZ health and visually test each affected layer.

There are no direct workspace-save commands. A proposal cannot be silently
rebased if the remote workspace changes. Applying a proposal requires both its
ID and `--confirm`.

Pre-approval visual commands render the current live workspace only. They are
baseline evidence and do not render an unapplied proposal candidate.

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
python -m pip install -e .
python -m pip install build twine
```

See [Installation](docs/installation.md) for upgrades, token files, and
non-interactive environments.

## Quick start

Create a CLI token in the remote configuration dashboard. Put it in a private
file rather than passing it on the command line:

```sh
install -m 0600 /dev/null ~/.config/mapp-config-cli/production.token
${EDITOR:-vi} ~/.config/mapp-config-cli/production.token
config-cli init https://config.example.com \
  --profile production \
  --token-file ~/.config/mapp-config-cli/production.token
```

Initialization copies the token into the CLI's private mode-`0600` credential
store. Remove the transfer file afterward if it is no longer needed.

Inspect the bound instance:

```sh
config-cli --profile production describe
config-cli --profile production workspace get
config-cli --profile production layers get "Bus Stops"
```

Use the revision returned by `describe` or `workspace get` when creating the
proposal:

```sh
config-cli --profile production proposals create \
  --base-revision WORKSPACE_REVISION \
  --set '/locale/layers/Bus Stops/style/default/icon/fillColor="#2563eb"' \
  --explanation 'Changes only the default Bus Stops point fill to blue.'
```

Review the returned proposal. After an approver explicitly accepts that
proposal ID:

```sh
config-cli --profile production proposals apply PROPOSAL_ID --confirm
config-cli --profile production xyz status
config-cli --profile production visual-test --layer "Bus Stops"
```

The original request to make a change is not approval to apply its proposal.

## Documentation

- [Installation](docs/installation.md)
- [Command reference](docs/commands.md)
- [Agent workflow](docs/agent-workflow.md)
- [Security](docs/security.md)
- [Compatibility](docs/compatibility.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

The live server remains authoritative for workspace structure and
XYZ-version-specific behavior. Use `config-cli schema`, `config-cli rules`,
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

Run the local quality checks before proposing a change:

```sh
python -m unittest discover -s tests -v
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
