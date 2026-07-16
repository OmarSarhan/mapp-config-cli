# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases are intended to follow [Semantic
Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Standalone Python package and `config-cli` console entry point.
- Profile-based connection and credential management.
- JSON-first inspection commands for workspace, layers, schema, rules,
  catalog, icons, SQL capabilities, XYZ health, and visual tests.
- Revision-bound proposal creation and retained proposal review.
- Explicit `--confirm` guard for applying an approved proposal.
- Structured errors and stable automation exit-code categories.
- Unit, contract, integration, security, and clean-package test foundations.
- Installation, command, agent-workflow, security, and compatibility
  documentation.
- Named-locale selection for visual plans, visual tests, and screenshots.
- Partial-success recovery guidance for proposal-apply timeouts and server
  failures.
- Effective default/named locale inspection matching the pinned XYZ
  composition model, including framework-specific array merge behavior and
  the empty synthetic default used when raw `workspace.locale` is absent.

### Changed

- Separated the remote client from the MAPP platform, database, configuration
  dashboard, browser runner, and XYZ deployment.
- Made the remote server authoritative for workspace rules and
  XYZ-version-specific behavior.
- Preserved structured committed-state details on apply failures so automation
  can inspect proposal/workspace state without blindly retrying.
- Preserved failed visual plans, reports, and authenticated artifact paths
  returned with browser-validation errors.
- Required dry-run mutation responses to prove `saved: false` instead of
  allowing the client to mask an unexpected server write.
- Tightened command-specific response validation and ensured locally verified
  target context cannot be replaced by response fields.
- Tightened API and contract version parsing to numeric SemVer-like dotted
  components and added a package check that metadata and runtime versions
  match.
- Correctly distinguished the RFC 6901 `/` empty-key member pointer from the
  empty root pointer, which remains unavailable for whole-workspace mutation.

### Removed

- Direct workspace-save commands from the supported client interface.
- Dependence on the former monorepo filesystem layout.

### Security

- Rejected every HTTP redirect so bearer authorization cannot be forwarded to
  another origin.
- Rejected endpoint user information, non-root paths, queries, and fragments.
- Added strict private-file, symlink, identity, contract, and secret-redaction
  checks.
- Bound proposals to an explicitly supplied workspace revision.
- Required separate approval and `--confirm` before proposal application.
- Documented HTTPS, token-file, instance-binding, and artifact-handling
  requirements.
- Serialized profile operations with a private inter-process lock and immutable
  credential references so interrupted or concurrent initialization cannot
  pair a token with the wrong endpoint; legacy name-keyed credentials remain
  readable and migrate on a later profile save.

## Release notes

No public release has been made. The repository owner must select and add a
license before distribution.
