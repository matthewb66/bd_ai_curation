"""Applies CurationDecision objects to the live BOM. Endpoint/payload patterns are
lifted directly from bd_sig_filter/bd_sig_filter/ComponentListClass.py
(ignore_components / review_components / confirm_match_review_components /
reject_match_review_components), generalized to operate on a list of
CurationDecision rather than Component objects, and parallelized: the
flag-mutating bulk-adjustment call is batched (as the original intended, up to
99 hrefs per call) and per-component bulk-comment calls run concurrently rather
than serially. Unlike the best-effort matched-files reads in bom_data.py, these
are writes to the live BOM, so a failure within one action group's own calls
(e.g. a chunked bulk-adjustment PATCH) still raises rather than being silently
swallowed. apply_decisions itself, though, isolates failures *across* action
groups: one group's exception (observed in practice as an opaque 400 from the
server for a specific component pair, reproducible across retries) must not
discard the results of every other group that succeeded, nor crash the run
before the decision log is written.
"""

import asyncio
import logging

import requests

from . import global_values
from .models import CurationAction, CurationDecision, SnippetBlock

BOM6_HEADERS = {
    "Accept": "application/vnd.blackducksoftware.bill-of-materials-6+json",
    "Content-Type": "application/vnd.blackducksoftware.bill-of-materials-6+json",
}
BOM7_HEADERS = {
    "Accept": "application/vnd.blackducksoftware.bill-of-materials-7+json",
    "Content-Type": "application/vnd.blackducksoftware.bill-of-materials-7+json",
}

COMMENT_PREFIX = "bd_ai_curation: "


def _chunk(items, size=99):
    chunks = []
    current = []
    for item in items:
        current.append(item)
        if len(current) >= size:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def _rest_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(max(1, global_values.max_concurrent_rest_requests))


async def _patch(sem: asyncio.Semaphore, url, body, headers):
    async with sem:
        r = await asyncio.to_thread(global_values.bd.session.patch, url, json=body, headers=headers)
    try:
        r.raise_for_status()
    except requests.HTTPError as err:
        global_values.bd.http_error_handler(err)
        raise
    return r


async def _post(sem: asyncio.Semaphore, url, body, headers):
    async with sem:
        r = await asyncio.to_thread(global_values.bd.session.post, url, json=body, headers=headers)
    try:
        r.raise_for_status()
    except requests.HTTPError as err:
        global_values.bd.http_error_handler(err)
        raise
    return r


async def _put(sem: asyncio.Semaphore, url, body, headers):
    async with sem:
        r = await asyncio.to_thread(global_values.bd.session.put, url, json=body, headers=headers)
    try:
        r.raise_for_status()
    except requests.HTTPError as err:
        global_values.bd.http_error_handler(err)
        raise
    return r


async def _bulk_adjust_bisect(sem, projver_href, hrefs: list[str], adjustment_fields: dict):
    """PATCHes one bulk-adjustment call for `hrefs` (<=99, already chunked by the
    caller). If the whole-batch call fails, bisects to find exactly which
    href(s) the server is rejecting - observed in practice as an opaque 400 for
    a specific component pair - rather than letting them block every
    well-formed component batched alongside them. Successful sub-batches stay
    batched; only the actually-bad hrefs end up isolated down to individual
    PATCH calls. Returns (succeeded_hrefs, {failed_href: error_message}).
    """
    if not hrefs:
        return [], {}

    body = {"components": hrefs, **adjustment_fields}
    try:
        await _patch(sem, projver_href + '/bulk-adjustment', body, BOM6_HEADERS)
        return list(hrefs), {}
    except Exception as exc:
        if len(hrefs) == 1:
            return [], {hrefs[0]: str(exc)}
        mid = len(hrefs) // 2
        left_ok, left_failed = await _bulk_adjust_bisect(sem, projver_href, hrefs[:mid], adjustment_fields)
        right_ok, right_failed = await _bulk_adjust_bisect(sem, projver_href, hrefs[mid:], adjustment_fields)
        return left_ok + right_ok, {**left_failed, **right_failed}


async def _bulk_adjust(sem, projver_href, hrefs, adjustment_fields) -> tuple[list[str], dict[str, str]]:
    """One bulk-adjustment PATCH per chunk of <=99 hrefs - lossless batching since
    this call carries no per-component text. Returns (succeeded_hrefs,
    {failed_href: error_message}) across all chunks combined.
    """
    succeeded: list[str] = []
    failed: dict[str, str] = {}
    for batch in _chunk(hrefs):
        batch_ok, batch_failed = await _bulk_adjust_bisect(sem, projver_href, batch, adjustment_fields)
        succeeded += batch_ok
        failed.update(batch_failed)
    return succeeded, failed


async def _bulk_comment_per_decision(sem, projver_href, decisions: list[CurationDecision]):
    """Each decision's reasoning is distinct and is meant to land as its own audit
    comment - collapsing these into one shared batch comment would destroy that
    per-component trail, so bulk-comment stays per-component, but all calls run
    concurrently instead of serially.
    """
    async def _one(decision: CurationDecision):
        body = {"components": [decision.target_href], "comment": f"{COMMENT_PREFIX}{decision.reasoning}"}
        await _post(sem, projver_href + '/bulk-comment', body, BOM6_HEADERS)

    await asyncio.gather(*(_one(d) for d in decisions))


async def _apply_flag_adjustment(projver_href, decisions: list[CurationDecision],
                                  adjustment_fields: dict, verb_infinitive: str,
                                  verb_past: str) -> tuple[int, dict[str, str]]:
    """Shared body for apply_ignore/apply_confirm_reviewed: bulk-adjusts every
    decision's href, then comments only on the ones that actually succeeded - a
    bisected-out bad href gets neither the flag change nor a misleading "done"
    comment. Returns (succeeded_count, {href: error_message}).
    """
    if not decisions:
        return 0, {}
    sem = _rest_semaphore()
    hrefs = [d.target_href for d in decisions]

    succeeded_hrefs, failed = await _bulk_adjust(sem, projver_href, hrefs, adjustment_fields)
    succeeded_decisions = [d for d in decisions if d.target_href in set(succeeded_hrefs)]
    await _bulk_comment_per_decision(sem, projver_href, succeeded_decisions)

    if failed:
        for href, err in failed.items():
            logging.warning(f"Failed to {verb_infinitive} '{href}': {err}")
    logging.info(f"- {verb_past.capitalize()} {len(succeeded_decisions)} of {len(decisions)} components"
                 + (f" ({len(failed)} failed)" if failed else ""))
    return len(succeeded_decisions), failed


async def apply_ignore(projver_href, decisions: list[CurationDecision]) -> tuple[int, dict[str, str]]:
    return await _apply_flag_adjustment(projver_href, decisions, {"ignored": True}, "ignore", "ignored")


async def apply_confirm_reviewed(projver_href, decisions: list[CurationDecision]) -> tuple[int, dict[str, str]]:
    return await _apply_flag_adjustment(
        projver_href, decisions, {"reviewStatus": "REVIEWED"}, "mark reviewed", "marked reviewed")


async def apply_set_version(projver_href, decisions: list[CurationDecision]) -> tuple[int, dict[str, str]]:
    """PUTs each unversioned BOM entry onto its inferred KB version. The body mirrors the
    component-version PUT: the component is the KB component of the version href, and
    the entry's usages are carried over. Each success gets its own audit comment; a
    failed PUT is reported and gets no comment.
    """
    if not decisions:
        return 0, {}
    sem = _rest_semaphore()

    async def _one(decision: CurationDecision):
        component_href = decision.new_version_href.partition('/versions/')[0]
        body = {
            "component": component_href,
            "usages": decision.usages,
            "componentModified": False,
            "componentVersion": decision.new_version_href,
            "origins": [],
        }
        try:
            await _put(sem, decision.target_href, body, BOM6_HEADERS)
            return decision, None
        except Exception as exc:
            return decision, str(exc)

    outcomes = await asyncio.gather(*(_one(d) for d in decisions))
    succeeded = [d for d, err in outcomes if err is None]
    failed = {d.target_href: err for d, err in outcomes if err is not None}

    await _bulk_comment_per_decision(sem, projver_href, succeeded)

    for href, err in failed.items():
        logging.warning(f"Failed to set version on '{href}': {err}")
    logging.info(f"- Set version on {len(succeeded)} of {len(decisions)} unversioned components"
                 + (f" ({len(failed)} failed)" if failed else ""))
    return len(succeeded), failed


async def apply_confirm_match_review(projver_href, decisions: list[CurationDecision]):
    if not decisions:
        return 0
    sem = _rest_semaphore()
    hrefs = [d.target_href for d in decisions]

    for batch in _chunk(hrefs):
        body = {"matchedComponents": batch, "unmatchedComponents": [], "ignored": False}
        await _patch(sem, projver_href + '/match-review/bulk-adjustment', body, BOM7_HEADERS)

        body = {"components": batch, "reviewStatus": "REVIEWED", "confirmed": True}
        await _patch(sem, projver_href + '/bulk-adjustment', body, BOM7_HEADERS)

    await _bulk_comment_per_decision(sem, projver_href, decisions)

    logging.info(f"- Confirmed {len(decisions)} Match Review components into the BOM")
    return len(decisions)


async def apply_reject_match_review(projver_href, decisions: list[CurationDecision]):
    if not decisions:
        return 0
    sem = _rest_semaphore()

    matched_hrefs = [d.target_href for d in decisions if d.match_review_type == 'LOW_MATCH_SCORE']
    unmatched_entries = [
        {"unmatchedComponentUrl": d.target_href, "externalId": d.input_external_id or d.target_name}
        for d in decisions if d.match_review_type == 'UNMATCHED'
    ]

    url = projver_href + '/match-review/bulk-adjustment'
    count = 0
    for batch in _chunk(matched_hrefs):
        body = {"matchedComponents": batch, "unmatchedComponents": [], "ignored": True}
        await _patch(sem, url, body, BOM7_HEADERS)
        count += len(batch)

    for batch in _chunk(unmatched_entries):
        body = {"matchedComponents": [], "unmatchedComponents": batch, "ignored": True}
        await _patch(sem, url, body, BOM7_HEADERS)
        count += len(batch)

    logging.info(f"- Rejected {count} Match Review components")
    return count


async def apply_consolidate(projver_href, decisions: list[CurationDecision]) -> tuple[int, dict[str, str]]:
    """No dedicated 'merge' API exists - consolidation is implemented as IGNORE on
    every href in ignore_hrefs, with a comment referencing the surviving keep_href.
    Only successfully-ignored hrefs get that comment, same as apply_ignore/
    apply_confirm_reviewed.
    """
    if not decisions:
        return 0, {}
    sem = _rest_semaphore()

    all_hrefs = []
    comment_by_href = {}
    for decision in decisions:
        hrefs = [h for h in decision.ignore_hrefs if h and h != decision.keep_href]
        if not hrefs:
            continue
        all_hrefs += hrefs
        comment = (f"{COMMENT_PREFIX}Consolidated as duplicate/overlap of "
                   f"'{decision.target_name}/{decision.target_version}' ({decision.keep_href}). "
                   f"{decision.reasoning}")
        for href in hrefs:
            comment_by_href[href] = CurationDecision(
                action=decision.action, target_name=decision.target_name,
                target_version=decision.target_version, target_href=href,
                confidence=decision.confidence, reasoning=comment,
            )

    if not all_hrefs:
        return 0, {}

    succeeded_hrefs, failed = await _bulk_adjust(sem, projver_href, all_hrefs, {"ignored": True})
    comment_decisions = [comment_by_href[h] for h in succeeded_hrefs]
    await _bulk_comment_per_decision(sem, projver_href, comment_decisions)

    if failed:
        for href, err in failed.items():
            logging.warning(f"Failed to consolidate (ignore) '{href}': {err}")
    logging.info(f"- Consolidated (ignored) {len(succeeded_hrefs)} of {len(all_hrefs)} duplicate/overlapping components"
                 + (f" ({len(failed)} failed)" if failed else ""))
    return len(succeeded_hrefs), failed


def _snippet_update_body(base: str, block: SnippetBlock, ignored: bool, review_status: str) -> dict:
    """Body shape lifted from ignore_snippets.py::create_snippet_update_body (HUB-18270).
    sourceEndLines is +1 and the start/end lines come from the match, as the reference does.
    Confirm sets reviewStatus REVIEWED; ignore keeps the block's NOT_REVIEWED status and sets
    ignored true, as the reference does with match['reviewStatus'].
    """
    raw = block.block
    source = block.source
    project_id = raw['project']['id']
    release_id = raw['release']['id']
    channel_id = raw['channelRelease']['id']
    return {
        "scanSummary": f"{base}/api/scan-summaries/{source['scanId']}",
        "uri": source['uri'],
        "compositePath": {
            "path": source['compositePath']['path'],
            "archiveContext": source['compositePath']['archiveContext'],
            "fileName": source['compositePath']['fileName'],
            "compositePathContext": source['compositePath']['compositePathContext'],
        },
        "snippetBomComponentRequests": [{
            "sourceStartLines": raw['matchStartLines'],
            "sourceEndLines": [line + 1 for line in raw['matchEndLines']],
            "hashId": block.hash_id,
            "component": f"{base}/api/components/{project_id}",
            "componentVersion": f"{base}/api/components/{project_id}/versions/{release_id}",
            "origin": f"{base}/api/components/{project_id}/versions/{release_id}/origins/{channel_id}",
            "reviewStatus": review_status,
            "ignored": "true" if ignored else "false",
        }],
    }


async def _apply_snippet_blocks(projver_href, blocks: list[SnippetBlock], *, ignored: bool,
                                 review_status: str, label: str):
    """PUTs bulk-snippet-bom-entries in chunks of 100 (as ignore_snippets.py does).
    Returns (succeeded_block_count, {hashId: error}) - a failed chunk is isolated and its
    blocks are reported, not silently dropped.
    """
    if not blocks:
        return 0, {}
    sem = _rest_semaphore()
    base = projver_href.partition('/api/')[0]
    url = projver_href + '/bulk-snippet-bom-entries'
    headers = {'Content-type': 'application/vnd.blackducksoftware.bill-of-materials-6+json'}

    bodies = [(block, _snippet_update_body(base, block, ignored=ignored, review_status=review_status))
              for block in blocks]

    succeeded = 0
    failed: dict[str, str] = {}
    for chunk in _chunk(bodies, size=100):
        try:
            await _put(sem, url, [body for _, body in chunk], headers)
            succeeded += len(chunk)
        except Exception as exc:
            logging.warning(f"Failed to {label} snippet chunk of {len(chunk)} blocks: {exc}")
            for block, _ in chunk:
                failed[block.hash_id] = str(exc)

    logging.info(f"- {label.capitalize()} {succeeded} of {len(bodies)} snippet blocks"
                 + (f" ({len(failed)} failed)" if failed else ""))
    return succeeded, failed


async def apply_confirm_snippets(projver_href, blocks: list[SnippetBlock]):
    return await _apply_snippet_blocks(projver_href, blocks, ignored=False,
                                       review_status="REVIEWED", label="confirm")


async def apply_ignore_snippets(projver_href, blocks: list[SnippetBlock]):
    return await _apply_snippet_blocks(projver_href, blocks, ignored=True,
                                       review_status="NOT_REVIEWED", label="ignore")


async def apply_decisions(projver_href, decisions: list[CurationDecision],
                          snippet_plan=None) -> dict:
    """snippet_plan is a snippet_curation.SnippetPlan or None."""
    """Groups decisions by action and applies each group via its endpoint,
    concurrently across action groups. Only called when global_values.apply is
    True - callers must gate on that themselves.

    Each action group's own writes still raise on failure (see module
    docstring), but a failure in one group is isolated here rather than
    propagating via asyncio.gather and discarding every other group's
    (already-applied, unrecoverable) results. A failed group's count is
    reported as 0 with its error recorded under "errors", so the caller can
    see exactly what did and didn't make it onto the BOM instead of losing
    that information to an unhandled exception.
    """
    by_action: dict[CurationAction, list[CurationDecision]] = {}
    for d in decisions:
        by_action.setdefault(d.action, []).append(d)

    groups = [
        ("ignored", apply_ignore(projver_href, by_action.get(CurationAction.IGNORE, []))),
        ("reviewed", apply_confirm_reviewed(projver_href, by_action.get(CurationAction.CONFIRM_REVIEWED, []))),
        ("match_review_confirmed",
         apply_confirm_match_review(projver_href, by_action.get(CurationAction.CONFIRM_MATCH_REVIEW, []))),
        ("match_review_rejected",
         apply_reject_match_review(projver_href, by_action.get(CurationAction.REJECT_MATCH_REVIEW, []))),
        ("consolidated", apply_consolidate(projver_href, by_action.get(CurationAction.CONSOLIDATE, []))),
        ("versions_set", apply_set_version(projver_href, by_action.get(CurationAction.SET_VERSION, []))),
        ("snippets_confirmed", apply_confirm_snippets(projver_href, snippet_plan.confirm if snippet_plan else [])),
        ("snippets_ignored", apply_ignore_snippets(projver_href, snippet_plan.ignore if snippet_plan else [])),
    ]
    results = await asyncio.gather(*(coro for _, coro in groups), return_exceptions=True)

    counts = {}
    errors = {}
    for (key, _), result in zip(groups, results):
        if isinstance(result, Exception):
            # The whole group raised (e.g. a non-bisectable failure, or an
            # unexpected error outside _bulk_adjust) - other groups are unaffected.
            logging.error(f"Applying '{key}' decisions failed - other action groups are unaffected: {result}")
            counts[key] = 0
            errors[key] = str(result)
        elif isinstance(result, tuple):
            # (succeeded_count, {href: error_message}) - bisected per-href failures
            # within an otherwise-successful group.
            count, failed_hrefs = result
            counts[key] = count
            if failed_hrefs:
                errors[key] = failed_hrefs
        else:
            counts[key] = result

    if errors:
        counts["errors"] = errors
    return counts
