# Installation

## Requirements

- Python 3.11 or newer
- Network access to the remote MAPP configuration endpoint
- A bearer token created by an administrator in the configuration dashboard
- HTTPS for every non-loopback endpoint

The CLI does not need Docker, PostgreSQL tools, a checkout of XYZ, or access to
the remote server's filesystem.

## Install with pipx

`pipx` is the recommended installation method for a workstation or an
automation host because it gives the CLI an isolated Python environment.

From a repository checkout:

```sh
python -m pip install --user pipx
python -m pipx ensurepath
pipx install .
config-cli --version
```

To replace an existing checkout-based installation:

```sh
pipx reinstall mapp-config-cli
```

To install a reviewed wheel:

```sh
pipx install ./dist/mapp_config_cli-VERSION-py3-none-any.whl
```

Do not install untrusted wheels on an agent host. Verify the artifact source
and checksum through the release channel established by the repository owner.

## Install in a virtual environment

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
config-cli --version
```

For development:

```sh
python -m pip install -e .
python -m pip install build twine
python -m unittest discover -s tests -v
```

## Create a profile

Create a token in the configuration dashboard under **Access & audit**. The
token is displayed once. Store it in a file readable only by its owner:

```sh
install -m 0600 /dev/null ~/.config/mapp-config-cli/production.token
${EDITOR:-vi} ~/.config/mapp-config-cli/production.token
```

Initialize the profile:

```sh
config-cli init https://config.example.com \
  --profile production \
  --token-file ~/.config/mapp-config-cli/production.token
```

Initialization reads the public instance identity, authenticates to the
contract endpoint, and binds the profile to the reported instance ID and
contract version. It copies the token into the CLI's private mode-`0600`
`credentials.json`; delete the transfer file after initialization if it is no
longer needed. Review the returned endpoint and identity before continuing.

Plain HTTP should be limited to loopback or `.localhost` development
endpoints, which the client accepts without an extra opt-in:

```sh
config-cli init http://config.localhost:3000 \
  --profile local \
  --token-file ~/.config/mapp-config-cli/local.token
```

`--allow-http` is required only for a trusted non-loopback development host.
Never use it for a remote production host.

## Configuration and credentials

By default, client state is stored below:

```text
~/.config/mapp-config-cli/
```

Set `CONFIG_CLI_HOME` to relocate it. Set `CONFIG_CLI_PROFILE` to select a
profile without repeating `--profile`.

Profile changes are serialized with a private state lock. Profiles refer to
immutable credential records so an interrupted or concurrent `init` cannot
publish an endpoint with another profile's token. Existing name-keyed
credentials remain readable and are migrated automatically on a later profile
save. Treat these files as private implementation state; do not edit or copy
individual records by hand.

For short-lived automation, `CONFIG_CLI_TOKEN_FILE` may identify a mode-`0600`
token file. Prefer a token file supplied by the host's secret manager over a
plaintext token environment variable. On commands after `init`, this is a
one-invocation override and does not replace the stored profile credential.
Never put a token in:

- a command argument;
- a repository file;
- shell history;
- CI logs;
- a proposal explanation;
- a screenshot or visual-test artifact.

The CLI rejects token files with unsafe permissions on platforms that expose
POSIX permission bits.

## Confirm the installation

```sh
config-cli --version
config-cli profiles list
config-cli --profile production describe
config-cli --profile production auth status
```

`describe` should report the selected profile, normalized endpoint, stored and
live instance IDs, workspace key, current revision, API and contract versions,
and compatibility status. Stop if the instance IDs differ or compatibility is
reported as unsupported.

## Upgrade and rollback

Before an upgrade:

1. Record `config-cli --version`.
2. Run `config-cli describe` against each important profile.
3. Read [CHANGELOG.md](../CHANGELOG.md) and
   [compatibility.md](compatibility.md).
4. Install the reviewed artifact in a non-production profile first.

With `pipx`, upgrade from the approved source and repeat the identity checks:

```sh
pipx upgrade mapp-config-cli
config-cli --profile production describe
```

To roll back, reinstall the previously retained wheel. Client installation
does not modify the remote workspace.

## Uninstall

```sh
pipx uninstall mapp-config-cli
```

Uninstalling does not remove profile state. After confirming it is no longer
needed, remove `~/.config/mapp-config-cli` with an appropriate secure deletion
policy for the host.
