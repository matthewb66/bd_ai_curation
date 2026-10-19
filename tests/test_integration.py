"""Integration test suite: upload BDIO test data and run bd_ai_curation on both projects.

Steps:
  1. Delete each project if it already exists (clean slate).
  2. Upload the project's BDIO files from testdata/.
  3. Wait for all scans to reach COMPLETE status.
  4. Run bd_ai_curation curate with all supported options.
  5. Validate the output (decisions file, report, exit code).

Requires the following environment variables:
  BLACKDUCK_URL           Black Duck server URL
  BLACKDUCK_API_TOKEN     Black Duck API token
  ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN
  BLACKDUCK_TRUST_CERT    (optional) set to "true" to skip TLS verification
"""

import json
import logging
import os
import subprocess
import sys
import time
import pathlib
import pytest

from blackduck import Client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TESTDATA_DIR = pathlib.Path(__file__).parent.parent / "testdata"

# BDIO files keyed by (project_name, version_name).
PROJECT_BDIO: dict[tuple[str, str], list[str]] = {
    ("ssk-asterisk-sca", "release"): [
        "53cb2297-9338-4ef4-9270-d7cbaee1ac54.bdio",  # BDIO2 / package-manager
        "23f25dbb-0464-4ea3-81a1-a7ee3a5d65de.bdio",  # BDBA binary
        "45304c01-6348-48aa-af96-b72d1af419dd.bdio",  # signature scan
    ],
    ("elasticsearch", "main"): [
        "30cee41a-1b7e-4fe5-a56e-7cbbfae791e4.bdio",  # signature scan
        "1fc2aed3-60ad-445a-b359-81d9c33629c2.bdio",  # package-manager
    ],
}

SCAN_POLL_INTERVAL_S = 10
SCAN_TIMEOUT_S = 1200  # 20 minutes — large signature scans (41+ entries) can take 10-15 min


# ---------------------------------------------------------------------------
# Client helpers (use only blackduck.Client's public API)
# ---------------------------------------------------------------------------

def _bd_client() -> Client:
    url = os.environ.get("BLACKDUCK_URL") or os.environ.get("BLACKDUCK_BDSCA_URL")
    token = os.environ.get("BLACKDUCK_API_TOKEN") or os.environ.get("BLACKDUCK_BDSCA_TOKEN")
    trust = os.environ.get("BLACKDUCK_TRUST_CERT", "").lower() == "true"
    assert url, "BLACKDUCK_URL environment variable not set"
    assert token, "BLACKDUCK_API_TOKEN environment variable not set"
    return Client(token=token, base_url=url, verify=not trust, timeout=60)


def _find_project(bd: Client, project_name: str) -> dict | None:
    for p in bd.get_resource('projects', params={'q': f'name:{project_name}'}):
        if p['name'] == project_name:
            return p
    return None


def _find_version(bd: Client, project: dict, version_name: str) -> dict | None:
    for v in bd.get_resource('versions', parent=project,
                              params={'q': f'versionName:{version_name}'}):
        if v['versionName'] == version_name:
            return v
    return None


def _delete_project_if_exists(bd: Client, project_name: str) -> None:
    project = _find_project(bd, project_name)
    if not project:
        logger.info(f"Project '{project_name}' does not exist, nothing to delete.")
        return

    logger.info(f"Deleting existing project '{project_name}' ...")
    for version in bd.get_resource('versions', parent=project):
        cl_url = bd.list_resources(version).get('codelocations')
        if cl_url:
            result = bd.get_json(cl_url + '?limit=200&offset=0',
                                  headers={'content-type': 'application/vnd.blackducksoftware.scan-4+json'})
            for cl in result.get('items', []):
                logger.info(f"  Deleting codelocation {cl['_meta']['href']}")
                bd.session.delete(cl['_meta']['href'])

    bd.session.delete(project['_meta']['href'])
    logger.info(f"Project '{project_name}' deleted.")


def _upload_bdio(bd: Client, bdio_files: list[str], retries: int = 3) -> None:
    for filename in bdio_files:
        path = str(TESTDATA_DIR / filename)
        headers = {'Content-Type': 'application/vnd.blackducksoftware.bdio+zip'}
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            logger.info(f"Uploading {filename} (attempt {attempt}) ...")
            try:
                with open(path, 'rb') as f:
                    response = bd.session.post('/api/scan/data/?mode=replace',
                                               headers=headers, data=f)
                assert response.status_code in (200, 201, 202), (
                    f"Upload of {filename} failed: HTTP {response.status_code} - {response.text[:200]}"
                )
                logger.info(f"  → HTTP {response.status_code}")
                last_exc = None
                break
            except Exception as exc:
                logger.warning(f"  Upload attempt {attempt} failed: {exc}")
                last_exc = exc
                if attempt < retries:
                    time.sleep(5)
        if last_exc is not None:
            raise last_exc


def _wait_for_bom_complete(bd: Client, project_name: str, version_name: str,
                            expected_scan_count: int) -> None:
    """Poll until every scan under the project version is COMPLETE."""
    deadline = time.monotonic() + SCAN_TIMEOUT_S
    statuses: list[str] = []

    while True:
        project = _find_project(bd, project_name)
        if project is None:
            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"Timed out waiting for project '{project_name}' to appear"
            )
            logger.info(f"  Project '{project_name}' not yet visible, retrying ...")
            time.sleep(SCAN_POLL_INTERVAL_S)
            continue

        version = _find_version(bd, project, version_name)
        if version is None:
            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"Timed out waiting for version '{project_name}/{version_name}' to appear"
            )
            logger.info(f"  Version '{version_name}' not yet visible, retrying ...")
            time.sleep(SCAN_POLL_INTERVAL_S)
            continue

        cl_url = bd.list_resources(version).get('codelocations')
        codelocations = []
        if cl_url:
            cl_data = bd.get_json(
                cl_url + '?limit=200&offset=0',
                headers={'content-type': 'application/vnd.blackducksoftware.scan-4+json'},
            )
            codelocations = cl_data.get('items', [])

        logger.info(f"  {len(codelocations)}/{expected_scan_count} code location(s) visible "
                    f"for {project_name}/{version_name}")

        if len(codelocations) >= expected_scan_count:
            statuses = []
            for cl in codelocations:
                cl_id = cl['_meta']['href'].split('/')[-1]
                summaries_url = f'/api/codelocations/{cl_id}/scan-summaries?limit=10&offset=0'
                summaries = bd.get_json(
                    summaries_url,
                    headers={'accept': 'application/vnd.blackducksoftware.scan-4+json'},
                ).get('items', [])
                if summaries:
                    statuses.append(summaries[0]['status'])

            logger.info(f"  Scan statuses: {statuses}")
            # IDENTICAL_RESCAN is a terminal success state: BD detected the
            # scan is unchanged from a prior run and reused the existing BOM.
            DONE = {'COMPLETE', 'IDENTICAL_RESCAN'}
            if statuses and all(s in DONE for s in statuses):
                logger.info(f"  All scans done for {project_name}/{version_name}: {statuses}")
                return

        remaining = deadline - time.monotonic()
        assert remaining > 0, (
            f"Timed out after {SCAN_TIMEOUT_S}s waiting for scans to complete for "
            f"'{project_name}/{version_name}'. Last statuses: {statuses}"
        )
        logger.info(f"  Not complete yet, retrying in {SCAN_POLL_INTERVAL_S}s ...")
        time.sleep(SCAN_POLL_INTERVAL_S)


def _run_curate(project_name: str, version_name: str,
                decisions_file: str, report_file: str) -> subprocess.CompletedProcess:
    """Run bd_ai_curation curate with all supported options. Raises on non-zero exit."""
    trust = os.environ.get("BLACKDUCK_TRUST_CERT", "").lower() == "true"

    cmd = [
        sys.executable, "-m", "bd_ai_curation.main",
        "curate",
        project_name,
        version_name,
        "--update_bom",
        "--include_match_review",
        "--filter_paths",
        "--decisions_file", decisions_file,
        "--report_file", report_file,
    ]
    if trust:
        cmd.append("--blackduck_trust_cert")

    logger.info(f"Running curate for {project_name}/{version_name} ...")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(pathlib.Path(__file__).parent.parent),
        env=os.environ.copy(),
    )
    logger.info(f"  Exit code: {result.returncode}")
    if result.stdout:
        logger.debug(f"stdout:\n{result.stdout}")
    if result.stderr:
        logger.debug(f"stderr:\n{result.stderr}")
    assert result.returncode == 0, (
        f"bd_ai_curation curate exited {result.returncode} for {project_name}/{version_name}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _validate_decisions(decisions_file: str, project_name: str, version_name: str,
                         applied: bool = True) -> None:
    assert os.path.exists(decisions_file), f"Decisions file not written: {decisions_file}"

    with open(decisions_file) as f:
        payload = json.load(f)

    assert payload["project"] == project_name, (
        f"decisions project mismatch: got {payload['project']!r}, expected {project_name!r}"
    )
    assert payload["version"] == version_name, (
        f"decisions version mismatch: got {payload['version']!r}, expected {version_name!r}"
    )
    assert payload["applied"] is applied, (
        f"Expected applied={applied} but got {payload['applied']!r}"
    )

    decisions = payload["decisions"]
    assert isinstance(decisions, list), "decisions must be a list"
    assert len(decisions) > 0, "No decisions were produced - the BOM appears empty"

    valid_actions = {
        "IGNORE", "CONFIRM_REVIEWED", "CONFIRM_MATCH_REVIEW",
        "REJECT_MATCH_REVIEW", "CONSOLIDATE", "NO_ACTION_MANUAL_REVIEW",
    }
    for d in decisions:
        assert d["action"] in valid_actions, f"Unknown action {d['action']!r}"
        assert isinstance(d["confidence"], (int, float)), "confidence must be numeric"
        assert 0.0 <= d["confidence"] <= 1.0, f"confidence out of range: {d['confidence']}"
        assert d["target_name"], "target_name must not be empty"
        assert d["reasoning"], "reasoning must not be empty"


def _validate_report(report_file: str) -> None:
    assert os.path.exists(report_file), f"Report file not written: {report_file}"
    content = pathlib.Path(report_file).read_text()
    assert "SUMMARY" in content, f"Report missing SUMMARY section"
    assert "Action" in content and "Count" in content, "Report missing Action/Count header"
    assert "TOTAL COMPONENTS CONSIDERED" in content, "Report missing totals row"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bd() -> Client:
    return _bd_client()


@pytest.fixture(scope="module", autouse=True)
def setup_all_projects(bd):
    """Delete, upload, and wait for both projects once per test module."""
    for (project_name, _), files in PROJECT_BDIO.items():
        _delete_project_if_exists(bd, project_name)

    for (_, _), files in PROJECT_BDIO.items():
        _upload_bdio(bd, files)

    for (project_name, version_name), files in PROJECT_BDIO.items():
        _wait_for_bom_complete(bd, project_name, version_name, expected_scan_count=len(files))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAsterisk:
    PROJECT = "ssk-asterisk-sca"
    VERSION = "release"

    def test_curate_exits_zero(self, tmp_path):
        result = _run_curate(self.PROJECT, self.VERSION,
                              str(tmp_path / "decisions.json"),
                              str(tmp_path / "report.txt"))
        assert result.returncode == 0, (
            f"bd_ai_curation exited {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_decisions_file_valid(self, tmp_path):
        _run_curate(self.PROJECT, self.VERSION,
                    str(tmp_path / "decisions.json"),
                    str(tmp_path / "report.txt"))
        _validate_decisions(str(tmp_path / "decisions.json"), self.PROJECT, self.VERSION)

    def test_report_file_written(self, tmp_path):
        _run_curate(self.PROJECT, self.VERSION,
                    str(tmp_path / "decisions.json"),
                    str(tmp_path / "report.txt"))
        _validate_report(str(tmp_path / "report.txt"))

    def test_no_uncovered_components(self, tmp_path):
        _run_curate(self.PROJECT, self.VERSION,
                    str(tmp_path / "decisions.json"),
                    str(tmp_path / "report.txt"))
        with open(tmp_path / "decisions.json") as f:
            payload = json.load(f)
        # Pipeline-coverage fallbacks have a fixed sentinel prefix; LLM-decided
        # NO_ACTION_MANUAL_REVIEW (genuine ambiguity) is acceptable.
        FALLBACK_PREFIX = "No valid decision returned by the LLM"
        fallbacks = [d for d in payload["decisions"]
                     if d["action"] == "NO_ACTION_MANUAL_REVIEW"
                     and d["reasoning"].startswith(FALLBACK_PREFIX)]
        assert len(fallbacks) == 0, (
            f"{len(fallbacks)} component(s) hit the pipeline-coverage fallback "
            f"(should not happen): {[d['target_name'] for d in fallbacks]}"
        )


class TestElasticsearch:
    PROJECT = "elasticsearch"
    VERSION = "main"

    def test_curate_exits_zero(self, tmp_path):
        result = _run_curate(self.PROJECT, self.VERSION,
                              str(tmp_path / "decisions.json"),
                              str(tmp_path / "report.txt"))
        assert result.returncode == 0, (
            f"bd_ai_curation exited {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_decisions_file_valid(self, tmp_path):
        _run_curate(self.PROJECT, self.VERSION,
                    str(tmp_path / "decisions.json"),
                    str(tmp_path / "report.txt"))
        _validate_decisions(str(tmp_path / "decisions.json"), self.PROJECT, self.VERSION)

    def test_report_file_written(self, tmp_path):
        _run_curate(self.PROJECT, self.VERSION,
                    str(tmp_path / "decisions.json"),
                    str(tmp_path / "report.txt"))
        _validate_report(str(tmp_path / "report.txt"))

    def test_no_uncovered_components(self, tmp_path):
        _run_curate(self.PROJECT, self.VERSION,
                    str(tmp_path / "decisions.json"),
                    str(tmp_path / "report.txt"))
        with open(tmp_path / "decisions.json") as f:
            payload = json.load(f)
        # Pipeline-coverage fallbacks have a fixed sentinel prefix; LLM-decided
        # NO_ACTION_MANUAL_REVIEW (genuine ambiguity) is acceptable.
        FALLBACK_PREFIX = "No valid decision returned by the LLM"
        fallbacks = [d for d in payload["decisions"]
                     if d["action"] == "NO_ACTION_MANUAL_REVIEW"
                     and d["reasoning"].startswith(FALLBACK_PREFIX)]
        assert len(fallbacks) == 0, (
            f"{len(fallbacks)} component(s) hit the pipeline-coverage fallback "
            f"(should not happen): {[d['target_name'] for d in fallbacks]}"
        )
