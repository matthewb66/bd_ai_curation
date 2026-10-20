"""Deterministic matched-file-path noise filtering, applied before any LLM call
when --filter_paths is set. Mirrors the dependency-match short-circuit in
grouping.py: known build-tooling/test/scan-artifact paths carry no real
evidence that a component is actually used in the scanned codebase, so they
are stripped out of a component's evidence before curation. A component left
with zero real evidence as a result is auto-decided (IGNORE, or
REJECT_MATCH_REVIEW for a Match Review candidate) without spending an LLM
call on it.
"""

import re

from .models import BomComponent, CurationAction, CurationDecision, SnippetBlock

# Black Duck composite paths join archive/container boundaries with '#' (e.g.
# '...jar!/foo#idir/tmp/stub.jar!/bar'), not just '/' - so segment boundaries
# must accept '#' as well as '/' and start/end of string.
_SEG = r'(?:^|[/#])'
_END = r'(?:[/#]|$)'

_NOISE_PATH_REGEXES = [
    re.compile(_SEG + r'\.[^/#]+' + _END),  # hidden/dotfile folders (.git, .venv, .m2, ...)
    re.compile(_SEG + r'(tests?|__tests__|spec|specs)' + _END, re.IGNORECASE),  # test folders
    re.compile(_SEG + r'java-api-stubs\.jar(?=[/#!]|$)', re.IGNORECASE),  # Coverity stub jar (only matchable file in its idir)
    re.compile(_SEG + r'(venv|virtualenv)' + _END, re.IGNORECASE),  # Python virtualenv folders
    re.compile(_SEG + r'(blackduck-detect|synopsys-detect)[^/#]*' + _END, re.IGNORECASE),  # Detect run folders/jars
]


def is_noise_path(path: str) -> bool:
    return any(rx.search(path) for rx in _NOISE_PATH_REGEXES)


def count_noise_paths(components: list[BomComponent]) -> tuple[int, int, int]:
    """Read-only: (noise path count, components with any noise path, components whose
    every matched path is noise). Used to report what --filter_paths would remove."""
    noise_paths = comps_any = comps_all = 0
    for comp in components:
        noisy = [p for p in comp.matched_file_paths if is_noise_path(p)]
        if not noisy:
            continue
        noise_paths += len(noisy)
        comps_any += 1
        if len(noisy) == len(comp.matched_file_paths):
            comps_all += 1
    return noise_paths, comps_any, comps_all


def split_noise_snippets(blocks: list[SnippetBlock]) -> tuple[list[SnippetBlock], list[SnippetBlock]]:
    """Returns (kept, noise). A snippet block is noise when its scanned file lives under a
    noise location - the same rules as for BOM matched-file paths."""
    kept, noise = [], []
    for block in blocks:
        (noise if is_noise_path(block.scanned_path) else kept).append(block)
    return kept, noise


def _filter_component_paths(comp: BomComponent) -> int:
    """Strips noise paths from comp.matched_file_paths in place. Returns the
    number of paths removed."""
    before = len(comp.matched_file_paths)
    if not comp.all_matched_file_paths:
        comp.all_matched_file_paths = list(comp.matched_file_paths)
    comp.matched_file_paths = [p for p in comp.matched_file_paths if not is_noise_path(p)]
    return before - len(comp.matched_file_paths)


def auto_decide_noise_only(components: list[BomComponent]) -> tuple[list[BomComponent], list[CurationDecision]]:
    """Strips noise paths out of every component's matched_file_paths, then
    deterministically decides any signature-only or Match Review component
    left with zero real evidence as a result - IGNORE for BOM components,
    REJECT_MATCH_REVIEW for Match Review candidates. Dependency matches
    (which don't rely on matched-file evidence) and components that still
    have real evidence after filtering are returned unchanged for normal
    curation.
    """
    remaining: list[BomComponent] = []
    decisions: list[CurationDecision] = []

    for comp in components:
        had_paths = bool(comp.matched_file_paths)
        removed = _filter_component_paths(comp)
        now_empty = had_paths and removed and not comp.matched_file_paths

        if now_empty and not comp.is_dependency():
            action = (CurationAction.REJECT_MATCH_REVIEW if comp.match_review_type == 'LOW_MATCH_SCORE'
                      else CurationAction.IGNORE)
            decisions.append(CurationDecision(
                action=action,
                target_name=comp.name,
                target_version=comp.version,
                target_href=comp.href,
                confidence=1.0,
                reasoning=f"All {removed} matched file path(s) fell under known noise locations "
                          f"(tests, Coverity scan dirs, Detect run folders/jars, virtualenv, or "
                          f"hidden dot-folders) - no real evidence this component is actually used.",
                match_review_type=comp.match_review_type,
                input_external_id=(comp.input_external_ids[0] if comp.input_external_ids else None),
            ))
            continue

        remaining.append(comp)

    return remaining, decisions
