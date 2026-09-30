# Codex MCP lifecycle compatibility shim

`memoryguard.codex_mcp_lifecycle` is a narrow, best-effort compatibility shim for a Codex Desktop for Windows defect where per-turn stdio MCP runtime cohorts may remain alive after the owning turn stops or is replaced.

## Ownership boundary

Codex remains the lifecycle authority and the only component that starts MCP servers. MemoryGuard never starts, restarts, proxies, or keeps MCP servers alive. The shim only observes Codex-owned child-process cohorts and may reclaim a proven leftover after Codex has had a chance to clean it up natively.

The shim is dynamically imported from the Codex host-hook path. Any import, discovery, state, or termination failure is fail-open and must never block Codex or MemoryGuard.

## Auto mode

`MEMORYGUARD_CODEX_MCP_LIFECYCLE=auto` is the default.

1. Every non-throttled probe snapshots the current `codex.exe` generation and its direct MCP roots.
2. Mutable lifecycle state is sharded by `codex.exe` PID plus process start time. Desktop Codex, another Desktop process, ephemeral `codex exec`, and PID reuse cannot overwrite one another's leases. The old stable JSON path is only a bounded diagnostics index pointing to generation state files.
3. The strongest ownership signal is a one-to-one match between a Codex `thread-writer-locks/<thread>.lock` timestamp, the thread's read-only `state_5.sqlite` activity, and a cohort anchor start time.
4. A one-to-one writer-lock match is preserved even when the thread has not produced a turn in the current Codex generation. Restored conversations are resumable; inactivity is not evidence that their stdio transport is disposable. Only an exactly matched older cohort superseded by a newer lease for the same thread is retired.
5. Writer-lock timing that matches multiple threads or cohorts is ambiguous. In that case the shim is observe-only and disables nearest/unique-unowned guesses.
6. For a newly spawned runtime, a unique before/after cohort snapshot binds the sole new cohort to the sole unresolved Hook lease before writer-lock reconciliation. That proven `snapshot_delta` owner remains protected on later pulses; a coincidentally timed restored writer lock cannot steal it. Multiple new cohorts or multiple unresolved leases remain fail-open.
7. Only when no writer evidence exists may the compatibility layer use the older bounded nearest/legacy-adoption fallbacks.
8. `PostToolUse` is a throttled lifecycle pulse so long tool-heavy turns can notice a replacement before the conversation ends.
9. Replacing a known live lease retires the prior cohort. Codex `Stop` is only a turn boundary: it marks the lease idle and preserves the live transport for a resumed turn.
10. Codex gets a native-cleanup grace window. If Codex removes the cohort itself, MemoryGuard records native cleanup and performs no termination.
11. Generic `auto` mode is observation-only for unmatched, replaced, legacy, or age-only leftovers. Those retired cohorts are still reported through `reclaim_candidate_pids`, but generic lifecycle observation never calls `taskkill`.
12. Automatic termination stays fail-closed. After the native-cleanup grace window, `auto` may terminate a cohort explicitly retired as `writer_lock_superseded` only when that cohort is still PID/parent/name/start-identity matched and no live lease still owns it. Shared, ambiguous, PID-reused, or currently active cohorts stay protected. A Codex child thread already proven terminal/deleted by `state_5.sqlite` may still reclaim its exclusive cohort even when another lease inherited the same `host_thread_id`, as long as the `cohort_key` itself is exclusive. Tests inject the process controller and never kill live host processes.
13. Diagnostic `force` remains available for attended maintenance, but normal sub-agent cleanup does not depend on an operator choosing a PID or thread ID. Every termination path revalidates PID, parent PID, executable name, and process start time immediately before acting.
14. Bookkeeping TTL removes only dead or empty receipts. A still-live process cohort keeps its lease regardless of turn inactivity; ordinary steady-state GC never kills an unknown cohort merely because it is old or unleased.
15. When enabled part-way through an existing `codex.exe` generation, automatic mode may observe/adopt pre-existing cohorts but never drains unknown legacy cohorts by age. The old legacy/orphan cleanup path is restricted to explicit diagnostic `force` mode.
16. Reacquiring the current or previously retired cohort cancels retirement, preserving intentional upstream long-lived runtime reuse.
17. A new `codex.exe` generation naturally selects a new state shard instead of invalidating another live process's state.
18. Lifecycle state uses its own tiny sidecar lock; discovery, SQLite-read, lock, state-write, index-write, or termination failures are all fail-open.

## Lease identity and trust boundary

The shim prefers the host-owned `CODEX_THREAD_ID` when Codex supplies it. Current Codex Desktop Hook processes do not always expose that environment value, so lifecycle leasing may fall back to MemoryGuard's existing Hook `session_id` scope **only as a local lease identity**. The fallback is hashed before persistence.

For the conservative global terminal sweep, a raw Hook `session_id` may authorize the scan only after read-only `state_5.sqlite` lookup proves that the thread exists and its cwd matches the Hook payload `cwd`/project path. The MemoryGuard control-data directory is never substituted for the Codex thread cwd. If the payload omits cwd, the older workspace-bound check is retained. Root-scoped Codex state mutation keeps the stricter rule and still requires host-owned `CODEX_THREAD_ID`; the verified fallback is never treated as a general authority token.

## Hook trust and enablement reconciliation

Codex stores effective Hook trust and enablement in user `config.toml`, separately from the generated `hooks.json`. A correct handler can therefore exist on disk while Codex reports it as disabled or modified. `memoryguard.codex_hook_trust` repairs that split state through Codex's official app-server protocol:

1. `hooks/list` discovers the effective user-level handlers and their current hashes.
2. A candidate must come from the user's exact `~/.codex/hooks.json`, invoke `memoryguard.host_hooks`, carry `--managed-by memoryguard`, bind `--provider codex`, and expose the expected event/workspace/Agent/group arguments.
3. The complete seven-event set must contain exactly one MemoryGuard handler per event. No extra `SubagentStop` Hook is required: terminal sub-agent reconciliation runs from the already trusted `PostToolUse`/`Stop` lifecycle. Missing or duplicate events are reported and no configuration write occurs.
4. `config/read` supplies the current user-layer version. `config/batchWrite` then upserts only each matched handler's `enabled` and `trusted_hash` leaves using that optimistic-concurrency version.
5. A second `hooks/list` must show all seven handlers as enabled and `trusted`/`managed`; otherwise installation remains `configured_untrusted`.

Provider installation, `memoryguard hooks install/ensure`, and GUI/MCP host-control installation invoke this reconciliation explicitly. Read-only Hook status uses the same `hooks/list` view, so a present `hooks.json` can no longer masquerade as an operational Hook. Unrelated user, project, plugin, and managed hooks are never modified. The maintenance entrypoint `scripts/set_codex_memoryguard_hooks_enabled.py` delegates to this API and does not edit TOML text itself.

## Current editable-install activation bridge

The local installation already contains editable `.pth` entries pointing at this repository's `src` directory. `src/sitecustomize.py` uses that existing seam only for the exact managed Codex Hook command. While installed distribution metadata is still `agent-memguard==0.7.1`, it verifies the pinned hotfix wheel hash and places the clean `0.7.1.post17` wheel ahead of editable/source paths for that Hook process only.

Ordinary Python, the MemoryGuard MCP server, Claude/Cursor Hooks, and unrelated providers are not redirected. Installing `0.7.1.post17` or any later MemoryGuard distribution makes the activation bridge inert automatically.

Official Codex/provider MCP install is a separate path: `prepare_provider_mcp_launch` refuses to keep an editable checkout as the MCP runtime. `MEMORYGUARD_RUNTIME_PYTHON` is honored when it points at an existing interpreter and is never overwritten. Otherwise it reuses `<project-or-MG-deployment>/.memoryguard/mcp-runtime/<source-key>` when that snapshot already matches the current packaged source (Python and package data such as GUI JS/icons; cache/bytecode ignored), or atomically builds a new venv there with `pip install --no-deps --upgrade <source>` (never `-e`, never in-place on a live snapshot). Installer subprocess scratch uses that deployment's `.memoryguard/cache` and is removed on exit; pip caching is disabled. A failed build leaves the previously selected runtime usable and does not rewrite provider config to a broken target. Runtime diagnostics report `install_kind` / `install_reason` only and never mutate the user install. Repository tests keep using `src`. After a snapshot is written, the host must restart its MemoryGuard MCP process so the live import set matches the snapshot.

## Transport repair and live acceptance

For a Codex configuration that returns `Transport closed` before requests enter MemoryGuard, run:

```text
python -X utf8 scripts/repair_codex_memoryguard_transport.py --touch --memory-id <memory-id>
```

The repair is atomic and keeps a bounded set of timestamped backups. It preserves the existing trusted MemoryGuard binding, forces the current Python interpreter plus UTF-8 stdio, enables the server, removes duplicate MemoryGuard sections, and performs `initialize`, `tools/list`, `memory_status`, and `memory_read` over a fresh stdio process. Its non-sensitive receipt is written to `hook-runtime/codex-mcp-transport-repair.json`.

The end-to-end verifier:

```text
python -X utf8 scripts/verify_codex_memoryguard_live.py --memory-id <memory-id>
```

starts a real Codex process with the user's configured MCP servers. It accepts only structured MCP tool-call records, rejects any `Transport closed` signal, and concurrently verifies the durable snapshot-delta/no-growth lifecycle receipt. The result is written to `hook-runtime/codex-memoryguard-live-acceptance.json` without storing the memory body.

## Compatibility with an upstream Codex fix

The shim detects behavior, not Codex version numbers. Once Codex correctly reclaims its own cohorts, retired cohorts disappear during the native grace period and MemoryGuard becomes an observer-only no-op. No MemoryGuard upgrade or hook change is required.

For an explicit A/B check or permanent disable after an upstream fix:

```text
MEMORYGUARD_CODEX_MCP_LIFECYCLE=off
```

`force` retains broad diagnostic cleanup authority. Normal automatic process termination is narrower: `writer_lock_superseded` leftovers after native grace, and Codex-owned terminal child-thread receipts, each only for one exclusive, revalidated cohort.

## Sub-agent UI reconciliation

Codex can leave an `open` row in `thread_spawn_edges` after a child history row is terminal, archived, or deleted. That stale edge makes the parent UI show a sub-agent as “处理中” even when no child is actually running. MemoryGuard repairs only the Codex index: terminal children are closed/archived, and a missing child row is treated as a terminal orphan only when it has no live or unknown open descendant. No missing thread row is recreated. The bounded global sweep runs at session start, user prompt, every parent `PostToolUse`, and stop, so a completed child or deleted history entry is reconciled on the next ordinary lifecycle event rather than waiting for a restart.

For top-level conversations, a normal `Stop` remains resumable and does not release the runtime. When a lease has previously stored a state-DB-verified `host_thread_id`, later lifecycle sweeps may reclaim that idle conversation only after the corresponding Codex thread row becomes archived or disappears. The currently executing root thread is always protected, and live rows, shared cohorts, ambiguous mappings, or leases without verified thread identity remain untouched.

## Safety boundary

A reclaimable cohort must be anchored by a direct `node_repl.exe` child of the current `codex.exe`. Direct Codex child roots in `ROOT_NAMES` (`node_repl.exe`, `node.exe`, `python.exe`, `cmd.exe`, `uvx.exe`) whose start times fall within the bounded spawn-wave window (`COHORT_WINDOW_MS`, 7000 ms) are associated with the nearest same-parent anchor. `pwsh.exe` is not an MCP root. Before each termination, PID, parent PID, executable name, and process start time are revalidated to prevent PID-reuse mistakes.

The shim never targets `codex.exe`, `ChatGPT.exe`, renderer/GPU processes, unrelated application processes, or arbitrary descendant processes discovered by name. Descendants are terminated only through the validated Codex-owned cohort root tree.

## Removal criteria

The module can remain installed indefinitely because healthy native cleanup makes it a no-op. It may be removed when all supported Codex Desktop builds have demonstrated native cleanup under the regression scenario and the `off` mode has shown no recurrence for a release window.

Regression coverage lives in:

- `tests/test_codex_mcp_lifecycle.py`
- `tests/test_codex_mcp_hook_integration.py`
