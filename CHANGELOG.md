# Changelog

## [0.7.15] - 2026-10-01

### Added

- Trusted Hook/MCP bootstrap can start a project's first CodeGraph build in a
  background process. Default and per-project automation switches persist,
  with explicit build states and retry support.
- Bounded Agent capability hints expose CodeGraph discovery independently of
  long-term memory budgets.
- File overview and symbol views, file filtering, cross-file relation
  aggregation, neighbor highlighting, and zoom controls.

### Fixed

- Ordinary CodeGraph queries read SQLite directly instead of creating and
  retaining full-database temporary copies. WAL reads and concurrent
  checkpoint detection retain read-only behavior.
- Validation/import/install temporary files use the owning project's or
  MemoryGuard deployment's cache and are cleaned up when the operation ends.
  Installed runtime snapshots use the same deployment root; pip caching is
  disabled during installation.
- Migrated graph scopes remain discoverable, individual parser diagnostics do
  not block other files, and structured paths render as readable text.

### Upgrade boundary

- Repair/reinstall the provider integration after upgrading, then reconnect
  or restart hosts that cache MCP configuration. Automation depends on host
  events and first builds are limited to 10,000 code files per project.

## [0.7.14] - 2026-09-28

### Changed

- Synchronized the MCP server namespace and repository metadata after the
  GitHub repository rename. Runtime behavior is unchanged.

## [0.7.13] - 2026-09-28

### Changed

- Mandatory-rule replacements validate the final package using audience
  matching, canonical heads, deduplication, sensitivity checks, and publication
  budgets. Equivalent unlocked predecessors retire atomically; handled failures
  roll back the replacement, revisions, evidence, and receipts.
- Native `agent`/`group` audience matching and deduplication no longer split
  the same audience solely by provider or runtime role. Distinct Agent
  identities remain separate; project-scoped audiences remain project-specific.
  Provider repair uses the target provider's verified Agent identity.
- Cursor MemoryGuard Hooks use a 30-second timeout and retain
  `failClosed: true`.

### Fixed

- Conflict views keep peer members readable. Resolution preserves external peer
  groups and applies the selected changes atomically; ambiguous conflicts
  cannot be resolved or closed.

## [0.7.12] - 2026-09-12

### Added

- The default MCP discovery surface now exposes 11 day-to-day tools through
  `tools/list`, while keeping the registered MCP operation catalog and
  reviewed headless GUI metadata available through `memoryguard_capabilities`.
- Added bounded response delivery for eligible oversized, replayable reads. The
  broker returns a short-lived process-local `response_ref` with UTF-8 paging
  and business-field selection instead of silently truncating a result.
- Added separate measured cache-read and cache-write input totals and explicit
  `complete`/`partial`/`unavailable` cache coverage states to local telemetry.

### Changed

- Successful replayable reads are bounded at 24,000 UTF-8 bytes across the full
  MCP envelope. References live for at most five minutes, are capped at 16
  in-memory snapshots of at most 512,000 bytes each, and are revalidated under
  the current trusted session, scope, and binding before every page.
- `memoryguard_invoke` preserves the existing mutation contract: mutating
  targets require `confirmed=true` and a non-empty `idempotency_key`, which are
  forwarded to the target's existing permission, scope, and readiness checks.
- Writes and `memoryguard_context_bootstrap` keep their complete receipt and
  mandatory-rule contracts and cannot request response pagination; paging never
  reruns a mutation. When a read cannot safely create a reference, the bounded
  response reports `delivery.status="unavailable"` with
  `action="narrow_query"`.
- Provider-reported zero remains zero, while missing cache or token data remains
  unknown. Deterministic MemoryGuard units remain explicitly estimated and are
  not presented as measured provider-token savings.

### Validation boundary

- The implementation tip passed the official four-job CI matrix: Ubuntu full
  pytest and acceptance on Python 3.10 and 3.12, plus Windows runtime lease
  acceptance on Python 3.10 and 3.12.
- Local targeted acceptance recorded 85 passed for runtime lease plus knowledge
  library and knowledge acceptance checks. The rule-merge acceptance reported
  `ACCEPTED`/`passed: true`; lifecycle acceptance reported
  `passed: true`, `total: 200`, and `categories_passed: true`, with empty
  production defects and errors.
- These records make no claim of an empirically measured token-reduction rate.

## [0.7.11] - 2026-09-04

### Changed

- Made public documentation and discovery copy durable: it now links readers
  to live GitHub, PyPI, and official MCP Registry records instead of embedding
  a release-state claim that can become stale.
- Clarified that runtime behavior summarized in public documentation originates
  in v0.7.9; this release changes documentation and discovery consistency only.

## [0.7.10] - 2026-09-04

### Changed

- Synchronized source version metadata and public documentation after the
  v0.7.9 GitHub, PyPI, and official MCP Registry publication.
- Clarified that the desktop console has seven governance pages plus one
  independent Token usage-and-savings entry, for eight visible navigation
  entries.
- Published v0.7.10 documentation and discovery metadata through GitHub OIDC
  to PyPI and the official MCP Registry; no runtime behavior changed.

## [0.7.9] - 2026-09-04

### Added

- Added a marker-bearing MCP Registry metadata path for the local stdio server,
  plus concise contributor and security guidance for public discovery and
  responsible reports. The released v0.7.9 package is active/latest in the
  official MCP Registry as `io.github.irisxc4/memoryguard`.
- Added a local token-evidence benchmark with explicit seven- and thirty-day
  windows, host coverage, measured-versus-derived totals, and a no-sample
  boundary. It never turns missing host telemetry into zero usage or a savings
  claim.

### Fixed

- Governed writes now reconcile related rules, habits, and memories through one
  canonical path while retaining source links, evidence, graph branches,
  supersede history, conflict decisions, and reversible settlement receipts.
- Governance and GUI responses preserve readable Agent/provider identities,
  shared-group context, safe family icons, actionable conflict states, and
  explicit stale-conflict closure instead of exposing opaque IDs as the main
  user-facing label.
- Local telemetry synchronization now accepts bounded data from every
  supported connected host, separates provider-reported totals from derived
  input/output totals, and removes raw paths, account identifiers, and legacy
  error text at the public boundary.

### Validation boundary

This source-preparation entry does not claim any Glama score, Smithery
publication, directory listing, or other third-party acceptance. GitHub CI,
PyPI, and official MCP Registry publication were verified for v0.7.9.

## [0.7.8] - 2026-08-29

### Added

- Added local-only usage telemetry for MCP conversion events and host-reported
  token measurements, with separate seven- and thirty-day views and explicit
  measurement-basis metadata. Token savings are estimates from deterministic
  MemoryGuard units; hosts without token reporting remain unsupported rather
  than receiving guessed values.

### Fixed

- Canonical memory/rule reads and writes now converge related rules, habits, and
  memories while preserving source links, evidence, graph branches, reversible
  history, and settlement receipts. Mandatory context remains independently
  budgeted and fail-closed on invalid or overflowing state.
- Governance surfaces expose stable, readable provider/program identities,
  shared-group scope, safe icons, automatic decisions, risk explanations, and
  stale-conflict closure. The GUI uses the seven-page governance shell and
  keeps technical identifiers in details.
- Codex lifecycle cleanup remains evidence-gated for terminal threads, while
  ordinary turns stay resumable. Provider repair/runtime alignment preserves
  the existing Agent/shared-group binding and makes installed MCP and lifecycle
  Hooks use the current interpreter.

### Privacy

- Usage telemetry is stored only in the local `.memoryguard/usage_telemetry.sqlite`
  database. It does not upload data and does not retain conversation bodies,
  account names, raw paths, or instance identifiers.

This release note records the source/documentation scope. CI and PyPI results
are reported by the release workflow after its gates complete.

## [0.7.7] - 2026-08-28

### Fixed

- A bare `memoryguard provider repair ...` command in a normal shell can now
  obtain an in-process trusted repair capability when the canonical control
  home is verified and exactly one active binding is available. Missing or
  ambiguous context remains fail-closed; no other CLI mutation path is
  widened.
- Non-editable wheel repairs ignore an inherited stale runtime snapshot and
  write the current `sys.executable` to the Codex MCP entry and all seven
  lifecycle Hooks. Editable and local-source installs continue to use their
  content-keyed immutable snapshots.
- Provider repair preserves the existing Agent identity and shared-group
  binding instead of creating a new binding during runtime alignment.

## [0.7.6] - 2026-08-28

### Fixed

- Codex lifecycle Hooks and the MCP server now use the same content-keyed,
  immutable runtime snapshot instead of resolving different Python runtimes
  through PATH and package metadata.
- Hook session-state updates use short, re-entrant cross-process lock windows,
  so concurrent tools and sub-agent dispatch do not wait behind a full hook
  execution or leave stale temporary state files.
- Bootstrap success and failure state is written consistently: failures clear
  stale success hashes and recoveries clear old errors. Ordinary `Stop`
  capability, signature, dispatch, and runtime failures remain fail-open;
  an explicit mandatory-budget overflow is recorded for the next tool call to
  fail closed.
- Agent discovery recognizes the installed Grok client through its built-in
  profile and exposes a stable, verified program identity. Unknown MCP names
  remain unresolved rather than being guessed from a generic label.
- The governance console now uses the seven-page reference shell: persistent
  navigation, a central workspace, a context rail, readable Agent cards, and
  safe family-aware icon fallbacks. Technical IDs stay in collapsed details.

## [0.7.5] - 2026-08-27

### Fixed

- Conflict responses now include bounded, redacted member snapshots sourced from
  V2 atom/tombstone history. The GUI no longer needs a second `list_memory`
  request that could turn stale or missing IDs into opaque rows.
- Known conflict reason codes now have readable Chinese labels while bounded
  diagnostic reasons remain available for troubleshooting.
- Deleted, superseded, rejected, quarantined, shadowed, missing, and otherwise
  invalid members cannot be selected as conflict keepers. Resolution fails
  closed unless at least two members remain live and selectable.
- Historical conflict groups remain visible for audit, while `actionable_total`
  separately reports groups that can still be resolved; the governance overview
  uses the actionable count.

## [0.7.4] - 2026-08-27

### Fixed

- Canonical governance now merges related rules, habits, and memories into one
  evolving record while preserving derived graph branches, provenance, scope,
  evidence, and reversible supersede history.
- Mandatory rules beyond 20 produce a visible health warning instead of a hard
  count limit. Character/token budget overflow, sensitive content, and corrupt
  governance state remain fail-closed.
- Agent, GUI, automatic-decision, and risk-signal labels expose readable names
  and explanations instead of opaque identifiers; governance views default to
  collapsed groups for safer, clearer navigation.
- `run_audit` and `get_audit` now return explicit completion state and timezone-
  aware timestamps. The GUI uses that same evidence, so a completed scan no
  longer remains `待扫描`; missing numeric health evidence stays unknown rather
  than becoming a fabricated score. The reader popover remains above the
  navigation, conflict and risk summaries open their detail views, and risk
  entries show reason, impact, and action. Governance phases show completed,
  current, pending, or undetermined state with readable contrast.
- CodeGraph and GUI governance paths now use the same controlled identity,
  scope, evidence, and incremental-refresh contracts.
- Codex Hook/bootstrap diagnostics distinguish mandatory-budget failures from
  generic context-build failures and neutral `NO_SOURCE` canonical fallback.
- Provider MCP installation avoids editable runtimes by selecting or building
  content-keyed immutable snapshots, atomically refreshing package static
  assets, and reporting PEP 610 copied-install origin diagnostics without
  mutating runtime state during inspection.
- GUI partial mutations and evidence publication conflicts now fail atomically
  and preserve the prior valid state.
- System `group_outbox` projection advances its event and checkpoint in one
  transaction. A lagging checkpoint can be repaired safely; pending or failed
  outbox events remain fail-closed and cannot be projected as successful.
- Migration replay coverage now asserts idempotent, read-only failure evidence
  and safe retry behavior.

Validation for this release uses the existing incremental CI tail (`447/447`
passed), related focused suites, and `git diff --check`; the full suite was not
rerun for this release preparation.

## [0.7.3] - 2026-08-22

### Fixed

- Shared-group history reads no longer filter by the caller host provider. Group members can list/search/read each other sessions; owner-only delete and personal provider isolation stay unchanged.

## [0.7.1.post16] - 2026-08-15

### Fixed

- Corrected verified Codex thread binding for globally installed Hooks: the payload `cwd`/project path is now matched against `state_5.sqlite`, instead of incorrectly comparing the thread cwd with MemoryGuard's control-data directory.
- Real Codex acceptance now persists a non-empty `host_thread_id` on lifecycle leases, enabling later archive/delete cleanup for top-level conversations without treating ordinary turn `Stop` as terminal.
- Keeps workspace isolation fail-closed: when Hook cwd is absent the older workspace-bound check remains, and a mismatched thread cannot authorize a global terminal sweep.

## [0.7.1.post15] - 2026-08-15

### Fixed

- Binds each observed lifecycle lease to the verified Codex thread ID when host state can prove the session-to-thread mapping, without accepting an unverified payload identity.
- Reclaims an idle exclusive cohort only after read-only `state_5.sqlite` evidence shows its bound Codex thread was archived or deleted. The current root thread, live rows, shared cohorts, ambiguous mappings, and leases without verified thread IDs remain protected.
- Runs this indexed-terminal sweep after the existing `PostToolUse` and `Stop` reconciliation paths, preserving the seven-hook post14 design and generic observation-only lifecycle mode.

## [0.7.1.post14] - 2026-08-15

### Fixed

- Finalized automatic Codex cleanup on the existing seven trusted MemoryGuard Hooks; no new `SubagentStop` Hook or trust migration is required.
- `PostToolUse` and `Stop` reconcile Codex terminal/deleted child state, then reclaim a process cohort only when the terminal thread maps to exactly one exclusive live cohort. Shared, ambiguous, or still-active branches are preserved.
- A missing child thread left behind by history deletion closes its orphan spawn edge, so stale sub-agent `处理中` indicators no longer remain backed by an `open` edge.
- Ordinary conversation `Stop` remains resumable and never grants generic process-kill authority. Generic lifecycle `auto` stays observation-only; hard termination is derived only from Codex-owned terminal thread evidence.
- Hook authorization tolerates Codex omitting `CODEX_THREAD_ID`: a session fallback is accepted only when `state_5.sqlite` proves the thread exists and its cwd matches the current workspace.

## [0.7.1.post12] - 2026-08-15

### Fixed

- Turns Codex sub-agent cleanup into a hard-terminal lifecycle path instead of a PID-age heuristic. Ordinary turn `Stop` remains non-destructive; only thread IDs already proven terminal by Codex state are eligible for process reclamation.
- A terminal sub-agent cohort is reclaimed automatically only when it maps to exactly one live cohort and no other live lease shares that cohort. Shared or ambiguous mappings remain untouched.
- Global terminal reconciliation now runs on `PostToolUse` as well as session/prompt/stop boundaries, so a completed child is closed and its exclusive runtime can be released immediately after the parent receives the result.
- History deletion now closes an orphan `thread_spawn_edges` row when its child thread no longer exists, while a missing node with any live/unknown descendant remains protected. This prevents deleted-history sub-agents from staying visually stuck as `处理中`.
- When Codex omits `CODEX_THREAD_ID`, the conservative global sweep may use the Hook `session_id` only after `state_5.sqlite` proves that thread exists and its cwd belongs to the current workspace; root-scoped mutation retains the stricter host-owned identity rule.
- Keeps generic MCP lifecycle `auto` mode observation-only. Automatic termination authority exists only in the hard-terminal sub-agent path above, where termination evidence comes from Codex thread state rather than process timing.

## [0.7.1.post9] - 2026-08-15

### Fixed

- Reconciles Codex sub-agent UI state after history deletion: an open spawn edge whose child thread row has been deleted is now treated as a terminal orphan, while live/unknown descendants remain a hard safety boundary.
- Runs the bounded global sub-agent reconciliation on `UserPromptSubmit` as well as session start/stop, so stale “处理中” badges are repaired on the next interaction without requiring a full Codex restart.
- Keeps MCP lifecycle auto mode observation-only from post8; no automatic Hook path regains process-termination authority.
- Reconciles only exact user-level MemoryGuard Hook hashes through Codex's official `hooks/list`, `config/read`, and `config/batchWrite` app-server APIs. Provider install, `memoryguard hooks install/ensure`, and GUI/MCP host-control installs now restore trusted+enabled Hook state without touching unrelated hooks.
- Makes Hook status consult Codex's effective runtime view instead of treating the presence of `hooks.json` as proof. Disabled, modified, duplicate, or incomplete MemoryGuard Hook sets are reported as `configured_untrusted` and cannot masquerade as operational.
- Replaces the emergency regex-based `config.toml` editor with the same optimistic-concurrency app-server reconciliation path.
- Makes an explicit `MEMORYGUARD_HOME` outrank cwd/ancestor V2 discovery for bare MCP launches, preventing a nested project from silently selecting another workspace as the control plane.

## [0.7.1.post8] - 2026-08-15

### Fixed

- Removed all automatic process termination from the default Codex lifecycle path. `auto` now records exact reclaim candidates but never calls `taskkill`; only an explicitly selected diagnostic `force` mode may terminate a validated cohort.
- Added a second termination gate inside `WindowsProcessController`, so even an accidental call cannot kill a process unless the controller was constructed with explicit termination permission.
- Lifecycle receipts now expose `termination_enabled` and `reclaim_candidate_pids`, distinguishing observation/quarantine from an actual process kill. This prevents an MCP transport replacement from invalidating in-flight tool outputs and triggering Codex session rebuild loops.
- The Hook-only activation bridge now pins the clean `0.7.1.post8` wheel. Existing Codex Hooks remain disabled until the safe wheel is verified and deliberately re-enabled.

## [0.7.1.post7] - 2026-08-15

### Fixed

- Isolated Codex lifecycle receipts by the concrete `codex.exe` generation (`PID + process start time`). Desktop Codex, parallel Desktop instances, ephemeral `codex exec`, and PID reuse no longer overwrite one another's thread leases or retirement evidence.
- Added a bounded diagnostics index at `hook-runtime/codex-mcp-lifecycle.json`; mutable per-generation leases live in separate state shards. A matching pre-shard receipt is adopted once, while a receipt owned by another Codex generation is never imported.
- Persisted generation key, state path, assignment reason, pulse count, and stable cohort counts so live lifecycle acceptance can distinguish a real `snapshot_delta` lease from a guessed association.
- A unique current-generation snapshot delta now reserves its cohort before writer-lock reconciliation. A restored writer lock can neither steal that initial transport nor reassign a cohort already held by a proven `snapshot_delta` lease on later pulses.
- Completed the MCP stdio read-only protocol surface for `resources/list`, `resources/templates/list`, and `ping`. Codex no longer logs method-not-found warnings merely because MemoryGuard exposes tools but no resources.

## [0.7.1.post3] - 2026-08-15

### Fixed

- Corrected the Codex lifecycle model: `Stop` is a turn boundary, not a conversation-close event. Live stdio transports remain leased across stopped and resumed turns, and a bookkeeping timeout never kills a still-live cohort.
- Automatic cleanup now requires positive replacement evidence for the same thread. Restored writer-lock cohorts and unknown legacy cohorts remain observe-only; one-generation legacy draining is available only through diagnostic `force` mode.
- Native `agent` memory audiences now follow the trusted Agent across projects. Project narrowing is explicit through `agent_project`; stale optional project/provider/runtime metadata no longer makes a successful body-only write unreadable from another cwd.
- Repaired the installed MemoryGuard Skill front matter so Codex no longer rejects its emoji as an invalid Unicode escape.

## [0.7.1.post3] - 2026-08-15

### Fixed

- Protected direct Python stdio MCP roots from lifecycle termination. Cohort timing alone is not proof that a Python server is disposable; killing one can close a shared MemoryGuard transport for every restored conversation.
- Added `scripts/repair_codex_memoryguard_transport.py` to atomically normalize the Codex MemoryGuard MCP entry, preserve the trusted binding, force UTF-8 stdio, recover even from duplicate invalid MemoryGuard sections, retain only the latest three config backups, and run live JSON-RPC initialize/list/status/read verification with a receipt.
- Updated the normal provider installation path to emit `python -X utf8 -m memoryguard.mcp_server`, so a later provider reinstall or MemoryGuard update does not recreate the transport configuration that this repair removes.

## [0.7.1.post2] - 2026-08-15

### Fixed

- Added a fail-open, opt-out Codex Desktop lifecycle compatibility shim for leaked Windows stdio MCP cohorts. Codex remains the startup/lifecycle authority; MemoryGuard waits through a native-cleanup grace period and only reclaims a cohort it previously observed as released or replaced.
- Hardened ownership under restored conversations and parallel subagents. Exact ownership now prefers one-to-one Codex `thread-writer-locks` plus read-only `state_5.sqlite` activity evidence; a unique before/after process snapshot remains the safe late-start fallback.
- Restored but inactive threads and superseded exact cohorts are retired conservatively. Ambiguous writer-lock mappings disable nearest/unique-unowned guesses and remain observe-only. PID, parent PID, executable name, and start time are revalidated before termination.
- The shim is independently switchable with `MEMORYGUARD_CODEX_MCP_LIFECYCLE=auto|off|force`; the current editable-install bridge loads the pinned clean `0.7.1.post2` wheel only for the managed Codex Hook process and self-retires after a real package upgrade.

## [0.7.1] - 2026-08-14

### Fixed

- `memoryguard upgrade` is now the complete verified user-data migration: a
  bare command selects the canonical data home, prepares and validates V2,
  activates it, and removes only the successful migration backup batch.
  `memoryguard upgrade --preview` remains read-only. Bare `gui`, `doctor`,
  `mcp-status`, `hooks`, `groups`, and storage commands use that same data home
  instead of accidentally switching databases with the terminal directory.
- V1 Agent bindings, shared/personal groups, source selections, memories,
  rules, and history migrate into V2 without requiring a new binding. Existing
  migrated groups remain editable. Unbound discovered Agents can create a
  personal memory layer; residual cleanup is no longer confused with binding.
- Desktop Agent discovery returns registered real products and safe source
  tokens without leaking local paths. File/folder and External MCP dialogs use
  valid pywebview filters, health scoring cannot render `NaN`, and trusted
  desktop mutations do not become request-queue operations merely because an
  IDE environment variable is present.
- Reconstructed projection builds list only executable local Agent CLIs. The
  selected CLI performs governed extraction/enrichment in the background;
  synthetic `host skill` claims are removed. Deterministic mode is explicit,
  selected-engine metadata records actual LLM use, and browser payloads cannot
  substitute an arbitrary executable path.
- Projection builds are exclusive per trusted scope and survive reloads with a
  durable `TaskRun` ID. Start, failure, timeout, and cancellation restore the
  neuron page. Cancellation terminates owned CLI children with bounded cleanup;
  stale owners are recovered and duplicate workers are not created.
- Canonical rule reconciliation, audience updates, retrieval, compaction, and
  context bootstrap now share the same governed source/evidence/scope path.
  Rule and memory deduplication keeps explicit supersede/conflict/quarantine
  outcomes; Knowledge contributes references, never raw source bodies.
- Windows acceptance helpers force UTF-8 machine output so non-ASCII user paths
  are decoded consistently on Python 3.10 through 3.14.
- Raw History reads now preserve the business `session_id` selector through the
  native identity scrubber. A real SafeBridge → native History → Content V2
  regression covers the button path, and Windows project references compare
  case-insensitively.
- Governance recent-event payloads now carry Agent/group/provider provenance;
  the GUI displays a resolved Agent name or stable fallback instead of rows of
  `Unknown Agent`.
- Restored the V2 Knowledge Library product surface. `/knowledge` is again a
  bookshelf with search, add/reingest, candidate review, deleted-book recovery,
  TaskRun status, book detail, document/occurrence reading, settings, and smart
  rebuild controls rather than a JSON debug page. The retired `KnowledgeStore`
  remains outside the V2 runtime path.
- Governance decisions group/collapse dominant actions, and the neuron graph
  uses compact level-aware layout plus root-outward soft signal bands, node
  arrival halos, and terminal flashes instead of random projectile particles.
- Embedded Graphify core ships under `memoryguard.graphify_core` with explicit
  grammar imports and license/notice files. V2 group control keeps host-hook
  side effects behind `HostHookExecutor`.
- Canonical-readiness and History-schema SQLite probes explicitly close their read-only handles, preventing lingering `rules.db` and `content.db` locks on Windows.

### Verification

- Final local suite: `1884 passed / 0 failed` across `205` test files.
- Forced sandbox GUI dispatch: `40 passed`; security regressions: `54 passed`.
- Projection/LLM/cancellation/interactivity focused gate: `53 passed`.
- Real canonical user data migrated to `V2_ACTIVE`; binding, group, memory,
  rule, history, Hook, `doctor`, and `mcp-status` checks passed.

Graphify remains an optional metadata extraction provider integrated behind
MemoryGuard's CodeGraph boundary. It is not a separate MemoryGuard runtime or
PyPI release.

## [0.7.0] - 2026-08-12 (published to GitHub and PyPI; local release acceptance passed)

### Scope

- V2-only production boundary: V1 runtime/store modules are physically absent
  from production entrypoint import closure. `V1_ACTIVE` is a migration starting
  state, not a runtime fallback; legacy format input is accepted only by
  `memoryguard.migration`, while other entrypoints fail closed with
  `v2_upgrade_required`.
- V2 Memory, Evidence, History, Source, Binding, and Group planes are explicit;
  raw History remains separate from governed Memory and evidence receipts.

### Added and changed

- Canonical reconciliation builds `shared_baseline`, `agent_overlay`, and
  `project_overlay` bundles, retains source links, activates canonical reads
  only after parity, and shadows old duplicates recoverably.
- Same-group V2 automatic organization performs exact/semantic deduplication;
  Rule duplicate scans create governed merge proposals, and merge/supersede/
  conflict/quarantine decisions retain scope, evidence, idempotency, and undo
  receipts across Agents sharing one trusted group.
- Knowledge accepts files and folders as governed books/documents. Source bodies
  stay in the Content Plane; Knowledge stores metadata/references and supports
  re-ingest, remove/restore/purge, and explicit candidate review.
- GUI Agent discovery/name/instance data, source selection, Binding/Group member
  management, drift, personal/shared groups, leave, and dissolve use V2-native
  system control. Build, Knowledge, import, history, maintenance, release, and
  compatibility work use durable `TaskRun` status, bounded cancellation, and
  owned background-worker/process cleanup before shutdown.
- Graphify metadata-only export feeds MemoryGuard's CodeGraph under trusted scope with source
  role, provenance, source maps, revisions, tombstones, outbox state, and
  bounded query/path/explain/affected operations. Source bodies are rejected.
- Fail-closed state/scope/provenance/path/metadata checks, isolation, public
  receipt redaction, audit/outbox decisions, and Content Plane held-occurrence
  rollback are part of the V2 release boundary.
- `memoryguard upgrade` provides the 0.6.2-to-0.7.0 preview, `V2_READY`, and
  exact `V2_ACTIVE` confirmation path; failed validation remains non-active and
  V1/migration evidence is retained.

### Local release acceptance evidence

- Local release acceptance passed: `1761 / 1761`, with no skip or xfail.
- V1 retirement + CodeGraph: `15 / 15`; Graphify focused checks: `3 / 3`;
  canonical reconciliation: `ACCEPTED`; RuleMerge: `46 / 46`; v3.2: `27 / 27`.
- The real full-repository Graphify export/projection covered `486 files / 11672
  nodes / 38714 edges → 11667 canonical symbols / 38714 edges`; query/path/
  affected passed and failure atomicity was `0` throughout.
- Final clean wheel: `206 files`, `legacy bad=0`; isolated package, CLI, and MCP
  all reported `0.7.0`; desktop help passed.
- These Graphify results record the focused checks and the real full-repository
  export/projection only. They do not claim that upstream Graphify's full-
  repository test suite passed.

### Release state

- Local release acceptance passed. v0.7.0 was published to GitHub and PyPI.
- The acceptance result is local and receipt-backed. The Graphify evidence is
  intentionally limited to the focused `3 / 3` checks and the real
  full-repository export/projection; it is not an upstream Graphify
  full-repository test-suite claim.

## [0.6.2] - 2026-08-10

### Fixed

- Memory, Evidence, and Content schema preflights now inspect a private copy of the SQLite main file plus any `-wal`/`-shm` companions. Older SQLite builds can therefore checkpoint the temporary handle without changing the live database.
- The post-failure no-write assertions establish their physical baseline after reading the expected marker, so an older SQLite observer cannot contaminate the no-write check.

### Verification

- Full local test suite: 1678 passed / 0 failed.
- GitHub Linux/Python 3.12 and Windows runtime acceptance: passed on the preceding release candidate.
- GitHub Linux/Python 3.10 rerun is the release gate for this correction.

## [0.6.1] - 2026-08-10

### Fixed

- Restored the Python 3.10 CI contract for V2 schema fail-closed checks.
- Memory and Evidence stores now preflight existing base schema metadata through SQLite `mode=ro` before opening any write-capable WAL connection, so future/unknown schema markers fail without changing the database image.
- V2 migration tests now use genuinely read-only SQLite connections for post-failure observations; older SQLite versions could otherwise checkpoint WAL state from the test itself and make a no-write assertion fail spuriously.

### Verification

- Targeted schema/migration/storage regressions: 81 passed.
- V2 test suite: 591 passed.
- Non-V2 test suite: 1087 passed.
- Split full-suite total: 1678 passed / 0 failed on the local release candidate.
- Release gate: GitHub Linux / Python 3.10 full CI must pass before tag/release.

## [0.6.0] - 2026-08-10

### Added

- Production-ready MemoryGuard V2 data plane with separate SQLite domains for Memory, Rules, Evidence, Content, Runtime, Projection, Assets, CodeGraph, Skills, and System state.
- Four-state cutover manifest: `V1_ACTIVE → V2_BUILDING → V2_READY → V2_ACTIVE` with fail-closed rollback paths.
- Packaged `memoryguard-v2` operator CLI for read-only status, frozen-source preparation, and explicit activation.
- Coherent SQLite online-backup migration from live V1 sources, immutable frozen-source validation, migration evidence, and repeated live-source drift gates.
- Two-epoch Reference Audit, per-domain storage health reporting, guarded maintenance, and native coverage readiness checks.
- Native V2 Rule lifecycle, RuleMerge, extraction/enrichment, External MCP import, provider control-plane, history, Knowledge reference, and GUI governance routes.
- Safe unbound CLI diagnostics: `doctor` and `mcp-status` expose workspace-level health without tenant counts when no Agent binding is present.

### Changed

- All 233 registered MCP/CLI/GUI/Hook surfaces are explicitly classified: 138 implemented, 95 retired, 0 neutral, 0 blocker.
- V2 no longer silently falls back to legacy stores after READY/ACTIVE. Retired V1 workflows return stable retired results.
- RuleMerge production mutations now use V2 `rules.db` instead of the legacy rule-intelligence store.
- Extraction/enrichment staging now uses the V2 Content Plane and GovernanceV2 Memory/Evidence/Decision flow.
- Memory partial updates preserve untouched fields; failed evidence projection events remain outstanding and retryable until projected.
- Reference Audit distinguishes authoritative references from opaque transport/audit metadata and preserved migration evidence.

### Safety and migration

Existing v0.5.x workspaces are not auto-activated. The supported explicit cutover is:

```bash
memoryguard-v2 status -w .
memoryguard-v2 prepare -w . --apply
memoryguard-v2 activate -w . --confirm V2_ACTIVE
```

Preparation preserves legacy V1 data and `.memoryguard/migration-backups`. Activation performs a fresh live-source drift check before changing the manifest.

### Verification

Release-candidate verification on the production-active workspace:

- V2 test suite: 589 passed.
- Non-V2 test suite: 1087 passed.
- Split full-suite total: 1676 passed / 0 failed.
- Production readiness: `READY`, blockers `[]`.
- 12-domain Reference Audit: `PASS`, blockers `[]`.
- All 12 authoritative SQLite domain reports: integrity `ok`, foreign-key errors `0`.
- Production manifest: `V2_ACTIVE`, generation 11.

## [0.5.2] - 2026-08-09

- Canonical rule reconciliation and governance diagnostics.
- Multi-process runtime lease protection.
- Desktop/neuron graph improvements.
- History and Knowledge Library reliability fixes.

Earlier release history is available from GitHub Releases.
