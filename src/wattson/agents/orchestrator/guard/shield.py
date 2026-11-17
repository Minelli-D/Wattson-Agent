"""The Guard's second check of the evidence and the documents: the small prompt-injection model
in the pod wattson-shield (src/wattson/shield/server.py, docs/study/guardrails.md).

Shield.scores() sends the parts and gets one score each (0..1). It never raises: when the
shield is down, slow, or answers something else, it returns None and the Guard goes on with its
rules alone, and says so. The parts go to the shield only; no text reaches a log or a span.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import Literal

import httpx

from wattson.shared.auth import bearer_client
from wattson.shared.contracts import ComponentHealth

log = logging.getLogger("wattson.orchestrator")

InjectionMode = Literal["off", "warn", "cut"]
BATCH = 64  # parts per request (the shield takes at most 256)


class Shield:
    def __init__(
        self, url: str, token: str, *, mode: InjectionMode, threshold: float, timeout_s: float, model: str
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.mode = mode
        self.threshold = threshold
        self.timeout_s = timeout_s
        self.model = model

    def __repr__(self) -> str:  # never the token
        return f"Shield({self.url!r}, mode={self.mode!r}, threshold={self.threshold}, model={self.model!r})"

    async def scores(self, parts: Sequence[str], timeout_s: float | None = None) -> list[float] | None:
        """One score per part; None when the shield fails. timeout_s: a shorter wait (the request)."""
        if not parts:
            return []
        out: list[float] = []
        try:
            async with bearer_client(self._token, timeout=timeout_s or self.timeout_s) as client:
                for i in range(0, len(parts), BATCH):
                    batch = list(parts[i : i + BATCH])
                    response = await client.post(f"{self.url}/v1/classify", json={"parts": batch})
                    if response.status_code != 200:
                        log.warning("injection model: the shield answered HTTP %s", response.status_code)
                        return None
                    scores = response.json().get("scores")
                    if not _valid(scores, len(batch)):
                        log.warning("injection model: the shield answered an unknown shape")
                        return None
                    out.extend(float(s) for s in scores)
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.warning("injection model: the shield did not answer (%s)", type(e).__name__)
            return None
        return out

    async def health(self) -> ComponentHealth:
        try:
            async with bearer_client(self._token, timeout=min(self.timeout_s, 10.0)) as client:
                response = await client.get(f"{self.url}/v1/health")
            if response.status_code != 200:
                return ComponentHealth(ok=False, detail=f"the shield answered HTTP {response.status_code}")
            data = response.json()
            detail = f"{self.model} ({self.mode}, threshold {self.threshold}, label {data.get('label')})"
            return ComponentHealth(ok=bool(data.get("ok")), detail=detail)
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            return ComponentHealth(ok=False, detail=f"the shield did not answer ({type(e).__name__})")


def _valid(scores: object, n: int) -> bool:
    return (
        isinstance(scores, list)
        and len(scores) == n
        and all(
            isinstance(s, int | float) and not isinstance(s, bool) and math.isfinite(s) and 0 <= s <= 1
            for s in scores
        )
    )
