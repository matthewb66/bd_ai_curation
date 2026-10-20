import asyncio
import logging

import pytest

from bd_ai_curation import curation_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@pytest.fixture
def run_batch(monkeypatch):
    """Runs one curation batch with the LLM stubbed to return the given raw decisions."""
    def _run(raw_decisions, batch):
        async def fake_call(client, sem, payload):
            return raw_decisions
        monkeypatch.setattr(curation_engine, '_call_llm', fake_call)
        return asyncio.run(curation_engine._decisions_for_batch(None, asyncio.Semaphore(1), 'task', batch))
    return _run
