# Mandatory rule replacement and recovery

A replacement keeps the logical memory ID and replaces the body. It does not
concatenate the prior body. Native `agent` and `group` audiences have the same
identity across projects, providers and runtime roles; those owner/provenance
fields no longer create separate dedup domains. `agent_project` and `project`
audiences remain project-specific. Legacy rows without native audience metadata
retain their existing scope semantics.

When equivalent historical copies exist in the same audience and injection
policy, replacement retires their unlocked predecessors in the same transaction.
Conflicting rules, different audiences and locked rules are not silently removed.
Bootstrap follows explicit successor IDs and suppresses a canonical rule's memory
mirror by logical ID as well as source-mapping ID.

## Commit boundary

Native memory writes and updates validate the **final** mandatory package before
commit, using bootstrap's audience matching, canonical heads, deduplication,
sensitivity checks and `ContextBudget`. The old and replacement bodies are not
counted together. Shared updates also check affected peers' private packages.
The item-count threshold remains a warning; character, token and per-item limits
remain hard limits. No budget is raised and no rule body is truncated to pass.

Related replacements, supersession edges, revisions, evidence projection and
governance receipts use one attached SQLite transaction. An exception or budget
rejection rolls back the whole request, including its idempotency claim. A caller
can retry after correcting the cause. Concurrent writers serialize at SQLite's
write boundary before reading candidates.

SQLite WAL does not guarantee power-loss atomicity across attached database files.
Existing outbox/receipt reconciliation remains necessary after a machine crash;
the rollback guarantee above covers handled publication/validation failures.

## Recovery through MCP

The MCP interface and the host Hook have separate gates. While the host is
blocked by a mandatory overflow, only `memory_update` with `preview: true`
passes its tool gate; committing updates remain blocked. Complete the mutation
through the controlled governance UI, then run a real bound bootstrap to clear
the host gate. A synthetic test that commits directly through MCP validates the
interface transaction, not permission to commit through a blocked host.

The existing `memoryguard_memory_update` recovery lane stays callable while
bootstrap is blocked. It retains transport binding and owner checks, including
for admin callers. It cannot create records, restore deleted records, edit a
foreign owner's memory, or bypass the final mandatory budget. Canonical rule
governance and non-owner recovery still use the governed GUI flows.
During an active host Hook mandatory block, only `memoryguard_memory_update`
with `preview: true` passes the Hook; commit remains blocked and must be
completed through controlled GUI/governance. Ordinary tools unlock only after a
real successful bootstrap; an offline script that commits directly through MCP
verifies the interface/process path, not that a blocked host commit is allowed.
An active canonical rule source cannot be edited via its memory mirror: the
update returns `canonical_rule_governance_required` without changing either
record. This prevents a successful-looking recovery that leaves the injected
canonical body unchanged. Explicit canonical successor chains suppress both
the predecessor definition and its linked memory mirrors at bootstrap.

Read the affected records with `memoryguard_memory_read`, then use their current
`revision` values. An optional `expected_revision` also protects single updates.
For related replacements, provide `expected_revision` on **every** item, a
nonempty audit `reason`, and an `idempotency_key`. At most 20 records may be updated
in one request. Each record can change `body`, `kind`, `injection_policy`,
`priority`, or `audience`; lifecycle status is not accepted.

Example `memoryguard_memory_update` arguments (IDs and revisions are placeholders):

```json
{
  "memory_id": "rule-a",
  "expected_revision": 3,
  "body": "Replacement text for rule A.",
  "related_updates": [
    {
      "memory_id": "rule-b",
      "expected_revision": 5,
      "body": "Replacement text for rule B."
    }
  ],
  "reason": "Replace the two linked rules together.",
  "idempotency_key": "rule-replacement-001",
  "preview": true
}
```

`preview: true` exercises the real publication and budget checks, then rolls back
all writes and returns `committed: false` with no durable receipt. Send the same
request with `preview: false` to apply it. Stale revisions or any failed item
leave every record unchanged. A successful retry with the same key returns the
existing batch receipt.

After successful repair, call `memoryguard_context_bootstrap` through MCP again
and let the host's normal post-tool handling clear the failed gate. Preview or
update success alone does not clear a blocked session. Ordinary shell/file tools
remain blocked until bootstrap succeeds. Do not edit databases, Hook state,
limits or native-memory files to recover.
PostToolUse clears a previous mandatory failure only after receiving a complete,
successful V2 `memoryguard_context_bootstrap` packet for the bound agent. A bare
transport success, an incomplete packet, a foreign agent packet, or a blocked
inner packet cannot clear the gate. The successful packet replaces stale rule
IDs and match receipts along with the failure flags.

## Runtime deployment

Repository changes do not update an existing `mcp-runtime/<source-key>` snapshot.
Build a new immutable snapshot with `prepare_provider_mcp_launch(mutate=True)`
and point both the existing MCP configuration and managed Hook commands at its
Python interpreter, preserving transport bindings and Hook trust. Keep an
explicitly disabled MCP disabled until the user reconnects. Do not verify a
disabled production connection by starting a substitute process against its
memory databases.

Native memory/evidence store construction validates existing schemas without
reinitializing them under read-only schema leases. This avoids Windows WAL
read-only/write handle conflicts during concurrent process startup. Private
preflight snapshots copy the main database and WAL; SQLite rebuilds their SHM
index locally rather than copying another process's locked SHM file.
