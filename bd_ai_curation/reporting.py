import json
import logging

from tabulate import tabulate

from . import global_values
from .models import BomComponent, CurationDecision

MIN_SUBFOLDER_MATCHES_TO_SUMMARISE = 3


class _PathNode:
    def __init__(self):
        self.children: dict[str, "_PathNode"] = {}
        self.files: list[str] = []
        self.count = 0


def _collect_summary(node: _PathNode, prefix: str, out: list[str]):
    subfolder_matches = node.count - len(node.files)
    if prefix and node.children and subfolder_matches >= MIN_SUBFOLDER_MATCHES_TO_SUMMARISE:
        out.append(prefix + "/")
        return
    for name, child in node.children.items():
        _collect_summary(child, f"{prefix}/{name}" if prefix else name, out)
    for filename in node.files:
        out.append(f"{prefix}/{filename}" if prefix else filename)


def summarise_matched_paths(paths: list[str]) -> list[str]:
    """Collapses matched file paths into the highest folder that holds several
    matches across its sub-folders (shown with a trailing '/'); other paths are
    listed as-is.
    """
    root = _PathNode()
    for path in set(paths):
        node = root
        node.count += 1
        *folders, filename = path.split("/")
        for folder in folders:
            node = node.children.setdefault(folder, _PathNode())
            node.count += 1
        node.files.append(filename)

    out: list[str] = []
    _collect_summary(root, "", out)
    return sorted(out)


def report_summary(decisions: list[CurationDecision], total_components: int):
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.action.value] = counts.get(d.action.value, 0) + 1

    table = [[action, count] for action, count in sorted(counts.items())]
    table.append(["TOTAL COMPONENTS CONSIDERED", total_components])

    mode = "APPLIED" if global_values.apply else "DRY-RUN (not applied - use --update_bom to write changes)"
    print(f"SUMMARY ({mode}):")
    print(tabulate(table, headers=["Action", "Count"], tablefmt="simple"))
    print()

    if global_values.report_file:
        with open(global_values.report_file, "w") as rfile:
            rfile.write(f"SUMMARY ({mode}):\n")
            rfile.write(tabulate(table, headers=["Action", "Count"], tablefmt="simple"))
            rfile.write("\n\n")


def report_full(decisions: list[CurationDecision]):
    table = [
        [d.action.value, f"{d.target_name[:30]}/{d.target_version[:15]}", f"{d.confidence:.2f}", d.reasoning[:120]]
        for d in decisions
    ]
    print(tabulate(table, headers=["Action", "Component/Version", "Confidence", "Reasoning"]))
    print()

    if global_values.report_file:
        with open(global_values.report_file, "a") as rfile:
            rfile.write(tabulate(table, headers=["Action", "Component/Version", "Confidence", "Reasoning"]))
            rfile.write("\n")


def write_decisions_log(decisions: list[CurationDecision], components: list[BomComponent],
                        apply_results: dict | None = None):
    if not global_values.decisions_file:
        return

    signature_paths = {c.href: c.matched_file_paths for c in components if c.is_signature() or not c.version}
    match_types_by_href = {c.href: c.match_types for c in components}

    decision_entries = []
    for d in decisions:
        entry = d.model_dump()
        entry["match_types"] = match_types_by_href.get(d.target_href, [])
        if d.target_href in signature_paths:
            paths = signature_paths[d.target_href]
            entry["matched_file_count"] = len(set(paths))
            entry["matched_paths"] = summarise_matched_paths(paths)
        decision_entries.append(entry)

    payload = {
        "project": global_values.bd_project,
        "version": global_values.bd_version,
        "applied": global_values.apply,
        "apply_results": apply_results,
        "decisions": decision_entries,
    }

    with open(global_values.decisions_file, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    logging.info(f"- Wrote decision log to '{global_values.decisions_file}'")
