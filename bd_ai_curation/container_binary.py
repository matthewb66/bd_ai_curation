"""BINARY-only BOM entries in container scans, judged against other BOM entries, and DDB matches that name only the package database."""

import re

from .models import BomComponent, CurationAction, CurationDecision, same_component

LAYER_BLOB_MARKER = '/blobs/sha256/'
DUPLICATE_EVIDENCE_TYPES = {'DIRECT_DEPENDENCY_BINARY', 'BINARY'}
_PACKAGE_DATABASE_PATH = re.compile(r"(^|/)var/lib/dpkg/status(?=[#!/]|$)", re.IGNORECASE)


def is_container_scan(all_components: list[BomComponent]) -> bool:
    return any(LAYER_BLOB_MARKER in path
               for comp in all_components
               for path in comp.all_matched_file_paths or comp.matched_file_paths)


def is_binary_only(comp: BomComponent) -> bool:
    return set(comp.match_types) == {'BINARY'}


def is_database_only_ddb(comp: BomComponent) -> bool:
    """True for a DDB match whose only evidence is the dpkg status database, which names no installed file."""
    if set(comp.match_types) != {'DIRECT_DEPENDENCY_BINARY'} or not comp.matched_file_paths:
        return False
    return all(_PACKAGE_DATABASE_PATH.search(p) for p in comp.matched_file_paths)


def _peers(entry: BomComponent, all_components: list[BomComponent]) -> list[BomComponent]:
    return [p for p in all_components
            if p.href != entry.href and p.match_review_type is None and p.version and not p.ignored
            and DUPLICATE_EVIDENCE_TYPES & set(p.match_types) and same_component(p, entry)]


def _make_decision(entry: BomComponent, action: CurationAction, reasoning: str,
                   confidence: float = 0.0) -> CurationDecision:
    return CurationDecision(
        action=action, target_name=entry.name, target_version=entry.version, target_href=entry.href,
        confidence=confidence, reasoning=reasoning,
        input_external_id=(entry.input_external_ids[0] if entry.input_external_ids else None),
    )


def decide(components: list[BomComponent], all_components: list[BomComponent]) \
        -> tuple[list[CurationDecision], list[BomComponent]]:
    """Returns (decisions for the BINARY-only entries, components left for normal curation).
    Only unsettled entries are taken; Match Review items are never container-assessed here."""
    targets = [c for c in components
               if c.match_review_type is None and not c.ignored and not c.is_reviewed() and is_binary_only(c)]
    target_hrefs = {c.href for c in targets}
    remaining = [c for c in components if c.href not in target_hrefs]
    decisions = [_decide_entry(c, _peers(c, all_components)) for c in targets]
    return decisions, remaining


def _decide_entry(entry: BomComponent, peers: list[BomComponent]) -> CurationDecision:
    label = f"{entry.name}/{entry.version}" if entry.version else entry.name
    if not entry.version:
        return _make_decision(entry, CurationAction.NO_ACTION_MANUAL_REVIEW,
                              f"Unversioned BINARY-only container match for {label}. Its version cannot be read "
                              f"from layer paths - flagged for manual review.")

    same_version = [p for p in peers if p.version == entry.version]
    evidence = [p for p in same_version if not is_binary_only(p)]
    if evidence:
        return _ignored_as_duplicate(entry, evidence[0])

    lower = [p for p in same_version if p.href < entry.href]
    if lower:
        return _ignored_as_duplicate(entry, min(lower, key=lambda p: p.href))

    if same_version:
        return _make_decision(entry, CurationAction.NO_ACTION_MANUAL_REVIEW,
                              f"BINARY-only container match for {label}. Other BINARY-only entries for this version "
                              f"are ignored in favour of this one, which is kept for manual review so the group is "
                              f"never ignored entirely.")

    if peers:
        versions = ', '.join(sorted({p.version for p in peers}))
        return _make_decision(entry, CurationAction.NO_ACTION_MANUAL_REVIEW,
                              f"BINARY-only container match for {label}. The BOM holds version(s) {versions} for "
                              f"this component, with no matching entry for {entry.version} - flagged for manual review.")

    return _make_decision(entry, CurationAction.NO_ACTION_MANUAL_REVIEW,
                          f"BINARY-only container match for {label}. The BOM has no other entry for this component "
                          f"to compare against, and layer paths cannot be judged by path - flagged for manual review.")


def _ignored_as_duplicate(entry: BomComponent, peer: BomComponent) -> CurationDecision:
    return _make_decision(entry, CurationAction.IGNORE,
                          f"BINARY-only container match for {entry.name}/{entry.version} duplicates the BOM entry "
                          f"{peer.name}/{peer.version} (match types: {', '.join(peer.match_types)}) - "
                          f"ignored as a duplicate.", confidence=0.9)
