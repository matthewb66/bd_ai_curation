# bd_ai_curation

Agentic Black Duck SCA BOM curation service. Replicates manual BOM curation tasks -
validating signature matches, finding duplicate/overlapping component entries, and
confirming/rejecting Match Review (snippet) matches - using an LLM (Claude) to make
the judgment calls that
[`bd_sig_filter`](https://github.com/matthewb66/bd_sig_filter) makes with
deterministic fuzzy-matching heuristics.

## Architecture

- **MCP** (`blackduck-bdsca-mcp`) is used only for what it actually exposes:
  listing projects/versions, and a basic component listing (name/version/origin,
  no `matchTypes`).
- **Direct REST API** (via the `blackduck` Python package) is used for everything
  curation-specific: BOM components with `matchTypes`, `matched-files`,
  `review-components` (Match Review/snippets), and all BOM mutations
  (`bulk-adjustment`, `bulk-comment`, `match-review/bulk-adjustment`).
- **Claude** (via the `anthropic` SDK) makes the ignore/confirm/consolidate
  decisions, given structured BOM + matched-file-path data, forced into a
  `submit_decisions` tool call validated against a pydantic schema.

See `/Users/mbrad/.claude/plans/pasted-content-id-02b3-i-would-toasty-snowflake.md`
for the full design rationale.

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
- `BLACKDUCK_BDSCA_URL` / `BLACKDUCK_BDSCA_TOKEN` (MCP server).
- `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` (or `ANTHROPIC_API_KEY`) for the
  Claude curation calls.

## Usage

```bash
# Discovery (MCP-backed)
bd-ai-curate list-projects
bd-ai-curate list-versions "Small Demo Main"
bd-ai-curate list-components "Small Demo Main" 1.0_latest

# Curation (REST + LLM). Dry-run by default - reports proposed decisions only.
bd-ai-curate curate "Small Demo Main" 1.0_latest --report_file report.txt

# Include Match Review (snippet/low-match-score + unmatched) components
bd-ai-curate curate "Small Demo Main" 1.0_latest --include_match_review

# Strip known-noise matched file paths (tests, Coverity 'idir' scan dirs, Detect
# run folders/jars, virtualenv folders, hidden dot-folders) before curation
bd-ai-curate curate "Small Demo Main" 1.0_latest --filter_paths

# Also curate unconfirmed (NOT_REVIEWED) snippet matches, grouped by component
bd-ai-curate curate "Small Demo Main" 1.0_latest --unconfirmed_snippets

# Apply decisions to the live BOM (only after reviewing a dry-run report!)
bd-ai-curate curate "Small Demo Main" 1.0_latest --apply
```

### `--filter_paths`

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

### `--unconfirmed_snippets`

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

Dry-run reports the proposed confirmations. Only `--apply` writes them, via
`PUT .../bulk-snippet-bom-entries` in chunks of 100.

Every `curate` run (dry-run or applied) writes a JSON decision log
(`--decisions_file`, default `bd_ai_curation_decisions.json`) for audit/replay,
and prints a summary + per-component report table.

## Performance

The REST BOM fetch (matched-files per origin) and the LLM batch calls both run
concurrently rather than one at a time, bounded by:

- `--max_concurrent_rest_requests` (default `10`) - Black Duck REST calls.
- `--max_concurrent_llm_calls` (default `5`) - Anthropic LLM calls.

On a 644-component real-world project ("elasticsearch"), this took the full
`curate` dry-run from several minutes down to ~100 seconds with zero coverage
loss (verified against the sync baseline).

## Safety

`curate` is **dry-run by default**. BOM mutations only happen when `--apply` is
explicitly passed. Decisions with action `NO_ACTION_MANUAL_REVIEW` are never
applied regardless of `--apply` - they're always left for a human.

`UNMATCHED` Match Review items (declared/manifest components with zero
signature-path evidence) are never sent to the LLM for an automatic decision -
there's no evidence for it to reason over - and are always flagged for manual
review, matching `bd_sig_filter`'s documented caution.

## Out of scope (v1)

Adding entirely new components to the BOM is not implemented - no verified Black
Duck REST endpoint for this was found during design, and no reference
implementation exists in sibling tooling. `CONSOLIDATE` decisions are implemented
as "pick a survivor, ignore the rest" (there is no BOM "merge" API).
