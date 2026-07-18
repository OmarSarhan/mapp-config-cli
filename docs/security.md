# Security

`config-cli` is a privileged remote administration client. Treat its host,
profiles, tokens, output, and visual artifacts as production control-plane
material.

For vulnerability reporting and supported-version policy, see
[SECURITY.md](../SECURITY.md).

## Trust boundaries

The CLI:

- reads local profile and credential material;
- sends bearer credentials to one configured HTTPS origin;
- reads server-supplied schema, rules, catalog, workspace, and evidence;
- creates revision-bound proposal lifecycle records;
- applies a proposal only after an explicit local confirmation.

The CLI does not need SSH, Docker, database credentials, or direct access to
the server filesystem. Do not add those privileges as a shortcut.

## Endpoint security

Use HTTPS for every production or Internet-reachable endpoint. The client must
validate certificates with the operating system trust store and must not send
an authorization header across an HTTP redirect. Plain HTTP is limited to
loopback, `.localhost`, or an explicitly approved isolated development host.
Profiles are bound to the instance ID observed at initialization;
state-changing requests must fail if the live instance ID or supported
contract no longer matches.

An endpoint must be an origin URL. Reject embedded usernames or passwords,
unexpected paths, queries, and fragments. Review the endpoint printed by
`describe` before every change workflow.

`--allow-http` exists only for trusted development endpoints and is unnecessary
for loopback or `.localhost` hosts. It permits plain HTTP; it does not disable
HTTPS certificate verification. Never use it for a remote production system.

## Token handling

Prefer `config-cli auth device` for agent access. It requests an expiring token
with `inspect`, `propose`, and `visual` scopes by default; `apply` and `reload`
are separate explicit grants. Existing dashboard-created `full` tokens remain
compatible for human operators and migration, but carry more authority than a
proposal-generating agent needs. Prefer separate tokens for separate hosts and
environments.

For a human-operated terminal, prefer `config-cli setup`: it reads the token
with hidden input, verifies the target, and never includes the token in its
JSON result. For scripts and CI, use `config-cli init --token-file PATH`.
Use `config-cli auth replace` for rotation: the new token is verified before
an atomic credential switch, so failure preserves the old credential.

- Store token files with mode `0600`.
- Keep the containing directory private.
- Prefer `--token-file` or `CONFIG_CLI_TOKEN_FILE`.
- Understand that `init --token-file` copies the token into the CLI's private
  mode-`0600` `credentials.json`; later command-level overrides do not replace
  that stored credential.
- Never pass a token as a command-line argument.
- Never commit credentials, `.env` files, profiles, captured HTTP traffic, or
  test fixtures containing real secrets.
- Never print authorization headers or token values in diagnostics.
- Treat request and operation IDs as safe correlation data, but keep operation
  results and visual artifacts access-controlled.
- Revoke a token immediately if its host, output, or logs may be compromised.
- Remove expired and unused tokens through the dashboard.

Profile removal deletes local material but does not revoke the server-side
token.

Successful proposal checks cache their exact operations, which may contain
sensitive SQL, in private mode-`0600` `checks.json`. The cache is bounded and
target-bound; protect it like other CLI credential/configuration state.
Files supplied through `--input` may also contain workspace or SQL material;
keep them private, reject symlinks, and remove temporary inputs according to
the same retention policy.

Profile mutations are serialized with a private mode-`0600` state lock.
Profiles publish immutable credential references only after target
verification, preventing interrupted or concurrent initialization from
cross-pairing an endpoint and token. Legacy name-keyed credentials remain
readable and migrate on a later profile save. Do not hand-edit the state files.

## Approval boundary

Proposals are the only supported mutation path. They must be bound to a checked
workspace revision—normally by creating from the target-bound fingerprint
returned by `proposals check`—and contain the smallest possible operation set.
The original change request does not authorize application.

When a service limitation forces replacement of a parent array to add one
entry, review the complete old and candidate arrays. Confirm that every
existing element and its order are preserved, and present the smaller semantic
addition separately from the transport-level replacement.

Applying requires:

1. a review packet showing the proposal ID, diff, explanation, warnings, and
   evidence;
2. a separate explicit approval;
3. `config-cli proposals apply PROPOSAL_ID --confirm`;
4. post-apply health and visual verification.

Do not restore direct-save commands, automatically confirm application, or
retry a stale proposal against a different revision.

Do not automatically retry an apply that times out or returns HTTP `5xx`.
Application and XYZ reload confirmation are separate phases, so the write may
already be committed. Inspect `proposals show`, `workspace get`, `describe`,
and `xyz status`; reconcile proposal status/applied revision with the live
revision before deciding what happened. If the proposal is already applied,
never submit it again. Escalate any ambiguous state.

## Output and artifacts

Workspace documents, catalog metadata, SQL samples, and screenshots may reveal
internal structure or sensitive data even when no bearer token is present.
Apply least-retention practices:

- capture only output needed for the task;
- redact secrets and sensitive sample values before sharing;
- store visual artifacts in access-controlled locations;
- do not paste full workspace output into public issues;
- keep CI fixtures synthetic.

Structured errors should retain diagnostic fields such as rule IDs and JSON
paths while redacting credentials and authorization data. A visual HTTP 422
may legitimately retain the failed plan, report, and authenticated artifact
paths; handle them as sensitive evidence rather than discarding them.

## SQL expressions

The remote service may support a constrained, scalar, read-only PostgreSQL
expression in `infoj[].fieldfx`. The server is responsible for validation and
bounded probes. The CLI is not an unrestricted SQL shell.

SQL can still disclose data, consume resources, return unexpected nulls, or
disable efficient index use. Inspect server capabilities, test the expression,
and disclose its purpose and risk before requesting approval.

## Workstation hardening

For a dedicated agent host:

- use a supported operating system and Python release;
- install the CLI into an isolated environment from a reviewed artifact;
- restrict interactive login and filesystem access;
- use a secret manager or private token file;
- send logs to an access-controlled destination with bounded retention;
- keep development tools and unrelated credentials off the host;
- allow network access only to required configuration endpoints and trusted
  package/update sources;
- monitor authentication and proposal audit events on the server.

The agent host should not have direct access to PostgreSQL, the Docker socket,
or the XYZ server filesystem.

## Dependency and release integrity

Changes must pass tests on supported Python versions and produce a clean wheel
and source distribution. Release artifacts should be built by CI, checksummed,
and published through a repository-owner-approved channel.

No license is currently present. The owner must select a license before any
public distribution; adding release automation does not itself grant
redistribution rights.
