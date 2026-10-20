"""Assessment of BINARY-only BOM entries in container scans.

A container scan reports binary matches against files inside image layers. Their matched
paths are layer blob references (e.g. 'var/lib/dpkg/status/#<layer>!/blobs/sha256/<digest>!/'),
which are not files in a source tree, so path evidence cannot judge them. Instead each
BINARY-only entry is compared with the other BOM entries for the same component: it is ignored
as a duplicate when another DIRECT_DEPENDENCY_BINARY or BINARY entry for the same version is
already in the BOM, and otherwise goes to manual review.
"""

from .models import BomComponent, CurationAction, CurationDecision

LAYER_BLOB_MARKER = '/blobs/sha256/'
DUPLICATE_EVIDENCE_TYPES = {'DIRECT_DEPENDENCY_BINARY', 'BINARY'}


def is_container_scan(all_components: list[BomComponent]) -> bool:
    return any(LAYER_BLOB_MARKER in path
               for comp in all_components
               for path in (comp.all_matched_file_paths or comp.matched_file_paths))


def is_binary_only(comp: BomComponent) -> bool:
    return set(comp.match_types) == {'BINARY'}


def _same_component(a: BomComponent, b: BomComponent) -> bool:
    return bool((a.component_id and a.component_id == b.component_id) or a.name.lower() == b.name.lower())


def _peers(entry: BomComponent, all_components: list[BomComponent]) -> list[BomComponent]:
    return [p for p in all_components
            if p.href != entry.href and p.match_review_type is None and p.version and not p.ignored
            and DUPLICATE_EVIDENCE_TYPES & set(p.match_types) and _same_component(p, entry)]


def _duplicate_evidence(peers: list[BomComponent], version: str) -> BomComponent | None:
    """A same-version peer justifies ignoring the entry only when it is not itself binary-only,
    so two binary-only matches can never ignore each other."""
    for peer in peers:
        if peer.version == version and not is_binary_only(peer):
            return peer
    return None


def decide(components: list[BomComponent], all_components: list[BomComponent]) \
        -> tuple[list[CurationDecision], list[BomComponent]]:
    """Returns (decisions for the BINARY-only entries, the components left for normal curation).
    Only unsettled BOM entries are taken; Match Review items are never container-assessed here."""
    targets = [c for c in components
               if c.match_review_type is None and not c.ignored and not c.is_reviewed() and is_binary_only(c)]
    target_hrefs = {c.href for c in targets}
    remaining = [c for c in components if c.href not in target_hrefs]
    decisions = [_decide_entry(c, _peers(c, all_components)) for c in targets]
    return decisions, remaining


def _decide_entry(entry: BomComponent, peers: list[BomComponent]) -> CurationDecision:
    def make(action, reasoning, confidence=0.0):
        return CurationDecision(
            action=action, target_name=entry.name, target_version=entry.version, target_href=entry.href,
            confidence=confidence, reasoning=reasoning,
            input_external_id=(entry.input_external_ids[0] if entry.input_external_ids else None),
        )

    label = f"{entry.name}/{entry.version}" if entry.version else entry.name
    if not entry.version:
        return make(CurationAction.NO_ACTION_MANUAL_REVIEW,
                    f"Unversioned BINARY-only container match for {label}. Its version cannot be read from "
                    f"layer paths - flagged for manual review.")

    peer = _duplicate_evidence(peers, entry.version)
    if peer:
        return make(CurationAction.IGNORE,
                    f"BINARY-only container match for {label} duplicates the BOM entry "
                    f"{peer.name}/{peer.version} (match types: {', '.join(peer.match_types)}) - ignored as a duplicate.",
                    confidence=0.9)

    if peers:
        versions = ', '.join(sorted({p.version for p in peers}))
        return make(CurationAction.NO_ACTION_MANUAL_REVIEW,
                    f"BINARY-only container match for {label}. The BOM holds version(s) {versions} for this "
                    f"component, with no non-binary evidence for {entry.version} - flagged for manual review.")

    return make(CurationAction.NO_ACTION_MANUAL_REVIEW,
                f"BINARY-only container match for {label}. The BOM has no other entry for this component to "
                f"compare against, and layer paths cannot be judged by path - flagged for manual review.")
