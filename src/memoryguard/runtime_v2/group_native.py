"""V2-native Agent/group/scope control plane.

This module owns GUI group membership and scope preferences in system/manifest.db.
It deliberately never imports AgentBindingStore or SharedMemoryStore.  Membership
changes, receipts and system-domain outbox events commit in one SQLite
transaction.  Cross-domain operations use compensating writes and only record a
success receipt after their owning domain has committed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..content.store import ContentStore, stable_id
from ..agent_mapping import normalize_program_identity
from ..storage.database import execute_sql_script, open_database, open_database_snapshot
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import (
    GUI_CONTROL_SCHEMA as CONTROL_SCHEMA,
    GUI_CONTROL_SCHEMA_MARKER as CONTROL_SCHEMA_MARKER,
    GUI_CONTROL_SCHEMA_VERSION as CONTROL_SCHEMA_VERSION,
    initialize_database,
)
from ..storage.transaction import transaction



_AUX_TABLES = frozenset({
    "gui_control_schema_meta", "agent_group_bindings", "governance_scopes",
    "selection_manifests", "control_preferences", "group_operation_receipts",
    "group_outbox", "agent_lifecycle_marks", "agent_archives",
    "agent_cleanup_history",
})


class GroupControlError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "group_control_failed")
        super().__init__(self.code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(item) for item in parts).encode("utf-8")).hexdigest()


def personal_group_id(agent_instance_id: str) -> str:
    agent = str(agent_instance_id or "").strip()
    if not agent:
        raise GroupControlError("agent_instance_id_required")
    return "personal-" + _digest("personal-memory-group", agent)


def _group_kind(group_id: str) -> str:
    return "personal" if str(group_id).startswith("personal-") else "shared"


class SystemControlStore:
    """Additive system.db control schema with fail-closed marker preflight."""

    def __init__(self, workspace: str | Path, *, write: bool = False) -> None:
        self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace = self.layout.workspace
        self.db_path = self.layout.manifest_db
        if write:
            self.layout.ensure_dirs()
            initialize_database(self.db_path, "system", layout=self.layout)
            self._ensure_aux()

    def _preflight(self) -> str:
        if not self.db_path.is_file():
            return "missing"
        try:
            # A private SQLite snapshot preserves uncheckpointed WAL state but
            # guarantees that validation/close can never create or checkpoint
            # sidecars beside the live control-plane database.
            with open_database_snapshot(self.db_path) as conn:
                tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "schema_meta" not in tables:
                    raise GroupControlError("system_schema_marker_missing")
                base = conn.execute(
                    "SELECT version,marker FROM schema_meta WHERE domain='system'"
                ).fetchone()
                if base is None or int(base[0]) != 1 or str(base[1]) != "memoryguard-v2-phase1":
                    raise GroupControlError("system_schema_marker_invalid")
                if "gui_control_schema_meta" not in tables:
                    partial = sorted(tables & (_AUX_TABLES - {"gui_control_schema_meta"}))
                    if partial:
                        raise GroupControlError("gui_control_schema_partial")
                    return "missing"
                rows = conn.execute("SELECT key,value FROM gui_control_schema_meta ORDER BY key").fetchall()
                if len(rows) != 1 or str(rows[0][0]) != "version":
                    raise GroupControlError("gui_control_schema_marker_invalid")
                marker = str(rows[0][1])
                if marker != str(CONTROL_SCHEMA_VERSION):
                    raise GroupControlError("gui_control_schema_future" if marker.isdigit() and int(marker) > CONTROL_SCHEMA_VERSION else "gui_control_schema_unsupported")
                missing = sorted(_AUX_TABLES - tables)
                if missing:
                    raise GroupControlError("gui_control_schema_incomplete")
            return "current"
        except GroupControlError:
            raise
        except Exception as exc:
            raise GroupControlError("gui_control_schema_unavailable") from exc

    def _ensure_aux(self) -> None:
        state = self._preflight()
        if state == "current":
            return
        with open_database(self.db_path) as conn:
            with transaction(conn):
                execute_sql_script(conn, CONTROL_SCHEMA)
                conn.execute(
                    "INSERT INTO gui_control_schema_meta(key,value) VALUES('version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(CONTROL_SCHEMA_VERSION),),
                )

    def connection(self, *, write: bool = False):
        if write:
            self._ensure_aux()
            return open_database(self.db_path)
        if self._preflight() != "current":
            raise GroupControlError("gui_control_not_initialized")
        return open_database_snapshot(self.db_path)

    @staticmethod
    def _next_sequence(conn: Any) -> int:
        return int(conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM group_outbox").fetchone()[0])

    @staticmethod
    def _receipt(conn: Any, operation: str, key: str, request_digest: str) -> Mapping[str, Any] | None:
        row = conn.execute(
            "SELECT request_digest,result_json FROM group_operation_receipts WHERE operation=? AND idempotency_key=?",
            (str(operation), str(key)),
        ).fetchone()
        if row is None:
            return None
        if str(row[0]) != request_digest:
            raise GroupControlError("idempotency_key_reused")
        try:
            value = json.loads(str(row[1] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GroupControlError("control_receipt_corrupt") from exc
        if not isinstance(value, Mapping):
            raise GroupControlError("control_receipt_corrupt")
        return dict(value)

    def read_receipt(self, operation: str, key: str) -> dict[str, Any] | None:
        """Read one trusted control-plane receipt without mutating the system DB."""
        if not self.db_path.is_file() or self._preflight() != "current":
            return None
        with open_database_snapshot(self.db_path) as conn:
            row = conn.execute(
                "SELECT receipt_id,request_digest,result_json,created_at FROM group_operation_receipts "
                "WHERE operation=? AND idempotency_key=?",
                (str(operation), str(key)),
            ).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(str(row[2] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GroupControlError("control_receipt_corrupt") from exc
        if not isinstance(result, Mapping):
            raise GroupControlError("control_receipt_corrupt")
        return {
            "receipt_id": str(row[0]),
            "request_digest": str(row[1]),
            "result": dict(result),
            "created_at": str(row[3]),
        }

    def mutate(
        self,
        operation: str,
        key: str,
        request: Mapping[str, Any],
        apply: Callable[[Any], tuple[Mapping[str, Any], str]],
    ) -> dict[str, Any]:
        self._ensure_aux()
        request_digest = hashlib.sha256(_json(dict(request)).encode("utf-8")).hexdigest()
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                replay = self._receipt(conn, operation, key, request_digest)
                if replay is not None:
                    public = dict(replay)
                    public["replayed"] = True
                    if "changed" in public:
                        public["changed"] = False
                    if "created" in public:
                        public["created"] = False
                    return public
                result, aggregate = apply(conn)
                public = dict(result)
                event_id = "group-event-" + _digest(operation, key, request_digest)
                conn.execute(
                    "INSERT INTO group_outbox(event_id,sequence,event_type,aggregate_id,payload_json,status,attempts,created_at,projected_at) "
                    "VALUES(?,?,?,?,?,'projected',1,?,?)",
                    (event_id, self._next_sequence(conn), operation, str(aggregate or operation), _json({"receipt_ref": key}), now, now),
                )
                sequence_row = conn.execute(
                    "SELECT sequence FROM group_outbox WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if sequence_row is None:
                    raise GroupControlError("group_outbox_event_missing")
                sequence = int(sequence_row[0])
                conn.execute(
                    "UPDATE outbox_checkpoints SET last_sequence=?,updated_at=? "
                    "WHERE domain='system' AND last_sequence<?",
                    (sequence, now, sequence),
                )
                receipt_id = "group-receipt-" + _digest(operation, key)
                conn.execute(
                    "INSERT INTO group_operation_receipts(receipt_id,operation,idempotency_key,request_digest,result_json,created_at) VALUES(?,?,?,?,?,?)",
                    (receipt_id, operation, key, request_digest, _json(public), now),
                )
                public.setdefault("receipt", {"receipt_id": receipt_id, "event_id": event_id})
                return public

    def project_outbox(self) -> dict[str, int]:
        """Advance the system checkpoint only for already-projected receipts.

        Group-control mutations are committed synchronously, so their outbox
        rows are created as ``projected``.  A lagging checkpoint is therefore
        repairable without replaying business writes.  Pending or failed rows
        remain a fail-closed condition for a real consumer.
        """
        self._ensure_aux()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                unresolved = int(conn.execute(
                    "SELECT COUNT(*) FROM group_outbox "
                    "WHERE status IN ('pending','failed')"
                ).fetchone()[0])
                if unresolved:
                    raise GroupControlError("system_outbox_projection_pending")
                maximum = int(conn.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM group_outbox"
                ).fetchone()[0])
                checkpoint = int(conn.execute(
                    "SELECT COALESCE(MAX(last_sequence),0) "
                    "FROM outbox_checkpoints WHERE domain='system'"
                ).fetchone()[0])
                if maximum > checkpoint:
                    conn.execute(
                        "UPDATE outbox_checkpoints SET last_sequence=?,updated_at=? "
                        "WHERE domain='system' AND last_sequence<?",
                        (maximum, _now(), maximum),
                    )
                return {
                    "projected": max(0, maximum - checkpoint),
                    "remaining": 0,
                }


class GroupControlService:
    """Authoritative V2 group membership, scope, selection and mode service."""

    def __init__(self, workspace: str | Path, *, write: bool = False) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.store = SystemControlStore(self.workspace, write=write)

    @staticmethod
    def _binding(row: Any) -> dict[str, Any]:
        try:
            redirects = json.loads(str(row[6] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            redirects = []
        return {
            "binding_id": str(row[0]),
            "agent_instance_id": str(row[1]),
            "share_group_id": str(row[2]),
            "group_id": str(row[2]),
            "group_kind": str(row[3]),
            "mcp_server_name": str(row[4]),
            "native_memory_mode": str(row[5]),
            "redirect_paths": [str(item) for item in redirects if isinstance(item, str)],
            "status": str(row[7]),
            "revision": int(row[8]),
            "created_at": str(row[9]),
            "updated_at": str(row[10]),
        }

    def identity_catalog(self) -> dict[str, dict[str, Any]]:
        """Build a bounded instance-id -> program identity catalog.

        Bindings are persisted with an opaque instance id for auditability, but
        the product identity is stable across Router accounts and sessions.  A
        current discovery entry is preferred; the profile registry is also
        consulted so a previously discovered (now missing) member remains
        readable and governable instead of becoming an invisible count.
        """
        catalog: dict[str, dict[str, Any]] = {}
        try:
            from ..agent_locator import AgentLocator
            from ..schema_v3 import stable_hash
            locator = AgentLocator(self.workspace)
            profiles = list(locator.registry.list_profiles())
            # Read the persisted identity records through the existing service
            # API.  Only exact canonical/alias ids from that registry are
            # eligible for endpoint mapping.
            registry: dict[str, dict[str, Any]] = {}
            for provider in sorted({str(item.product or "").strip() for item in profiles}):
                record = self.provider_identity(provider)
                if record:
                    registry[provider.casefold()] = record
            from ..provider_adapters import configured_provider_agent_ids
            configured = configured_provider_agent_ids()
            for provider, endpoint_ids in configured.items():
                if provider in registry or len(endpoint_ids) != 1:
                    continue
                registry[provider] = {
                    "canonical_id": next(iter(endpoint_ids)),
                    "aliases": [],
                    "share_group_id": "",
                    "identity_source": "provider_configuration",
                }
            for provider, record in registry.items():
                identity = normalize_program_identity(provider)
                if identity["program_id"] == "unknown":
                    continue
                canonical = str(record.get("canonical_id") or "").strip()
                for endpoint_id in [
                    canonical,
                    *[str(item).strip() for item in (record.get("aliases") or ())],
                ]:
                    if not endpoint_id:
                        continue
                    is_canonical = endpoint_id == canonical
                    catalog[endpoint_id] = {
                        **identity,
                        "canonical_program_id": identity["program_id"],
                        "canonical_agent_instance_id": canonical,
                        "provider_identity_provider": provider,
                        "provider_identity_group_id": str(record.get("share_group_id") or ""),
                        "identity_source": str(record.get("identity_source") or "provider_identity_registry"),
                        "resolution": "canonical" if is_canonical else "alias",
                        "member_status": "historical_missing",
                        "identity_role": "canonical" if is_canonical else "alias",
                        "is_canonical_endpoint": is_canonical,
                        "is_alias_endpoint": not is_canonical,
                        "is_current_endpoint": False,
                    }
            for profile in profiles:
                identity = normalize_program_identity(str(getattr(profile, "product", "") or ""))
                expected_id = stable_hash(
                    str(getattr(profile, "profile_id", "") or ""),
                    str(locator.context.host_id or ""),
                    str(self.workspace),
                )
                if expected_id and identity["program_id"] != "unknown":
                    fallback = {
                        **identity,
                        "member_status": "historical_missing",
                        "identity_source": "profile_registry",
                        "is_canonical_endpoint": False,
                        "is_alias_endpoint": False,
                        "is_current_endpoint": False,
                    }
                    # Exact provider-registry rows win over profile-derived
                    # guesses.  A profile may still enrich an existing row
                    # with current status below.
                    catalog.setdefault(expected_id, fallback)
            instances, _ledgers = locator.detect_instances()
            for instance in instances:
                identity = normalize_program_identity(str(getattr(instance, "product", "") or ""))
                instance_id = str(getattr(instance, "instance_id", "") or "").strip()
                if instance_id:
                    existing = dict(catalog.get(instance_id) or {})
                    provider = str(identity.get("program_id") or "")
                    record = registry.get(provider) if provider else None
                    if record and provider != "unknown" and not existing:
                        canonical = str(record.get("canonical_id") or "").strip()
                        aliases = {
                            str(item).strip()
                            for item in (record.get("aliases") or ())
                            if str(item).strip()
                        }
                        catalog[instance_id] = {
                            **identity,
                            "canonical_program_id": provider,
                            "canonical_agent_instance_id": canonical,
                            "provider_identity_provider": provider,
                            "provider_identity_group_id": str(record.get("share_group_id") or ""),
                            "member_status": "active_detected",
                            "identity_source": "current_discovery",
                            "resolution": "verified_program",
                            "identity_role": (
                                "canonical" if instance_id == canonical
                                else ("alias" if instance_id in aliases else "current")
                            ),
                            "is_canonical_endpoint": instance_id == canonical,
                            "is_alias_endpoint": instance_id in aliases,
                            "is_current_endpoint": True,
                        }
                    else:
                        existing.update({
                            **({} if existing else identity),
                            "member_status": "active_detected",
                            "is_current_endpoint": True,
                        })
                        catalog[instance_id] = existing
        except Exception:
            # Identity is presentation/enrichment only.  A failed probe must
            # never hide a persisted binding or block its safe unbind action.
            return catalog
        return catalog

    @staticmethod
    def _unknown_member_identity(binding: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "canonical_program_id": "",
            "program_id": "unknown",
            "display_name": "未识别的 MCP 助手",
            "canonical_display_name": "未识别的 MCP 助手",
            "identity_resolution": "unresolved",
            "identity_source": "historical_binding",
            "member_status": "historical_unknown",
            "member_source": str(binding.get("mcp_server_name") or "") or "MCP binding",
            "can_unbind": bool(str(binding.get("binding_id") or "").strip()),
        }

    def _enrich_binding(
        self,
        binding: Mapping[str, Any],
        catalog: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Add human identity and safe-management metadata to one binding."""
        item = dict(binding)
        identities = catalog if catalog is not None else self.identity_catalog()
        agent_id = str(item.get("agent_instance_id") or "").strip()
        identity = dict(identities.get(agent_id) or {})
        if not identity:
            identity = normalize_program_identity(
                "",
                mcp_name=str(item.get("mcp_server_name") or ""),
            )
            identity["member_status"] = "historical_unknown"
            identity["identity_source"] = "historical_binding"
        if str(identity.get("program_id") or "") == "unknown":
            identity = {**self._unknown_member_identity(item), **identity}
            # The localized label is a product-level UI contract, not an
            # opaque token supplied by an MCP server.
            identity["display_name"] = "未识别的 MCP 助手"
            identity["canonical_display_name"] = "未识别的 MCP 助手"
            identity["program_id"] = "unknown"
        else:
            identity.setdefault("canonical_program_id", str(identity.get("program_id") or ""))
            identity.setdefault("canonical_display_name", str(identity.get("display_name") or ""))
            identity.setdefault("can_unbind", bool(str(item.get("binding_id") or "").strip()))
            identity.setdefault("member_source", str(item.get("mcp_server_name") or "") or "discovery")
        canonical_program_id = "" if str(identity.get("program_id") or "") == "unknown" else str(
            identity.get("canonical_program_id") or identity.get("program_id") or ""
        )
        item.update({
            "canonical_program_id": canonical_program_id,
            "program_id": str(identity.get("program_id") or "unknown"),
            "provider": str(identity.get("provider") or ""),
            "product": str(identity.get("program_id") or ""),
            "display_name": str(identity.get("display_name") or "未识别的 MCP 助手"),
            "canonical_display_name": str(identity.get("canonical_display_name") or identity.get("display_name") or "未识别的 MCP 助手"),
            "identity_resolution": str(identity.get("resolution") or "unresolved"),
            "identity_source": str(identity.get("identity_source") or identity.get("source") or "historical_binding"),
            "member_status": str(identity.get("member_status") or "historical_unknown"),
            "member_source": str(identity.get("member_source") or item.get("mcp_server_name") or "MCP binding"),
            "can_unbind": bool(identity.get("can_unbind")),
            "canonical_agent_instance_id": str(identity.get("canonical_agent_instance_id") or ""),
            "provider_identity_provider": str(identity.get("provider_identity_provider") or ""),
            "provider_identity_group_id": str(identity.get("provider_identity_group_id") or ""),
            "identity_role": str(identity.get("identity_role") or "unknown"),
            "is_canonical_endpoint": bool(identity.get("is_canonical_endpoint")),
            "is_alias_endpoint": bool(identity.get("is_alias_endpoint")),
            "is_current_endpoint": bool(
                identity.get("is_current_endpoint")
                or str(identity.get("member_status") or "") == "active_detected"
            ),
        })
        item["endpoint_id"] = agent_id
        item["canonical_member_key"] = (
            item["canonical_program_id"]
            or "unknown-binding:" + str(item.get("binding_id") or agent_id)
        )
        return item

    def _enriched_bindings(
        self,
        *,
        include_inactive: bool = True,
        group_id: str = "",
        identity_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        catalog = identity_catalog if identity_catalog is not None else self.identity_catalog()
        rows = [
            self._enrich_binding(item, catalog)
            for item in self._read_bindings(include_inactive=include_inactive, group_id=group_id)
        ]
        # Preserve every endpoint for audit and unbind, while designating one
        # deterministic representative per program/group for user-facing
        # projections. Unknown endpoints never share a bucket: opaque ids are
        # not evidence that two records belong to the same program.
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            program = str(row.get("canonical_program_id") or "")
            bucket_key = (
                str(row.get("share_group_id") or ""),
                program or "unknown-binding:" + str(row.get("binding_id") or row.get("endpoint_id") or ""),
            )
            buckets.setdefault(bucket_key, []).append(row)
        for bucket in buckets.values():
            bucket.sort(key=lambda value: (
                0 if value.get("is_canonical_endpoint") else 1,
                0 if value.get("is_current_endpoint") else 1,
                str(value.get("endpoint_id") or value.get("agent_instance_id") or ""),
            ))
            for index, row in enumerate(bucket):
                row["is_redundant_endpoint"] = index > 0
                row["user_member_representative"] = index == 0
                row["endpoint_role"] = (
                    "canonical" if row.get("is_canonical_endpoint") else
                    ("alias" if row.get("is_alias_endpoint") else
                     ("current" if row.get("is_current_endpoint") else "historical"))
                )
        return rows

    @staticmethod
    def program_member_projection(bindings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Collapse endpoint bindings into readable program members.

        ``member_details`` remains the complete endpoint-level audit surface.
        This companion projection is intentionally derived, deterministic and
        body-free so GUI callers can show one row per program without losing
        the endpoint ids/actions needed to unbind a stale or redundant entry.
        """
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for item in bindings:
            program = str(item.get("canonical_program_id") or "")
            key = program or "unknown-binding:" + str(
                item.get("binding_id") or item.get("endpoint_id") or item.get("agent_instance_id") or ""
            )
            buckets.setdefault(key, []).append(item)
        details: list[dict[str, Any]] = []
        for key, rows in sorted(buckets.items()):
            representative = next(
                (item for item in rows if item.get("user_member_representative")),
                sorted(rows, key=lambda item: str(item.get("endpoint_id") or item.get("agent_instance_id") or ""))[0],
            )
            known = bool(str(representative.get("canonical_program_id") or ""))
            details.append({
                "canonical_program_id": str(representative.get("canonical_program_id") or ""),
                "program_id": str(representative.get("program_id") or "unknown"),
                "provider": str(representative.get("provider") or representative.get("program_id") or ""),
                "product": str(representative.get("product") or representative.get("program_id") or ""),
                "display_name": str(representative.get("display_name") or "未识别的 MCP 助手"),
                "canonical_display_name": str(representative.get("canonical_display_name") or representative.get("display_name") or "未识别的 MCP 助手"),
                "canonical_agent_instance_id": str(representative.get("canonical_agent_instance_id") or ""),
                "representative_endpoint_id": str(representative.get("endpoint_id") or representative.get("agent_instance_id") or ""),
                "endpoint_count": len(rows),
                "endpoint_ids": [str(item.get("endpoint_id") or item.get("agent_instance_id") or "") for item in rows],
                "binding_ids": [str(item.get("binding_id") or "") for item in rows],
                "current": any(bool(item.get("is_current_endpoint")) for item in rows),
                "redundant": any(bool(item.get("is_redundant_endpoint")) for item in rows),
                "known": known,
                "identity_source": str(representative.get("identity_source") or "historical_binding"),
                "can_unbind": any(bool(item.get("can_unbind")) for item in rows),
            })
        program_members = sorted({
            str(item.get("canonical_program_id") or "")
            for item in details
            if str(item.get("canonical_program_id") or "")
        })
        return {
            "members": details,
            "program_members": program_members,
            "program_member_count": len(program_members),
            # Unknown records remain separate in ``members`` and retain their
            # own unbind ids. This count is the unresolved identity category
            # (normally one), while endpoint count exposes its true size.
            "unresolved_member_count": sum(1 for item in details if not item["known"]),
            "unresolved_endpoint_count": sum(1 for item in details if not item["known"]),
            "endpoint_member_count": sum(int(item.get("endpoint_count") or 0) for item in details),
            "extra_connection_count": max(
                0,
                sum(int(item.get("endpoint_count") or 0) for item in details) - len(program_members),
            ),
        }

    def _read_bindings(self, *, include_inactive: bool = True, group_id: str = "", agent_id: str = "") -> list[dict[str, Any]]:
        if not self.store.db_path.is_file() or self.store._preflight() != "current":
            return []
        clauses = ["1=1"]
        params: list[Any] = []
        if not include_inactive:
            clauses.append("status='active'")
        if group_id:
            clauses.append("share_group_id=?")
            params.append(str(group_id))
        if agent_id:
            clauses.append("agent_instance_id=?")
            params.append(str(agent_id))
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT binding_id,agent_instance_id,share_group_id,group_kind,mcp_server_name,native_memory_mode,redirect_paths_json,status,revision,created_at,updated_at "
                "FROM agent_group_bindings WHERE " + " AND ".join(clauses) + " ORDER BY agent_instance_id,share_group_id,binding_id",
                tuple(params),
            ).fetchall()
        return [self._binding(row) for row in rows]

    def list_bindings(
        self,
        *,
        include_inactive: bool = True,
        identity_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        rows = self._enriched_bindings(
            include_inactive=bool(include_inactive),
            identity_catalog=identity_catalog,
        )
        return {"ok": True, "status": "succeeded", "bindings": rows, "total": len(rows)}

    def active_binding_for_agent(
        self,
        agent_instance_id: str,
        *,
        identity_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        agent = str(agent_instance_id or "").strip()
        rows = self._read_bindings(include_inactive=False, agent_id=agent)
        if len(rows) > 1:
            raise GroupControlError("multiple_active_bindings")
        catalog = identity_catalog if identity_catalog is not None else self.identity_catalog()
        requested = catalog.get(agent) or {}
        requested_program = str(requested.get("canonical_program_id") or requested.get("program_id") or "")
        if requested_program == "unknown":
            requested_program = ""
        requested_group = str(requested.get("provider_identity_group_id") or "")
        # Validate all exact/alias endpoints for this program even when the
        # requested id has a direct row. Two active groups are ambiguous and
        # must fail closed instead of silently selecting one.
        enriched_all = [
            self._enrich_binding(item, catalog)
            for item in self._read_bindings(include_inactive=False)
        ]
        if rows:
            result = self._enrich_binding(rows[0], catalog)
            program = str(result.get("canonical_program_id") or "") or requested_program
            if program:
                matches = [
                    item for item in enriched_all
                    if str(item.get("canonical_program_id") or "") == program
                ]
                groups = {str(item.get("share_group_id") or "") for item in matches}
                if len(groups) > 1:
                    raise GroupControlError("multiple_active_bindings")
                if requested_group and any(group != requested_group for group in groups):
                    raise GroupControlError("provider_identity_group_mismatch")
                # One governed program has one effective binding. Prefer the
                # registry's canonical endpoint when both canonical and alias
                # rows exist; the alias remains in member_details for audit
                # and can still be explicitly unbound there.
                preferred = next(
                    (item for item in matches if item.get("is_canonical_endpoint")),
                    next(
                        (item for item in matches if item.get("is_current_endpoint")),
                        matches[0] if matches else result,
                    ),
                )
                if str(preferred.get("binding_id") or "") != str(result.get("binding_id") or ""):
                    preferred = dict(preferred)
                    preferred["resolved_for_agent_instance_id"] = agent
                    preferred["binding_alias"] = True
                return preferred
            return result

        # A Router/account switch must not split one program's governed group.
        # Resolve only exact canonical identities from the trusted profile
        # registry/current discovery; never infer from an opaque id substring.
        if not requested_program:
            return None
        matches = [
            item for item in enriched_all
            if str(item.get("canonical_program_id") or "") == requested_program
        ]
        groups = {str(item.get("share_group_id") or "") for item in matches}
        if len(groups) > 1:
            raise GroupControlError("multiple_active_bindings")
        if requested_group and any(group != requested_group for group in groups):
            raise GroupControlError("provider_identity_group_mismatch")
        if not matches:
            return None
        result = dict(matches[0])
        result["resolved_for_agent_instance_id"] = agent
        result["binding_alias"] = True
        return result

    @staticmethod
    def _group_lifecycle_key(group_id: str) -> str:
        return "group_lifecycle:" + _digest(str(group_id))

    def _dissolved_groups(self) -> set[str]:
        if not self.store.db_path.is_file() or self.store._preflight() != "current":
            return set()
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT value_json FROM control_preferences WHERE pref_key LIKE 'group_lifecycle:%'"
            ).fetchall()
        dissolved: set[str] = set()
        for row in rows:
            try:
                value = json.loads(str(row[0] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if str(value.get("status") or "") == "dissolved" and str(value.get("group_id") or ""):
                dissolved.add(str(value["group_id"]))
        return dissolved

    @staticmethod
    def _validate_memory_snapshot(conn: Any) -> None:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        if not {"memory_schema_meta", "atoms", "domain_state"} <= tables:
            raise GroupControlError("v2_memory_schema_unavailable")
        row = conn.execute(
            "SELECT version,marker FROM memory_schema_meta WHERE domain='memory'"
        ).fetchone()
        if row is None or int(row[0]) != 1 or str(row[1]) != "memoryguard-v2-phase2-memory":
            raise GroupControlError("v2_memory_schema_unavailable")

    def aggregate_groups(self) -> dict[str, Any]:
        """Return a read-only aggregate over authoritative V2 group state.

        The control database contributes active membership; the V2 memory
        database contributes record state.  Both reads are optional, so a
        fresh workspace is a stable empty result and does not create a
        database, directory, WAL, receipt, or other side effect.
        """

        bindings = self._enriched_bindings(include_inactive=False)
        members: dict[str, list[str]] = {}
        member_details: dict[str, list[dict[str, Any]]] = {}
        for item in bindings:
            group = str(item["share_group_id"] or "")
            agent = str(item["agent_instance_id"] or "")
            if group and agent:
                members.setdefault(group, []).append(agent)
                member_details.setdefault(group, []).append(dict(item))

        memory_groups: dict[str, dict[str, Any]] = {}
        memory_db = self.store.layout.memory_db
        memory_state: dict[str, Any] = {}
        if memory_db.is_file():
            try:
                with open_database_snapshot(memory_db) as conn:
                    self._validate_memory_snapshot(conn)
                    rows = conn.execute(
                        "SELECT atom_id,memory_id,share_group_id,status,visibility,canonical_hash,revision,metadata_json "
                        "FROM atoms WHERE workspace_id=? AND share_group_id<>'' "
                        "ORDER BY share_group_id,created_at,atom_id",
                        (str(self.workspace),),
                    ).fetchall()
                    state_row = conn.execute(
                        "SELECT state,generation,updated_at FROM domain_state WHERE domain='memory'"
                    ).fetchone()
                if state_row is not None:
                    memory_state = {
                        "state": str(state_row[0] or ""),
                        "generation": int(state_row[1] or 0),
                        "updated_at": str(state_row[2] or ""),
                    }
            except FileNotFoundError:
                rows = []
            except Exception as exc:
                raise GroupControlError("v2_group_aggregate_unavailable") from exc

            for row in rows:
                group = str(row["share_group_id"] or "")
                if not group:
                    continue
                item = memory_groups.setdefault(
                    group,
                    {
                        "record_count": 0,
                        "status_counts": {},
                        "visibility_counts": {},
                        "revision_max": 0,
                        "version_rows": [],
                    },
                )
                item["record_count"] += 1
                status = str(row["status"] or "unknown")
                visibility = str(row["visibility"] or "unknown")
                item["status_counts"][status] = item["status_counts"].get(status, 0) + 1
                item["visibility_counts"][visibility] = item["visibility_counts"].get(visibility, 0) + 1
                item["revision_max"] = max(item["revision_max"], int(row["revision"] or 0))
                try:
                    metadata = json.loads(str(row["metadata_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                item["version_rows"].append({
                    "atom_id": str(row["atom_id"] or ""),
                    "memory_id": str(row["memory_id"] or ""),
                    "status": status,
                    "visibility": visibility,
                    "canonical_hash": str(row["canonical_hash"] or ""),
                    "revision": int(row["revision"] or 0),
                    "conflict": bool(metadata.get("conflict_group_id")) or status == "conflicted",
                    "quarantined": status == "quarantined",
                })

        group_ids = sorted((set(members) | set(memory_groups)) - self._dissolved_groups())
        groups: list[dict[str, Any]] = []
        total_status_counts: dict[str, int] = {}
        total_visibility_counts: dict[str, int] = {}
        for group in group_ids:
            item = memory_groups.get(group, {})
            group_member_details = member_details.get(group, [])
            member_projection = self.program_member_projection(group_member_details)
            status_counts = dict(item.get("status_counts") or {})
            visibility_counts = dict(item.get("visibility_counts") or {})
            version_rows = list(item.get("version_rows") or [])
            active_count = sum(
                1 for row in version_rows
                if row["status"] == "active" and row["visibility"] in {"ready", "active"}
            )
            visible_count = sum(
                count for visibility, count in visibility_counts.items()
                if visibility in {"ready", "active"}
            )
            conflict_count = sum(bool(row.get("conflict")) for row in version_rows)
            quarantined_count = sum(bool(row.get("quarantined")) for row in version_rows)
            version_digest = _digest(
                "v2-group-active-version",
                group,
                sorted(version_rows, key=lambda value: (value["atom_id"], value["revision"])),
            ) if version_rows else None
            for key, value in status_counts.items():
                total_status_counts[key] = total_status_counts.get(key, 0) + int(value)
            for key, value in visibility_counts.items():
                total_visibility_counts[key] = total_visibility_counts.get(key, 0) + int(value)
            group_status = (
                "READY" if visible_count else
                ("EMPTY" if version_rows else "NO_SOURCE")
            )
            groups.append({
                "share_group_id": group,
                "group_id": group,
                "group_kind": _group_kind(group),
                "members": sorted(set(members.get(group, []))),
                # Keep the historical endpoint-count contract. Consumers
                # wanting the readable projection use program_member_count
                # and the program_member_details list below.
                "member_count": len(set(members.get(group, []))),
                "endpoint_member_count": int(member_projection["endpoint_member_count"]),
                "extra_connection_count": int(member_projection["extra_connection_count"]),
                # ``members`` remains the opaque audit ids for compatibility;
                # these projections are the user-facing, canonical view. An
                # unresolved historical binding gets its own key so two
                # unknown records are never guessed to be the same program.
                "member_details": sorted(
                    group_member_details,
                    key=lambda value: (str(value.get("display_name") or ""), str(value.get("agent_instance_id") or "")),
                ),
                "program_members": member_projection["program_members"],
                "program_member_count": int(member_projection["program_member_count"]),
                "unresolved_member_count": int(member_projection["unresolved_member_count"]),
                "unresolved_endpoint_count": int(member_projection["unresolved_endpoint_count"]),
                "program_member_details": [
                    value for value in member_projection["members"] if value.get("known")
                ],
                "record_count": int(item.get("record_count", 0)),
                "total_records": int(item.get("record_count", 0)),
                "active_count": active_count,
                "active_records": active_count,
                "deleted_count": int(status_counts.get("deleted", 0)),
                "conflict_count": conflict_count,
                "quarantined_count": quarantined_count,
                "status_counts": status_counts,
                "visibility_counts": visibility_counts,
                "active_version": version_digest,
                "active_version_status": group_status,
                "status": group_status,
            })

        total_records = sum(int(item["record_count"]) for item in groups)
        total_active = sum(int(item["active_count"]) for item in groups)
        total_deleted = sum(int(item["deleted_count"]) for item in groups)
        total_conflicts = sum(int(item["conflict_count"]) for item in groups)
        total_quarantined = sum(int(item["quarantined_count"]) for item in groups)
        active_versions = [
            (item["share_group_id"], item["active_version"])
            for item in groups
            if item.get("active_version")
        ]
        return {
            "ok": True,
            "status": "READY" if total_records else ("EMPTY" if memory_db.is_file() or groups else "NO_SOURCE"),
            "available": bool(memory_db.is_file() or groups),
            "groups": groups,
            "total": len(groups),
            "total_groups": len(groups),
            "total_records": total_records,
            "active_count": total_active,
            "active_records": total_active,
            "deleted_count": total_deleted,
            "conflict_count": total_conflicts,
            "quarantined_count": total_quarantined,
            "status_counts": total_status_counts,
            "visibility_counts": total_visibility_counts,
            "active_version": _digest("v2-global-active-version", active_versions) if active_versions else None,
            "active_version_generation": memory_state.get("generation") if memory_state else None,
            "active_version_status": memory_state.get("state") if memory_state else "NO_SOURCE",
        }

    def list_groups(self) -> dict[str, Any]:
        """Compatibility spelling for the native group-list surface."""

        return self.aggregate_groups()

    def list_share_groups(self) -> dict[str, Any]:
        """Formal V2 group-list spelling used by adapters and GUI callers."""

        return self.aggregate_groups()

    def get_global_memory_status(self) -> dict[str, Any]:
        """Return the body-free V2 aggregate and cross-group duplicate candidates.

        Exact candidates use the persisted V2 canonical fingerprint.  The
        deterministic V2 ``HashBackend`` adds semantic candidates without
        consulting a provider or opening any legacy store.  Candidate output
        contains only IDs, group IDs, counts, digests, and similarity.
        """

        aggregate = self.aggregate_groups()
        duplicate_candidates: list[dict[str, Any]] = []
        memory_db = self.store.layout.memory_db
        if memory_db.is_file():
            try:
                from .dedup import HashBackend, canonical_hash, cosine_similarity

                with open_database_snapshot(memory_db) as conn:
                    self._validate_memory_snapshot(conn)
                    rows = conn.execute(
                        "SELECT atom_id,memory_id,share_group_id,body,status,visibility,canonical_hash "
                        "FROM atoms WHERE workspace_id=? AND status IN "
                        "('active','low_confidence','conflicted','quarantined') "
                        "AND visibility IN ('ready','active') "
                        "ORDER BY share_group_id,created_at,atom_id",
                        (str(self.workspace),),
                    ).fetchall()
            except FileNotFoundError:
                rows = []
            except Exception as exc:
                raise GroupControlError("v2_global_memory_status_unavailable") from exc

            exact: dict[str, list[dict[str, str]]] = {}
            candidates: list[dict[str, Any]] = []
            for row in rows:
                group = str(row["share_group_id"] or "")
                body = str(row["body"] or "")
                digest = str(row["canonical_hash"] or "") or canonical_hash(body)
                if not group or not digest:
                    continue
                exact.setdefault(digest, []).append({
                    "memory_id": str(row["memory_id"] or ""),
                    "atom_id": str(row["atom_id"] or ""),
                    "share_group_id": group,
                })

            for digest, records in sorted(exact.items()):
                groups = sorted({item["share_group_id"] for item in records})
                if len(groups) < 2:
                    continue
                candidates.append({
                    "match_type": "exact",
                    "canonical_hash": digest,
                    "digest": digest,
                    "share_group_ids": groups,
                    "groups": groups,
                    "group_count": len(groups),
                    "record_count": len(records),
                    "memory_ids": sorted(item["memory_id"] for item in records),
                    "atom_ids": sorted(item["atom_id"] for item in records),
                    "records": sorted(records, key=lambda item: (item["share_group_id"], item["atom_id"])),
                    "similarity": 1.0,
                })

            # Use the existing deterministic V2 semantic backend only for
            # records whose exact fingerprint did not already form a group.
            semantic_rows = [
                row for row in rows
                if len({
                    str(item["share_group_id"] or "")
                    for item in exact.get(str(row["canonical_hash"] or "") or canonical_hash(str(row["body"] or "")), [])
                }) < 2
            ]
            backend = HashBackend()
            vectors = [(row, backend.embed_text(str(row["body"] or ""))) for row in semantic_rows]
            for index, (left, left_vector) in enumerate(vectors):
                left_group = str(left["share_group_id"] or "")
                if not left_group:
                    continue
                for right, right_vector in vectors[index + 1:]:
                    right_group = str(right["share_group_id"] or "")
                    if not right_group or right_group == left_group:
                        continue
                    similarity = float(cosine_similarity(left_vector, right_vector))
                    if similarity < 0.85:
                        continue
                    left_id = str(left["atom_id"] or "")
                    right_id = str(right["atom_id"] or "")
                    pair = sorted((left_id, right_id))
                    digest = _digest("v2-semantic-duplicate", *pair)
                    records = [
                        {
                            "memory_id": str(left["memory_id"] or ""),
                            "atom_id": left_id,
                            "share_group_id": left_group,
                        },
                        {
                            "memory_id": str(right["memory_id"] or ""),
                            "atom_id": right_id,
                            "share_group_id": right_group,
                        },
                    ]
                    candidates.append({
                        "match_type": "semantic",
                        "canonical_hash": "",
                        "digest": digest,
                        "share_group_ids": sorted((left_group, right_group)),
                        "groups": sorted((left_group, right_group)),
                        "group_count": 2,
                        "record_count": 2,
                        "memory_ids": sorted(item["memory_id"] for item in records),
                        "atom_ids": pair,
                        "records": sorted(records, key=lambda item: (item["share_group_id"], item["atom_id"])),
                        "similarity": similarity,
                    })

            duplicate_candidates = candidates
            duplicate_candidates.sort(
                key=lambda item: (
                    str(item["match_type"]),
                    str(item["digest"]),
                    tuple(item["share_group_ids"]),
                )
            )

        return {
            **aggregate,
            "cross_group_duplicates": duplicate_candidates,
        }

    @staticmethod
    def _binding_id(agent_id: str, group_id: str, server: str) -> str:
        return "binding-" + _digest("v2-agent-binding", agent_id, group_id, server)

    def _binding_state_seed(self, agent_ids: Sequence[str], group_id: str) -> list[tuple[str, str, str, int]]:
        agents = sorted({str(item) for item in agent_ids if str(item)})
        if not agents or not self.store.db_path.is_file() or self.store._preflight() != "current":
            return []
        placeholders = ",".join("?" for _ in agents)
        with self.store.connection() as conn:
            active = conn.execute(
                "SELECT agent_instance_id,share_group_id,status,revision FROM agent_group_bindings "
                f"WHERE agent_instance_id IN ({placeholders}) AND status='active' "
                "ORDER BY agent_instance_id,share_group_id,binding_id",
                tuple(agents),
            ).fetchall()
            lifecycle = conn.execute(
                "SELECT value_json,revision FROM control_preferences WHERE pref_key=?",
                (self._group_lifecycle_key(group_id),),
            ).fetchone()
        seed = [
            (str(row[0]), str(row[1]), str(row[2]), int(row[3]))
            for row in active if str(row[1]) != str(group_id)
        ]
        if lifecycle is not None:
            try:
                status = str(json.loads(str(lifecycle[0] or "{}")).get("status") or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                status = "invalid"
            seed.append(("__group__", str(group_id), status, int(lifecycle[1])))
        return seed

    @staticmethod
    def _validate_group(agent_id: str, group_id: str) -> None:
        if not agent_id:
            raise GroupControlError("agent_instance_id_required")
        if not group_id:
            raise GroupControlError("share_group_id_required")
        if _group_kind(group_id) == "personal" and group_id != personal_group_id(agent_id):
            raise GroupControlError("personal_group_owner_mismatch")

    def _bind_tx(
        self,
        conn: Any,
        *,
        agent_id: str,
        group_id: str,
        server: str,
        native_memory_mode: str,
        redirect_paths: Sequence[str],
    ) -> tuple[dict[str, Any], bool]:
        self._validate_group(agent_id, group_id)
        now = _now()
        current = conn.execute(
            "SELECT binding_id,share_group_id,mcp_server_name,native_memory_mode,redirect_paths_json,status,revision FROM agent_group_bindings "
            "WHERE agent_instance_id=? AND status='active'",
            (agent_id,),
        ).fetchone()
        clean_paths = [str(item) for item in redirect_paths if str(item)]
        if current is not None and str(current[1]) == group_id and str(current[2]) == server:
            changed = str(current[3]) != native_memory_mode or str(current[4]) != _json(clean_paths) or str(current[5]) != "active"
            if changed:
                conn.execute(
                    "UPDATE agent_group_bindings SET native_memory_mode=?,redirect_paths_json=?,status='active',revision=revision+1,updated_at=? WHERE binding_id=?",
                    (native_memory_mode, _json(clean_paths), now, str(current[0])),
                )
            binding_id = str(current[0])
            revision = int(current[6]) + (1 if changed else 0)
            return ({
                "binding_id": binding_id, "agent_instance_id": agent_id, "share_group_id": group_id,
                "group_id": group_id, "group_kind": _group_kind(group_id), "mcp_server_name": server,
                "native_memory_mode": native_memory_mode, "redirect_paths": clean_paths, "status": "active",
                "revision": revision,
            }, changed)
        if current is not None:
            conn.execute(
                "UPDATE agent_group_bindings SET status='inactive',revision=revision+1,updated_at=? WHERE binding_id=?",
                (now, str(current[0])),
            )
        binding_id = self._binding_id(agent_id, group_id, server)
        prior = conn.execute("SELECT revision,created_at FROM agent_group_bindings WHERE binding_id=?", (binding_id,)).fetchone()
        revision = int(prior[0]) + 1 if prior else 1
        created_at = str(prior[1]) if prior else now
        conn.execute(
            "INSERT INTO agent_group_bindings(binding_id,agent_instance_id,share_group_id,group_kind,mcp_server_name,native_memory_mode,redirect_paths_json,status,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'active',?,?,?) ON CONFLICT(binding_id) DO UPDATE SET native_memory_mode=excluded.native_memory_mode,redirect_paths_json=excluded.redirect_paths_json,status='active',revision=excluded.revision,updated_at=excluded.updated_at",
            (binding_id, agent_id, group_id, _group_kind(group_id), server, native_memory_mode, _json(clean_paths), revision, created_at, now),
        )
        # Rebinding a previously dissolved group is an explicit restore.  Its
        # governed data was preserved in place, so removing the lifecycle
        # tombstone makes the group visible again without copying records.
        lifecycle_key = self._group_lifecycle_key(group_id)
        lifecycle = conn.execute(
            "SELECT value_json,revision FROM control_preferences WHERE pref_key=?",
            (lifecycle_key,),
        ).fetchone()
        if lifecycle is not None:
            conn.execute(
                "UPDATE control_preferences SET value_json=?,revision=revision+1,updated_at=? WHERE pref_key=?",
                (_json({"group_id": group_id, "status": "active", "data_preserved": True}), now, lifecycle_key),
            )
        return ({
            "binding_id": binding_id, "agent_instance_id": agent_id, "share_group_id": group_id,
            "group_id": group_id, "group_kind": _group_kind(group_id), "mcp_server_name": server,
            "native_memory_mode": native_memory_mode, "redirect_paths": clean_paths, "status": "active",
            "revision": revision,
        }, True)

    def bind_agent(
        self,
        agent_instance_id: str,
        share_group_id: str,
        *,
        mcp_server_name: str = "memoryguard",
        native_memory_mode: str = "observed",
        redirect_paths: Sequence[str] = (),
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        agent = str(agent_instance_id or "").strip()
        group = str(share_group_id or "").strip()
        server = str(mcp_server_name or "memoryguard").strip() or "memoryguard"
        mode = str(native_memory_mode or "observed").strip() or "observed"
        request = {
            "agent": agent, "group": group, "server": server, "mode": mode,
            "redirect_paths": list(redirect_paths),
            "binding_state": self._binding_state_seed((agent,), group),
        }
        key = str(idempotency_key or _digest("bind_agent", _json(request)))

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            binding, changed = self._bind_tx(conn, agent_id=agent, group_id=group, server=server, native_memory_mode=mode, redirect_paths=redirect_paths)
            members = [str(row[0]) for row in conn.execute("SELECT agent_instance_id FROM agent_group_bindings WHERE share_group_id=? AND status='active' ORDER BY agent_instance_id", (group,)).fetchall()]
            return ({"ok": True, "status": "succeeded", "binding": binding, "binding_id": binding["binding_id"], "share_group_id": group, "members": members, "member_count": len(members), "created": changed, "changed": changed}, binding["binding_id"])

        return self.store.mutate("bind_agent", key, request, apply)

    def bind_agents(
        self,
        agent_instance_ids: Sequence[str],
        *,
        share_group_id: str = "",
        mcp_server_name: str = "memoryguard",
        native_memory_modes: Mapping[str, str] | None = None,
        redirect_paths: Mapping[str, Sequence[str]] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        agents = sorted({str(item or "").strip() for item in agent_instance_ids if str(item or "").strip()})
        if len(agents) < 2:
            raise GroupControlError("shared_group_requires_at_least_two_agents")
        requested_group = str(share_group_id or "").strip()
        group = requested_group or ("shared-" + _digest("v2-shared-group", *agents))
        if _group_kind(group) == "personal":
            raise GroupControlError("personal_group_cannot_be_shared")
        modes = dict(native_memory_modes or {})
        redirects = dict(redirect_paths or {})
        request = {
            "agents": agents, "group": group, "server": mcp_server_name,
            "modes": modes, "redirects": redirects,
            "binding_state": self._binding_state_seed(agents, group),
        }
        key = str(idempotency_key or _digest("bind_agents", _json(request)))

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            bindings: list[dict[str, Any]] = []
            changed = False
            for agent in agents:
                item, item_changed = self._bind_tx(
                    conn,
                    agent_id=agent,
                    group_id=group,
                    server=str(mcp_server_name or "memoryguard"),
                    native_memory_mode=str(modes.get(agent) or "redirected"),
                    redirect_paths=redirects.get(agent, ()),
                )
                bindings.append(item)
                changed = changed or item_changed
            members = [str(row[0]) for row in conn.execute("SELECT agent_instance_id FROM agent_group_bindings WHERE share_group_id=? AND status='active' ORDER BY agent_instance_id", (group,)).fetchall()]
            return ({"ok": True, "status": "succeeded", "share_group_id": group, "bindings": bindings, "members": members, "member_count": len(members), "created": changed, "changed": changed}, group)

        return self.store.mutate("bind_agents_to_shared_group", key, request, apply)

    def ensure_personal(self, agent_instance_id: str, *, idempotency_key: str = "") -> dict[str, Any]:
        agent = str(agent_instance_id or "").strip()
        current = self.active_binding_for_agent(agent)
        if current is not None:
            return {"ok": True, "status": "succeeded", "binding": current, "binding_id": current["binding_id"], "share_group_id": current["share_group_id"], "created": False, "changed": False}
        return self.bind_agent(agent, personal_group_id(agent), idempotency_key=idempotency_key or _digest("ensure_personal", agent))

    def leave_to_personal(self, agent_instance_id: str, *, idempotency_key: str = "") -> dict[str, Any]:
        agent = str(agent_instance_id or "").strip()
        current = self.active_binding_for_agent(agent)
        personal = personal_group_id(agent)
        if current is not None and current["share_group_id"] == personal:
            return {"ok": True, "status": "succeeded", "binding": current, "binding_id": current["binding_id"], "share_group_id": personal, "created": False, "changed": False}
        result = self.bind_agent(agent, personal, idempotency_key=idempotency_key or _digest("leave_personal", agent, personal))
        result["previous_group_id"] = current["share_group_id"] if current else ""
        return result

    def unbind(self, binding_id: str, *, idempotency_key: str = "") -> dict[str, Any]:
        binding = str(binding_id or "").strip()
        if not binding:
            raise GroupControlError("binding_id_required")
        request = {"binding_id": binding}
        key = idempotency_key or _digest("unbind", binding)

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            row = conn.execute("SELECT agent_instance_id,share_group_id,status,revision FROM agent_group_bindings WHERE binding_id=?", (binding,)).fetchone()
            if row is None:
                raise GroupControlError("binding_not_found")
            changed = str(row[2]) != "inactive"
            if changed:
                conn.execute("UPDATE agent_group_bindings SET status='inactive',revision=revision+1,updated_at=? WHERE binding_id=?", (_now(), binding))
            return ({"ok": True, "status": "succeeded", "binding_id": binding, "agent_instance_id": str(row[0]), "share_group_id": str(row[1]), "changed": changed}, binding)

        return self.store.mutate("unbind_agent", key, request, apply)

    def dissolve(self, group_id: str, *, idempotency_key: str = "") -> dict[str, Any]:
        group = str(group_id or "").strip()
        if not group:
            raise GroupControlError("share_group_id_required")
        preview = self.group_preview(group)
        lifecycle_key = self._group_lifecycle_key(group)
        revision_seed: list[tuple[str, int, str]] = []
        lifecycle_revision = 0
        if self.store.db_path.is_file() and self.store._preflight() == "current":
            with self.store.connection() as conn:
                revision_seed = [
                    (str(row[0]), int(row[1]), str(row[2]))
                    for row in conn.execute(
                        "SELECT binding_id,revision,status FROM agent_group_bindings WHERE share_group_id=? ORDER BY binding_id",
                        (group,),
                    ).fetchall()
                ]
                lifecycle = conn.execute(
                    "SELECT revision FROM control_preferences WHERE pref_key=?",
                    (lifecycle_key,),
                ).fetchone()
                lifecycle_revision = int(lifecycle[0]) if lifecycle is not None else 0
        request = {"group": group, "binding_revisions": revision_seed, "lifecycle_revision": lifecycle_revision}
        key = idempotency_key or _digest("dissolve", _json(request))
        hook_cleanup: Any = None

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            nonlocal hook_cleanup
            active_rows = conn.execute(
                "SELECT binding_id,agent_instance_id,mcp_server_name FROM agent_group_bindings "
                "WHERE share_group_id=? AND status='active' ORDER BY binding_id",
                (group,),
            ).fetchall()
            hook_rows = conn.execute(
                "SELECT agent_instance_id FROM agent_group_bindings "
                "WHERE share_group_id=? ORDER BY binding_id",
                (group,),
            ).fetchall()
            prior = conn.execute(
                "SELECT value_json,revision FROM control_preferences WHERE pref_key=?",
                (lifecycle_key,),
            ).fetchone()
            already_dissolved = False
            if prior is not None:
                try:
                    already_dissolved = str(json.loads(str(prior[0] or "{}")).get("status") or "") == "dissolved"
                except (TypeError, ValueError, json.JSONDecodeError):
                    already_dissolved = False
            known = bool(active_rows) or bool(revision_seed) or int(preview.get("memory_count") or 0) > 0
            # Host configs are user-level and can hold several MemoryGuard
            # bindings for one provider.  Delete only generated commands that
            # carry this former member's exact (provider, agent, shared-group)
            # identity; never blanket-uninstall a provider.
            from ..host_hook_executor import HostHookExecutor

            hook_cleanup = HostHookExecutor(self.workspace).remove_generated_bindings(
                {
                    "agent_instance_id": str(row[0]),
                    "share_group_id": group,
                }
                for row in hook_rows
            )
            personal_bindings: list[dict[str, Any]] = []
            for row in active_rows:
                binding, _ = self._bind_tx(
                    conn,
                    agent_id=str(row[1]),
                    group_id=personal_group_id(str(row[1])),
                    server=str(row[2] or "memoryguard"),
                    native_memory_mode="observed",
                    redirect_paths=(),
                )
                personal_bindings.append(binding)
            conn.execute("DELETE FROM governance_scopes WHERE share_group_id=?", (group,))
            if known:
                revision = int(prior[1]) + (0 if already_dissolved else 1) if prior is not None else 1
                conn.execute(
                    "INSERT INTO control_preferences(pref_key,value_json,revision,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(pref_key) DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision,updated_at=excluded.updated_at",
                    (lifecycle_key, _json({"group_id": group, "status": "dissolved", "data_preserved": True}), revision, _now()),
                )
            changed = bool(active_rows) or (known and not already_dissolved)
            return ({
                "ok": True, "status": "succeeded", "share_group_id": group,
                "unbound_count": len(active_rows), "members": [str(row[1]) for row in active_rows],
                "changed": changed, "removed_from_active_groups": bool(known),
                "data_preserved": True,
                "personal_bindings": personal_bindings,
                "personal_binding_count": len(personal_bindings),
                "hook_cleanup": hook_cleanup.public_result(),
            }, group)

        try:
            return self.store.mutate("dissolve_shared_group", key, request, apply)
        except Exception:
            # Hook files are outside system.db.  A failed DB mutation must not
            # strand a valid shared binding without its generated Hook.
            if hook_cleanup is not None:
                hook_cleanup.restore()
            raise

    def group_preview(self, group_id: str) -> dict[str, Any]:
        group = str(group_id or "").strip()
        bindings = self._enriched_bindings(include_inactive=False, group_id=group)
        memory_count = 0
        try:
            from ..memory.store import MemoryAtomStore, MemoryReadScope
            if WorkspaceV2Layout(self.workspace).memory_db.is_file():
                memory = MemoryAtomStore(self.workspace, readonly=True)
                memory_count = len(memory.list_atoms(scope=MemoryReadScope(workspace_id=str(self.workspace), share_group_id=group, admin=True), include_building=True))
        except Exception:
            memory_count = 0
        member_projection = self.program_member_projection(bindings)
        return {
            "ok": True,
            "status": "succeeded",
            "share_group_id": group,
            "group_kind": _group_kind(group),
            "bindings": bindings,
            "members": [item["agent_instance_id"] for item in bindings],
            "member_details": bindings,
            "member_count": len(bindings),
            "endpoint_member_count": int(member_projection["endpoint_member_count"]),
            "extra_connection_count": int(member_projection["extra_connection_count"]),
            "program_members": member_projection["program_members"],
            "program_member_count": int(member_projection["program_member_count"]),
            "program_member_details": [
                item for item in member_projection["members"] if item.get("known")
            ],
            "unresolved_member_count": int(member_projection["unresolved_member_count"]),
            "unresolved_endpoint_count": int(member_projection["unresolved_endpoint_count"]),
            "memory_count": memory_count,
        }

    def check_drift(self, binding_id: str) -> dict[str, Any]:
        target = next((item for item in self._read_bindings(include_inactive=True) if item["binding_id"] == str(binding_id)), None)
        if target is None:
            raise GroupControlError("binding_not_found")
        missing = [path for path in target["redirect_paths"] if path and not Path(path).expanduser().exists()]
        return {"ok": True, "status": "succeeded", "binding_id": target["binding_id"], "agent_instance_id": target["agent_instance_id"], "share_group_id": target["share_group_id"], "binding_status": "drifted" if missing else target["status"], "missing_redirect_paths": missing, "drifted": bool(missing)}

    def scope_state(self, principal_agent_id: str, *, admin: bool = False) -> dict[str, Any]:
        principal = str(principal_agent_id or "").strip()
        if not self.store.db_path.is_file() or self.store._preflight() != "current":
            return {"ok": True, "status": "succeeded", "empty": True, "scope": None}
        with self.store.connection() as conn:
            row = conn.execute("SELECT mode,agent_instance_id,share_group_id,revision,updated_at FROM governance_scopes WHERE principal_agent_id=?", (principal,)).fetchone()
        if row is None:
            return {"ok": True, "status": "succeeded", "empty": True, "scope": None}
        mode = str(row[0] or "")
        target_agent = str(row[1] or principal)
        group = str(row[2] or "")
        # The desktop GUI stores its selection under the server-admin
        # principal.  An admin read may therefore validate the selected target
        # against its own active binding, while ordinary callers remain
        # restricted to their own membership in the persisted scope.
        binding_agent = target_agent if mode == "agent" and admin else principal
        with self.store.connection() as conn:
            if mode == "share_group":
                binding_rows = conn.execute(
                    "SELECT binding_id,agent_instance_id,share_group_id,group_kind,mcp_server_name,"
                    "native_memory_mode,redirect_paths_json,status,revision,created_at,updated_at "
                    "FROM agent_group_bindings WHERE "
                    + ("share_group_id=? AND status='active' " if admin else "agent_instance_id=? AND share_group_id=? AND status='active' ")
                    + "ORDER BY agent_instance_id,share_group_id,binding_id",
                    (group,) if admin else (principal, group),
                ).fetchall()
            else:
                binding_rows = conn.execute(
                    "SELECT binding_id,agent_instance_id,share_group_id,group_kind,mcp_server_name,"
                    "native_memory_mode,redirect_paths_json,status,revision,created_at,updated_at "
                    "FROM agent_group_bindings WHERE agent_instance_id=? AND share_group_id=? "
                    "AND status='active' ORDER BY binding_id",
                    (binding_agent, group),
                ).fetchone()
                binding_rows = [] if binding_rows is None else [binding_rows]
            if not binding_rows:
                # A persisted GUI selection is not authority.  Once its
                # principal leaves the group, expose an empty scope so the
                # frontend clears the stale activeShareGroupId.
                return {"ok": True, "status": "succeeded", "empty": True, "scope": None}
            if mode == "agent" and len(binding_rows) != 1:
                # Multiple active bindings make an agent scope ambiguous.  Do
                # not choose one arbitrarily or expose a partial scope.
                return {"ok": True, "status": "succeeded", "empty": True, "scope": None}
            active_binding = self._binding(binding_rows[0])
            members = [
                str(item[0]) for item in conn.execute(
                    "SELECT DISTINCT agent_instance_id FROM agent_group_bindings "
                    "WHERE share_group_id=? AND status='active' ORDER BY agent_instance_id",
                    (group,),
                ).fetchall()
            ]
            if not members:
                return {"ok": True, "status": "succeeded", "empty": True, "scope": None}
        return {
            "ok": True,
            "status": "succeeded",
            "empty": False,
            "active_binding": active_binding,
            # Keep the long-lived ``scope`` object backward compatible.  The
            # GUI may use these authoritative members for presentation, but
            # older V2 clients compare ``scope`` as an exact persisted record.
            "members": members,
            "member_count": len(members),
            "scope": {
                "mode": mode,
                "agent_instance_id": str(row[1] or ""),
                "share_group_id": group,
                "revision": int(row[3]),
                "updated_at": str(row[4]),
            },
        }

    def set_scope(self, principal_agent_id: str, requested: Mapping[str, Any], *, admin: bool = False, idempotency_key: str = "") -> dict[str, Any]:
        principal = str(principal_agent_id or "").strip()
        if not principal:
            raise GroupControlError("trusted_agent_required")
        mode = str(requested.get("mode") or "").strip()
        target_agent = str(requested.get("agent_instance_id") or "").strip()
        target_group = str(requested.get("share_group_id") or requested.get("group_id") or "").strip()
        if mode not in {"agent", "share_group"}:
            raise GroupControlError("governance_scope_mode_invalid")
        request = {"principal": principal, "mode": mode, "agent": target_agent, "group": target_group, "admin": bool(admin)}
        key = idempotency_key or _digest("scope", _json(request))

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            if mode == "agent":
                agent = target_agent or principal
                if agent != principal and not admin:
                    raise GroupControlError("governance_scope_forbidden")
                binding = conn.execute("SELECT share_group_id FROM agent_group_bindings WHERE agent_instance_id=? AND status='active'", (agent,)).fetchone()
                if binding is None:
                    raise GroupControlError("active_binding_required")
                group = str(binding[0])
            else:
                group = target_group
                if not group:
                    raise GroupControlError("share_group_id_required")
                member = conn.execute("SELECT 1 FROM agent_group_bindings WHERE agent_instance_id=? AND share_group_id=? AND status='active'", (principal, group)).fetchone()
                if member is None and not admin:
                    raise GroupControlError("governance_scope_forbidden")
                agent = ""
            now = _now()
            existing = conn.execute("SELECT revision,created_at FROM governance_scopes WHERE principal_agent_id=?", (principal,)).fetchone()
            revision = int(existing[0]) + 1 if existing else 1
            created_at = str(existing[1]) if existing else now
            scope_id = "scope-" + _digest("gui-governance-scope", principal)
            conn.execute(
                "INSERT INTO governance_scopes(scope_id,principal_agent_id,mode,agent_instance_id,share_group_id,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(principal_agent_id) DO UPDATE SET mode=excluded.mode,agent_instance_id=excluded.agent_instance_id,share_group_id=excluded.share_group_id,revision=excluded.revision,updated_at=excluded.updated_at",
                (scope_id, principal, mode, agent, group, revision, created_at, now),
            )
            return ({"ok": True, "status": "succeeded", "scope": {"mode": mode, "agent_instance_id": agent, "share_group_id": group, "revision": revision}, "changed": True}, scope_id)

        return self.store.mutate("set_governance_scope", key, request, apply)

    def set_mode(self, mode: str, *, idempotency_key: str = "") -> dict[str, Any]:
        value = str(mode or "").strip()
        if value not in {"multi_agent_shared_mcp", "single_agent"}:
            raise GroupControlError("host_mode_invalid")
        key = idempotency_key or _digest("host-mode", value)
        request = {"mode": value}

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            now = _now()
            row = conn.execute("SELECT value_json,revision FROM control_preferences WHERE pref_key='host_mode'").fetchone()
            previous = ""
            if row is not None:
                try:
                    previous = str(json.loads(str(row[0] or "{}" )).get("mode") or "")
                except Exception:
                    previous = ""
            changed = previous != value
            revision = int(row[1]) + (1 if changed else 0) if row else 1
            conn.execute(
                "INSERT INTO control_preferences(pref_key,value_json,revision,updated_at) VALUES('host_mode',?,?,?) "
                "ON CONFLICT(pref_key) DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision,updated_at=excluded.updated_at",
                (_json({"mode": value}), revision, now),
            )
            return ({"ok": True, "status": "succeeded", "mode": value, "changed": changed, "revision": revision}, "host_mode")

        return self.store.mutate("set_host_mode", key, request, apply)

    @staticmethod
    def _provider_identity_key(provider: str) -> str:
        return "canonical_provider_identity:" + str(provider or "").strip().casefold()

    def provider_identity(self, provider: str) -> dict[str, Any] | None:
        key = self._provider_identity_key(provider)
        if not key.endswith(":") and self.store.db_path.is_file() and self.store._preflight() == "current":
            with self.store.connection() as conn:
                row = conn.execute(
                    "SELECT value_json FROM control_preferences WHERE pref_key=?",
                    (key,),
                ).fetchone()
            if row is None:
                return None
            try:
                data = json.loads(str(row[0] or "{}"))
            except Exception:
                return None
            if not isinstance(data, dict):
                return None
            canonical = str(data.get("canonical_id") or "").strip()
            if not canonical:
                return None
            aliases = [
                str(item).strip()
                for item in (data.get("aliases") or ())
                if str(item).strip() and str(item).strip() != canonical
            ]
            return {
                "provider": str(provider or "").strip().casefold(),
                "canonical_id": canonical,
                "share_group_id": str(data.get("share_group_id") or "").strip(),
                "aliases": aliases,
                "program_id": normalize_program_identity(str(provider or "")).get("program_id", "unknown"),
                "display_name": normalize_program_identity(str(provider or "")).get("display_name", "unknown"),
            }
        return None

    def record_provider_identity(
        self,
        provider: str,
        canonical_id: str,
        share_group_id: str,
        aliases: Sequence[str] = (),
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        product = str(provider or "").strip().casefold()
        canonical = str(canonical_id or "").strip()
        group = str(share_group_id or "").strip()
        extra = sorted({
            str(item).strip()
            for item in aliases
            if str(item).strip() and str(item).strip() != canonical
        })
        if not product or not canonical:
            raise GroupControlError("provider_identity_required")
        pref_key = self._provider_identity_key(product)
        request = {
            "provider": product,
            "canonical_id": canonical,
            "share_group_id": group,
            "aliases": extra,
        }
        key = idempotency_key or _digest("provider-identity", product, canonical, group, *extra)

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            now = _now()
            row = conn.execute(
                "SELECT value_json,revision FROM control_preferences WHERE pref_key=?",
                (pref_key,),
            ).fetchone()
            previous_aliases: list[str] = []
            previous_canonical = ""
            previous_group = ""
            if row is not None:
                try:
                    previous = json.loads(str(row[0] or "{}"))
                except Exception:
                    previous = {}
                if isinstance(previous, dict):
                    previous_canonical = str(previous.get("canonical_id") or "").strip()
                    previous_group = str(previous.get("share_group_id") or "").strip()
                    previous_aliases = [
                        str(item).strip()
                        for item in (previous.get("aliases") or ())
                        if str(item).strip()
                    ]
            merged = sorted({
                *previous_aliases,
                *extra,
                *(
                    [previous_canonical]
                    if previous_canonical and previous_canonical != canonical
                    else []
                ),
            })
            payload = {
                "provider": product,
                "canonical_id": canonical,
                "share_group_id": group or previous_group,
                "aliases": merged,
            }
            changed = (
                previous_canonical != canonical
                or previous_group != payload["share_group_id"]
                or previous_aliases != merged
            )
            revision = int(row[1]) + (1 if changed else 0) if row else 1
            conn.execute(
                "INSERT INTO control_preferences(pref_key,value_json,revision,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(pref_key) DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision,updated_at=excluded.updated_at",
                (pref_key, _json(payload), revision, now),
            )
            return ({"ok": True, "status": "succeeded", "changed": changed, "revision": revision, **payload}, pref_key)

        return self.store.mutate("record_provider_identity", key, request, apply)

    def record_selection(self, agent_instance_id: str, source_ids: Sequence[str], selection_digest: str, *, idempotency_key: str = "") -> dict[str, Any]:
        agent = str(agent_instance_id or "").strip()
        sources = sorted({str(item) for item in source_ids if str(item)})
        request = {"agent": agent, "sources": sources, "digest": str(selection_digest)}
        key = idempotency_key or _digest("selection", _json(request))

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            now = _now()
            conn.execute("UPDATE selection_manifests SET status='superseded',updated_at=? WHERE agent_instance_id=? AND status='active'", (now, agent))
            selection_id = "selection-" + _digest(agent, selection_digest)
            conn.execute(
                "INSERT INTO selection_manifests(selection_id,agent_instance_id,selection_digest,source_ids_json,status,created_at,updated_at) VALUES(?,?,?,?, 'active',?,?) "
                "ON CONFLICT(selection_id) DO UPDATE SET source_ids_json=excluded.source_ids_json,status='active',updated_at=excluded.updated_at",
                (selection_id, agent, str(selection_digest), _json(sources), now, now),
            )
            return ({"ok": True, "status": "succeeded", "selection_id": selection_id, "agent_instance_id": agent, "source_ids": sources, "source_count": len(sources)}, selection_id)

        return self.store.mutate("commit_selection", key, request, apply)

    def selected_source_ids(self, agent_instance_id: str) -> list[str]:
        if not self.store.db_path.is_file() or self.store._preflight() != "current":
            return []
        with self.store.connection() as conn:
            row = conn.execute("SELECT source_ids_json FROM selection_manifests WHERE agent_instance_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1", (str(agent_instance_id),)).fetchone()
        if row is None:
            return []
        try:
            value = json.loads(str(row[0] or "[]"))
        except Exception:
            return []
        return [str(item) for item in value if isinstance(item, str)] if isinstance(value, list) else []

    @staticmethod
    def _admin_memory_scope(workspace: Path, group_id: str):
        from ..memory.store import MemoryReadScope
        return MemoryReadScope(workspace_id=str(workspace), share_group_id=str(group_id), admin=True)

    @staticmethod
    def _governance_context(workspace: Path, group_id: str, trusted: Mapping[str, Any]):
        from ..governance_v2 import V2MutationContext
        return V2MutationContext(
            workspace_id=str(workspace),
            share_group_id=str(group_id),
            agent_instance_id=str(trusted.get("agent_instance_id") or ""),
            project_ref=str(trusted.get("project_ref") or ""),
            provider=str(trusted.get("provider") or "gui"),
            runtime_role=str(trusted.get("runtime_role") or "gui"),
            actor=str(trusted.get("agent_instance_id") or "gui-admin"),
            admin=True,
            authority="admin",
        )

    def export_group(self, group_id: str) -> dict[str, Any]:
        """Explicit user export of V2 Memory; this file is not an authority."""
        group = str(group_id or "").strip()
        if not group:
            raise GroupControlError("share_group_id_required")
        from ..memory.store import MemoryAtomStore
        layout = WorkspaceV2Layout(self.workspace)
        atoms = []
        if layout.memory_db.is_file():
            memory = MemoryAtomStore(self.workspace, readonly=True)
            atoms = memory.list_atoms(scope=self._admin_memory_scope(self.workspace, group), include_building=True)
        rows = [atom.to_dict() for atom in atoms]
        export_digest = hashlib.sha256(_json(rows).encode("utf-8")).hexdigest()
        export_id = "group-export-" + _digest(group, export_digest)
        path = self.workspace / ".memoryguard" / "exports" / f"{export_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "memoryguard-v2-group-export-1",
            "share_group_id": group,
            "export_digest": export_digest,
            "records": rows,
        }
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
            raise GroupControlError("group_export_collision")
        path.write_bytes(data)
        return {
            "ok": True, "status": "succeeded", "share_group_id": group,
            "export_id": export_id, "export_path": str(path),
            "records_written": len(rows), "export_digest": export_digest,
        }

    def clear_group(self, group_id: str, *, trusted: Mapping[str, Any]) -> dict[str, Any]:
        """Export then tombstone all V2 atoms, compensating on partial failure."""
        group = str(group_id or "").strip()
        exported = self.export_group(group)
        from ..governance_v2 import GovernanceV2
        from ..memory.store import MemoryAtomStore
        layout = WorkspaceV2Layout(self.workspace)
        if not layout.memory_db.is_file():
            return {**exported, "before": 0, "after": 0, "changed": False}
        memory = MemoryAtomStore(self.workspace, readonly=False)
        governance = GovernanceV2(self.workspace, memory_store=memory)
        ctx = self._governance_context(self.workspace, group, trusted)
        atoms = memory.list_atoms(scope=self._admin_memory_scope(self.workspace, group), status="active", include_building=True)
        decisions: list[str] = []
        try:
            for atom in atoms:
                _persisted, decision = governance.tombstone(
                    atom.memory_id,
                    context=ctx,
                    reason="clear V2 memory group",
                    idempotency_key=f"clear_group:{group}:{atom.atom_id}:{atom.revision}",
                )
                decisions.append(decision.decision_id)
        except Exception:
            for decision_id in reversed(decisions):
                try:
                    governance.undo(decision_id, context=ctx, reason="compensate failed group clear")
                except Exception:
                    pass
            raise
        after = memory.list_atoms(scope=self._admin_memory_scope(self.workspace, group), status="active", include_building=True)
        result = {
            "ok": True, "status": "succeeded", "share_group_id": group,
            "export_id": exported["export_id"], "export_path": exported["export_path"],
            "before": len(atoms), "after": len(after), "changed": bool(atoms),
            "binding_preserved": True, "native_files_changed": False,
        }
        request = {"group": group, "export_id": exported["export_id"], "before": len(atoms)}
        self.store.mutate(
            "clear_memory_group", _digest("clear-group-receipt", group, exported["export_id"]), request,
            lambda _conn: (result, group),
        )
        return result

    def archive_group(self, group_id: str) -> dict[str, Any]:
        """Export then deactivate bindings; V2 Memory remains preserved in-place."""
        group = str(group_id or "").strip()
        exported = self.export_group(group)
        dissolved = self.dissolve(group, idempotency_key=_digest("archive-group-dissolve", group, exported["export_id"]))
        return {
            **dissolved,
            "export_id": exported["export_id"],
            "export_path": exported["export_path"],
            "data_preserved": True,
            "native_files_changed": False,
        }

    def commit_governance(
        self,
        group_id: str,
        *,
        reason: str,
        trusted: Mapping[str, Any],
    ) -> dict[str, Any]:
        group = str(group_id or "").strip()
        if not group:
            raise GroupControlError("share_group_id_required")
        from ..memory.store import MemoryAtomStore
        layout = WorkspaceV2Layout(self.workspace)
        atoms = []
        if layout.memory_db.is_file():
            memory = MemoryAtomStore(self.workspace, readonly=True)
            atoms = memory.list_atoms(scope=self._admin_memory_scope(self.workspace, group), status="active")
        atom_digest = hashlib.sha256(_json([(atom.atom_id, atom.revision, atom.canonical_hash) for atom in atoms]).encode("utf-8")).hexdigest()
        version_id = "governance-" + _digest(group, atom_digest, reason)
        result = {
            "ok": True, "status": "succeeded", "share_group_id": group,
            "version_id": version_id, "active_records": len(atoms),
            "takeover_mode": "shared_mcp", "projection_warning": "",
        }
        request = {"group": group, "atom_digest": atom_digest, "reason_digest": hashlib.sha256(str(reason).encode("utf-8")).hexdigest()}
        self.store.mutate(
            "commit_shared_memory_governance", _digest("governance", version_id), request,
            lambda _conn: (result, group),
        )
        try:
            from .projection_build import ProjectionBuildService, projection_scope_from_context
            scope_context = {**dict(trusted), "share_group_id": group}
            ProjectionBuildService(self.workspace).build(
                mode="reconstructed",
                scope=projection_scope_from_context(self.workspace, scope_context),
                runtime_role=str(trusted.get("runtime_role") or "gui"),
            )
        except Exception as exc:
            result["projection_warning"] = str(getattr(exc, "code", type(exc).__name__))
        return result

    def install_redirects(self, group_id: str) -> dict[str, Any]:
        """Install user-level provider config with rollback on partial failure."""
        group = str(group_id or "").strip()
        bindings = self._read_bindings(include_inactive=False, group_id=group)
        if not bindings:
            raise GroupControlError("group_has_no_active_bindings")
        from ..agent_locator import AgentLocator
        from ..provider_adapters import get_provider_adapter_class
        instances, _ = AgentLocator(self.workspace).detect_instances()
        product_by_id = {str(item.instance_id): str(item.product) for item in instances}
        catalog = self.identity_catalog()
        product_by_id.update({key: str(value.get("program_id") or "") for key, value in catalog.items()})
        installed: list[dict[str, Any]] = []
        newly_configured: list[Any] = []
        installed_programs: set[str] = set()
        failure = False
        for binding in bindings:
            agent_id = binding["agent_instance_id"]
            product = product_by_id.get(agent_id, "")
            adapter_cls = get_provider_adapter_class(product)
            if adapter_cls is None:
                installed.append({"agent_instance_id": agent_id, "product": product, "status": "skipped", "skipped": True, "reason": "automatic_install_adapter_not_implemented"})
                continue
            if product in installed_programs:
                continue
            canonical = self.active_binding_for_agent(agent_id, identity_catalog=catalog)
            if canonical is None or str(canonical.get("share_group_id") or "") != group:
                raise GroupControlError("provider_identity_group_mismatch")
            agent_id = str(canonical["agent_instance_id"])
            installed_programs.add(product)
            adapter = adapter_cls(self.workspace)
            try:
                before = adapter.status()
            except Exception:
                before = {}
            preconfigured = bool(before.get("configured") or before.get("installed")) if isinstance(before, Mapping) else False
            try:
                result = adapter.install(
                    workspace=self.workspace,
                    share_group_id=group,
                    agent_instance_id=agent_id,
                    global_scope=True,
                )
                item = dict(result)
                item.update({"agent_instance_id": agent_id, "product": product})
                hook_error = isinstance(item.get("hook"), Mapping) and str(item["hook"].get("status") or "") == "error"
                if not bool(item.get("configured", item.get("installed", False))) or hook_error:
                    failure = True
                    item["status"] = "error"
                else:
                    item["status"] = "configured"
                    if not preconfigured:
                        # Preserve the exact adapter instance that performed the
                        # install.  Global installs may mutate its effective
                        # workspace/config target; reconstructing a fresh
                        # adapter here would uninstall the wrong (project)
                        # location during compensation.
                        newly_configured.append(adapter)
                installed.append(item)
            except Exception as exc:
                failure = True
                installed.append({"agent_instance_id": agent_id, "product": product, "status": "error", "error": type(exc).__name__})
        rollback_errors = 0
        if failure:
            for adapter in reversed(newly_configured):
                try:
                    adapter.uninstall()
                except Exception:
                    rollback_errors += 1
            return {
                "ok": False, "status": "failure", "share_group_id": group,
                "installed": installed, "configured_count": 0,
                "installed_count": 0,
                "skipped_count": sum(item.get("status") == "skipped" for item in installed),
                "error_count": sum(item.get("status") == "error" for item in installed),
                "hook_configured_count": 0,
                "hook_unsupported_count": sum(isinstance(item.get("hook"), Mapping) and item["hook"].get("supported") is False for item in installed),
                "hook_error_count": sum(isinstance(item.get("hook"), Mapping) and item["hook"].get("status") == "error" for item in installed),
                "warning_count": sum(len(item.get("warnings", [])) for item in installed),
                "rollback_errors": rollback_errors,
                "runtime_verified": False,
            }
        configured_count = sum(item.get("status") == "configured" for item in installed)
        result = {
            "ok": True, "status": "configured", "share_group_id": group,
            "installed": installed, "configured_count": configured_count,
            "installed_count": configured_count,
            "skipped_count": sum(item.get("status") == "skipped" for item in installed),
            "error_count": 0,
            "hook_configured_count": sum(bool((item.get("hook") or {}).get("configured")) for item in installed if isinstance(item.get("hook"), Mapping)),
            "hook_unsupported_count": sum(isinstance(item.get("hook"), Mapping) and item["hook"].get("supported") is False for item in installed),
            "hook_error_count": 0,
            "warning_count": sum(len(item.get("warnings", [])) for item in installed),
            "restart_required": configured_count > 0,
            "runtime_verified": False,
        }
        self.store.mutate(
            "install_shared_group_mcp_redirects", _digest("redirects", group, *(item["binding_id"] for item in bindings)),
            {"group": group, "bindings": [item["binding_id"] for item in bindings]},
            lambda _conn: (result, group),
        )
        return result

    @staticmethod
    def _bounded_files(path: Path, *, limit: int = 2000) -> list[Path]:
        if path.is_file():
            return [path]
        if not path.is_dir():
            return []
        result: list[Path] = []
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                continue
            if item.is_file():
                result.append(item)
                if len(result) >= limit:
                    break
        return result

    def import_native_memories(
        self,
        group_id: str,
        *,
        agent_instance_ids: Sequence[str] | None,
        trusted: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Import selected native-memory files through Content then GovernanceV2."""
        group = str(group_id or "").strip()
        bindings = self._read_bindings(include_inactive=False, group_id=group)
        agents = [str(item) for item in (agent_instance_ids or ()) if str(item)] or [item["agent_instance_id"] for item in bindings]
        if not agents:
            raise GroupControlError("no_agents_in_group")
        bound = {item["agent_instance_id"] for item in bindings}
        if any(agent not in bound for agent in agents):
            raise GroupControlError("agent_not_bound_to_group")
        content = ContentStore(self.workspace)
        connectors = {str(row.get("source_id") or ""): row for row in content.list_source_connectors(workspace_id=str(self.workspace), enabled=True)}
        selected: list[tuple[str, Mapping[str, Any]]] = []
        for agent in agents:
            for source_id in self.selected_source_ids(agent):
                row = connectors.get(source_id)
                if row is not None:
                    selected.append((agent, row))
        if not selected:
            return {"ok": True, "status": "succeeded", "share_group_id": group, "records_written": 0, "sources": 0, "changed": False}

        from ..content_parsers import parse_file
        from ..governance_v2 import GovernanceV2
        from ..memory.store import MemoryAtom, MemoryAtomStore
        memory = MemoryAtomStore(self.workspace, readonly=False)
        governance = GovernanceV2(self.workspace, memory_store=memory)
        ctx = self._governance_context(self.workspace, group, trusted)
        decisions: list[str] = []
        occurrences: list[str] = []
        written = 0
        try:
            for agent, connector in selected:
                raw_root = str(connector.get("external_root_key") or "").strip()
                root = Path(raw_root).expanduser()
                if not root.is_absolute():
                    root = self.workspace / root
                root = root.resolve()
                for file_path in self._bounded_files(root):
                    try:
                        if file_path.stat().st_size > 2 * 1024 * 1024:
                            continue
                        raw = file_path.read_bytes()
                    except OSError:
                        continue
                    file_digest = hashlib.sha256(raw).hexdigest()
                    segments = parse_file(file_path, content=raw, verbatim=False)
                    for index, segment in enumerate(segments[:256]):
                        body = str(segment.body or "").strip()
                        if not body:
                            continue
                        blob_id = content.put_blob(body)
                        if not blob_id:
                            continue
                        object_id = stable_id("native-import-object", str(connector.get("source_id") or ""), str(file_path))
                        occurrence_id = content.upsert_occurrence(
                            source_object_id=object_id,
                            occurrence_key=f"segment:{index}:{file_digest[:16]}",
                            blob_id=blob_id,
                            source_id=str(connector.get("source_id") or ""),
                            source_kind="native_memory",
                            external_object_key=str(file_path),
                            object_type="document",
                            source_revision=file_digest,
                            ordinal=index,
                            locator={"line_start": int(getattr(segment, "line_start", 0) or 0), "line_end": int(getattr(segment, "line_end", 0) or 0)},
                            content_role="memory_import",
                            sensitivity="normal",
                            workspace_id=str(self.workspace),
                            agent_instance_id=agent,
                            project_ref=str(trusted.get("project_ref") or ""),
                            share_group_id=group,
                            policy_class="private",
                            provider=str(trusted.get("provider") or "gui"),
                        )
                        occurrences.append(occurrence_id)
                        body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
                        memory_id = "native-import-" + _digest(str(connector.get("source_id") or ""), file_digest, index, body_digest)[:32]
                        atom = MemoryAtom(
                            memory_id=memory_id,
                            body=body,
                            kind=str(getattr(segment, "kind_hint", "") or "fact"),
                            workspace_id=str(self.workspace),
                            share_group_id=group,
                            agent_instance_id=str(trusted.get("agent_instance_id") or ""),
                            project_ref=str(trusted.get("project_ref") or ""),
                            provider=str(trusted.get("provider") or "gui"),
                            runtime_role=str(trusted.get("runtime_role") or "gui"),
                            metadata={"origin": "v2_native_group_import", "source_occurrence_id": occurrence_id},
                            provenance=[{"source": "content", "source_ref": f"content:{blob_id}", "source_digest": body_digest}],
                        )
                        _persisted, decision = governance.put_atom(
                            atom,
                            context=ctx,
                            evidence=[{"source_ref": f"content:{blob_id}", "revision": file_digest, "digest": body_digest, "authority": "governance", "metadata": {"occurrence_id": occurrence_id}}],
                            reason="import selected native memory into V2 group",
                            confidence=1.0,
                            idempotency_key=f"native-group-import:{group}:{memory_id}",
                        )
                        decisions.append(decision.decision_id)
                        written += 1
            while memory.pending_outbox(include_failed=True):
                state = memory.project_evidence(governance.evidence)
                if int(state.get("projected", 0)) == 0:
                    break
            building = memory.list_building_atoms(scope=self._admin_memory_scope(self.workspace, group))
            if building:
                memory.set_visibility("active", atom_ids=[item.atom_id for item in building])
        except Exception:
            for decision_id in reversed(decisions):
                try:
                    governance.undo(decision_id, context=ctx, reason="compensate failed native group import")
                except Exception:
                    pass
            for occurrence_id in occurrences:
                try:
                    content.tombstone_occurrence(occurrence_id, reason="native_group_import_rollback")
                except Exception:
                    pass
            raise
        result = {"ok": True, "status": "succeeded", "share_group_id": group, "records_written": written, "sources": len(selected), "changed": written > 0}
        self.store.mutate(
            "import_native_memories_to_group", _digest("native-import-receipt", group, *(sorted(agents))),
            {"group": group, "agents": sorted(agents), "records_written": written},
            lambda _conn: (result, group),
        )
        return result


__all__ = [
    "CONTROL_SCHEMA_MARKER", "CONTROL_SCHEMA_VERSION", "GroupControlError",
    "GroupControlService", "SystemControlStore", "personal_group_id",
]
