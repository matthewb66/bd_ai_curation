"""Assigns a Knowledge Base version to unversioned BOM entries (typically binary matches)
and decides whether that assignment duplicates a component already in the BOM.

Version evidence is the version strings found in the entry's matched file paths, checked
only against the KB versions of the entry's own component. A version is accepted only when
it is the single candidate found and appears in at least half of the matched paths.
"""

import re
from collections import Counter
from dataclasses import dataclass

from .models import BomComponent, CurationAction, CurationDecision

MIN_PATH_SUPPORT = 0.5
DUPLICATE_OVERLAP = 0.5
MAX_CANDIDATE_TOKENS = 25

_VERSION_TOKEN = re.compile(r'(?<!\d)(\d+(?:\.\d+)+)(?!\d)')


@dataclass
class VersionInference:
    chosen: tuple[str, str] | None  # (version name, KB version href)
    candidates: list[str]
    support: int = 0
    total_paths: int = 0


def _version_pattern(names) -> re.Pattern:
    alternatives = sorted(names, key=len, reverse=True)
    body = '|'.join(re.escape(n) for n in alternatives)
    return re.compile(r'(?<![0-9A-Za-z.])[vV]?(' + body + r')(?![0-9A-Za-z]|\.[0-9])')


def candidate_version_tokens(paths: list[str]) -> set[str]:
    """Dotted numeric tokens (e.g. '6.0.1') in the file's own path, most frequent first.
    Only these are looked up in the KB, so single-number and underscore versions are not
    inferred from paths."""
    counts: Counter = Counter()
    for path in paths:
        inner = path.split('#', 1)[0]
        counts.update(set(_VERSION_TOKEN.findall(inner)))
    return {token for token, _ in counts.most_common(MAX_CANDIDATE_TOKENS)}


def infer_version(paths: list[str], kb_versions: list[tuple[str, str]]) -> VersionInference:
    by_name: dict[str, str] = {}
    for name, href in kb_versions:
        by_name.setdefault(name, href)
    if not paths or not by_name:
        return VersionInference(None, [], 0, len(paths))

    pattern = _version_pattern(by_name)
    votes: Counter = Counter()
    for path in paths:
        # Only the file's own path counts: text after the first '#' is the archive/container
        # chain, which carries the scanned firmware's version (e.g. R7000-V1.0.7.2_1.1.93.chk).
        inner = path.split('#', 1)[0]
        votes.update({m.group(1) for m in pattern.finditer(inner)})

    candidates = sorted(votes, key=lambda v: (-votes[v], v))
    if len(candidates) != 1:
        return VersionInference(None, candidates, 0, len(paths))

    name = candidates[0]
    support = votes[name]
    if support < MIN_PATH_SUPPORT * len(paths):
        return VersionInference(None, candidates, support, len(paths))
    return VersionInference((name, by_name[name]), candidates, support, len(paths))


def _is_same_component(bom_comp: BomComponent, entry: BomComponent) -> bool:
    return bool((entry.component_id and bom_comp.component_id == entry.component_id)
                or bom_comp.name.lower() == entry.name.lower())


def _is_same_version(bom_comp: BomComponent, name: str, href: str) -> bool:
    version_id = href.rsplit('/', 1)[-1]
    return bom_comp.version == name or bool(bom_comp.component_version_id
                                            and bom_comp.component_version_id == version_id)


def _decide_for_version(entry: BomComponent, name: str, href: str, support: int, total: int,
                        bom_versioned: list[BomComponent]) -> CurationDecision:
    dups = [c for c in bom_versioned if _is_same_component(c, entry)]

    if any(_is_same_version(d, name, href) for d in dups):
        return CurationDecision(
            action=CurationAction.IGNORE, target_name=entry.name, target_version=name,
            target_href=entry.href, confidence=1.0,
            reasoning=f"Binary match resolves to {entry.name}/{name}, which is already in the BOM at the same "
                      f"version - ignored to avoid a duplicate component.",
        )

    binary_paths = set(entry.matched_file_paths)
    comparable = [d for d in dups if d.matched_file_paths]
    for dup in comparable:
        shared = binary_paths & set(dup.matched_file_paths)
        if len(shared) / len(binary_paths) >= DUPLICATE_OVERLAP:
            return CurationDecision(
                action=CurationAction.IGNORE, target_name=entry.name, target_version=name,
                target_href=entry.href, confidence=round(len(shared) / len(binary_paths), 2),
                reasoning=f"Binary match resolves to {entry.name}/{name}, but {len(shared)} of "
                          f"{len(binary_paths)} matched file path(s) are already attributed to the BOM "
                          f"component {dup.name}/{dup.version} - ignored as a duplicate.",
            )

    if dups and not comparable:
        return CurationDecision(
            action=CurationAction.NO_ACTION_MANUAL_REVIEW, target_name=entry.name, target_version=name,
            target_href=entry.href, confidence=0.0,
            reasoning=f"Binary match resolves to {entry.name}/{name}, but the existing BOM version(s) "
                      f"{', '.join(sorted({d.version for d in dups}))} have no matched-file evidence to "
                      f"cross-reference against - flagged for manual review.",
        )

    if dups:
        existing = ', '.join(sorted({d.version for d in dups}))
        reasoning = (f"Binary match version {name} inferred from {support} of {total} matched file path(s). "
                     f"Existing BOM version(s) {existing} have distinct matched paths, so this is added as "
                     f"a separate version of {entry.name}.")
    else:
        reasoning = (f"Binary match version {name} inferred from {support} of {total} matched file path(s). "
                     f"No version of {entry.name} is in the BOM yet.")
    return CurationDecision(
        action=CurationAction.SET_VERSION, target_name=entry.name, target_version=name,
        target_href=entry.href, confidence=round(support / total, 2), reasoning=reasoning,
        new_version_href=href, usages=entry.usages,
    )


def decide_unversioned(entries: list[BomComponent], kb_versions: dict[str, list[tuple[str, str]]],
                       bom_versioned: list[BomComponent]) -> list[CurationDecision]:
    decisions = []
    for entry in entries:
        inference = infer_version(entry.matched_file_paths, kb_versions.get(entry.component_id, []))
        if inference.chosen is None:
            found = (f"candidates found: {', '.join(inference.candidates)}" if inference.candidates
                     else "no KB version found in the matched file paths")
            decisions.append(CurationDecision(
                action=CurationAction.NO_ACTION_MANUAL_REVIEW, target_name=entry.name,
                target_version="", target_href=entry.href, confidence=0.0,
                reasoning=f"Unversioned binary match for {entry.name}: version not identifiable "
                          f"unambiguously ({found}) - flagged for manual review.",
            ))
            continue
        name, href = inference.chosen
        decisions.append(_decide_for_version(entry, name, href, inference.support,
                                             inference.total_paths, bom_versioned))
    return decisions
