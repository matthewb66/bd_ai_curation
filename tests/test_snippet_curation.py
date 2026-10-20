from bd_ai_curation import bom_writer, snippet_curation
from bd_ai_curation.models import CurationAction, SnippetBlock

BASE = 'https://bd'


def _block(hash_id, path, score=0.95, coverage=100, lines=50, comp="Pkg", ver="1.0", rel='rel-1'):
    raw = {
        'hashId': hash_id,
        'matchScore': score,
        'matchCoverage': coverage,
        'matchStartLines': [1],
        'matchEndLines': [50],
        'project': {'id': 'proj-1', 'name': comp},
        'release': {'id': rel, 'version': ver},
        'channelRelease': {'id': 'chan-1'},
        'reviewStatus': 'NOT_REVIEWED',
    }
    folder, _, _ = path.rpartition('/')
    return SnippetBlock(
        hash_id=hash_id,
        component_name=comp,
        component_version=ver,
        component_href=f"{BASE}/api/components/proj-1/versions/{rel}",
        scanned_path=path,
        match_score=score,
        match_coverage=coverage,
        matched_lines=lines,
        source={'scanId': 'scan-1', 'uri': f'{BASE}/api/x',
                'compositePath': {'path': 'p', 'archiveContext': 'a', 'fileName': 'f',
                                  'compositePathContext': path}},
        block=raw,
    )


def _href(rel):
    return f"{BASE}/api/components/proj-1/versions/{rel}"


def test_strong_blocks_in_main_version_are_confirmed():
    blocks = [_block('h1', 'src/a.c'), _block('h2', 'src/b.c')]
    plan = snippet_curation.plan_snippet_curation(blocks)
    assert {b.hash_id for b in plan.confirm} == {'h1', 'h2'}
    assert plan.ignore == [] and plan.indeterminate == []
    assert [d.action for d in plan.decisions] == [CurationAction.CONFIRM_SNIPPET]


def test_single_strong_block_in_folder_is_still_confirmed():
    plan = snippet_curation.plan_snippet_curation([_block('h1', 'src/a.c')])
    assert [b.hash_id for b in plan.confirm] == ['h1']


def test_low_score_block_of_main_version_is_still_confirmed_by_folder_consensus():
    blocks = [_block('h1', 'src/a.c'), _block('h2', 'src/b.c'),
              _block('h3', 'src/c.c', score=0.4, coverage=10, lines=3)]
    plan = snippet_curation.plan_snippet_curation(blocks)
    assert {b.hash_id for b in plan.confirm} == {'h1', 'h2', 'h3'}


def test_other_version_in_folder_is_ignored_when_main_version_is_an_alternate():
    main = _block('h1', 'lib/a.h', ver='4.10.0', rel='r410')
    other = _block('h2', 'lib/b.h', ver='4.0.0', rel='r400')
    other2 = _block('h3', 'lib/c.h', ver='4.10.0', rel='r410')
    alternates = {'h2': {(_href('r410'), '4.10.0'), (_href('r400'), '4.0.0')}}
    plan = snippet_curation.plan_snippet_curation([main, other, other2], alternates)
    assert [b.hash_id for b in plan.ignore] == ['h2']
    assert {b.hash_id for b in plan.confirm} == {'h1', 'h3'}


def test_other_version_without_main_version_in_alternates_is_indeterminate():
    main = _block('h1', 'lib/a.h', ver='4.10.0', rel='r410')
    other = _block('h2', 'lib/b.h', ver='4.0.0', rel='r400')
    other2 = _block('h3', 'lib/c.h', ver='4.10.0', rel='r410')
    plan = snippet_curation.plan_snippet_curation([main, other, other2], {'h2': set()})
    assert plan.ignore == []
    assert [b.hash_id for b in plan.indeterminate] == ['h2']


def test_folder_without_clear_majority_is_all_indeterminate():
    blocks = [_block('h1', 'x/a.c', ver='1.0', rel='r1'),
              _block('h2', 'x/b.c', ver='2.0', rel='r2')]
    alternates = {'h1': {(_href('r2'), '2.0')}, 'h2': {(_href('r1'), '1.0')}}
    plan = snippet_curation.plan_snippet_curation(blocks, alternates)
    assert plan.confirm == [] and plan.ignore == []
    assert {b.hash_id for b in plan.indeterminate} == {'h1', 'h2'}


def _exact(block):
    block.exact_file = True
    return block


def test_exact_file_outvotes_a_single_partial_match_in_folder():
    exact = _exact(_block('h1', 'x/a.c', ver='1.0', rel='r1'))
    partial = _block('h2', 'x/b.c', ver='2.0', rel='r2')
    alternates = {'h2': {(_href('r1'), '1.0')}}
    plan = snippet_curation.plan_snippet_curation([exact, partial], alternates)
    assert [b.hash_id for b in plan.confirm] == ['h1']
    assert [b.hash_id for b in plan.ignore] == ['h2']


def test_exact_file_of_non_main_version_is_never_ignored():
    exact_other = _exact(_block('h1', 'x/a.c', ver='2.0', rel='r2'))
    main_a = _block('h2', 'x/b.c', ver='1.0', rel='r1')
    main_b = _block('h3', 'x/c.c', ver='1.0', rel='r1')
    main_c = _block('h4', 'x/d.c', ver='1.0', rel='r1')
    alternates = {'h1': {(_href('r1'), '1.0')}}
    plan = snippet_curation.plan_snippet_curation([exact_other, main_a, main_b, main_c], alternates)
    assert plan.ignore == []
    assert [b.hash_id for b in plan.indeterminate] == ['h1']
    assert {b.hash_id for b in plan.confirm} == {'h2', 'h3', 'h4'}


def test_folder_majority_below_half_is_indeterminate():
    blocks = [_block('h1', 'x/a.c', ver='1.0', rel='r1'), _block('h2', 'x/b.c', ver='1.0', rel='r1'),
              _block('h3', 'x/c.c', ver='2.0', rel='r2'), _block('h4', 'x/d.c', ver='3.0', rel='r3'),
              _block('h5', 'x/e.c', ver='4.0', rel='r4')]
    plan = snippet_curation.plan_snippet_curation(blocks)
    assert plan.confirm == [] and plan.ignore == []
    assert len(plan.indeterminate) == 5


def test_confirm_body_matches_reference_shape():
    block = _block('h1', 'a.c')
    body = bom_writer._snippet_update_body(BASE, block, ignored=False, review_status='REVIEWED')
    req = body['snippetBomComponentRequests'][0]
    assert body['scanSummary'] == 'https://bd/api/scan-summaries/scan-1'
    assert req['reviewStatus'] == 'REVIEWED'
    assert req['ignored'] == 'false'
    assert req['sourceStartLines'] == [1]
    assert req['sourceEndLines'] == [51]
    assert req['component'] == 'https://bd/api/components/proj-1'
    assert req['componentVersion'] == 'https://bd/api/components/proj-1/versions/rel-1'
    assert req['origin'] == 'https://bd/api/components/proj-1/versions/rel-1/origins/chan-1'


def test_ignore_body_keeps_not_reviewed_and_sets_ignored():
    body = bom_writer._snippet_update_body(BASE, _block('h1', 'a.c'), ignored=True, review_status='NOT_REVIEWED')
    req = body['snippetBomComponentRequests'][0]
    assert req['reviewStatus'] == 'NOT_REVIEWED'
    assert req['ignored'] == 'true'


def test_noise_scanned_blocks_are_split_out_and_excluded_from_vote():
    from bd_ai_curation import path_filters
    noise = [_block(f'n{i}', f'tmp/cov/java-api-stubs.jar!/c{i}.h', ver='9.9', rel='r9') for i in range(5)]
    real = [_block('h1', 'src/a.h', ver='1.0', rel='r1'), _block('h2', 'src/b.h', ver='1.0', rel='r1')]
    kept, dropped = path_filters.split_noise_snippets(noise + real)
    assert {b.hash_id for b in kept} == {'h1', 'h2'}
    assert len(dropped) == 5
    plan = snippet_curation.plan_snippet_curation(kept)
    assert {b.hash_id for b in plan.confirm} == {'h1', 'h2'}
