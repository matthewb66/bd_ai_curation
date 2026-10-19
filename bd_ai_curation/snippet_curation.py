"""Decides, deterministically, what to do with each unconfirmed snippet block.

Snippet blocks are per scanned file. Several files in the same folder often match the
same component/version, and occasionally one file in that folder matches a different
version of the same library. Decisions are therefore made per folder hierarchy:

- CONFIRM: a block whose component/version is its folder's main version.
- IGNORE: a block whose component/version differs from the folder's main version,
  when the main version is also among the block's alternate matches. Only applied
  under --apply.
- INDETERMINATE: anything else (no clear folder majority, or no alternate support for
  the main version). Left NOT_REVIEWED.

Confirmation and ignore both write to the live BOM, so this is rule-based rather than
LLM-based: the same blocks always produce the same decisions and the rule is visible in
the log.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from posixpath import dirname

from .models import CurationAction, CurationDecision, SnippetBlock

MIN_FOLDER_MAJORITY = 0.5
EXACT_FILE_WEIGHT = 2


@dataclass
class SnippetPlan:
    decisions: list[CurationDecision] = field(default_factory=list)
    confirm: list[SnippetBlock] = field(default_factory=list)
    ignore: list[SnippetBlock] = field(default_factory=list)
    indeterminate: list[SnippetBlock] = field(default_factory=list)


def _component_key(block: SnippetBlock) -> tuple[str, str]:
    return block.component_href, block.component_version


def folder_of(block: SnippetBlock) -> str:
    return dirname(block.scanned_path.rstrip('/'))


def block_weight(block: SnippetBlock) -> int:
    """A byte-identical (whole-file) match is worth EXACT_FILE_WEIGHT partial matches."""
    return EXACT_FILE_WEIGHT if block.exact_file else 1


def main_version_by_folder(blocks: list[SnippetBlock]) -> dict[str, tuple[str, str] | None]:
    """Weighted-majority (component_href, version) per folder. None where there is no clear
    majority (below MIN_FOLDER_MAJORITY of the folder's weight, or a tie)."""
    by_folder: dict[str, Counter] = defaultdict(Counter)
    for block in blocks:
        by_folder[folder_of(block)][_component_key(block)] += block_weight(block)

    mains = {}
    for folder, counts in by_folder.items():
        total = sum(counts.values())
        ranked = counts.most_common(2)
        top_key, top_n = ranked[0]
        tied = len(ranked) > 1 and ranked[1][1] == top_n
        mains[folder] = top_key if (not tied and top_n / total >= MIN_FOLDER_MAJORITY) else None
    return mains


def plan_snippet_curation(blocks: list[SnippetBlock],
                          alternates: dict[str, set[tuple[str, str]]] | None = None) -> SnippetPlan:
    """alternates maps hashId -> set of (component_href, version) the alternate endpoint
    lists for that block. A block with no entry is treated as having no alternates."""
    alternates = alternates or {}
    mains = main_version_by_folder(blocks)
    plan = SnippetPlan()

    by_component: dict[tuple[str, str], list[SnippetBlock]] = defaultdict(list)
    for block in blocks:
        by_component[_component_key(block)].append(block)

    for block in blocks:
        folder = folder_of(block)
        main = mains.get(folder)
        if main is None:
            plan.indeterminate.append(block)
        elif _component_key(block) == main:
            plan.confirm.append(block)
        elif block.exact_file:
            # Byte-identical to the upstream file for its own version: keep it for review.
            plan.indeterminate.append(block)
        elif main in alternates.get(block.hash_id, set()):
            plan.ignore.append(block)
        else:
            plan.indeterminate.append(block)

    plan.decisions = _build_decisions(plan, by_component, mains)
    return plan


def _build_decisions(plan: SnippetPlan, by_component, mains) -> list[CurationDecision]:
    decisions = []
    confirm_ids = {b.hash_id for b in plan.confirm}
    ignore_ids = {b.hash_id for b in plan.ignore}

    for (href, version), group in sorted(by_component.items(), key=lambda kv: (kv[1][0].component_name, kv[0][1])):
        name = group[0].component_name
        n_confirm = sum(1 for b in group if b.hash_id in confirm_ids)
        n_ignore = sum(1 for b in group if b.hash_id in ignore_ids)
        n_left = len(group) - n_confirm - n_ignore
        if n_confirm:
            action = CurationAction.CONFIRM_SNIPPET
        elif n_ignore:
            action = CurationAction.IGNORE_SNIPPET
        else:
            action = CurationAction.NO_ACTION_MANUAL_REVIEW
        decisions.append(CurationDecision(
            action=action,
            target_name=name,
            target_version=version,
            target_href=href,
            confidence=round(n_confirm / len(group), 2) if n_confirm else round(n_ignore / len(group), 2),
            reasoning=(f"Snippet blocks for {name}/{version}: {n_confirm} confirmed (strong and in the folder's "
                       f"main version), {n_ignore} ignored (folder main version is a different component/version "
                       f"and appears in their alternate matches), {n_left} left NOT_REVIEWED (indeterminate)."),
        ))
    return decisions
