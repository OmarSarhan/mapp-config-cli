# Security policy

## Supported versions

This project has not yet made its first public release. Security fixes are
developed against the current repository state. Once releases begin, this
section must list the supported release lines and end-of-support dates.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability, exposed credential,
authorization bypass, cross-origin token disclosure, unsafe redirect, or
proposal/revision integrity failure.

Report it privately to the repository owner through the project's established
security contact. If no private contact is published, ask the owner to
establish one before sharing exploit details. The owner must add a durable
security contact and response expectations before public distribution.

Include, where safe:

- affected CLI and server versions;
- operating system and Python version;
- the command or request sequence using synthetic values;
- expected and observed behavior;
- security impact;
- whether a token or production instance may be affected;
- a minimal reproduction that contains no real credentials or sensitive
  workspace data.

Do not include bearer tokens, authorization headers, database URLs, passwords,
private workspace documents, or unredacted screenshots.

## Immediate containment

If a real token may have been exposed:

1. Revoke it in the remote configuration dashboard.
2. Review authentication and proposal audit records.
3. Replace any related secrets.
4. Preserve relevant redacted logs for investigation.
5. Verify that no unexpected proposal was created or applied.

Removing a local profile does not revoke its remote token.

## Security-sensitive behavior

Changes in these areas require focused tests and review:

- endpoint parsing, TLS, redirects, and authorization headers;
- profile identity and API-contract binding;
- credential storage, token-file permissions, and redaction;
- structured error and artifact handling;
- JSON Pointer parsing and strict JSON values;
- proposal base revisions, retained original/candidate data, lifecycle
  transitions, approval confirmation, and stale apply conflicts;
- command exit codes used by automation;
- SQL capability boundaries;
- dependency and release integrity.

See [docs/security.md](docs/security.md) for deployment and operating guidance.

## Disclosure

Coordinate disclosure with the repository owner after a fix is available and
affected credentials or deployments are contained. Public advisories must use
synthetic examples and must not reveal customer or instance data.

## Licensing prerequisite

No license has been selected. The owner must add a license and an appropriate
security contact before distributing this software publicly.
