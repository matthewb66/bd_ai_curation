import asyncio
import logging
from datetime import date

from . import bom_data
from . import bom_writer
from . import config
from . import container_binary
from . import curation_engine
from . import global_values
from . import grouping
from . import path_filters
from . import reporting
from . import rest_client
from . import snippet_curation
from . import version_resolver
from .models import CurationAction, CurationDecision

DEPENDENCY_CONFIRM_REASONING = ("Dependency match (declared manifest/package-manager dependency) - "
                                "high confidence, no signature-path judgment required.")


async def cmd_curate_async():
    projver = rest_client.get_project_version(global_values.bd_project, global_values.bd_version)
    projver_href = projver['_meta']['href']

    logging.info("Fetching BOM components (with matchTypes and matched-files) ...")
    components = await bom_data.fetch_bom_components(projver_href)

    if global_values.include_match_review:
        logging.info("Fetching Match Review components ...")
        components += await bom_data.fetch_review_components(projver_href)

    all_components = components

    noise_decisions = []
    noise_path_count, noise_comp_count, _ = path_filters.count_noise_paths(components)
    if global_values.filter_paths:
        components, noise_decisions = path_filters.auto_decide_noise_only(components)
        logging.info(f"- {len(noise_decisions)} components auto-ignored/rejected - "
                     f"all matched file paths were known noise locations (no LLM call needed)")
    elif noise_path_count:
        logging.info(f"- {noise_path_count} matched file paths across {noise_comp_count} components sit under "
                     f"known noise locations and would be excluded by --filter_paths")

    container_decisions = []
    if container_binary.is_container_scan(all_components):
        noise_hrefs = {d.target_href for d in noise_decisions}
        container_decisions, components = container_binary.decide(
            components, [c for c in all_components if c.href not in noise_hrefs])
        logging.info(f"- Container scan detected - {len(container_decisions)} BINARY-only entries assessed "
                     f"against other BOM entries for the same component")

    unversioned = [c for c in components if not c.version]
    components = [c for c in components if c.version]

    auto_reviewed, needs_judgment, settled_decisions = grouping.split_dependency_vs_judgment(components)
    logging.info(f"- {len(auto_reviewed)} dependency matches short-circuited to CONFIRM_REVIEWED "
                 f"(no LLM call needed)")
    logging.info(f"- {len(settled_decisions)} components already settled (reviewed/ignored in the BOM) "
                 f"- no new action needed")

    version_decisions = []
    if unversioned:
        u_auto, u_todo, u_settled = grouping.split_dependency_vs_judgment(unversioned)
        settled_decisions += u_settled
        version_decisions += [
            CurationDecision(
                action=CurationAction.CONFIRM_REVIEWED, target_name=c.name, target_version=c.version,
                target_href=c.href, confidence=1.0, reasoning=DEPENDENCY_CONFIRM_REASONING,
            )
            for c in u_auto
        ]
        versioned_bom = [c for c in all_components if c.version and c.match_review_type is None]
        kb_versions = await bom_data.fetch_kb_versions(projver_href, u_todo)
        version_decisions += version_resolver.decide_unversioned(u_todo, kb_versions, versioned_bom)
        logging.info(f"- {len(unversioned)} unversioned components: {len(version_decisions)} resolved to a "
                     f"version decision (set version, ignore duplicate, or manual review)")

    duplicate_groups = grouping.find_duplicate_groups(needs_judgment)
    grouped_hrefs = {c.href for g in duplicate_groups for c in g.components}
    remaining = [c for c in needs_judgment if c.href not in grouped_hrefs]

    overlap_groups = grouping.find_overlap_groups(remaining)
    overlap_hrefs = {c.href for g in overlap_groups for c in g.components}
    individual = [c for c in remaining if c.href not in overlap_hrefs]

    logging.info(f"- {len(duplicate_groups)} duplicate groups, {len(overlap_groups)} overlap groups, "
                 f"{len(individual)} individual components need LLM review")

    decisions = [
        CurationDecision(
            action=CurationAction.CONFIRM_REVIEWED,
            target_name=c.name,
            target_version=c.version,
            target_href=c.href,
            confidence=1.0,
            reasoning=DEPENDENCY_CONFIRM_REASONING,
        )
        for c in auto_reviewed
    ]
    decisions += noise_decisions
    decisions += container_decisions
    decisions += settled_decisions
    decisions += version_decisions

    # Duplicate/overlap/individual LLM reviews are independent of each other -
    # run all three concurrently rather than waiting on each in turn. Each
    # curate_* function already handles an empty input list correctly.
    llm_results = await asyncio.gather(
        curation_engine.curate_duplicate_groups(duplicate_groups),
        curation_engine.curate_overlap_groups(overlap_groups),
        curation_engine.curate_components(individual),
    )
    for result in llm_results:
        decisions += result

    # Defense in depth: curation_engine guarantees per-batch coverage, but verify
    # here too that every fetched component ended up covered by some decision -
    # a silent gap here means a component was dropped from curation entirely.
    all_hrefs = {c.href for c in all_components}
    covered_hrefs = set()
    for d in decisions:
        covered_hrefs.add(d.target_href)
        covered_hrefs.update(d.ignore_hrefs)
    uncovered = all_hrefs - covered_hrefs
    if uncovered:
        logging.error(f"{len(uncovered)} components have NO decision after curation "
                       f"(this should not happen) - adding manual-review fallbacks")
        href_to_comp = {c.href: c for c in all_components}
        for href in uncovered:
            c = href_to_comp[href]
            decisions.append(CurationDecision(
                action=CurationAction.NO_ACTION_MANUAL_REVIEW,
                target_name=c.name,
                target_version=c.version,
                target_href=c.href,
                confidence=0.0,
                reasoning="Component had no curation decision after processing - "
                          "flagged for manual review rather than silently dropped.",
            ))

    snippet_plan = None
    noise_snippets_count = 0
    if global_values.unconfirmed_snippets:
        logging.info("Fetching unconfirmed snippet matches ...")
        snippet_blocks = await bom_data.fetch_unconfirmed_snippets(projver_href)
        snippet_blocks, noise_snippets = path_filters.split_noise_snippets(snippet_blocks)
        noise_snippets_count = len(noise_snippets)
        if global_values.filter_paths:
            logging.info(f"- {len(noise_snippets)} snippet blocks excluded - scanned file under a known noise "
                         f"location (no decision, left NOT_REVIEWED)")
        else:
            logging.info(f"- {len(noise_snippets)} snippet blocks sit under known noise locations and would be "
                         f"excluded by --filter_paths")
        alternates = await bom_data.fetch_snippet_alternates(projver_href, snippet_blocks)
        snippet_plan = snippet_curation.plan_snippet_curation(snippet_blocks, alternates)
        logging.info(f"- Snippet blocks: {len(snippet_plan.confirm)} confirm, {len(snippet_plan.ignore)} ignore, "
                     f"{len(snippet_plan.indeterminate)} indeterminate (left NOT_REVIEWED)")
        decisions += snippet_plan.decisions

    reporting.report_summary(decisions, len(all_components))
    reporting.report_full(decisions)

    apply_results = None
    if global_values.apply:
        # settled_decisions reflect state the BOM already has (already
        # reviewed/ignored) - applying them again would be a no-op PATCH plus a
        # duplicate audit comment on every single run, so they're reported but
        # never sent to bom_writer.
        already_settled = set(id(d) for d in settled_decisions)
        actionable = [d for d in decisions
                      if d.action.value != "NO_ACTION_MANUAL_REVIEW" and id(d) not in already_settled]
        logging.info(f"Applying {len(actionable)} decisions to the BOM ...")
        apply_results = await bom_writer.apply_decisions(projver_href, actionable, snippet_plan)
        logging.info(f"Apply results: {apply_results}")
        try:
            await bom_writer.apply_project_version_comment(projver_href, projver, date.today().isoformat())
        except Exception as exc:
            logging.error(f"Failed to add the curation comment to the project version: {exc}")
    else:
        logging.info("Dry-run complete - no changes applied. Re-run with --update_bom to write these decisions.")

    reporting.write_decisions_log(decisions, all_components, apply_results)

    if not global_values.filter_paths and (noise_path_count or noise_snippets_count):
        logging.info(f"Recommendation: re-run with --filter_paths. {noise_path_count} component matched paths and "
                     f"{noise_snippets_count} snippet blocks sit under build, test, Coverity or hidden folders, "
                     f"which carry no evidence the component is used. Filtering them stops those files from "
                     f"driving confirm or ignore decisions.")


def main():
    config.check_args()
    asyncio.run(cmd_curate_async())


if __name__ == '__main__':
    main()
