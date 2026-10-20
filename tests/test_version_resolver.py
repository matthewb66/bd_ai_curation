import asyncio

from bd_ai_curation import bom_writer, global_values, version_resolver
from bd_ai_curation.models import BomComponent, CurationAction, Origin

BASE = 'https://bd'
KB = [('1.3.5', f'{BASE}/api/components/c1/versions/v135'),
      ('1.3.6', f'{BASE}/api/components/c1/versions/v136'),
      ('1.4.0', f'{BASE}/api/components/c1/versions/v140')]


def _entry(paths, name='libvorbis', comp_id='c1', href='https://bd/api/projects/p/versions/v/components/e1'):
    return BomComponent(name=name, version='', href=href, component_id=comp_id,
                        match_types=['BINARY'], matched_file_paths=paths, usages=['DYNAMICALLY_LINKED'])


def _bom(name, version, paths, comp_id='c1', version_id='', href='https://bd/bom/x'):
    return BomComponent(name=name, version=version, href=href, component_id=comp_id,
                        component_version_id=version_id, match_types=['FILE_EXACT'],
                        matched_file_paths=paths)


def test_infers_single_version_from_path_majority():
    paths = ['usr/lib/libvorbis-1.3.6/a.c', 'usr/lib/libvorbis-1.3.6/b.c', 'other/1.3.6/c.c']
    inf = version_resolver.infer_version(paths, KB)
    assert inf.chosen == ('1.3.6', f'{BASE}/api/components/c1/versions/v136')


def test_ambiguous_versions_yield_no_choice():
    paths = ['x/1.3.5/a', 'x/1.4.0/b']
    inf = version_resolver.infer_version(paths, KB)
    assert inf.chosen is None
    assert set(inf.candidates) == {'1.3.5', '1.4.0'}


def test_version_in_minority_of_paths_is_rejected():
    paths = ['a/1.3.6/x', 'b/y', 'c/z', 'd/w']
    inf = version_resolver.infer_version(paths, KB)
    assert inf.chosen is None


def test_version_must_not_match_inside_longer_number():
    inf = version_resolver.infer_version(['pkg/11.3.6.9/a'], KB)
    assert inf.chosen is None


def test_container_archive_version_is_not_used_as_evidence():
    paths = ['usr/sbin/ntpclient/#f9de!/R7000-V1.3.6_1.1.93.chk!/x.xz!/']
    inf = version_resolver.infer_version(paths, KB)
    assert inf.chosen is None
    assert inf.candidates == []


def test_no_matched_paths_gives_no_version():
    assert version_resolver.infer_version([], KB).chosen is None


def test_same_version_already_in_bom_is_ignored():
    entry = _entry(['x/1.3.6/a.c'])
    bom = [_bom('libvorbis', '1.3.6', ['other/a.c'], version_id='v136')]
    [d] = version_resolver.decide_unversioned([entry], {'c1': KB}, bom)
    assert d.action == CurationAction.IGNORE
    assert '1.3.6' in d.reasoning


def test_different_version_with_shared_paths_is_ignored_as_duplicate():
    entry = _entry(['x/1.3.6/a.c', 'x/1.3.6/b.c'])
    bom = [_bom('libvorbis', '1.3.5', ['x/1.3.6/a.c', 'x/1.3.6/b.c'], version_id='v135')]
    [d] = version_resolver.decide_unversioned([entry], {'c1': KB}, bom)
    assert d.action == CurationAction.IGNORE
    assert 'duplicate' in d.reasoning


def test_different_version_with_distinct_paths_is_added_as_new_version():
    entry = _entry(['x/1.3.6/a.c', 'x/1.3.6/b.c'])
    bom = [_bom('libvorbis', '1.3.5', ['y/1.3.5/z.c'], version_id='v135')]
    [d] = version_resolver.decide_unversioned([entry], {'c1': KB}, bom)
    assert d.action == CurationAction.SET_VERSION
    assert d.new_version_href == f'{BASE}/api/components/c1/versions/v136'
    assert 'separate version' in d.reasoning


def test_duplicate_without_matched_paths_is_manual_review():
    entry = _entry(['x/1.3.6/a.c'])
    bom = [_bom('libvorbis', '1.3.5', [], version_id='v135')]
    [d] = version_resolver.decide_unversioned([entry], {'c1': KB}, bom)
    assert d.action == CurationAction.NO_ACTION_MANUAL_REVIEW


def test_unidentifiable_version_is_manual_review_with_candidates():
    entry = _entry(['x/1.3.5/a', 'x/1.4.0/b'])
    [d] = version_resolver.decide_unversioned([entry], {'c1': KB}, [])
    assert d.action == CurationAction.NO_ACTION_MANUAL_REVIEW
    assert 'candidates' in d.reasoning


def test_set_version_put_body_matches_reference_shape(monkeypatch):
    captured = {}

    class FakeSession:
        def put(self, url, json=None, headers=None):
            captured['url'] = url
            captured['json'] = json
            captured['headers'] = headers

            class R:
                def raise_for_status(self):
                    return None
            return R()

    class FakeBd:
        session = FakeSession()

        def http_error_handler(self, err):
            raise err

    monkeypatch.setattr(global_values, 'bd', FakeBd())
    monkeypatch.setattr(global_values, 'max_concurrent_rest_requests', 4)

    entry_href = f'{BASE}/api/projects/P/versions/V/components/cdaea46a'
    d = version_resolver.decide_unversioned(
        [_entry(['x/1.3.6/a.c', 'x/1.3.6/b.c'], href=entry_href)], {'c1': KB}, [])[0]
    assert d.action == CurationAction.SET_VERSION

    async def no_comments(sem, projver_href, decisions):
        captured.setdefault('comments', []).extend(x.target_href for x in decisions)
    monkeypatch.setattr(bom_writer, '_bulk_comment_per_decision', no_comments)

    count, failed = asyncio.run(bom_writer.apply_set_version(f'{BASE}/api/projects/P/versions/V', [d]))
    assert count == 1 and failed == {}
    assert captured['url'] == entry_href
    assert captured['json'] == {
        'component': f'{BASE}/api/components/c1',
        'usages': ['DYNAMICALLY_LINKED'],
        'componentModified': False,
        'componentVersion': f'{BASE}/api/components/c1/versions/v136',
        'origins': [],
    }
    assert captured['comments'] == [entry_href]


def test_candidate_tokens_are_dotted_numbers_from_own_path_only():
    paths = ['usr/lib/avahi-6.0.1/libavahi-core.so.6.0.1#R7000-V1.0.7.2_1.1.93.chk',
             'usr/lib/avahi-6.0.1/a.c']
    tokens = version_resolver.candidate_version_tokens(paths)
    assert tokens == {'6.0.1'}
