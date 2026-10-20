import asyncio

from bd_ai_curation import curation_engine
from bd_ai_curation.models import BomComponent, CurationAction

HREF = 'https://bd/api/projects/p/versions/v/components/a/versions/va'
DPKG = 'var/lib/dpkg/status/#a0e794d5!/blobs/sha256/4a5d42c7!/'
RPM = '/var/lib/rpm/rpmdb.sqlite/#05c24c50!/blobs/sha256/e22cb8fe!/'
INSTALLED = '/usr/bin/which/#05c24c50!/blobs/sha256/e22cb8fe!/'


def _comp(paths, match_types=('DIRECT_DEPENDENCY_BINARY',)):
    return BomComponent(name='GNU which', version='2.21', href=HREF, match_types=list(match_types),
                        matched_file_paths=paths)


def _run(monkeypatch, comp):
    async def fake_call(client, sem, payload):
        return [dict(action='CONFIRM_REVIEWED', target_name='GNU which', target_version='2.21', target_href=HREF,
                     confidence=0.7, reasoning='Matched package database entry.')]
    monkeypatch.setattr(curation_engine, '_call_llm', fake_call)
    return asyncio.run(curation_engine._decisions_for_batch(None, asyncio.Semaphore(1), 'task', [comp]))


def test_dpkg_database_only_confirm_is_downgraded(monkeypatch):
    decisions = _run(monkeypatch, _comp([DPKG]))
    assert decisions[0].action == CurationAction.NO_ACTION_MANUAL_REVIEW


def test_rpm_database_only_confirm_is_kept(monkeypatch):
    decisions = _run(monkeypatch, _comp([RPM]))
    assert decisions[0].action == CurationAction.CONFIRM_REVIEWED


def test_confirm_with_an_installed_file_is_kept(monkeypatch):
    decisions = _run(monkeypatch, _comp([INSTALLED, RPM]))
    assert decisions[0].action == CurationAction.CONFIRM_REVIEWED


def test_non_dependency_binary_confirm_is_kept(monkeypatch):
    decisions = _run(monkeypatch, _comp([DPKG], match_types=('BINARY',)))
    assert decisions[0].action == CurationAction.CONFIRM_REVIEWED
