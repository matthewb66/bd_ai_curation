from bd_ai_curation import global_values, path_filters, reporting
from bd_ai_curation.models import BomComponent, CurationAction, CurationDecision


def test_decision_log_keeps_paths_removed_by_filter(tmp_path):
    comp = BomComponent(name='WebGoat-x', version='', href='https://bd/c1', match_types=['FILE_SOME_FILES_MODIFIED'],
                        matched_file_paths=['tests/a.java', 'tests/b.java'])
    path_filters.auto_decide_noise_only([comp])
    assert comp.matched_file_paths == []

    decision = CurationDecision(action=CurationAction.IGNORE, target_name='WebGoat-x', target_version='',
                                target_href='https://bd/c1', confidence=1.0, reasoning='noise')
    global_values.decisions_file = str(tmp_path / 'log.json')
    global_values.bd_project = 'p'
    global_values.bd_version = 'v'
    reporting.write_decisions_log([decision], [comp])

    import json
    entry = json.loads((tmp_path / 'log.json').read_text())['decisions'][0]
    assert entry['matched_paths'] == ['tests/a.java', 'tests/b.java']
    assert entry['matched_file_count'] == 2
    assert 'new_version_href' not in entry
    assert 'usages' not in entry
