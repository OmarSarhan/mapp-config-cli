# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases are intended to follow [Semantic
Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Optional `--background` derived-layer create, replace, and refresh polling
  for known slow jobs, while retaining synchronous operation by default.
- Confirmed `derived-layers replace` for atomic definition updates and
  view/materialized-view conversion with structured in-use feedback.
- Documented H3-derived layer practices for candidate-cell expansion, exact
  PostGIS acceptance predicates, restricted search-path wrapper failures, and
  indeterminate mutation recovery.
- Group-aware layer inspection with `layers list --group`, plus documented
  revision-bound proposal recipes for adding, moving, and removing XYZ
  layer-folder membership.
- Added `layers style-elements` inspection and proposal guidance for ordered
  XYZ Styling-panel controls and panel visibility.
- Added `layers filters` inspection with framework-compatible inference,
  include/exclude behavior, and fixed-filter safety guidance.
- Capability discovery, operation show/wait commands, JSON file/stdin request
  input, scalar extraction, private output files, request correlation, and
  browser-approved scoped device credential rotation without changing the
  revision-bound proposal model.
- Interactive `config-cli setup` wizard with hidden token entry and verified
  profile creation; non-interactive `init` remains available for automation.
- Read-only `config-cli doctor` readiness checks and authoritative
  `config-cli proposals check` previews that do not persist a proposal.
- Verified atomic token rotation, safe profile inspection/removal, checked
  proposal handoff, shell completion, and optional human-readable reports.
- Standalone Python package and `config-cli` console entry point.
- Profile-based connection and credential management.
- JSON-first inspection commands for workspace, layers, schema, rules,
  catalog, icons, SQL capabilities, XYZ health, and visual tests.
- Revision-bound proposal creation and retained proposal review.
- Isolated proposal-bound visual plans, browser tests, and screenshot evidence
  with strict proposal/candidate identity validation.
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
- Top-level `config-cli reload-xyz --confirm` alias for the existing
  `config-cli xyz reload --confirm` operation.

### Changed

- Expanded the command, compatibility, and agent documentation with verified
  input-merging limits, array-edit constraints, SQL renderer coordination,
  exact capability matching, and visual-evidence limitations.
- Clarified that grouped layers can produce useful visual artifacts even when
  strict layer-name text assertions fail against folder-oriented UI output.
- Handled Ctrl+C and closed terminal input as structured exit-`130` failures,
  including transactional setup rollback, without displaying a traceback.
- Made the isolated CLI devcontainer resolve `config.localhost` through the
  Docker host's working IPv4 path on every start, preserving Caddy host routing
  across Docker Desktop IPv6 and native Linux environments. The API client no
  longer overrides an explicitly resolved `.localhost` transport with
  `127.0.0.1`.
- Added a pinned development dependency extra for `mypy`, package builds, and
  distribution checks; the same tools are installed in the devcontainer and
  type checking now runs in CI.
- Added Fish and Zsh to the standalone and combined development containers and
  syntax-check all three generated completion scripts in CI.
- Made interactive setup transactional and added explicit old/new target
  confirmation before replacing an existing profile.
- Enriched rejected proposal creation with safe pointer, rule, type, and SQL
  test remediation details while omitting SQL expressions from suggestions.
- Separated the remote client from the MAPP platform, database, configuration
  dashboard, browser runner, and XYZ deployment.
- Made the remote server authoritative for workspace rules and
  XYZ-version-specific behavior.
- Require the server-advertised `layers effective` capability before layer
  inspection, failing closed against older servers instead of composing XYZ
  locales in the client.
- Preserved structured committed-state details on apply failures so automation
  can inspect proposal/workspace state without blindly retrying.
- Aligned confirmed proposal application with the server contract's explicit
  `approved: true` request guard.
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
