"""Deterministic pre-grouping done before any LLM call, to keep LLM inputs small
and bounded. Mirrors the two-phase structure of
bd_sig_filter/bd_sig_filter/ComponentListClass.py::process(), but produces
*candidate groups* for the LLM to adjudicate rather than a final verdict.
"""

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .models import BomComponent, CurationAction, CurationDecision, same_component


@dataclass
class DuplicateGroup:
    components: list[BomComponent] = field(default_factory=list)


@dataclass
class OverlapGroup:
    components: list[BomComponent] = field(default_factory=list)
    common_prefix: str = ""


def split_dependency_vs_judgment(components: list[BomComponent]):
    """Dependency matches are high-confidence and can be short-circuited (reviewed)
    without an LLM call. Everything else (signature-only, Match Review) needs
    judgment and is returned for further grouping/LLM review.

    Already-reviewed/ignored BOM components are settled here too - they don't need
    a new action, so they're reported as explicit no-op decisions and excluded from
    the LLM pipeline. UNMATCHED Match Review items are dropped entirely in
    bom_data.fetch_review_components before reaching this function.
    """
    auto_reviewed = []
    needs_judgment = []
    settled_decisions: list[CurationDecision] = []

    for comp in components:
        # reviewStatus/ignored are BOM-entry concepts - Match Review candidates can
        # come back REVIEWED from the server independent of their own Match Review
        # confirm/reject state, so this only applies to real BOM entries, never to
        # Match Review items.
        if comp.match_review_type is None and comp.ignored:
            settled_decisions.append(CurationDecision(
                action=CurationAction.IGNORE,
                target_name=comp.name,
                target_version=comp.version,
                target_href=comp.href,
                confidence=1.0,
                reasoning="Component is already ignored in the BOM - no action needed.",
            ))
            continue
        if comp.match_review_type is None and comp.is_reviewed():
            settled_decisions.append(CurationDecision(
                action=CurationAction.CONFIRM_REVIEWED,
                target_name=comp.name,
                target_version=comp.version,
                target_href=comp.href,
                confidence=1.0,
                reasoning="Component is already marked reviewed in the BOM - no action needed.",
            ))
            continue

        if comp.is_dependency():
            auto_reviewed.append(comp)
        else:
            needs_judgment.append(comp)

    return auto_reviewed, needs_judgment, settled_decisions


def find_duplicate_groups(components: list[BomComponent]) -> list[DuplicateGroup]:
    """Group components sharing the same component_id or case-insensitive name
    across different versions/origins - same comparison
    ComponentListClass.process() uses for its duplicate-filter phase.
    """
    groups: list[DuplicateGroup] = []
    assigned: set[int] = set()

    for i, comp1 in enumerate(components):
        if i in assigned:
            continue

        group = [comp1]
        for j in range(i + 1, len(components)):
            if j in assigned:
                continue
            comp2 = components[j]
            if same_component(comp1, comp2):
                group.append(comp2)
                assigned.add(j)

        if len(group) > 1:
            assigned.add(i)
            groups.append(DuplicateGroup(components=group))

    return groups


def _common_path_prefix(paths: list[str]) -> str:
    if not paths:
        return ""
    segments = [p.split('/') for p in paths]
    shortest = min(len(s) for s in segments)
    common = []
    for idx in range(shortest):
        seg = segments[0][idx]
        if all(s[idx] == seg for s in segments):
            common.append(seg)
        else:
            break
    return '/'.join(common)


def find_overlap_groups(components: list[BomComponent], similarity_threshold: int = 80) -> list[OverlapGroup]:
    """Group signature-matched components whose matched file paths share a common
    folder/sub-folder prefix - candidates for consolidation into a single match.
    New logic (not present in bd_sig_filter), using rapidfuzz for fuzzy prefix
    similarity rather than exact string comparison.
    """
    sig_components = [c for c in components if c.is_only_signature() and c.matched_file_paths]
    groups: list[OverlapGroup] = []
    assigned: set[int] = set()

    for i, comp1 in enumerate(sig_components):
        if i in assigned:
            continue
        prefix1 = _common_path_prefix(comp1.matched_file_paths)
        if not prefix1:
            continue

        group = [comp1]
        for j in range(i + 1, len(sig_components)):
            if j in assigned:
                continue
            comp2 = sig_components[j]
            prefix2 = _common_path_prefix(comp2.matched_file_paths)
            if not prefix2:
                continue
            if fuzz.ratio(prefix1, prefix2) >= similarity_threshold:
                group.append(comp2)
                assigned.add(j)

        if len(group) > 1:
            assigned.add(i)
            groups.append(OverlapGroup(components=group, common_prefix=prefix1))

    return groups
