<h1 align="center">MemoryGuard</h1>

<!-- mcp-name: io.github.MerakOsiris/memoryguard -->

<p align="center">
  <strong>Governed shared memory for coding agents.</strong><br />
  Local-first MCP memory with automatic organization, scoped rules, evidence, and rollback.
</p>

<p align="center">
  <a href="https://pypi.org/project/agent-memguard/"><img src="https://img.shields.io/pypi/v/agent-memguard.svg?label=PyPI" alt="PyPI version" /></a>
  <a href="https://github.com/irisxc4/memoryguard/actions/workflows/ci.yml"><img src="https://github.com/irisxc4/memoryguard/actions/workflows/ci.yml/badge.svg" alt="CI status" /></a>
  <a href="https://github.com/irisxc4/memoryguard/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10 or newer" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-2ea44f" alt="MIT license" /></a>
  <a href="README.zh-CN.md">中文文档</a>
</p>

> Let agents write without turning shared memory into an unreviewed pile.
> MemoryGuard organizes each write, preserves the evidence behind changes, and
> keeps governance decisions reversible.
>
> **No account. No remote server. No remote telemetry. Local-only usage telemetry
> is optional and stores bounded, privacy-preserving aggregates locally.**

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#upgrade">Upgrade</a> ·
  <a href="#knowledge-library">Knowledge Library</a> ·
  <a href="#system-architecture">Architecture</a> ·
  <a href="#supported-hosts">Supported hosts</a> ·
  <a href="#privacy-and-safety">Privacy and safety</a>
</p>

<p align="center">
  <img src="docs/assets/neuron-graph-live.gif" alt="Animated MemoryGuard neuron graph with governed memory categories and signals moving through the local projection" width="1120" />
</p>

<p align="center">
  <sub>A synthetic governed projection: signals move through memory categories while raw conversation text remains outside the graph.</sub>
</p>

## What's New in v0.7.13

v0.7.13 strengthens mandatory-rule replacement and recovery, improves
audience-aware identity handling, and improves conflict resolution:

- **Safe mandatory-rule replacement:** Replacements validate the final
  mandatory package against audience matching, canonical rules, deduplication,
  sensitivity checks, and publication budgets. Equivalent unlocked predecessors
  retire in the same transaction; failed validation rolls back the complete
  update. See the [replacement and recovery details](docs/mandatory-rule-replacement.md).
- **Native Agent/group audience matching:** Matching and deduplication no longer
  split the same native audience solely by provider or runtime role. Distinct
  Agent identities remain separate, and project-scoped audiences retain their
  project boundaries. Provider repair uses the verified identity for its target
  provider.
- **Readable, atomic conflict resolution:** Conflict views preserve readable
  peer information, and resolution keeps peer groups intact while applying the
  selected changes atomically. Ambiguous conflicts remain unresolved.
- **Cursor Hook protection:** Cursor MemoryGuard Hooks now use a 30-second
  timeout, up from 15 seconds, with `failClosed: true`.

v0.7.14 only corrects release metadata for the GitHub repository rename; it does not change runtime behavior. See the [v0.7.14 release note](docs/releases/v0.7.14.md).

See the [v0.7.13 release note](docs/releases/v0.7.13.md) and
[release history](CHANGELOG.md).

Earlier release details are kept in the [Changelog](CHANGELOG.md) and
[GitHub release records](https://github.com/irisxc4/memoryguard/releases).

### Token evidence and demo

Usage events distinguish `measured_cached_input` from
`measured_cache_write_input`. `measured_cache_coverage.cache_read` and
`cache_write` report `complete`, `partial`, or `unavailable`; measured zero
remains `0`, while missing provider data remains `None`/`unavailable`.
Character-based estimates remain explicitly labelled
`estimated mg_deterministic_unit`, never provider tokens.

Run the benchmark only against an authorized local workspace:

```powershell
python scripts/benchmark_usage_telemetry.py --workspace . --window-days 7 --sync
```

Read [the benchmark guide](docs/benchmarks/README.md) for measured,
estimated, derived, and unsupported semantics. Use [the demo recording
checklist](docs/benchmarks/demo-script.md) for a sanitized walkthrough. The
repository's synthetic graph artwork is not a live product capture; it is not
evidence of usage or savings.

## Major V2 refactor in v0.6.0

v0.6.0 was a production data-plane refactor, not a storage-only upgrade:

- **Authoritative V2 domains:** Memory, Rules, Evidence, Content, Runtime, Projection, Assets, CodeGraph, Skills, and System state are separated into explicit SQLite domains with governed boundaries.
- **Explicit cutover:** `V1_ACTIVE → V2_BUILDING → V2_READY → V2_ACTIVE` is fail-closed; V2 never silently falls back to legacy stores or dual-writes after READY/ACTIVE.
- **Lossless migration:** frozen-source preparation uses coherent SQLite online backups, validates source/target evidence, rechecks live-source drift, and preserves V1 data plus migration backups for rollback.
- **Native routing:** MCP, CLI, GUI, and Hook surfaces are classified explicitly; the release closed the 233-surface cutover with 138 implemented routes, 95 retired routes, and zero neutral/blocker routes.
- **Governed intelligence:** Rule lifecycle and RuleMerge, extraction/enrichment, External MCP import, provider control-plane, conversation history, Knowledge Library, and GUI governance all use the V2 evidence and decision paths.
- **Operational evidence:** Reference Audit, per-domain SQLite health, guarded maintenance, rollback evidence, and safe unbound diagnostics are part of readiness and operations.

## Why MemoryGuard

Persistent memory solves storage. It does not solve governance.

When several coding agents write into the same context, records become
duplicated, stale, contradictory, over-broad, or unsafe to reuse. MemoryGuard
sits between coding agents and their shared memory to keep that context usable.

| Without governance | With MemoryGuard |
|---|---|
| Notes accumulate without a canonical state | Writes are classified, deduplicated, superseded, or surfaced as conflicts |
| A correction silently destroys the old value | Evidence and supersede chains preserve what changed and why |
| Tokens and credentials can remain active | Sensitive-looking content is quarantined from active memory |
| Every write needs manual approval | Agents write normally; people review exceptions and outcomes |
| Raw chat logs leak into future context | Conversation history remains a separate, explicitly read evidence archive |

## System architecture

```mermaid
%%{init: {"theme":"base","themeVariables":{"background":"#071521","fontFamily":"Arial, sans-serif","fontSize":"14px","primaryTextColor":"#EEF4F8","lineColor":"#557287","edgeLabelBackground":"#071521","clusterBkg":"#0A1A29","clusterBorder":"#27445A"},"flowchart":{"htmlLabels":true,"curve":"basis","nodeSpacing":32,"rankSpacing":48,"padding":14}}}%%
flowchart TB
    Hosts["CODING-AGENT HOSTS<br/>Claude Code · Codex · Cursor · TRAE&nbsp;&nbsp;&nbsp;&nbsp;"]:::host
    Gateway["LOCAL INTEGRATION<br/>MCP stdio · redirect rules · lifecycle hooks&nbsp;&nbsp;&nbsp;&nbsp;"]:::gateway

    subgraph Core["GOVERNANCE CORE&nbsp;&nbsp;&nbsp;&nbsp;"]
        direction LR
        Identity["TRUST<br/>identity · scope&nbsp;&nbsp;&nbsp;&nbsp;"]:::core
        MemoryAPI["MEMORY<br/>governed I/O&nbsp;&nbsp;&nbsp;&nbsp;"]:::active
        Rules["RULES<br/>scope · assignment&nbsp;&nbsp;&nbsp;&nbsp;"]:::rule
        HistoryAPI["HISTORY<br/>search · timeline&nbsp;&nbsp;&nbsp;&nbsp;"]:::history
        Security["SAFETY<br/>validate · quarantine&nbsp;&nbsp;&nbsp;&nbsp;"]:::danger

        Identity --> MemoryAPI
        Identity --> Rules
        Identity --> HistoryAPI
        MemoryAPI --> Security
    end

    subgraph Stores["LOCAL GOVERNED STORES&nbsp;&nbsp;&nbsp;&nbsp;"]
        direction LR
        SharedDB[("V2 DOMAIN STORES<br/>Memory · Rules · Evidence · Content&nbsp;&nbsp;&nbsp;&nbsp;")]:::store
        HistoryDB[("HISTORY STORE<br/>isolated conversations&nbsp;&nbsp;&nbsp;&nbsp;")]:::historyStore
        AuditDB[("RECOVERY STORE<br/>versions · receipts · backups&nbsp;&nbsp;&nbsp;&nbsp;")]:::store
    end

    Bootstrap["BOUNDED CONTEXT BOOTSTRAP<br/>mandatory rule pack · relevant recall&nbsp;&nbsp;&nbsp;&nbsp;"]:::bootstrap
    Control["HUMAN CONTROL<br/>CLI · desktop governance console&nbsp;&nbsp;&nbsp;&nbsp;"]:::surface

    Hosts --> Gateway --> Identity
    MemoryAPI --> SharedDB
    Rules --> SharedDB
    HistoryAPI --> HistoryDB
    Security --> AuditDB
    SharedDB --> Bootstrap
    Control --> Identity

    classDef host fill:#12243A,stroke:#38D5C8,color:#EEF4F8,stroke-width:1.4px;
    classDef gateway fill:#0D3338,stroke:#38D5C8,color:#EEF4F8,stroke-width:2.4px;
    classDef core fill:#12243A,stroke:#557287,color:#EEF4F8,stroke-width:1.4px;
    classDef active fill:#0D383A,stroke:#38D5C8,color:#EEF4F8,stroke-width:2px;
    classDef rule fill:#3B2C18,stroke:#F3B562,color:#EEF4F8,stroke-width:1.8px;
    classDef history fill:#102F45,stroke:#73C7F5,color:#EEF4F8,stroke-width:1.8px;
    classDef danger fill:#3A2028,stroke:#EA6A6A,color:#EEF4F8,stroke-width:1.8px;
    classDef bootstrap fill:#EEF4F8,stroke:#38D5C8,color:#071521,stroke-width:2.4px;
    classDef store fill:#0B1624,stroke:#7F96A8,color:#EEF4F8,stroke-width:1.4px;
    classDef historyStore fill:#102436,stroke:#73C7F5,color:#EEF4F8,stroke-width:1.4px;
    classDef surface fill:#EEF4F8,stroke:#38D5C8,color:#071521,stroke-width:2px;

    style Core fill:#081827,stroke:#27445A,stroke-width:1px,color:#EEF4F8
    style Stores fill:#081827,stroke:#27445A,stroke-width:1px,color:#EEF4F8
    linkStyle default stroke:#557287,stroke-width:1.4px;
```

## Quick start

### MCP Registry metadata

This package exposes a local stdio MCP server as `io.github.MerakOsiris/memoryguard`.
Registry metadata is kept in [`server.json`](server.json), and the marker above
ships with the PyPI package README. Releases are published through GitHub OIDC
to PyPI and the official MCP Registry. Verify the current package version and
the Registry entry's active/latest state through their live public records.

### 1. Install

```bash
python -m pip install agent-memguard
```

For the desktop governance console:

```bash
python -m pip install "agent-memguard[gui]"
```

### 2. Authorize the current project

```bash
memoryguard source add .
```

### 3. Connect or repair your coding agent

Global provider configuration is rebuilt from the real binding in the canonical user data home. The command is idempotent and removes superseded MemoryGuard project-level overrides after a successful global takeover.

```bash
# Repair one provider
memoryguard provider repair claude
memoryguard provider repair codex
memoryguard provider repair cursor
memoryguard provider repair trae

# Repair every detected provider
memoryguard provider repair all
```

Restart the host after installation, then verify the integration:

```bash
memoryguard doctor
memoryguard mcp-status
memoryguard hooks status --provider all
```

Launch the desktop console:

```bash
memoryguard gui
```

`memoryguard-gui .` remains available for desktop shortcuts. A bare
`memoryguard gui` always opens the canonical user-level control directory
(default `%LOCALAPPDATA%\MemoryGuard` on Windows), so running it from a project
or from `C:\Windows\System32` cannot silently switch databases.
`MEMORYGUARD_WORKSPACE` is an explicit operator override; an explicit
`memoryguard gui <project-path>` or `memoryguard gui --workspace <project-path>`
selects a specific workspace.
It does not remember a previously selected project or open a folder picker.
On Windows, `memoryguard gui` detaches the native window from the terminal, so
closing PowerShell does not close the GUI.

Provider-specific setup and behavior:

- [Claude Code installation](docs/install-claude-code.md)
- [Codex installation](docs/install-codex.md)
- [Cursor installation](docs/install-cursor.md)

### Stable Codex / Router binding

Codex/Router binds MemoryGuard to the stable local Codex program and control
installation. An account profile is an endpoint/alias, not a new memory owner:
switching profiles automatically discovers or repairs the profile and reuses the
verified Agent binding and active group. Request identity remains fail-closed;
this does not share records across machines or with arbitrary accounts.

## Upgrade

MemoryGuard currently upgrades through Python's package manager:

```bash
python -m pip install --upgrade agent-memguard
memoryguard --version
memoryguard doctor
```

If you installed the GUI extra, keep it during the upgrade:

```bash
python -m pip install --upgrade "agent-memguard[gui]"
```

There is no package self-update command. The package manager is the
authoritative package-upgrade path; `memoryguard upgrade` below is the explicit
workspace migration flow, not a package updater.

### Upgrade an existing V1 data home

Upgrade the package, then run the verified migration. No workspace, data-home,
apply, or confirmation arguments are required for the normal user-level data
home:

```bash
python -m pip install --upgrade agent-memguard
memoryguard --version                    # confirms installed version
memoryguard upgrade
memoryguard doctor
```

The command prepares V2, validates the frozen and live source evidence,
migrates Agent/Group control, activates only after all gates pass, and removes
only the backup batch belonging to that successful migration. Re-running it on
`V2_ACTIVE` is idempotent. For a zero-write report, use:

```bash
memoryguard upgrade --preview
```

Advanced explicit workspace/data-home options remain available for operators
managing an isolated installation. A failed gate stays non-active and preserves
its evidence; successful activation does not keep a redundant migration backup.

### Existing pre-V2 workspaces: explicit V2 cutover

v0.6.0 never auto-activates an existing workspace. Upgrade the package first,
then use the packaged operator CLI:

```bash
# Read-only manifest status
memoryguard-v2 status -w .

# Build a frozen-source V2 shadow and stop at V2_READY
memoryguard-v2 prepare -w . --apply

# Activate only after the prepare result is V2_READY / ready=true
memoryguard-v2 activate -w . --confirm V2_ACTIVE
```

The prepare step uses coherent SQLite online backups, preserves V1 and
`migration-backups`, and rechecks live-source drift before READY. Activation
performs another fresh drift check before changing the manifest. Do not delete
legacy V1 data or migration backups as part of the upgrade.

## Knowledge Library

The desktop console can turn a selected folder or file set into one governed
local knowledge library. Source files remain where they are; MemoryGuard stores
the searchable index in its user data home instead of copying a runtime
database into every source project. Knowledge metadata never becomes a second
source-body store.

| Capability | Current behavior |
|---|---|
| File/folder ingestion | Add a folder as a book or selected files as documents |
| Structure | Parse documents, preserve chapter/section context, and create traceable chunks |
| Retrieval | Full-text search, optional embeddings, and a layered knowledge graph |
| Natural synchronization | Re-ingest changed files; a partial or failed scan does not silently remove previously indexed content |
| Lifecycle | Move a book to the library trash, restore it, or explicitly purge its recovery snapshot |
| Memory candidates | Preview evidence-backed candidates before accepting them into governed long-term memory |

Open the desktop console and choose **Knowledge Library**. Remote embedding or
model-backed indexing is opt-in and requires explicit authorization; local
full-text retrieval remains available without sending source text to a remote
provider. Background imports, re-ingests, and smart rebuilds have durable task
receipts: retrying the same request reuses its task, while reusing that key for
a different request is rejected. A live task for a different request reports
busy rather than claiming that work was accepted.

### CodeGraph refresh

The first CodeGraph build is an explicit, confirmed full build. After a scope
has been built, each successful trusted file write can trigger an incremental
refresh for that scope, subject to strict source-path and active-binding
validation. Unchanged content hashes are a no-op; deleted files are retired;
the next context receives one bounded `affected` receipt. MemoryGuard does not
run a daemon or watcher for this path and does not infer paths from shell or
free-form text. A projectless MCP caller first builds an already-bound directory
source, then passes its `codegraph_source_id` to select that exact scope for
query, status, update, and graph reads.

### Desktop console surfaces

The GUI has eight visible navigation entries: seven governance pages plus a
separate Token usage-and-savings view:

1. Governance Overview
2. Data Sources & Agents
3. Memory Core
4. CodeGraph
5. Rules & Habits
6. Conversation History
7. Risk Signals & Governance Console
8. Token Usage & Savings (separate from the seven governance pages)

Agent lists use readable program/provider names; the underlying ID remains
available in the detail view. Empty data is shown as an explicit empty state.

## Write and governance lifecycle

```mermaid
%%{init: {"theme":"base","themeVariables":{"background":"#071521","fontFamily":"Arial, sans-serif","fontSize":"14px","primaryTextColor":"#EEF4F8","lineColor":"#557287","edgeLabelBackground":"#071521","clusterBkg":"#0A1A29","clusterBorder":"#27445A"},"flowchart":{"htmlLabels":true,"curve":"basis","nodeSpacing":30,"rankSpacing":42,"padding":14}}}%%
flowchart TD
    subgraph Intake["01 · INTAKE&nbsp;&nbsp;&nbsp;&nbsp;"]
        direction LR
        Write(["Memory write&nbsp;&nbsp;&nbsp;&nbsp;"]):::entry
        Scope["Resolve identity<br/>scope · audience&nbsp;&nbsp;&nbsp;&nbsp;"]:::core
        Validate{"Authorized?&nbsp;&nbsp;&nbsp;&nbsp;"}:::decision
        Reject["Reject<br/>no persistence&nbsp;&nbsp;&nbsp;&nbsp;"]:::danger
        Write --> Scope --> Validate
        Validate -- NO --> Reject
    end

    subgraph Organize["02 · ORGANIZE&nbsp;&nbsp;&nbsp;&nbsp;"]
        direction TB
        Secret{"Sensitive?&nbsp;&nbsp;&nbsp;&nbsp;"}:::decision
        Quarantine["Quarantine<br/>outside active set&nbsp;&nbsp;&nbsp;&nbsp;"]:::danger
        Compare["Classify · compare<br/>governed records&nbsp;&nbsp;&nbsp;&nbsp;"]:::active
        Relation{"Relationship&nbsp;&nbsp;&nbsp;&nbsp;"}:::decision
        New["NEW<br/>create active record&nbsp;&nbsp;&nbsp;&nbsp;"]:::result
        Duplicate["DUPLICATE<br/>merge provenance&nbsp;&nbsp;&nbsp;&nbsp;"]:::result
        Correction["CORRECTION<br/>supersede old record&nbsp;&nbsp;&nbsp;&nbsp;"]:::rule
        Conflict["CONFLICT<br/>preserve both sides&nbsp;&nbsp;&nbsp;&nbsp;"]:::danger

        Secret -- YES --> Quarantine
        Secret -- NO --> Compare --> Relation
        Relation --> New
        Relation --> Duplicate
        Relation --> Correction
        Relation --> Conflict
    end

    subgraph Govern["03 · GOVERN&nbsp;&nbsp;&nbsp;&nbsp;"]
        direction LR
        Receipt[("Evidence event<br/>version receipt&nbsp;&nbsp;&nbsp;&nbsp;")]:::store
        Review["CLI or desktop review&nbsp;&nbsp;&nbsp;&nbsp;"]:::surface
        Action["Correct · merge<br/>restore · delete&nbsp;&nbsp;&nbsp;&nbsp;"]:::rule
        Snapshot["Reversible<br/>snapshot&nbsp;&nbsp;&nbsp;&nbsp;"]:::active
        Receipt --> Review --> Action --> Snapshot
    end

    Validate -- YES --> Secret
    Quarantine --> Receipt
    New --> Receipt
    Duplicate --> Receipt
    Correction --> Receipt
    Conflict --> Receipt

    classDef entry fill:#EEF4F8,stroke:#38D5C8,color:#071521,stroke-width:2.4px;
    classDef core fill:#12243A,stroke:#557287,color:#EEF4F8,stroke-width:1.5px;
    classDef decision fill:#0D3338,stroke:#38D5C8,color:#EEF4F8,stroke-width:2px;
    classDef active fill:#0D383A,stroke:#38D5C8,color:#EEF4F8,stroke-width:2px;
    classDef result fill:#12243A,stroke:#38D5C8,color:#EEF4F8,stroke-width:1.6px;
    classDef rule fill:#3B2C18,stroke:#F3B562,color:#EEF4F8,stroke-width:1.8px;
    classDef danger fill:#3A2028,stroke:#EA6A6A,color:#EEF4F8,stroke-width:1.8px;
    classDef store fill:#0B1624,stroke:#7F96A8,color:#EEF4F8,stroke-width:1.4px;
    classDef surface fill:#EEF4F8,stroke:#38D5C8,color:#071521,stroke-width:2px;

    style Intake fill:#081827,stroke:#27445A,stroke-width:1px,color:#EEF4F8
    style Organize fill:#081827,stroke:#27445A,stroke-width:1px,color:#EEF4F8
    style Govern fill:#081827,stroke:#27445A,stroke-width:1px,color:#EEF4F8
    linkStyle default stroke:#557287,stroke-width:1.4px;
```

The console is not an approval queue. Agents keep moving. MemoryGuard records
the outcome and exposes the evidence needed to correct it later.

## What you can govern

| Signal | Governance action |
|---|---|
| Duplicate or stale memory | Inspect the canonical record and supersede chain; restore an earlier version when needed |
| Conflicting memories | Keep both visible until the conflict is resolved deliberately |
| Secrets, tokens, or credentials | Quarantine the record so it cannot enter active shared memory |
| Incorrect automatic organization | Correct, merge, lock, restore, or roll back with evidence |
| Multiple coding agents | Bind agents to one shared group while preserving source identity and scope |
| Mandatory rules | Assign rules to an Agent, project, provider, runtime role, or shared group |

## Rules and history stay separate

MemoryGuard deliberately keeps governed long-term memory and raw conversation
history on different paths.

| Surface | Purpose | Context behavior |
|---|---|---|
| **Rules and habits** | Preferences, procedures, corrections, facts, projects, and scoped mandatory rules | Mandatory rules use an independent char/token budget after scope, exclude, conflict, and semantic dedup. Effective count above 20 is a health warning, not a hard block; storage is not capped by count. Sensitive, corrupt, per-item oversize, and aggregate overflow still fail closed with no silent truncation. Ordinary records are recalled when relevant |
| **Conversation history** | Local raw-evidence archive with owner and shared-group access controls | Never enters bootstrap automatically; raw text is read only through explicit history tools |
| **Neuron graph** | Navigation and governance over memory, rules, projects, agents, and sessions | History nodes contain safe metadata and summaries, not raw chat content |

History retrieval is progressive: search results, then a bounded timeline, then
an explicitly selected turn or session. Extracting from history creates a
preview first; it does not silently write a long-term memory.

```mermaid
%%{init: {"theme":"base","themeVariables":{"background":"#071521","fontFamily":"Arial, sans-serif","fontSize":"14px","primaryTextColor":"#EEF4F8","lineColor":"#557287","edgeLabelBackground":"#071521","clusterBkg":"#0A1A29","clusterBorder":"#27445A"},"flowchart":{"htmlLabels":true,"curve":"basis","nodeSpacing":30,"rankSpacing":42,"padding":14}}}%%
flowchart LR
    subgraph HistoryPath["CONVERSATION EVIDENCE&nbsp;&nbsp;&nbsp;&nbsp;"]
        direction TB
        Archive[("Raw local history&nbsp;&nbsp;&nbsp;&nbsp;")]:::historyStore
        Search["Search summaries&nbsp;&nbsp;&nbsp;&nbsp;"]:::history
        Timeline["Bounded timeline&nbsp;&nbsp;&nbsp;&nbsp;"]:::history
        Read["Explicit turn or session&nbsp;&nbsp;&nbsp;&nbsp;"]:::history
        Preview["Evidence-backed<br/>extraction preview&nbsp;&nbsp;&nbsp;&nbsp;"]:::history
        Confirm["Explicit acceptance&nbsp;&nbsp;&nbsp;&nbsp;"]:::surface
        Isolation["NO AUTOMATIC<br/>BOOTSTRAP PATH&nbsp;&nbsp;&nbsp;&nbsp;"]:::barrier

        Archive --> Search --> Timeline --> Read --> Preview --> Confirm
        Archive -.-> Isolation
    end

    subgraph GovernedMemory["GOVERNED LONG-TERM MEMORY&nbsp;&nbsp;&nbsp;&nbsp;"]
        direction TB
        Mandatory["Scoped mandatory rules&nbsp;&nbsp;&nbsp;&nbsp;"]:::rule
        Assignments["Agent · project<br/>role · group scope&nbsp;&nbsp;&nbsp;&nbsp;"]:::core
        RulePack["Mandatory-rule<br/>budget&nbsp;&nbsp;&nbsp;&nbsp;"]:::budget
        Ordinary["Facts · preferences<br/>projects · procedures&nbsp;&nbsp;&nbsp;&nbsp;"]:::memory
        Recall["Task-relevant<br/>recall budget&nbsp;&nbsp;&nbsp;&nbsp;"]:::budget
        Context["BOUNDED CONTEXT PACKET&nbsp;&nbsp;&nbsp;&nbsp;"]:::context

        Mandatory --> Assignments --> RulePack --> Context
        Ordinary --> Recall --> Context
    end

    HistoryPath ==>|GOVERNED WRITE&nbsp;&nbsp;&nbsp;&nbsp;| GovernedMemory

    classDef rule fill:#3B2C18,stroke:#F3B562,color:#EEF4F8,stroke-width:1.8px;
    classDef core fill:#12243A,stroke:#557287,color:#EEF4F8,stroke-width:1.4px;
    classDef memory fill:#0D383A,stroke:#38D5C8,color:#EEF4F8,stroke-width:1.8px;
    classDef budget fill:#12243A,stroke:#38D5C8,color:#EEF4F8,stroke-width:1.6px;
    classDef context fill:#EEF4F8,stroke:#38D5C8,color:#071521,stroke-width:2.4px;
    classDef history fill:#102F45,stroke:#73C7F5,color:#EEF4F8,stroke-width:1.6px;
    classDef historyStore fill:#102436,stroke:#73C7F5,color:#EEF4F8,stroke-width:1.6px;
    classDef surface fill:#EEF4F8,stroke:#73C7F5,color:#071521,stroke-width:2px;
    classDef barrier fill:#3A2028,stroke:#EA6A6A,color:#EEF4F8,stroke-width:2px;

    style GovernedMemory fill:#081827,stroke:#27445A,stroke-width:1px,color:#EEF4F8
    style HistoryPath fill:#081827,stroke:#27445A,stroke-width:1px,color:#EEF4F8
    linkStyle default stroke:#557287,stroke-width:1.4px;
```

## Supported hosts

| Host | Integration | Current boundary |
|---|---|---|
| Claude Code | Global MCP binding, redirect rules, user-level lifecycle Hook | Verified takeover path |
| Codex | Global MCP binding, redirect rules, user-level lifecycle Hook | Verified takeover path |
| Cursor | Global MCP binding, redirect rules, user-level lifecycle Hook | Verified takeover path |
| TRAE | MCP binding and redirect rules | No verified Hook seam; reported as a fallback instead of full takeover |

Provider status is reported honestly as redirected, observed, operational, or
unsupported. MemoryGuard does not claim it can disable every host's native
memory when the host exposes no reliable integration point.

## Architecture

| Layer | Responsibility |
|---|---|
| **Evidence & Content** | Authorized sources, immutable evidence, content-addressed blobs/occurrences, source manifests, and conversation archives |
| **Memory & Rules** | Scoped memory atoms, revisions, bindings, rule definitions, decisions, evidence links, and compensating governance operations |
| **Runtime & Projection** | Bounded working context, scenario/profile projections, CodeGraph, Assets, and Skills metadata |
| **Cutover & Governance** | Four-state manifest, native MCP/CLI/GUI/Hook routing, Reference Audit, maintenance, provider adapters, and rollback evidence |

V2 uses separate authoritative SQLite domains rather than one shared-memory
database. The runtime reads and writes V2 only after the manifest reaches
`V2_ACTIVE`; `V2_BUILDING` and `V2_READY` never silently fall back or dual-write.
Evidence remains traceable without being treated as automatically trusted memory.

## Privacy and safety

- MemoryGuard runs as a local MCP stdio server.
- All governed data stays local unless you explicitly authorize a remote model
  or embedding operation. Optional usage telemetry is local-only: its measured
  host token events and deterministic conversion events are stored under
  `.memoryguard/usage_telemetry.sqlite`; it does not upload data. Token savings
  are estimates based on MemoryGuard deterministic units, not a provider billing
  statement. Hosts without token reporting remain unsupported in the measured
  columns.
- The Knowledge Library database uses `MEMORYGUARD_HOME` or the platform user
  data directory, so a selected source folder does not receive its own
  knowledge database.
- V2 authoritative workspace state is separated under `.memoryguard/` into
  explicit Memory, Rules, Evidence, Content, Runtime, Projection, Assets,
  CodeGraph, Skills, and System domains; History, Source, Binding, and Group
  control are V2-native surfaces. Legacy V1 artifacts are preserved as local
  rollback/audit evidence after cutover and are no longer the active V2 runtime
  write path; only `memoryguard.migration` may read them.
- Source scanning is read-only by default.
- Mutating governance paths use validation, explicit scope, provenance, and
  reversible state.
- Quarantined records stay outside active shared memory.
- Raw conversation history is never injected into bootstrap automatically.
- Shared-group history access follows current active membership and does not
  grant deletion rights over another Agent's source.

## CLI

The installed `memoryguard` command exposes these top-level operations:

| Command | Purpose |
|---|---|
| `audit [path]` | Run a read-only audit and generate a report |
| `open [path]` | Open the latest interactive report |
| `explain <finding_id>` | Explain evidence and risk for a finding |
| `source <action>` | List, add, remove, or preview authorized sources |
| `scan` | Scan authorized sources and build the coverage ledger |
| `doctor` | Diagnose V2 manifest, domain availability, and native coverage |
| `mcp-status` | Inspect V2 MCP/backend health; tenant counts require a bound Agent scope |
| `hooks <action>` | Install, inspect, pause, repair, or remove host Hooks |
| `provider <action>` | Inspect or repair global provider integrations |
| `storage audit|report` | Run read-only V2 Reference Audit and per-domain SQLite health reports |
| `storage sweep|compact` | Run guarded V2 maintenance; physical changes require ACTIVE state, lease, generation, and safety proofs |
| `groups <action>` | Inspect governed group state |
| `gui [path]` | Launch the interactive governance console |
| `desktop` | Launch the trusted desktop executor |

The old V1 `plan`, `apply`, `verify`, `undo`, `import`, and `gc` workflows may
remain parseable as explicit retired compatibility surfaces, but are not a V1
runtime path. Under `V2_ACTIVE` they return a stable retired result instead of
writing through a legacy store. Legacy data input is accepted only by the
explicit `memoryguard.migration` upgrade flow.

Run `memoryguard --help` or `memoryguard <command> --help` for the live command
reference.

## MCP API

The default MCP discovery surface is intentionally compact. New MCP clients
receive these eleven day-to-day tools through `tools/list`:

| Tool | Purpose |
|---|---|
| `memoryguard_context_bootstrap` | Load bounded mandatory rules and relevant memory context |
| `memoryguard_memory_search` | Search governed memories by query, lifecycle status, and bounded limit. `kind` is not an MCP search filter; semantic duplicate/conflict checks are separate advanced governance. |
| `memoryguard_memory_read` | Read one governed memory |
| `memoryguard_memory_write` | Write and organize a governed memory |
| `memoryguard_memory_update` | Update the body, kind, recall policy, or priority of one known memory. It does not change lifecycle status. |
| `memoryguard_memory_delete` | Soft-delete a governed memory |
| `memoryguard_memory_status` | Inspect shared-memory status |
| `memoryguard_audit` | Run a read-only local governance audit |
| `memoryguard_explain` | Explain one audit finding and its evidence |
| `memoryguard_capabilities` | Discover registered MCP operations and reviewed headless GUI operations with bounded pagination and optional on-demand JSON Schema |
| `memoryguard_invoke` | Invoke one discovered MCP or reviewed headless GUI operation; mutating targets require confirmation and an idempotency key |

Advanced governance remains available through the GUI and CLI: rule lifecycle,
bindings and shared groups, source scanning, CodeGraph, knowledge and history
review, provider controls, external MCP import, and maintenance operations.
Existing advanced MCP names remain callable for compatibility when an installed
client invokes an exact name, but they are not returned by the default
`tools/list`. This reduces discovery/schema overhead without removing those
governance capabilities.

`memoryguard_capabilities` is the discovery path for the broader compatibility
catalog. It supports exact operation lookup, English or Chinese query text,
domain filtering, and offset pagination; schemas are returned only when
`include_schema=true` is requested for the selected page. The catalog exposes
162 reviewed headless GUI business operations through `memoryguard_invoke`.
Eight GUI operations remain explicitly restricted by their existing authority:
desktop-only path/folder actions, desktop-admin CodeGraph selection/build, and
SafeBridge protocol actions.

### Bounded read responses

MemoryGuard minifies JSON text by default. A replayable read response is capped
at **24,000 UTF-8 bytes across the complete MCP envelope**, including every
`content` block and existing `structuredContent`. Small responses keep their
existing shape. An oversized read returns a compact receipt with `response_ref`
and required identifiers; it does not silently truncate the original result.

Fetch a page through the existing broker, after discovering
`memoryguard_response_read` with `memoryguard_capabilities`:

```json
{
  "operation": "memoryguard_response_read",
  "arguments": {
    "response_ref": "opaque-id",
    "fields": ["/data/memory_id"],
    "offset": 0,
    "limit": 3000
  }
}
```

Pages are UTF-8 JSON fragments with `next_offset`; concatenate them in order.
`limit` is 4–4096 bytes and offsets must be UTF-8 character boundaries. On a
single JSON text payload, `fields` selects business fields: use a top-level
name or an object-only JSON Pointer such as `/data/memory_id`. Multi-content
and non-JSON results reject field selection and remain available only as
whole-envelope pages.
Private references live only in the MCP process for at most five minutes: at
most 16 snapshots, each at most 512,000 bytes. They are bound to the exact
trusted session, principal, scope, and active binding revision. Each page
reruns the original **read** under current authorization and compares its
digest. A denial, changed output, binding/session change, or expired reference
returns a stable refusal such as `response_ref_access_denied`,
`response_ref_expired`, or `response_ref_result_changed`; cached old content
is never used to bypass the current read. Public capability metadata uses its
existing offset pagination. Writes and context bootstrap keep their existing
complete receipt/mandatory-rule contracts and cannot request response
pagination, so a page read never reruns a mutation. If an oversized read cannot
safely create a reference, its bounded receipt reports
`delivery.status="unavailable"` and `action="narrow_query"` rather than
promising the whole result can be retrieved.

Example discovery and invocation using the published schemas:

```json
{"operation":"memoryguard_task_list","include_schema":true,"limit":1}
```

```json
{"operation":"memoryguard_task_list","arguments":{"limit":20}}
```

For a mutating target, the invoke envelope must also carry
`"confirmed":true` and a non-empty `"idempotency_key"`; the broker forwards
those proofs to the target's existing permission and scope checks.

The underlying compatibility catalog also covers:

- governed memory read, search, write, update, delete, and status;
- bounded context bootstrap with mandatory-rule isolation;
- rule creation, feedback, merge governance, undo, and scope statistics;
- Agent binding and shared-group inspection;
- source scanning, graph projection, import previews, and build planning;
- external MCP discovery and import;
- document extraction previews and candidate acceptance;
- conversation-history search, timeline, explicit read, export, deletion, and
  extraction preview;
- provider installation and host-agent enrichment.

Use MCP `tools/list` for the compact default discovery set. Use
`memoryguard_capabilities` for the registered compatibility catalog and its
reviewed operation metadata.

## Project links

- [PyPI package](https://pypi.org/project/agent-memguard/)
- [GitHub releases](https://github.com/irisxc4/memoryguard/releases)
- [Changelog](CHANGELOG.md)
- [v0.7.13 release note](docs/releases/v0.7.13.md)
- [v0.7.12 release note](docs/releases/v0.7.12.md)
- [v0.7.11 release note](docs/releases/v0.7.11.md)
- [v0.7.9 release record](docs/releases/v0.7.9.md)
- [v0.7.8 release record](docs/releases/v0.7.8.md)
- [v0.7.7 release record](docs/releases/v0.7.7.md)
- [v0.7.6 release record](docs/releases/v0.7.6.md)
- [v0.7.5 release record](docs/releases/v0.7.5.md)
- [v0.7.4 release record](docs/releases/v0.7.4.md)
- [v0.7.3 release record](docs/releases/v0.7.3.md)
- [v0.7.2 release record](docs/releases/v0.7.2.md)
- [v0.7.1 release record](docs/releases/v0.7.1.md)
- [v0.7.0 release gate](docs/releases/v0.7.0.md)
- [Memory continuity and lossless storage spec](docs/memory-continuity-storage-spec-v1.md)
- [Privacy policy](PRIVACY.md)
- [Terms of use](TERMS.md)
- [Contributing guide](CONTRIBUTING.md)
- [Contributor License Agreement](CLA.md)
- [Issue tracker](https://github.com/irisxc4/memoryguard/issues)

## Roadmap

- **Release history:** v0.7.9 consolidates canonical governance, local-only token evidence, readable multi-agent governance, and public distribution through GitHub, PyPI, and the official MCP Registry. v0.7.8 records the preceding governance, telemetry, and Codex runtime work; v0.7.7 makes bare provider repair safe in a verified, uniquely bound control home and aligns installed Codex MCP/Hook repairs to the current interpreter while preserving Agent and shared-group identity. v0.7.6 makes Codex Hook/MCP runtime selection consistent through one immutable snapshot, shortens Hook state lock windows, and keeps bootstrap success/failure state honest with explicit mandatory-overflow fail-closed handling. Earlier release records retain the detailed v0.7.5 conflict-review, v0.7.4 canonical-governance, v0.7.3 shared-history, and v0.7.2 write/read and Codex lifecycle changes. The
  v0.7.1 V2-only migration and desktop lifecycle work remains documented as
  historical release context.
- **Acceptance boundary:** the Graphify evidence is the focused `3 / 3` result
  plus the real full-repository export/projection described above. It does not
  claim that upstream Graphify's full-repository test suite passed.
- **Next after release:** broader CodeGraph/Skills ingestion, more operator-friendly
  maintenance reports, and additional migration observability. Long-term records
  are not retired merely because they are old.
- **Later:** team and enterprise capabilities only after validated demand.

## Contributing

Issues and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md)
before submitting a change. Pull requests require agreement to the
[CLA](CLA.md).

## License

[MIT](LICENSE)
