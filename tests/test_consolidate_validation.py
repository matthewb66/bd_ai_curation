from bd_ai_curation.models import BomComponent, CurationAction

HREF_A = 'https://bd/api/projects/p/versions/v/components/a/versions/va'
HREF_B = 'https://bd/api/projects/p/versions/v/components/b/versions/vb'


def _comp(href, name, version):
    return BomComponent(name=name, version=version, href=href, match_types=['BINARY'])


def _raw(action, **extra):
    base = dict(action=action, target_name='Apache HttpClient', target_version='4.5.5', target_href=HREF_A,
                confidence=0.5, reasoning='Paths support this occurrence.')
    base.update(extra)
    return base


def test_consolidate_without_keep_href_is_downgraded(run_batch):
    batch = [_comp(HREF_A, 'Apache HttpClient', '4.5.5')]
    out = run_batch([_raw('CONSOLIDATE', ignore_hrefs=[HREF_B])], batch)
    assert len(out) == 1
    assert out[0].action == CurationAction.NO_ACTION_MANUAL_REVIEW
    assert out[0].ignore_hrefs == []


def test_consolidate_with_empty_ignore_hrefs_is_downgraded(run_batch):
    batch = [_comp(HREF_A, 'Apache HttpClient', '4.5.5')]
    out = run_batch([_raw('CONSOLIDATE', keep_href=HREF_A, ignore_hrefs=[])], batch)
    assert out[0].action == CurationAction.NO_ACTION_MANUAL_REVIEW


def test_consolidate_referring_to_another_decision_is_downgraded(run_batch):
    batch = [_comp(HREF_A, 'Apache HttpClient', '4.5.5')]
    out = run_batch([_raw('CONSOLIDATE', keep_href=HREF_A, ignore_hrefs=[HREF_B],
                          reasoning='Placeholder - see second decision.')], batch)
    assert out[0].action == CurationAction.NO_ACTION_MANUAL_REVIEW
    assert out[0].keep_href is None


def test_well_formed_consolidate_is_kept(run_batch):
    batch = [_comp(HREF_A, 'Apache HttpClient', '4.5.5'), _comp(HREF_B, 'Apache HttpClient', '4.5.5')]
    out = run_batch([_raw('CONSOLIDATE', keep_href=HREF_A, ignore_hrefs=[HREF_B])], batch)
    consolidate = [d for d in out if d.action == CurationAction.CONSOLIDATE]
    assert len(consolidate) == 1
    assert consolidate[0].keep_href == HREF_A
    assert consolidate[0].ignore_hrefs == [HREF_B]
