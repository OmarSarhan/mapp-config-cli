# Contributing

This repository is being prepared as the standalone MAPP configuration CLI.
Changes should keep the client small, auditable, server-authoritative, and safe
for use by automation agents.

## Before contributing

No license has been selected. The repository owner must choose a license and
document contribution terms before accepting public contributions or
distributing the project. Until then, coordinate internal contributions
directly with the owner.

Report security issues privately according to [SECURITY.md](SECURITY.md).

## Development setup

Python 3.11 or newer is required:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the checks:

```sh
python -m unittest discover -s tests -v
python -m mypy src
python -m build
python -m twine check dist/*
```

Use synthetic fixtures. Tests must never contact a production configuration
service or contain real credentials, endpoints, database metadata, workspace
content, or visual artifacts.

## Design principles

- Keep XYZ, PostgreSQL, dashboard, browser-runner, and deployment code out of
  this repository.
- Treat the remote service as authoritative for schema, rules, catalog, SQL
  capabilities, and XYZ-specific behavior.
- Preserve JSON-first output and structured error details.
- Never expose bearer credentials across redirects, logs, tracebacks, URLs, or
  fixtures.
- Bind profiles and writes to the expected instance and contract.
- Require a base revision for every proposal.
- Keep proposals as the only workspace mutation path.
- Require explicit approval plus `--confirm` for apply.
- Fail closed on stale revisions; never silently rebase.
- Preserve stable, documented exit-code meanings.
- Prefer the Python standard library unless a dependency materially improves
  safety or maintainability.

## Making a change

1. Open or reference a focused issue when the project workflow provides one.
2. Add or update tests before changing security-sensitive behavior.
3. Keep the patch limited to one coherent concern.
4. Update command, security, compatibility, and changelog documentation when
   user-visible behavior changes.
5. Run the full test and build suite.
6. Inspect the built wheel/sdist to ensure credentials and local state are not
   included.

Do not mix generated artifacts, local profiles, environment files, or unrelated
formatting changes into a source patch.

## Test expectations

Relevant changes should cover:

- parser behavior and JSON output;
- endpoint normalization and same-origin redirect policy;
- corrupt/unsafe profile and token files;
- request and response contract fixtures;
- instance and contract mismatch protection;
- server validation details and exit-code mapping;
- required proposal base revisions and confirmation;
- stale proposal conflicts;
- visual failure handling;
- installation from a clean wheel.

Tests should be deterministic and should not require network access unless they
are explicitly marked integration tests against an ephemeral local service.

## Documentation

Examples must use placeholder domains, synthetic layer names/data, and
obviously fake revisions and proposal IDs. Never suggest putting a token
directly in a command argument. Operational instructions must preserve the
inspect → propose → approve → apply → verify workflow.

## Review checklist

- Does the change preserve the approval boundary?
- Can any secret reach output, a URL, a redirect target, or an exception?
- Does it fail closed on unexpected identity, contract, or revision?
- Are errors useful to both humans and automation?
- Are compatibility and exit-code changes documented?
- Do tests exercise failure paths, not only success?
- Does the package still install and run from a clean environment?

## Releases

Release tags and artifacts are created only by repository owners. A release
must have passing CI, reviewed changelog entries, an explicit compatibility
range, and checksummed build artifacts. Public releases must wait until the
owner selects and adds a `LICENSE` file.
