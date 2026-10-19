# bd_ai_curation - v0.1.0

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
- When `--update_bom` is specified, components will be marked as ignored or marked as reviewed if confirmed (with comments added to explain the decision). Components left without review status are intended for manual review. A project version comment is added to document the AI Review process.

Multiple match types are considered in the analysis including Dependency (all of which will be confirmed), Signature (exact file, exact folder, files modified), snippet (components already added to the BOM as snippets), binary with and without version and optionally match review (low match score only)/unconfirmed snipper matches.

Duplicate or overlapping components across match types will be reviewed, choosing the strongest version match where appropriate across all match types.

## Installation

```bash
cd bd_ai_curation
uv venv --python 3.13
source .venv/bin/activate
uv pip install -e .
```

## Configuration

Environment variables (already set in this environment, pointing at the same
Black Duck server used by the connected MCP plugin):

- `BLACKDUCK_URL` / `BLACKDUCK_API_TOKEN` (REST API) - falls back to
  `BLACKDUCK_BDSCA_URL` / `BLACKDUCK_BDSCA_TOKEN` if unset.
- `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` (or `ANTHROPIC_API_KEY`) for the
  Claude curation calls.

## Usage

```bash
# Curation (REST + LLM). Dry-run by default - reports proposed decisions only.
bd-ai-curate "Small Demo Main" 1.0_latest --report_file report.txt

# Include Match Review (snippet/low-match-score + unmatched) components
bd-ai-curate "Small Demo Main" 1.0_latest --include_match_review

# Strip known-noise matched file paths (tests, Coverity 'idir' scan dirs, Detect
# run folders/jars, virtualenv folders, hidden dot-folders) before curation
bd-ai-curate "Small Demo Main" 1.0_latest --filter_paths

# Also curate unconfirmed (NOT_REVIEWED) snippet matches, grouped by component
bd-ai-curate "Small Demo Main" 1.0_latest --include_unconfirmed_snippets

# Apply decisions to the live BOM (only after reviewing a dry-run report!)
bd-ai-curate "Small Demo Main" 1.0_latest --update_bom
```

### `--filter_paths` Option

Strips matched file paths that fall under known build-tooling/scan-artifact
locations before any grouping or LLM call: hidden dot-folders (`.git`, `.venv`,
`.m2`, ...), test folders (`test`, `tests`, `__tests__`, `spec`/`specs`),
Coverity scan intermediate dirs (everything under `idir`, e.g. `idir/emit`),
Detect run folders/jars (`blackduck-detect*`, `synopsys-detect*`), and
`venv`/`virtualenv` folders. A signature-only or Match Review component left
with zero matched file paths after filtering is auto-decided deterministically
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
groups them by reported component/version. These are per-file snippet blocks, so
they are not in `review-components` and `--include_match_review` does not cover
them.

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

The utility (dry-run or applied) writes a JSON decision log
(`--decisions_file`, default `bd_ai_curation_decisions.json`) for audit/replay,
and prints a summary + per-component report table.

## Performance

The REST BOM fetch (matched-files per origin) and the LLM batch calls both run
concurrently rather than one at a time, bounded by:

- `--max_concurrent_rest_requests` (default `10`) - Black Duck REST calls.
- `--max_concurrent_llm_calls` (default `5`) - Anthropic LLM calls.

## Safety

The script is **dry-run by default**. BOM mutations only happen when `--update_bom` is

explicitly passed.
