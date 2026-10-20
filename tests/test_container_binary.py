from bd_ai_curation import container_binary
from bd_ai_curation.models import BomComponent, CurationAction

BLOB = 'var/lib/dpkg/status/#a0e794d5!/blobs/sha256/4a5d42c7!/'


def _comp(name, version, match_types, cid='c1', href=None, paths=None, review=None, ignored=False):
    return BomComponent(name=name, version=version, href=href or f'https://bd/{name}/{version}/{cid}',
                        component_id=cid, match_types=match_types,
                        matched_file_paths=[BLOB] if paths is None else paths,
                        match_review_type=review, ignored=ignored)


def test_detects_container_scan_from_layer_blob_paths():
    assert container_binary.is_container_scan([_comp('x', '1', ['BINARY'])])


def test_binary_only_with_no_blob_paths_is_not_container_scan():
    assert not container_binary.is_container_scan([_comp('x', '1', ['BINARY'], paths=['src/a.so'])])


def test_binary_only_duplicate_of_dependency_entry_is_ignored():
    dep = _comp('zlib', '1.2.11', ['DIRECT_DEPENDENCY_BINARY', 'BINARY'], href='https://bd/dep')
    bin_only = _comp('zlib', '1.2.11', ['BINARY'], href='https://bd/bin')
    comps = [dep, bin_only]
    decisions, remaining = container_binary.decide(comps, comps)
    assert [d.action for d in decisions] == [CurationAction.IGNORE]
    assert decisions[0].target_href == 'https://bd/bin'
    assert [c.href for c in remaining] == ['https://bd/dep']


def test_binary_only_duplicate_of_binary_entry_is_ignored_except_lowest_href():
    other = _comp('zlib', '1.2.11', ['BINARY'], href='https://bd/other')
    bin_only = _comp('zlib', '1.2.11', ['BINARY'], href='https://bd/bin')
    comps = [other, bin_only]
    decisions, remaining = container_binary.decide(comps, comps)
    by_href = {d.target_href: d.action for d in decisions}
    assert by_href == {'https://bd/bin': CurationAction.NO_ACTION_MANUAL_REVIEW,
                       'https://bd/other': CurationAction.IGNORE}
    assert remaining == []


def test_binary_only_group_always_keeps_one_entry_for_review():
    group = [_comp('zlib', '1.2.11', ['BINARY'], href=f'https://bd/{i}') for i in range(3)]
    decisions, _ = container_binary.decide(group, group)
    kept = [d.target_href for d in decisions if d.action == CurationAction.NO_ACTION_MANUAL_REVIEW]
    assert kept == ['https://bd/0']
    assert sorted(d.target_href for d in decisions if d.action == CurationAction.IGNORE) == ['https://bd/1', 'https://bd/2']


def test_binary_only_with_no_peer_is_manual_review():
    bin_only = _comp('zlib', '1.2.11', ['BINARY'], href='https://bd/bin')
    decisions, _ = container_binary.decide([bin_only], [bin_only])
    assert decisions[0].action == CurationAction.NO_ACTION_MANUAL_REVIEW


def test_binary_only_with_different_version_in_bom_is_manual_review():
    dep = _comp('zlib', '1.2.8', ['DIRECT_DEPENDENCY_BINARY'], href='https://bd/dep')
    bin_only = _comp('zlib', '1.2.11', ['BINARY'], href='https://bd/bin')
    comps = [dep, bin_only]
    decisions, _ = container_binary.decide(comps, comps)
    assert decisions[0].action == CurationAction.NO_ACTION_MANUAL_REVIEW
    assert '1.2.8' in decisions[0].reasoning


def test_unversioned_binary_only_is_manual_review():
    bin_only = _comp('zlib', '', ['BINARY'], href='https://bd/bin')
    decisions, _ = container_binary.decide([bin_only], [bin_only])
    assert decisions[0].action == CurationAction.NO_ACTION_MANUAL_REVIEW


def test_match_review_and_settled_entries_are_not_taken():
    review = _comp('zlib', '1.2.11', ['BINARY'], href='https://bd/r', review='LOW_MATCH_SCORE')
    reviewed = _comp('zlib', '1.2.11', ['BINARY'], href='https://bd/rv')
    reviewed.review_status = 'REVIEWED'
    comps = [review, reviewed]
    decisions, remaining = container_binary.decide(comps, comps)
    assert decisions == []
    assert len(remaining) == 2


def test_non_binary_only_entries_pass_through():
    dep = _comp('zlib', '1.2.11', ['DIRECT_DEPENDENCY_BINARY', 'BINARY'], href='https://bd/dep')
    decisions, remaining = container_binary.decide([dep], [dep])
    assert decisions == []
    assert remaining == [dep]


def test_ignored_bom_entry_is_not_duplicate_evidence():
    dep = _comp('zlib', '1.2.11', ['DIRECT_DEPENDENCY_BINARY'], href='https://bd/dep', paths=[])
    dep.ignored = True
    bin_only = _comp('zlib', '1.2.11', ['BINARY'], href='https://bd/bin')
    comps = [dep, bin_only]
    decisions, _ = container_binary.decide(comps, comps)
    assert decisions[0].action == CurationAction.NO_ACTION_MANUAL_REVIEW
