"""Fixtures for the knowledge agent tests: the Grimoire stub, the app in-process, a login done."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

FIXTURES = str(Path(__file__).resolve().parents[1] / "fixtures")
if FIXTURES not in sys.path:
    sys.path.insert(0, FIXTURES)

from grimoire_stub import GrimoireStub, start_grimoire_stub  # noqa: E402
from knowledge_helpers import Knowledge, make_settings  # noqa: E402


@pytest.fixture
def stub() -> Iterator[GrimoireStub]:
    with start_grimoire_stub() as running:
        yield running


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "knowledge-data"


@pytest.fixture
async def knowledge(stub: GrimoireStub, data_dir: Path) -> AsyncIterator[Knowledge]:
    k = Knowledge(make_settings(stub, data_dir))
    yield k
    await k.close()


@pytest.fixture
async def connected(knowledge: Knowledge, stub: GrimoireStub) -> Knowledge:
    status = await knowledge.connect(stub)
    assert status["connected"] is True
    stub.calls.clear()
    return knowledge
