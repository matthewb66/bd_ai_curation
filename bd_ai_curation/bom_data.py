import asyncio
import logging
import re
from urllib.parse import urlencode

from . import global_values
from . import version_resolver
from .models import BomComponent, Origin, SnippetBlock

BOM6_ACCEPT = "application/vnd.blackducksoftware.bill-of-materials-6+json"
REVIEW5_ACCEPT = "application/vnd.blackducksoftware.bill-of-materials-5+json"
SNIPPET_ACCEPT = "application/json"


def _rest_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(max(1, global_values.max_concurrent_rest_requests))


def _get_json_sync(url, accept_hdr):
    return global_values.bd.get_json(url, headers={'accept': accept_hdr})


async def _get_json(sem: asyncio.Semaphore, url, accept_hdr):
    async with sem:
        return await asyncio.to_thread(_get_json_sync, url, accept_hdr)


async def get_paginated_data(url, accept_hdr, sem: asyncio.Semaphore | None = None):
    """Async equivalent of bd_sig_filter/bd_sig_filter/bd_data.py::get_paginated_data.

    Fetches page 1 to learn totalCount/page size, then fetches every remaining
    page concurrently (bounded by `sem`) instead of looping one offset at a time.
    """
    sem = sem or _rest_semaphore()

    res = await _get_json(sem, url, accept_hdr)
    if 'totalCount' not in res or 'items' not in res:
        return []

    total_count = res['totalCount']
    items = list(res['items'])
    page_size = len(items) or 1

    offsets = range(page_size, total_count, page_size)
    pages = await asyncio.gather(*(_get_json(sem, f"{url}&offset={off}", accept_hdr) for off in offsets))
    for page in pages:
        if 'items' in page:
            items += page['items']

    return items


def _get_link(data, rel):
    for link in data.get('_meta', {}).get('links', []):
        if link.get('rel') == rel:
            return link.get('href')
    return None


def _parse_origins(data):
    origins = []
    for ori in data.get('origins', []):
        href = _get_link(ori, 'matched-files') or ori.get('_meta', {}).get('href', '')
        origins.append(Origin(
            external_id=ori.get('externalId', ''),
            name=ori.get('name', ''),
            href=href or '',
        ))
    return origins


def _build_component(data) -> BomComponent:
    """Builds a BomComponent from a raw BOM/review-components entry. No I/O - matched
    file paths are attached separately by _attach_matched_files so callers can fan
    the matched-files fetch out across every component at once.
    """
    origins = _parse_origins(data)

    comp_href = data.get('_meta', {}).get('href', '')
    component_id = ''
    if 'component' in data:
        component_id = data['component'].split('/')[-1]
    component_version_id = ''
    if 'componentVersion' in data:
        component_version_id = data['componentVersion'].split('/')[-1]

    return BomComponent(
        name=data.get('componentName', ''),
        version=data.get('componentVersionName', ''),
        href=comp_href,
        component_id=component_id,
        component_version_id=component_version_id,
        match_types=data.get('matchTypes', []),
        origins=origins,
        matched_file_paths=[],
        ignored=data.get('ignored', False),
        review_status=data.get('reviewStatus'),
        policy_status=data.get('policyStatus'),
        match_review_type=data.get('matchReviewType'),
        input_external_ids=data.get('inputExternalIds', []),
        usages=data.get('usages', []),
        matched_files_href=_get_link(data, 'matched-files') or '',
    )


async def _fetch_origin_paths(sem: asyncio.Semaphore, href: str) -> list[str]:
    if not href:
        return []
    items = await get_paginated_data(f"{href}?limit=1000", BOM6_ACCEPT, sem=sem)
    paths = []
    for file_entry in items:
        try:
            paths.append(file_entry['filePath']['compositePathContext'])
        except KeyError:
            continue
    return paths


async def _attach_matched_files(components: list[BomComponent]):
    """Fans out matched-files requests for every origin across every component in
    one bounded-concurrency batch - the fix for the elasticsearch-scale slowdown
    (hundreds of sequential per-origin calls -> one gather). Best-effort: a failed
    origin fetch is logged and skipped rather than aborting the whole run, since
    missing matched-files evidence degrades a decision's confidence rather than
    losing the component outright (it still gets built and still gets a decision).
    """
    sem = _rest_semaphore()

    jobs = []  # (component_index, origin_href)
    for idx, comp in enumerate(components):
        for origin in comp.origins:
            if origin.href:
                jobs.append((idx, origin.href))

    if not jobs:
        return

    results = await asyncio.gather(
        *(_fetch_origin_paths(sem, href) for _, href in jobs),
        return_exceptions=True,
    )

    for (idx, href), result in zip(jobs, results):
        if isinstance(result, Exception):
            logging.warning(f"Failed to fetch matched-files for origin '{href}' of "
                             f"'{components[idx].name}/{components[idx].version}': {result}")
            continue
        components[idx].matched_file_paths.extend(result)


async def _attach_unversioned_matched_files(components: list[BomComponent]):
    """Unversioned BOM entries (binary matches) have no origins, so their matched files
    come from the entry's own matched-files link."""
    sem = _rest_semaphore()
    targets = [c for c in components if c.matched_files_href and not c.origins]
    if not targets:
        return

    results = await asyncio.gather(
        *(_fetch_origin_paths(sem, c.matched_files_href) for c in targets),
        return_exceptions=True,
    )
    for comp, result in zip(targets, results):
        if isinstance(result, Exception):
            logging.warning(f"Failed to fetch matched-files for unversioned '{comp.name}': {result}")
            continue
        comp.matched_file_paths.extend(result)


async def fetch_bom_components(projver_href, fetch_matched_files=True):
    """GET {projver_href}/components - bill-of-materials-6, carries matchTypes/_meta.href.
    Unversioned entries (no componentVersion) are kept with version '' so the version
    resolver can assign one.

    Lifted from bd_sig_filter/bd_sig_filter/BOMClass.py::__init__.
    """
    href = f"{projver_href}/components?limit=1000"
    bom_arr = await get_paginated_data(href, BOM6_ACCEPT)

    components = [_build_component(entry) for entry in bom_arr]

    if fetch_matched_files:
        await _attach_matched_files(components)
        await _attach_unversioned_matched_files([c for c in components if not c.version])

    unversioned = sum(1 for c in components if not c.version)
    logging.info(f"- Fetched {len(components)} BOM components ({unversioned} unversioned)")
    return components


async def fetch_kb_versions(projver_href, entries: list[BomComponent]) -> dict[str, list[tuple[str, str]]]:
    """component_id -> [(versionName, version_href)] for the KB versions named by the
    dotted version tokens in each component's matched paths. Listing all versions of a
    large component times out, so each token is looked up with a versionName filter and
    the version_resolver then picks the exact name from the path. A failed lookup is
    logged and yields no versions for that token (the entry then gets no version)."""
    base = projver_href.partition('/api/')[0]
    sem = _rest_semaphore()

    tokens_by_component: dict[str, set[str]] = {}
    for entry in entries:
        if entry.component_id:
            tokens_by_component.setdefault(entry.component_id, set()).update(
                version_resolver.candidate_version_tokens(entry.matched_file_paths))

    async def lookup(component_id, token):
        query = urlencode({"q": f"versionName:{token}", "limit": 1000})
        url = f"{base}/api/components/{component_id}/versions?{query}"
        items = await get_paginated_data(url, SNIPPET_ACCEPT, sem=sem)
        return [(v.get('versionName', ''), v.get('_meta', {}).get('href', ''))
                for v in items if v.get('versionName')]

    async def one(component_id, tokens):
        if not tokens:
            return component_id, []
        results = await asyncio.gather(*(lookup(component_id, t) for t in sorted(tokens)),
                                       return_exceptions=True)
        versions = []
        for token, result in zip(sorted(tokens), results):
            if isinstance(result, Exception):
                logging.warning(f"Failed to fetch KB version '{token}' for component {component_id}: {result}")
                continue
            versions += result
        return component_id, versions

    results = await asyncio.gather(*(one(cid, toks) for cid, toks in tokens_by_component.items()))
    return dict(results)


async def fetch_review_components(projver_href):
    """GET {projver_href}/review-components - bill-of-materials-5 (Match Review: snippet
    low-match-score + unmatched declared components).

    Lifted from bd_sig_filter/bd_sig_filter/BOMClass.py::get_match_review_components.
    """
    href = f"{projver_href}/review-components?limit=1000"
    review_arr = await get_paginated_data(href, REVIEW5_ACCEPT)

    components = [_build_component(item) for item in review_arr]

    # UNMATCHED items have no origins/matched-files data to fetch.
    unmatched_count = sum(1 for c in components if c.match_review_type == 'UNMATCHED')
    components = [c for c in components if c.match_review_type != 'UNMATCHED']

    low_match = [c for c in components if c.match_review_type == 'LOW_MATCH_SCORE']
    await _attach_matched_files(low_match)

    logging.info(f"- Retrieved {len(low_match)} LOW_MATCH_SCORE components from Match Review "
                 f"({unmatched_count} UNMATCHED skipped - no signature evidence)")
    return components


def _scanned_file_sha1(entry) -> str | None:
    for link in entry.get('_meta', {}).get('links', []):
        m = re.search(r'source-upload-file-contents/([0-9a-f]{40})$', link.get('href', ''))
        if m:
            return m.group(1)
    return None


def _build_snippet_block(entry, raw, base) -> SnippetBlock:
    matched_lines = sum(end - start + 1 for start, end in zip(raw.get('sourceStartLines', []),
                                                             raw.get('sourceEndLines', [])))
    project = raw['project']
    release = raw['release']
    scanned_sha = _scanned_file_sha1(entry)
    return SnippetBlock(
        exact_file=bool(scanned_sha) and scanned_sha == raw.get('matchFileSha1'),
        hash_id=raw['hashId'],
        component_name=project['name'],
        component_version=release['version'],
        component_href=f"{base}/api/components/{project['id']}/versions/{release['id']}",
        scanned_path=entry['compositePath']['compositePathContext'],
        match_file_path=raw.get('matchFilePath', ''),
        match_score=raw.get('matchScore', 0.0),
        match_coverage=raw.get('matchCoverage', 0),
        matched_lines=matched_lines,
        source={
            'scanId': entry['scanId'],
            'uri': entry['uri'],
            'compositePath': entry['compositePath'],
        },
        block=raw,
    )


async def fetch_unconfirmed_snippets(projver_href) -> list[SnippetBlock]:
    """GET the internal source-bom-entries for NOT_REVIEWED snippet matches - the same
    endpoint ignore_snippets.py uses (HUB-18270). One SnippetBlock per
    fileSnippetBomComponents block that is itself NOT_REVIEWED.
    """
    base = projver_href.partition('/api/')[0]
    internal = projver_href.replace('/api/projects/', '/api/internal/projects/', 1)
    url = (f"{internal}/source-bom-entries?limit=1000"
           "&filter=bomMatchType:snippet&filter=bomMatchReviewStatus:not_reviewed")
    entries = await get_paginated_data(url, SNIPPET_ACCEPT)

    blocks = [
        _build_snippet_block(entry, raw, base)
        for entry in entries
        for raw in entry.get('fileSnippetBomComponents', [])
        if raw.get('reviewStatus') == 'NOT_REVIEWED'
    ]
    logging.info(f"- Fetched {len(blocks)} unconfirmed (NOT_REVIEWED) snippet blocks "
                 f"across {len(entries)} scanned files")
    return blocks


async def fetch_snippet_alternates(projver_href, blocks: list[SnippetBlock]) -> dict[str, set[tuple[str, str]]]:
    """hashId -> {(component_href, version)} from each block's alternateSnippetMatchesUrl.
    A failed lookup is logged and the block gets no alternates (so it cannot be ignored)."""
    base = projver_href.partition('/api/')[0]
    sem = _rest_semaphore()

    async def one(block):
        url = block.block.get('alternateSnippetMatchesUrl')
        if not url:
            return block.hash_id, set()
        if url.startswith('/'):
            url = base + url
        try:
            data = await _get_json(sem, url, SNIPPET_ACCEPT)
        except Exception as exc:
            logging.warning(f"Failed to fetch alternates for snippet {block.hash_id}: {exc}")
            return block.hash_id, set()
        candidates = set()
        for match in data.get('snippetMatches', []):
            for cand in match.get('snippetBomComponents', []):
                project, release = cand.get('project', {}), cand.get('release', {})
                if project.get('id') and release.get('id'):
                    candidates.add((f"{base}/api/components/{project['id']}/versions/{release['id']}",
                                    release.get('version', '')))
        return block.hash_id, candidates

    results = await asyncio.gather(*(one(b) for b in blocks))
    logging.info(f"- Fetched alternate snippet matches for {len(blocks)} blocks")
    return dict(results)
