# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases are intended to follow [Semantic
Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Backend connection failures from any command in the devcontainer now prompt
  the operator to start the configured `config.localhost` platform and rerun
  `.devcontainer/configure-platform-host.sh` from the trusted CLI source root.
  The prompt covers unreachable, reset, and incomplete-response transport
  failures while unrelated endpoints and authoritative HTTP errors remain
  unchanged.
- Corrected the federation availability guidance in
  `docs/agent-workflow.md`, which told agents federation is unavailable under
  `MAPP_DATABASE_MODE=external`. An external deployment can now federate when
  its operator has provisioned a host role and opted in, so the mode no longer
  answers the question. Agents are directed to read `host.federationReady` from
  `federation list`, which is probed live from the database catalog, and a test
  pins that the CLI passes the whole `host` object through -- a summary alone
  cannot tell an operator which grant is missing.

- Added backend-aligned operator guidance for XYZ layer-group colours through
  a verified deployed `groupClassList`, including first-member precedence,
  consistent member values, and candidate drawer evidence.
- Updated derived-layer scope guidance to treat the selected effective
  locale's configured north/east/south/west extent as authoritative, while
  documenting the server's legacy view-derived fallback for incomplete bounds.
- Visual tests and candidate screenshots now use durable server operations,
  poll them to completion, preserve failed report artifacts, and return the
  operation ID when the local wait expires.
- Documented and regression-tested preservation of server-classified
  derived-database contention as a conflict, including its retryable flag,
  closed contention scope, corrective guidance, and authoritative rollback
  state.
- H3 derived-layer create and replace now validate fresh server readiness before
  mutation, preserve bounded stage-specific remediation when unavailable, and
  avoid blocking queries that do not invoke H3 functions.

### Added

- Strict `derived-layers jobs` discovery for the bounded background queue,
  detached create/replace/refresh submission with validated operation binding,
  and opt-in `operations wait --progress` status/stage transitions. Detached
  and interrupted waits retain their durable operation IDs and never retry a
  mutation automatically.
- Read-only `derived-layers plan-area-weighted-h3` support with replayable
  reviewed create requests, semantic/source resolution, bounded spatial-scope
  evidence, preflight probes, and a separate confirmed create boundary.
- Capability-gated `layers statistics` inspection for bounded numeric
  distributions, thresholds, and candidate breaks without returning raw rows,
  with strict request and response validation and raw-field styling guidance.
- Closed validation for the optional versioned derived query-planning
  capability and success probe, plus generic CLI-owned authoring guidance for
  proven over-limit nested-loop pair work across synchronous and durable
  failures. Guidance now orders indexed candidate matching before
  materialization, pair-local aggregation, complete-input totals, and repeated
  metric computations without rewriting SQL or changing the server error envelope.
- Live and proposal evidence controls for required hover-tooltip and
  clicked-feature text, with strict verification of dedicated hover,
  information, Filtering, and Styling artifacts before the CLI reports
  requested evidence as complete.
- Capability-gated semantic status, catalog discovery/history, derived-profile
  readiness/repair, and curated proposal commands with strict response
  validation and catalog-revision context.
- Separately scoped source-relation discovery and confirmed semantic
  synchronization for registering or refreshing generated schema metadata
  without accepting SQL or table rows, including an explicit unchanged
  catalog no-op.
- Capability-gated, confirmed `semantic catalog archive` and `semantic source
  archive-excluded` commands for hiding semantic profiles without changing
  database data, while preserving exact-ID administrator audit history.
- Capability-gated Gemini semantic draft generation for whole assets and
  stable field IDs, with metadata-only defaults, explicit server-bounded 5%
  sample/statistics opt-ins guarded by `semantic:data`, exact context response
  validation, and no automatic proposal or retry behavior.
- Server-authoritative `plugins list` and `plugins show` inspection for pinned
  registry, dynamic loading, dispatch, prerequisites, and security behavior.
- Optional `--background` derived-layer create, replace, and refresh polling
  for known slow jobs, while retaining synchronous operation by default.
- Server-resolved map-extent previews and mandatory fixed map-bounded
  derived-layer creation/replacement, with `--map-extent` retained for command
  compatibility.
- Server-advertised materialization-size guard and returned probe/error
  handling, including an explicit approval boundary before substituting the
  recommended ordinary-view fallback.
- Server-advertised universal query-plan guard validation and evidence for every
  derived kind, including bounded H3 expansion and an explicit prohibition on
  treating an unsafe query as eligible for the ordinary-view fallback.
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
- Capability discovery—including exact live-visual and durable derived-layer
  action metadata—operation show/wait commands, JSON file/stdin request input,
  scalar extraction, private output files, request correlation, and
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
- Proposal screenshot panel capture flags for XYZ Filtering and Styling
  drawers, with optional expected text checks and local artifact download.
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

- Preserved authoritative derived-mutation commit-state fields, distinguished
  request/response ambiguity from lost operation polling, and kept every
  indeterminate path non-retryable until its retained identity is reconciled.
- Required `--confirm` for `derived-layers create` as a local command guard
  without adding it to the server request. Existing create automation must add
  the flag; it does not replace separate user authorization for the database
  action.
- Validated the bounded semantic delivery-blocker batch and its explicit
  `deliveryBlockersMore` continuation signal so an incomplete repair queue
  cannot be mistaken for a complete result.
- Preserved hardened derived-layer validation, policy, compute, storage, and
  indeterminate-operation guidance through automatic background polling and
  `operations wait`, including stable server codes and safe primary messages
  without promoting technical database detail; strictly validated the 1.3
  query-guard stages, shape limits, and error categories while retaining the
  exact earlier 1.x capability shape.
- Added manifest-backed external plugin inspection, validation, workspace usage,
  catalogue fingerprints, configuration schemas, and preview requirements to
  the server-authoritative `plugins` commands.
- Clarified that server schema properties advertise capabilities audited
  against the server's pinned XYZ version, with unknown contract properties
  rejected rather than preserved or silently removed; added focused template, bundled-plugin,
  and layer-gazetteer inspection guidance.
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

- Explicitly limited operational support to POSIX hosts, including WSL, and
  added Windows CI that proves native Windows commands fail before local-state
  access or a remote request. This removes weaker path-based Windows file and
  artifact handling without pretending it has POSIX descriptor guarantees.
- Namespaced checked-operation caches by workspace or semantic domain,
  preserved `semantic.*` server errors, restricted semantic edits to curated
  paths, and added only `semantic:inspect` to default device authority.
  `semantic:generate` and optional generation data access via `semantic:data`
  remain separate explicit grants.
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
