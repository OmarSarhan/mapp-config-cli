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

Prefer `config-cli auth device` for agent access. It accepts only recognized,
supported, non-elevated defaults from the verified server contract; the
current platform advertises `inspect`, `propose`, `visual`, and
`semantic:inspect`, while a legacy 1.0 server without semantic support falls
back to the first three. Workspace apply/reload, derived-layer lifecycle
authority, and elevated semantic source/generate/data/propose/apply/admin
scopes are separate explicit grants. Source synchronization can register or
refresh generated schema metadata. Generation also requires semantic inspection
authority and is deliberately absent from safe defaults because authorized
schema metadata leaves the MAPP control plane for Gemini. The CLI refuses to
save a newly issued credential unless the
device start, credential record, and authenticated identity all report
exactly the requested scope set. Existing dashboard-created `full` tokens
remain compatible for human operators and migration, but carry more authority
than a proposal-generating agent needs. Prefer separate tokens for separate
hosts and environments.

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
- Token files are limited to 64 KiB, and the private profile configuration is
  limited to 128 MiB; both are validated as regular non-symlink files before
  bounded reads.
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

Successful workspace and semantic proposal checks cache their exact
operations, which may contain sensitive SQL or catalog meaning, in private
mode-`0600` `checks.json`. The cache is bounded, target-bound, and namespaced
by proposal domain so fingerprints cannot cross between workspace and
semantic proposals. Protect it like other CLI credential/configuration state.
Files supplied through `--input` may also contain workspace or SQL material;
keep them private, reject symlinks, and remove temporary inputs according to
the same retention policy.

Source-relation discovery and synchronization require both `semantic:inspect`
and the separate `semantic:source` scope. Discovery exposes configured
database aliases and relation identities, while synchronization reads
authorized schema metadata and can change the generated semantic catalog; an
unchanged definition is a catalog no-op. Neither operation accepts SQL or
returns rows. Keep source authority out of default agent credentials.
`SEMANTIC_SOURCE_EXCLUSIONS` is operator configuration rather than a hard-coded
client list and blocks future discovery/synchronization only. Archiving
already-registered matches or one selected ready profile requires explicit
confirmation plus `semantic:inspect + semantic:admin`; it leaves the database
unchanged. Archived records are hidden from catalog/search/derived-profile
collections for every caller. Exact show/history audit reads remain available
only by a previously retained ID with both scopes, so record intended audit
identities before archival. Removing an exclusion does not restore a
tombstone.

On-demand semantic generation sends only the schema and semantic metadata the
server authorizes for the caller by default, never table rows or server
credentials. `--sample-rows` is the explicit boundary at which raw row values
from a server-bounded 5% sample leave MAPP for Gemini. `--statistics` sends
relevant data-derived table or target-column aggregates, but no raw values
unless the sample option is also selected. Both options require the additional
`semantic:data` scope; keep that scope out of default agent credentials and
inspect `semantic status` for the server-advertised row and payload caps before
using it. Current platform ceilings are 100 rows, 96 KiB, 20 eligible table
columns, and 512 characters per serialized value; field statistics aggregate
at most 1,000 rows from a 5% sample. The Gemini API key remains server-side.
Treat field names,
descriptions, sampled values, statistics, generated drafts, and model/provider
diagnostics as controlled data. Generation is read-only but can incur provider
cost and disclosure, so grant `semantic:generate` separately, review its audit
trail and reported `generation.contextOptions`, and do not add it to default
agent credentials. Provider processing and retention follow the configured
project's [Gemini API terms](https://ai.google.dev/gemini-api/terms); the
server's `store: false` request does not replace that operator review. The CLI
issues one request and does not retry provider errors.

Generated bindings, relation fields, and types are lifecycle-owned. Curated
table/field annotations are proposal-owned. Removing only an annotation uses a
reviewed `/curated/...` `unset` proposal and does not remove generated metadata
or database data; archiving the entire profile is a distinct administrative
lifecycle action. Preserve that distinction in review packets and audit
reports.

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

Semantic proposal application has the same indeterminate-result rule. Inspect
`semantic proposals show` and the affected `semantic catalog show` version
before an operator decides whether any further action is safe.

## Output and artifacts

Workspace documents, catalog metadata, SQL samples, and screenshots may reveal
internal structure or sensitive data even when no bearer token is present.
Apply least-retention practices:

- capture only output needed for the task;
- redact secrets and sensitive sample values before sharing;
- store visual artifacts in access-controlled locations;
- do not paste full workspace output into public issues;
- keep CI fixtures synthetic.

Local JSON, SQL query, and validation inputs are descriptor-opened as regular,
non-symlink files and limited to 5 MiB before UTF-8 decoding. Token files are
limited to 64 KiB, while private configuration/cache JSON is limited to 128
MiB to accommodate the bounded retained-check cache.

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

Managed derived-layer SQL remains a separate `derive`-scoped administrative
operation. Its mandatory map extent filters final output geometry; it is not
row-level security and does not stop the declared query reading its sources.
The advertised materialization guard uses a planner estimate rather than
creating or sampling the result. A second actual-size check runs only after
population and indexing inside the transaction, so rollback cannot prevent
transient relation, index, TOAST, or WAL growth. Treat both as storage-safety
checks, not query-cost guarantees or replacements for database quotas and
monitoring. Policy failures are distinct from malformed or over-budget SQL and
must keep their reason-specific remediation. The CLI surfaces safe background
`userMessage` text while retaining diagnostic details; an indeterminate result
must never be presented as known unchanged state. A database error's optional
technical detail is restricted to bounded SQLSTATE and primary-message fields,
never the SQL text, PostgreSQL context, detail, or hint.

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
