"""Shared deterministic governance semantics for V2 memory and rule planes.

This module intentionally owns only *classification*.  It does not mutate any
store.  Both the memory organizer and canonical rule reconciliation call the
same functions so exact duplicates, semantic equivalents, updates and
conflicts cannot drift into two incompatible policy implementations.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import re
from typing import Any, Mapping

from ..rule_definition import build_definition, normalize_rule_text
from ..rule_scope import canonical_project_ref


_MERGEABLE = frozenset({"exact", "equivalent", "update", "additive"})
_NEGATIVE_MARKERS = (
    "不要", "不得", "禁止", "严禁", "不能", "不允许", "不应", "不要再",
    "do not", "don't", "must not", "never", "forbid", "forbidden", "without",
)
_TEXT_REPLACEMENTS = (
    ("預設使用", "默认使用"),
    ("預設", "默认"),
    ("默认用", "默认使用"),
    ("优先用", "优先使用"),
    ("採用", "使用"),
    ("采用", "使用"),
    ("启用", "使用"),
    ("sub-agents", "subagent"),
    ("sub agents", "subagent"),
    ("subagents", "subagent"),
    ("sub-agent", "subagent"),
    ("sub agent", "subagent"),
    ("子-agent", "subagent"),
    ("子 agent", "subagent"),
    ("子agent", "subagent"),
    ("子代理", "subagent"),
    ("以及", "和"),
    ("并且", "和"),
    ("同时", "和"),
    ("與", "和"),
    ("与", "和"),
)
_FILLER = frozenset({
    "default", "defaults", "please", "always", "must", "for", "rule", "rules",
    "agent", "subagent",
    "\u5168\u5c40", "\u5168\u5e73\u53f0", "\u7528\u6237", "\u8981\u6c42",
    "\u4e3b", "\u6240\u6709", "\u4e0e", "\u548c", "\u5e76\u884c", "\u5b50\u4ee3\u7406",
    "默认", "规则", "请", "应", "应该", "需要", "必须",
})
_GENERIC_LATIN = frozenset({
    "use", "using", "default", "always", "must", "subagent", "mandatory", "agent",
    "rule", "rules", "required", "require", "procedure", "policy",
    "the", "a", "an", "for", "to", "and", "or", "by", "is",
})
# Closed list: user-opt-out hedges, not project/environment exception clauses.
_USER_DISABLE_HEDGES = (
    "除非用户明确要求关闭",
    "除非用户明确关闭",
    "unless the user explicitly asks to turn it off",
    "unless the user explicitly disables it",
    "unless the user turns it off",
    "unless explicitly disabled",
)
_LATIN_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.+/#&|-]*", re.I)
_HAN_RUN = re.compile(r"[\u3400-\u9fff]+")
_LATIN_SPLIT = re.compile(r"[/,+#&|]+")
_CLAIM_BREAK = re.compile(r"[\r\n]+|(?<=[。！？!?；;])\s*|(?<=[.!?])(?=\s|$)")
_FORMAT_PREFIX = re.compile(
    r"^\s*(?:(?:[-*+•◦▪▸‣])\s+|\[\d+\]\s+|\d+[.)]\s+)+"
)
_EXCEPTION_MARK = re.compile(r"(除非|unless\b)\s*(.*)$", re.I)
_TRAILING_PUNCT = re.compile(r"[\s,，、;；。.!！？?]+$")


@dataclass(frozen=True)
class GovernanceRelation:
    """Body-only semantic relationship; scope is checked separately."""

    kind: str
    score: float
    reason: str
    winner: str = ""  # "left" | "right" | ""

    @property
    def mergeable(self) -> bool:
        return self.kind in _MERGEABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "score": round(float(self.score), 6),
            "reason": self.reason,
            "winner": self.winner,
            "mergeable": self.mergeable,
        }


def _semantic_surface(value: str) -> str:
    text = str(value or "").strip().casefold()
    for old, new in _TEXT_REPLACEMENTS:
        text = text.replace(old, new)
    text = re.sub(r"[\s\u3000]+", " ", text)
    return text


def _latin_terms(value: str) -> set[str]:
    terms: set[str] = set()
    for item in _LATIN_TOKEN.findall(value):
        for part in _LATIN_SPLIT.split(item):
            token = part.strip("._+/#&|-")
            if token:
                terms.add(token)
    return terms


def semantic_tokens(value: str) -> frozenset[str]:
    """Return bounded language-neutral-ish terms for deterministic comparison."""

    text = _semantic_surface(value)
    tokens: set[str] = set()
    for token in _latin_terms(text):
        if token not in _FILLER and token not in _GENERIC_LATIN:
            tokens.add(token)
    for run in _HAN_RUN.findall(text):
        normalized = run
        for filler in _FILLER:
            if any("\u3400" <= ch <= "\u9fff" for ch in filler):
                normalized = normalized.replace(filler, "")
        if not normalized:
            continue
        # Single characters preserve short Chinese object names; bigrams make
        # paraphrases comparable without treating the whole sentence as one token.
        tokens.update(ch for ch in normalized if ch.strip())
        tokens.update(normalized[index:index + 2] for index in range(max(0, len(normalized) - 1)))
    return frozenset(tokens)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _polarity(value: str) -> str:
    text = _semantic_surface(value)
    if any(marker in text for marker in _NEGATIVE_MARKERS):
        return "negative"
    try:
        return str(build_definition(value).polarity or "positive")
    except Exception:
        return "positive"


def _intent_key(value: str) -> tuple[str, tuple[str, ...]]:
    try:
        definition = build_definition(value)
        payload = json.loads(definition.normalized_intent or "{}")
        if not isinstance(payload, Mapping):
            return "", ()
        action = str(payload.get("action") or "").casefold()
        object_value = str(payload.get("object") or "").casefold()
        params = tuple(sorted(str(item).casefold() for item in payload.get("parameters", []) if str(item)))
        return f"{action}|{object_value}", params
    except Exception:
        return "", ()


def _specificity(value: str) -> tuple[int, int]:
    surface = _semantic_surface(value)
    return len(semantic_tokens(surface)), len(surface)


def _strip_format_prefix(value: str) -> str:
    current = str(value or "").strip()
    previous = None
    while current and current != previous:
        previous = current
        current = _FORMAT_PREFIX.sub("", current).strip()
    return current


def _split_governance_claims(value: str) -> tuple[str, ...]:
    claims: list[str] = []
    for piece in _CLAIM_BREAK.split(str(value or "")):
        claim = _strip_format_prefix(piece)
        if claim:
            claims.append(claim)
    return tuple(claims) if claims else ((str(value or "").strip(),) if str(value or "").strip() else ())


def _strip_user_disable_hedges(value: str) -> str:
    surface = _semantic_surface(value)
    for hedge in sorted(_USER_DISABLE_HEDGES, key=len, reverse=True):
        needle = _semantic_surface(hedge)
        if needle:
            surface = surface.replace(needle, " ")
    surface = re.sub(r"[\s,，、;；]+", " ", surface)
    return _TRAILING_PUNCT.sub("", surface).strip(" ,，、;；")


def _claim_core_and_exception(value: str) -> tuple[str, str]:
    stripped = _strip_user_disable_hedges(value)
    match = _EXCEPTION_MARK.search(stripped)
    if not match:
        return stripped, ""
    core = _TRAILING_PUNCT.sub("", stripped[:match.start()]).strip(" ,，、;；")
    exception = _semantic_surface(match.group(2))
    exception = _TRAILING_PUNCT.sub("", exception).strip(" ,，、;；")
    return core, exception


def _content_latin(value: str) -> frozenset[str]:
    return frozenset(
        item for item in _latin_terms(_semantic_surface(value))
        if item not in _GENERIC_LATIN
    )


def _remaining_exceptions(value: str) -> frozenset[str]:
    return frozenset(
        exception
        for claim in _split_governance_claims(value)
        for exception in (_claim_core_and_exception(claim)[1],)
        if exception
    )


def _exceptions_compatible(left: str, right: str) -> bool:
    return _remaining_exceptions(left) == _remaining_exceptions(right)


def _latin_subset_gate(left: frozenset[str], right: frozenset[str]) -> bool:
    if not left or not right:
        return False
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    return len(smaller) >= 2 and smaller <= larger


def _claim_update_relation(
    left_core: str,
    right_core: str,
    left_latin: frozenset[str],
    right_latin: frozenset[str],
    score: float,
) -> GovernanceRelation:
    left_tokens, right_tokens = semantic_tokens(left_core), semantic_tokens(right_core)
    # Equal content anchors are usually a rephrase.  A strict residual-token
    # extension is still a real update: Latin anchors intentionally ignore
    # generic syntax, but they must not hide an added CJK constraint.
    if left_latin == right_latin:
        if right_tokens < left_tokens:
            return GovernanceRelation("update", max(score, 0.82), "left_semantic_superset", "left")
        if left_tokens < right_tokens:
            return GovernanceRelation("update", max(score, 0.82), "right_semantic_superset", "right")
        return GovernanceRelation("equivalent", max(score, 0.82), "same_claim_anchors")
    if right_latin < left_latin:
        return GovernanceRelation("update", max(score, 0.82), "left_semantic_superset", "left")
    if left_latin < right_latin:
        return GovernanceRelation("update", max(score, 0.82), "right_semantic_superset", "right")
    if right_tokens <= left_tokens and len(left_tokens) > len(right_tokens):
        return GovernanceRelation("update", max(score, 0.82), "left_semantic_superset", "left")
    if left_tokens <= right_tokens and len(right_tokens) > len(left_tokens):
        return GovernanceRelation("update", max(score, 0.82), "right_semantic_superset", "right")
    winner = "right" if _specificity(right_core) > _specificity(left_core) else "left"
    reason = "right_semantic_superset" if winner == "right" else "left_semantic_superset"
    return GovernanceRelation("update", max(score, 0.82), reason, winner)


def _claim_level_relation(left: str, right: str, score: float) -> GovernanceRelation | None:
    """Match same-topic claims inside mixed documents before document fallback.

    Document polarity, intent, and Han Jaccard look at the whole body, so a
    positive caveman/RTK claim can be hidden by an unrelated negative sibling.
    This path only fires on content-Latin subset containment of at least two
    anchors, and it refuses unmatched exception clauses.
    """

    left_claims = _split_governance_claims(left)
    right_claims = _split_governance_claims(right)
    if not left_claims or not right_claims:
        return None

    conflict: GovernanceRelation | None = None
    update: GovernanceRelation | None = None
    insufficient_anchors: GovernanceRelation | None = None
    update_rank = -1
    for left_claim in left_claims:
        left_core, left_exception = _claim_core_and_exception(left_claim)
        left_latin = _content_latin(left_core)
        left_polarity = _polarity(left_core or left_claim)
        for right_claim in right_claims:
            right_core, right_exception = _claim_core_and_exception(right_claim)
            right_latin = _content_latin(right_core)
            right_polarity = _polarity(right_core or right_claim)
            if (
                left_polarity == right_polarity
                and left_exception == right_exception
                and left_latin
                and right_latin
                and (left_latin <= right_latin or right_latin <= left_latin)
                and min(len(left_latin), len(right_latin)) == 1
                and max(len(left_latin), len(right_latin)) >= 2
                and _intent_key(left_core)[0] != _intent_key(right_core)[0]
                and insufficient_anchors is None
            ):
                # One named tool cannot safely stand in for a multi-tool claim.
                # Return this only if no full claim relation appears below.
                insufficient_anchors = GovernanceRelation(
                    "distinct",
                    min(score, 0.49),
                    "insufficient_claim_anchors",
                )
            if not _latin_subset_gate(left_latin, right_latin):
                continue
            if left_polarity != right_polarity:
                if left_latin == right_latin and conflict is None:
                    conflict = GovernanceRelation(
                        "conflict",
                        max(score, 0.8),
                        "opposite_polarity",
                    )
                continue
            if left_exception != right_exception:
                continue
            candidate = _claim_update_relation(
                left_core, right_core, left_latin, right_latin, score,
            )
            rank = abs(len(left_latin) - len(right_latin))
            if update is None or rank > update_rank:
                update = candidate
                update_rank = rank
    if conflict is not None:
        return conflict
    return update or insufficient_anchors


def classify_governance_relation(left: str, right: str) -> GovernanceRelation:
    """Classify two governed statements without mutating either plane.

    The thresholds are deliberately conservative.  A false negative merely
    leaves two records for later model reconciliation; a false positive could
    widen or erase a rule, which is the dangerous direction.
    """

    left_raw, right_raw = str(left or "").strip(), str(right or "").strip()
    if not left_raw or not right_raw:
        return GovernanceRelation("distinct", 0.0, "empty_side")
    left_norm, right_norm = normalize_rule_text(left_raw), normalize_rule_text(right_raw)
    if left_norm == right_norm:
        winner = "right" if _specificity(right_raw) > _specificity(left_raw) else "left"
        return GovernanceRelation("exact", 1.0, "canonical_surface_equal", winner)

    left_tokens, right_tokens = semantic_tokens(left_raw), semantic_tokens(right_raw)
    jaccard = _jaccard(left_tokens, right_tokens)
    sequence = SequenceMatcher(None, _semantic_surface(left_raw), _semantic_surface(right_raw)).ratio()
    score = max(jaccard, sequence * 0.85)
    left_intent, left_params = _intent_key(left_raw)
    right_intent, right_params = _intent_key(right_raw)
    left_latin = {
        item for item in _latin_terms(_semantic_surface(left_raw))
        if item not in _GENERIC_LATIN
    }
    right_latin = {
        item for item in _latin_terms(_semantic_surface(right_raw))
        if item not in _GENERIC_LATIN
    }
    anchor_conflict = bool(
        left_latin
        and right_latin
        and (left_latin - right_latin)
        and (right_latin - left_latin)
    )
    same_action_object = bool(
        left_intent
        and left_intent == right_intent
    )
    params_compatible = left_params == right_params or (
        bool(left_params)
        and bool(right_params)
        and (left_params <= right_params or right_params <= left_params)
    )
    same_intent = bool(same_action_object and params_compatible and not anchor_conflict)
    left_polarity, right_polarity = _polarity(left_raw), _polarity(right_raw)

    claim_relation = _claim_level_relation(left_raw, right_raw, score)
    if claim_relation is not None:
        return claim_relation

    if left_polarity != right_polarity and (same_intent or score >= 0.58):
        return GovernanceRelation("conflict", max(score, 0.8 if same_intent else score), "opposite_polarity")

    if anchor_conflict:
        return GovernanceRelation("distinct", min(score, 0.49), "different_lexical_anchor")

    if left_polarity == right_polarity and _exceptions_compatible(left_raw, right_raw):
        left_surface = _semantic_surface(left_raw)
        right_surface = _semantic_surface(right_raw)
        if same_intent:
            if left_surface in right_surface or left_norm in right_norm:
                return GovernanceRelation("update", max(score, 0.94), "same_intent_right_extends_left", "right")
            if right_surface in left_surface or right_norm in left_norm:
                return GovernanceRelation("update", max(score, 0.94), "same_intent_left_extends_right", "left")
            return GovernanceRelation("equivalent", max(score, 0.90), "same_normalized_intent")

        overlap = left_tokens & right_tokens
        if overlap:
            left_subset = left_tokens <= right_tokens
            right_subset = right_tokens <= left_tokens
            if left_subset and len(right_tokens) > len(left_tokens) and score >= 0.58:
                return GovernanceRelation("update", max(score, 0.82), "right_semantic_superset", "right")
            if right_subset and len(left_tokens) > len(right_tokens) and score >= 0.58:
                return GovernanceRelation("update", max(score, 0.82), "left_semantic_superset", "left")
        if score >= 0.78:
            winner = "right" if _specificity(right_raw) > _specificity(left_raw) else "left"
            return GovernanceRelation("equivalent", score, "high_semantic_overlap", winner)

    return GovernanceRelation("distinct", score, "insufficient_safe_overlap")


def _audience_mapping(metadata: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    raw = metadata.get("audience")
    return raw if isinstance(raw, Mapping) else {}


def governance_scope_key(
    *,
    metadata: Mapping[str, Any] | None = None,
    agent_instance_id: str = "",
    share_group_id: str = "",
    project_ref: str = "",
    provider: str = "",
    runtime_role: str = "",
) -> tuple[str, str, str, str, str, str, str]:
    """Return the audience identity that must match before semantic merging."""

    audience = _audience_mapping(metadata)
    target_type = str(audience.get("target_type") or "").strip().casefold()
    target_id = str(audience.get("target_id") or "").strip()
    group = str(share_group_id or audience.get("share_group_id") or "").strip()
    project = canonical_project_ref(str(audience.get("project_ref") or project_ref or ""))
    provider_value = str(audience.get("provider") or provider or "").strip().casefold()
    runtime = str(audience.get("runtime_role") or audience.get("runtime") or runtime_role or "").strip().casefold()
    effect = str(audience.get("effect") or "include").strip().casefold() or "include"
    if not target_type:
        target_type = "agent" if agent_instance_id else "group"
    if not target_id:
        if target_type in {"agent", "agent_project"}:
            target_id = str(agent_instance_id or "").strip()
        elif target_type == "project":
            target_id = project
        elif target_type == "group":
            target_id = group
    # Native audience types are the full read contract.  Owner provenance
    # (project/provider/runtime) must not split one agent/group rule into a
    # new dedup domain whenever the same owner changes tasks or models.
    if audience.get("source") == "native_v2":
        if target_type in {"agent", "group"}:
            project = ""
        if target_type in {"agent", "agent_project", "project", "group"}:
            provider_value = runtime = ""
    return target_type, target_id, group, project, provider_value, runtime, effect


def same_governance_scope(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return governance_scope_key(**left) == governance_scope_key(**right)


__all__ = [
    "GovernanceRelation",
    "classify_governance_relation",
    "governance_scope_key",
    "same_governance_scope",
    "semantic_tokens",
]
