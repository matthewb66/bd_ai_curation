"""Builds LLM prompts/tool schemas, calls Claude, and validates returned decisions
against models.CurationDecision before they are ever acted on.
"""

import asyncio
import json
import logging
import re

import anthropic
from pydantic import ValidationError

from . import global_values
from .grouping import DuplicateGroup, OverlapGroup
from .models import BomComponent, CurationAction, CurationDecision

SUBMIT_DECISIONS_TOOL = {
    "name": "submit_decisions",
    "description": "Submit curation decisions for the Black Duck BOM components under review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "IGNORE", "CONFIRM_REVIEWED", "CONFIRM_MATCH_REVIEW",
                                "REJECT_MATCH_REVIEW", "CONSOLIDATE", "NO_ACTION_MANUAL_REVIEW",
                            ],
                        },
                        "target_name": {"type": "string"},
                        "target_version": {"type": "string"},
                        "target_href": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "reasoning": {"type": "string"},
                        "match_review_type": {"type": ["string", "null"]},
                        "input_external_id": {"type": ["string", "null"]},
                        "keep_href": {"type": ["string", "null"],
                                      "description": "For CONSOLIDATE only: href of the surviving component."},
                        "ignore_hrefs": {"type": "array", "items": {"type": "string"},
                                         "description": "For CONSOLIDATE only: hrefs of components to ignore."},
                    },
                    "required": ["action", "target_name", "target_version", "target_href",
                                 "confidence", "reasoning"],
                },
            },
        },
        "required": ["decisions"],
    },
}

SYSTEM_PROMPT = """You are assisting a Black Duck SCA curator in reviewing Bill of \
Materials (BOM) component matches identified by signature scanning.

For each component or group of components you are given:
- name, version, origin external IDs
- matchTypes (from the Black Duck Knowledge Base)
- matched file paths within the scanned codebase (signature match evidence)
- for duplicate/overlap groups, the sibling components being compared

Decide, for each component, one of:
- CONFIRM_REVIEWED: the component name and/or version is clearly supported by the \
matched file paths - mark it reviewed (keep it in the BOM, confirmed valid). This \
still applies when sibling components of the same name with other versions are \
also matched: judge each version by its own matched paths. Strong evidence for a \
version (e.g. a file whose name carries that exact version, in its own module or \
location) should be confirmed even if another version is matched elsewhere. Different \
versions in different locations are normally separate real artifacts, not duplicates.

Binary matches (match_types such as BINARY or DIRECT_DEPENDENCY_BINARY) are judged \
differently from source matches. Compiled artifacts (.a, .so, .dylib, .dll, .jar) \
rarely carry a version in their file name, so the version not appearing in the \
matched path is not evidence against it. If the binary is a genuine build artifact \
of the component and the version comes from the Knowledge Base origin, CONFIRM_REVIEWED, \
even though the path does not name the version. Only withhold a confirm for a binary \
match when the binary's own path names a different version of this same artifact, or \
when the artifact is clearly not this library. A different version of the library \
found elsewhere in the scan (e.g. a source tree) is not by itself a contradiction for \
a binary match - judge the binary's own path.
- IGNORE: the matched file paths do not support this being a real occurrence of this \
component. Examples: the paths only reference unrelated tooling/cache/test folders; \
the component name/version has no correspondence to the paths; or the paths are \
clearly sources of a different library (e.g. libwebp files attributed to a Qt \
component with no Qt paths or references). When the paths point to another owner, \
IGNORE and name that owner in the reasoning - do not defer it to manual review just \
because the true owner is a different component.
- CONFIRM_MATCH_REVIEW / REJECT_MATCH_REVIEW: use these only for Match Review \
(match_review_type=LOW_MATCH_SCORE) items instead of CONFIRM_REVIEWED/IGNORE, since \
these are not yet in the BOM and must be confirmed or rejected via the Match Review \
API rather than simply reviewed/ignored.
- CONSOLIDATE: only when entries describe the same occurrence of the software (e.g. \
the same component id, or heavily overlapping matched paths pointing at the same file \
or artifact). Pick the single best-supported entry as keep_href and list every other \
entry's href in ignore_hrefs. Do not consolidate different versions that are matched \
in different locations - confirm each one that has strong evidence instead.

For overlapping_match_consolidation tasks, the sibling components all match the same \
folder (common_path_prefix). Decide what that location is once, then give the siblings \
verdicts that agree with that decision. If the folder is one real occurrence of a \
library, the siblings that describe that library should be treated alike, not confirmed \
on one entry and sent to manual review on another. If the folder belongs to a different \
library, IGNORE the siblings that do not describe it. Do not write "uncertain" or \
"version unverified" in the reasoning of a CONFIRM_REVIEWED verdict; if you have that \
doubt, the verdict is not a confirm. A CONFIRM_REVIEWED reasoning must not say the \
component needs human review or was flagged for it - that wording downgrades the \
decision to manual review.
- NO_ACTION_MANUAL_REVIEW: use this only when the paths are genuinely ambiguous - they \
could support either confirming or ignoring, and you cannot tell which. Do not use it \
when the paths clearly point to a different owner (use IGNORE), and do not use it \
merely because another version of the same component is also present.

Always give a concise, specific reasoning citing the actual file paths or match types \
that drove your decision - this reasoning is written back into the BOM as an audit \
comment. Choose the verdict the evidence supports; manual review is for real ambiguity, \
not for a lack of confidence in a clear answer."""


def _client() -> anthropic.AsyncAnthropic:
    kwargs = {"api_key": global_values.anthropic_api_key}
    if global_values.anthropic_base_url:
        kwargs["base_url"] = global_values.anthropic_base_url
    return anthropic.AsyncAnthropic(**kwargs)


def _llm_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(max(1, global_values.max_concurrent_llm_calls))


def _component_payload(comp: BomComponent) -> dict:
    return {
        "name": comp.name,
        "version": comp.version,
        "href": comp.href,
        "match_types": comp.match_types,
        "origins": [o.external_id for o in comp.origins],
        "matched_file_paths": comp.matched_file_paths[:200],
        "match_review_type": comp.match_review_type,
        "input_external_ids": comp.input_external_ids,
    }


def _batched(items, size):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


MAX_TOKENS = 8192


async def _call_llm(client, sem: asyncio.Semaphore, user_payload: dict) -> list[dict] | None:
    """Returns parsed decisions, or None if max_tokens was hit (caller should retry smaller)."""
    async with sem:
        message = await client.messages.create(
            model=global_values.llm_model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[SUBMIT_DECISIONS_TOOL],
            tool_choice={"type": "tool", "name": "submit_decisions"},
            messages=[{"role": "user", "content": json.dumps(user_payload)}],
        )

    batch_size = len(user_payload.get('components', []))
    if message.stop_reason == "max_tokens":
        logging.warning(
            f"LLM response hit max_tokens ({MAX_TOKENS}) for a batch of "
            f"{batch_size} components - will retry with smaller batches"
        )
        return None  # sentinel: caller should split and retry

    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_decisions":
            decisions = block.input.get("decisions", [])
            if isinstance(decisions, str):
                try:
                    decisions = json.loads(decisions)
                except json.JSONDecodeError as exc:
                    logging.warning(
                        f"LLM returned malformed decisions JSON for a batch of {batch_size} "
                        f"components ({exc}) - will retry with smaller batches"
                    )
                    return None  # sentinel: caller should split and retry
            return decisions
    return []


_DEFER_PATTERNS = re.compile(
    r"\b(human|manual) review\b|rather than (confirm|ignor)|not (directly )?confirm|"
    r"flagg?ing for (human|manual) review|leave for (human|manual)",
    re.IGNORECASE,
)


def _reasoning_defers(reasoning: str) -> bool:
    return bool(_DEFER_PATTERNS.search(reasoning))


def _fallback_decision(comp: BomComponent, reason: str) -> CurationDecision:
    return CurationDecision(
        action=CurationAction.NO_ACTION_MANUAL_REVIEW,
        target_name=comp.name,
        target_version=comp.version,
        target_href=comp.href,
        confidence=0.0,
        reasoning=reason,
        match_review_type=comp.match_review_type,
        input_external_id=(comp.input_external_ids[0] if comp.input_external_ids else None),
    )


async def _decisions_for_batch(client, sem: asyncio.Semaphore, task: str, batch,
                                extra_fields: dict | None = None) -> list[CurationDecision]:
    """Calls the LLM for one batch and guarantees every component in the batch is
    covered by a *validated* CurationDecision.

    If the LLM hits max_tokens the batch is split in half and retried recursively,
    down to single-component batches. Other failures (network errors, schema
    validation) fall back to NO_ACTION_MANUAL_REVIEW rather than silently dropping.
    """
    if not batch:
        return []

    payload = {"task": task, "components": [_component_payload(c) for c in batch]}
    if extra_fields:
        payload.update(extra_fields)

    try:
        raw_decisions = await _call_llm(client, sem, payload)
    except Exception as exc:
        logging.warning(f"LLM call failed for a batch of {len(batch)} components "
                         f"({task}): {exc} - falling back to NO_ACTION_MANUAL_REVIEW for all of them")
        raw_decisions = []

    # None means the response was truncated or unparseable - split and retry unless already at 1 component
    if raw_decisions is None:
        if len(batch) == 1:
            logging.warning(
                f"Single-component batch for '{batch[0].name}/{batch[0].version}' still "
                f"returned a truncated or unparseable response - falling back to NO_ACTION_MANUAL_REVIEW"
            )
            return [_fallback_decision(
                batch[0],
                "No valid decision returned by the LLM for this component (single-component "
                "response was truncated or unparseable) - flagged for manual review.",
            )]
        mid = len(batch) // 2
        logging.info(f"Splitting batch of {len(batch)} into {mid} + {len(batch) - mid} and retrying")
        left, right = await asyncio.gather(
            _decisions_for_batch(client, sem, task, batch[:mid], extra_fields),
            _decisions_for_batch(client, sem, task, batch[mid:], extra_fields),
        )
        return left + right

    decisions: list[CurationDecision] = []
    covered_hrefs: set[str] = set()
    for raw in raw_decisions:
        if not isinstance(raw, dict):
            logging.warning(f"Discarding non-object LLM decision: {raw!r:.200}")
            continue
        try:
            decision = CurationDecision(**raw)
        except ValidationError as exc:
            logging.warning(f"Discarding invalid LLM decision for "
                             f"'{raw.get('target_name', '?')}': {exc}")
            continue
        if decision.action == CurationAction.CONFIRM_REVIEWED and _reasoning_defers(decision.reasoning):
            logging.warning(f"CONFIRM_REVIEWED for '{decision.target_name}/{decision.target_version}' "
                             f"contradicts its own reasoning - downgrading to NO_ACTION_MANUAL_REVIEW")
            decision.action = CurationAction.NO_ACTION_MANUAL_REVIEW
        decisions.append(decision)
        covered_hrefs.add(decision.target_href)
        covered_hrefs.update(decision.ignore_hrefs)

    for comp in batch:
        if comp.href in covered_hrefs:
            continue
        logging.warning(
            f"LLM did not return a valid decision for '{comp.name}/{comp.version}' - "
            f"falling back to NO_ACTION_MANUAL_REVIEW so it is not silently dropped"
        )
        decisions.append(_fallback_decision(
            comp,
            "No valid decision returned by the LLM for this component (possibly a "
            "truncated/incomplete batch response, a schema-validation failure, or a "
            "failed LLM call) - flagged for manual review rather than silently dropped.",
        ))

    return decisions


async def curate_components(components: list[BomComponent]) -> list[CurationDecision]:
    """Signature-only components with no duplicate/overlap grouping - curated
    individually in batches, run concurrently (bounded by max_concurrent_llm_calls).
    Every component is guaranteed a decision (falling back to
    NO_ACTION_MANUAL_REVIEW rather than being silently dropped).
    """
    client = _client()
    sem = _llm_semaphore()
    batches = list(_batched(components, global_values.max_components_per_llm_call))

    results = await asyncio.gather(
        *(_decisions_for_batch(client, sem, "individual_component_review", batch) for batch in batches)
    )
    return [d for batch_decisions in results for d in batch_decisions]


async def curate_duplicate_groups(groups: list[DuplicateGroup]) -> list[CurationDecision]:
    client = _client()
    sem = _llm_semaphore()

    results = await asyncio.gather(
        *(_decisions_for_batch(client, sem, "duplicate_consolidation", g.components) for g in groups)
    )
    return [d for group_decisions in results for d in group_decisions]


async def curate_overlap_groups(groups: list[OverlapGroup]) -> list[CurationDecision]:
    client = _client()
    sem = _llm_semaphore()

    results = await asyncio.gather(
        *(_decisions_for_batch(client, sem, "overlapping_match_consolidation", g.components,
                                extra_fields={"common_path_prefix": g.common_prefix})
          for g in groups)
    )
    return [d for group_decisions in results for d in group_decisions]
