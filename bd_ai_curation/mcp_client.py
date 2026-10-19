"""Thin client for the ONLY two capabilities the blackduck-bdsca-mcp server
actually exposes that this service needs: listing projects/versions, and a
basic (name + origin, no matchTypes) component listing. Everything curation-
specific (matchTypes, matched-files, review-components, BOM mutation) goes
through bom_data.py / bom_writer.py directly against the REST API instead -
the MCP server's components:// resource does not expose matchTypes or _meta
hrefs needed to act on a component.

Spawns the same `uvx --from blackduck-sca-mcp bdsca` process Claude Code's MCP
plugin uses, over stdio.
"""

import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import global_values


def _rest_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(max(1, global_values.max_concurrent_rest_requests))


def _server_params():
    return StdioServerParameters(
        command="uvx",
        args=["--managed-python", "--python", "3.13", "--from", "blackduck-sca-mcp", "bdsca"],
        env={
            "BLACKDUCK_BDSCA_URL": global_values.mcp_url,
            "BLACKDUCK_BDSCA_TOKEN": global_values.mcp_token,
        },
    )


@asynccontextmanager
async def _session():
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _tool_json(result):
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise ValueError("No text content block in tool result")


async def list_projects_async(query: str | None = None):
    async with _session() as session:
        result = await session.call_tool("search_projects_versions", {"query": query} if query else {})
        return _tool_json(result).get("projects", [])


async def list_versions_async(project_id: str):
    async with _session() as session:
        result = await session.call_tool("search_projects_versions", {"project_id": project_id})
        return _tool_json(result).get("project-versions", [])


def _parse_csv_rows(res) -> list[dict]:
    rows = []
    for content in res.contents:
        text = getattr(content, "text", None)
        if text is None:
            continue
        reader = csv.DictReader(io.StringIO(text.split("\n# total_count=")[0]))
        rows.extend(reader)
    return rows


async def list_components_async(project_id: str, project_version_id: str):
    async with _session() as session:
        result = await session.call_tool(
            "fetch_project_components",
            {"project_id": project_id, "project_version_id": project_version_id},
        )
        payload = _tool_json(result)
        resource_uri = payload["resource_uri"]

        base_offset = payload.get("pagination", {}).get("offset", 0)
        limit = payload.get("pagination", {}).get("limit", 100)
        total_count = payload.get("total_count", 0)

        # Fetch page 1 directly, then every remaining page concurrently (bounded)
        # instead of one offset at a time.
        first_page_uri = resource_uri.replace(f"offset={base_offset}", f"offset={base_offset}")
        first_res = await session.read_resource(first_page_uri)
        rows = _parse_csv_rows(first_res)

        sem = _rest_semaphore()

        async def _fetch_page(offset: int) -> list[dict]:
            async with sem:
                uri = resource_uri.replace(f"offset={base_offset}", f"offset={offset}")
                res = await session.read_resource(uri)
            return _parse_csv_rows(res)

        remaining_offsets = range(base_offset + limit, total_count, limit)
        pages = await asyncio.gather(*(_fetch_page(off) for off in remaining_offsets))
        for page_rows in pages:
            rows.extend(page_rows)

        return rows
