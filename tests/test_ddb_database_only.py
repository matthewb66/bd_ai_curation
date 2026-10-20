from bd_ai_curation.models import BomComponent, CurationAction

HREF = 'https://bd/api/projects/p/versions/v/components/a/versions/va'
DPKG = 'var/lib/dpkg/status/#a0e794d5!/blobs/sha256/4a5d42c7!/'
RPM = '/var/lib/rpm/rpmdb.sqlite/#05c24c50!/blobs/sha256/e22cb8fe!/'
INSTALLED = '/usr/bin/which/#05c24c50!/blobs/sha256/e22cb8fe!/'
CONFIRM = dict(action='CONFIRM_REVIEWED', target_name='GNU which', target_version='2.21', target_href=HREF,
               confidence=0.7, reasoning='Matched package database entry.')


def _comp(paths, match_types=('DIRECT_DEPENDENCY_BINARY',)):
    return BomComponent(name='GNU which', version='2.21', href=HREF, match_types=list(match_types),
                        matched_file_paths=paths)


def test_dpkg_database_only_confirm_is_downgraded(run_batch):
    decisions = run_batch([CONFIRM], [_comp([DPKG])])
    assert decisions[0].action == CurationAction.NO_ACTION_MANUAL_REVIEW


def test_rpm_database_only_confirm_is_kept(run_batch):
    decisions = run_batch([CONFIRM], [_comp([RPM])])
    assert decisions[0].action == CurationAction.CONFIRM_REVIEWED


def test_confirm_with_an_installed_file_is_kept(run_batch):
    decisions = run_batch([CONFIRM], [_comp([INSTALLED, RPM])])
    assert decisions[0].action == CurationAction.CONFIRM_REVIEWED


def test_non_dependency_binary_confirm_is_kept(run_batch):
    decisions = run_batch([CONFIRM], [_comp([DPKG], match_types=('BINARY',))])
    assert decisions[0].action == CurationAction.CONFIRM_REVIEWED
