# bd_ai_curation - v0.1.1

Agentic Black Duck SCA BOM curation utility.
Replicates manual BOM curation tasks -
validating dependency/signature/binary matches, finding duplicate/overlapping component entries, and
optionally confirming/rejecting Match Review matches and snippet matches - using an LLM (Claude).

##  Script Operation

- Unless `--update_bom` is specified, no changes are made to the project version
- Analysis results are shown in the log (console) and a decision JSON file is created with results of the analysis per component (default 'bd_ai_curation_decisions.json')
- Where appropriate, match paths are inspected to assess the validity of components and versions.
- The option `--include_match_review` will also process low match score components in the Match Review page
- The option `--include_unconfirmed_snippets` will also process unconfirmed snippets, adding components to the BOM where snippet matches indicate their presence
- The option `filter_paths` will look for test folders, dot folders, virtualenv, Black Duck tool and other folders and mark components within them as ignored
- Unversioned binary matches get a version from the dotted version numbers in their matched file paths. Only the KB versions named by those numbers are looked up (with a `versionName` filter), so components with very large version histories are no longer skipped by a timeout. The version is set only when a single candidate is found in at least half of the matched paths; otherwise the entry is left for manual review.
- When `--update_bom` is specified, components will be marked as ignored or marked as reviewed if confirmed (with comments added to explain the decision). Components left without review status are intended for manual review. A project version comment is added to document the AI Review process.

Multiple match types are considered in the analysis including Dependency (all of which will be confirmed), Signature (exact file, exact folder, files modified), snippet (components already added to the BOM as snippets), binary with and without version and optionally match review (low match score only)/unconfirmed snippet matches.

Duplicate or overlapping components across match types will be reviewed, choosing the strongest version match where appropriate across all match types.

## Installation

```bash
cd bd_ai_curation
uv venv --python 3.13
source .venv/bin/activate
uv pip install -e .
```

## Configuration

Required Environment variables:

- `BLACKDUCK_URL` / `BLACKDUCK_API_TOKEN` (REST API) - falls back to
  `BLACKDUCK_BDSCA_URL` / `BLACKDUCK_BDSCA_TOKEN` if unset.
- `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` (or `ANTHROPIC_API_KEY`) for the
  Claude curation calls.

## Usage

Ensure you are in the same location where the virtualenv was created in Installation step above.

```bash
source .venv/bin/activate

# Curation (REST + LLM). Dry-run by default - reports proposed decisions only.
bd-ai-curate "Small Demo Main" 1.0_latest

# Include Match Review (low-match-score + unmatched) components
bd-ai-curate "Small Demo Main" 1.0_latest --include_match_review

# Also curate unconfirmed (NOT_REVIEWED) snippet matches, grouped by component
bd-ai-curate "Small Demo Main" 1.0_latest --include_unconfirmed_snippets

# Strip known-noise matched file paths (tests, Coverity 'java-api-stubs.jar', Detect
# run folders/jars, virtualenv folders, hidden dot-folders) before curation
bd-ai-curate "Small Demo Main" 1.0_latest --filter_paths

# Apply decisions to the live BOM (only after reviewing a dry-run report!)
bd-ai-curate "Small Demo Main" 1.0_latest --update_bom
```

### `--filter_paths` Option

Strips matched file paths that fall under known build-tooling/scan-artifact
locations before any grouping or LLM call:
- hidden dot-folders (`.git`, `.venv`, `.m2`, ...)
- test folders (`test`, `tests`, `__tests__`, `spec`/`specs`)
- Coverity stub jar (`java-api-stubs.jar`, the only matchable file in a Coverity `idir`; the top-level `idir` folder name varies, so the file name is matched instead)
- Detect run folders/jars (`blackduck-detect*`, `synopsys-detect*`)
- `venv`/`virtualenv` folders.

A signature-only or Match Review component left
with zero matched file paths after path filtering is auto-decided deterministically
(`IGNORE`, or `REJECT_MATCH_REVIEW` for a Match Review candidate) without
spending an LLM call on it - these are reported with confidence 1.0 and a
reasoning string that cites the number of noise paths removed. Dependency
matches are unaffected (they don't rely on matched-file evidence).

### `--include_match_review` Option

Adds Match Review items to the run. Without this flag, only the components already
in the BOM are curated. The flag is additive: BOM components are still curated, and
Match Review items are added to the same run.

Match Review items come from `review-components` (bill-of-materials-5), which the
server returns in two kinds:

- `LOW_MATCH_SCORE` - snippet or low-score matches not yet in the BOM. These are
  fetched with their matched-file paths and go through the same LLM and grouping
  path as BOM components. The LLM may propose `CONFIRM_MATCH_REVIEW` or
  `REJECT_MATCH_REVIEW` for these, since they must be actioned through the
  `match-review/bulk-adjustment` endpoint rather than `bulk-adjustment`.
- `UNMATCHED` - declared components with no signature-path evidence. These are
  dropped in `bom_data.fetch_review_components` before curation, and the count is
  logged as "UNMATCHED skipped - no signature evidence". They produce no decision
  and are not written to the BOM.

Under `--update_bom`, a confirmed `LOW_MATCH_SCORE` item is added to the BOM and
marked `REVIEWED`, and gets an audit comment (`apply_confirm_match_review`). A
rejected one is marked `ignored` in Match Review (`apply_reject_match_review`);
this path does not post a comment.

`--filter_paths` applies to Match Review items too. A `LOW_MATCH_SCORE` item with
no real matched paths left after filtering is auto-rejected without an LLM call.

Match Review snippets are a different source from the per-file snippet blocks in
`--include_unconfirmed_snippets`, which are not covered by this flag.

### `--include_unconfirmed_snippets` Option

Fetches the snippet matches that are still `NOT_REVIEWED` in the version's
`source-bom-entries` (the same internal endpoint `ignore_snippets.py` uses) and
groups them by reported component/version.

The decision is deterministic and rule-based, not LLM-based, because confirmation
writes to the live BOM:

- A block is strong when `matchScore >= 0.90`, `matchCoverage >= 90`, and it
  covers `>= 20` source lines.
- A component is confirmed when at least 2 distinct scanned files each have a
  strong block. Only the strong blocks are confirmed (`CONFIRM_SNIPPET`, marked
  `REVIEWED`, `ignored: false`). Weak blocks of the same component stay
  `NOT_REVIEWED`.
- Otherwise the component is `NO_ACTION_MANUAL_REVIEW` and left untouched.

Dry-run reports the proposed confirmations. Only `--update_bom` writes them, via
`PUT .../bulk-snippet-bom-entries` in chunks of 100.

## Options Reference

Usage: `bd-ai-curate PROJECT VERSION [options]`. `PROJECT` and `VERSION` are the Black Duck project and version names. Run `bd-ai-curate -h` for the same list.

### Positional arguments

| Argument | Description |
|---|---|
| `project` | Black Duck project name. If it is not found, the command lists projects whose names contain the string and exits. |
| `version` | Black Duck project version name. If it is not found, the command lists the project's versions and exits. |

### Connection

| Option | Default | Description |
|---|---|---|
| `--blackduck_url URL` | `BLACKDUCK_URL` (or `BLACKDUCK_BDSCA_URL`) | Black Duck server URL. Required if the environment variable is not set. |
| `--blackduck_api_token TOKEN` | `BLACKDUCK_API_TOKEN` (or `BLACKDUCK_BDSCA_TOKEN`) | Black Duck API token. Required if the environment variable is not set. Prefer the environment variable so the token does not appear in shell history. |
| `--blackduck_trust_cert` | off | Trust the server certificate. Also enabled by `BLACKDUCK_TRUST_CERT=true`. |

The Anthropic credentials are read from `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`, with `ANTHROPIC_BASE_URL` optional. The run stops if neither key is set.

### Curation scope

| Option | Default | Description |
|---|---|---|
| `--update_bom` | off (dry-run) | Write the decisions to the project version: ignore, mark reviewed, set versions, confirm or reject Match Review and snippet matches, and add comments. Without it, nothing is changed. |
| `--include_match_review` | off | Also curate Match Review `LOW_MATCH_SCORE` components. Additive to the BOM components. See the section above. |
| `--include_unconfirmed_snippets` | off | Also curate unconfirmed (`NOT_REVIEWED`) snippet matches. See the section above. |
| `--filter_paths` | off | Ignore matched paths under test, build, Coverity, Detect, virtualenv and hidden folders before curation. See the section above. |

### Output

| Option | Default | Description |
|---|---|---|
| `--report_file FILE` | none | Write the text report to FILE. The run stops if FILE already exists. |
| `--decisions_file FILE` | `bd_ai_curation_decisions.json` | JSON decision log, written on every run. Includes the match types for each decision. |
| `--logfile FILE` | none (console only) | Also write the log to FILE. The run stops if FILE already exists. |
| `--debug` | off | Enable debug logging. |

### Model and concurrency

| Option | Default | Description |
|---|---|---|
| `--model NAME` | `claude-haiku-5-5` | Anthropic model used for curation decisions. |
| `--max_components_per_llm_call N` | `20` | Maximum components or groups sent in one LLM call. |
| `--max_concurrent_llm_calls N` | `5` | Maximum concurrent LLM calls. |
| `--max_concurrent_rest_requests N` | `10` | Maximum concurrent Black Duck REST requests. |

## Performance

The REST BOM fetch (matched-files per origin) and the LLM batch calls both run
concurrently rather than one at a time, bounded by:

- `--max_concurrent_rest_requests` (default `10`) - Black Duck REST calls.
- `--max_concurrent_llm_calls` (default `5`) - Anthropic LLM calls.

## Safety

The script is **dry-run by default**. BOM mutations only happen when `--update_bom` is explicitly passed.
