"""Calls to the triage and knowledge agents (A2A) and to their admin routes (plain HTTP).

Why not Strands' A2AAgent: in strands 1.62 it turns its input into text parts only
(`convert_content_blocks_to_parts` keeps "text" blocks), and the contract asks for a DataPart.
So the call uses the a2a-sdk JSON-RPC transport over the same bearer httpx client. It posts to
the configured URL, not to the URL an Agent Card advertises (in a pod that is often 127.0.0.1).
The answer is the completed task's artifact text, parsed with the contract model."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from a2a.client.errors import A2AClientError, A2AClientHTTPError, A2AClientJSONRPCError, A2AClientTimeoutError
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import (
    DataPart,
    Message,
    MessageSendConfiguration,
    MessageSendParams,
    Part,
    Role,
    Task,
    TaskState,
)
from pydantic import BaseModel, ValidationError

from wattson.shared.auth import bearer_client
from wattson.shared.contracts import (
    FIND_PROCEDURES_KEY,
    LIST_PROCEDURES_KEY,
    TRIAGE_KEY,
    ComponentHealth,
    GrimoireStatus,
    KnowledgeRequest,
    KnowledgeResult,
    ProcedureList,
    ProcedureListRequest,
    TriageRequest,
    TriageResult,
)

RETRY_PAUSE_S = 0.5
ADMIN_TIMEOUT_S = 5.0


class RemoteError(Exception):
    """Another agent did not give a usable answer. The message is safe to show and to record."""


async def send_data[M: BaseModel](
    url: str, token: str, key: str, payload: BaseModel, result_model: type[M], timeout: float, name: str
) -> M:
    """One A2A message/send with a DataPart {key: payload}; the completed task's artifact as M.
    One retry when the connection could not be made (nothing was sent yet)."""
    message = Message(
        role=Role.user,
        message_id=uuid.uuid4().hex,
        parts=[Part(root=DataPart(data={key: payload.model_dump(mode="json")}))],
    )
    params = MessageSendParams(message=message, configuration=MessageSendConfiguration(blocking=True))
    answer: Task | Message | None = None
    for attempt in (1, 2):
        async with bearer_client(token, timeout) as http:
            transport = JsonRpcTransport(http, url=url.rstrip("/") + "/")
            try:
                answer = await transport.send_message(params)
                break
            except A2AClientTimeoutError:
                raise RemoteError(f"the {name} agent did not answer within {timeout:g} s") from None
            except A2AClientJSONRPCError as e:
                raise RemoteError(f"the {name} agent refused the request: {e.error.message}") from None
            except A2AClientHTTPError as e:
                if attempt == 1 and isinstance(e.__cause__, httpx.ConnectError):
                    await asyncio.sleep(RETRY_PAUSE_S)
                    continue
                if isinstance(e.__cause__, httpx.RequestError):
                    raise RemoteError(
                        f"the {name} agent is not reachable ({type(e.__cause__).__name__})"
                    ) from None
                raise RemoteError(f"the {name} agent answered HTTP {e.status_code}") from None
            except A2AClientError as e:
                raise RemoteError(f"the {name} agent gave a broken answer ({type(e).__name__})") from None
    text = _answer_text(answer, name)
    try:
        return result_model.model_validate_json(text)
    except ValidationError:
        raise RemoteError(f"the {name} agent's answer is not a valid {result_model.__name__}") from None


def _answer_text(answer: Task | Message | None, name: str) -> str:
    if isinstance(answer, Message):
        return _join_text(answer.parts)
    if not isinstance(answer, Task):
        raise RemoteError(f"the {name} agent gave no answer")
    if answer.status.state != TaskState.completed:
        raise RemoteError(f"the {name} agent ended in state {answer.status.state.value}")
    return "".join(_join_text(a.parts) for a in answer.artifacts or [])


def _join_text(parts: list[Part]) -> str:
    return "".join(getattr(p.root, "text", "") or "" for p in parts)


async def admin_call(
    base_url: str, token: str, method: str, path: str, body: Any = None, timeout: float = ADMIN_TIMEOUT_S
) -> tuple[int, Any]:
    """A plain JSON call to another agent's admin route. Raises RemoteError when unreachable."""
    async with bearer_client(token, timeout, base_url=base_url) as http:
        try:
            response = await http.request(method, path, json=body)
        except httpx.TimeoutException:
            raise RemoteError(f"no answer from {path} within {timeout:g} s") from None
        except httpx.RequestError as e:
            raise RemoteError(f"cannot reach {path} ({type(e).__name__})") from None
    try:
        data = response.json()
    except ValueError:
        data = {"error": "bad_gateway", "message": f"{path} answered no JSON"}
    return response.status_code, data


@dataclass(frozen=True)
class Remotes:
    """The two agents the orchestrator talks to. Tests replace this with a fake of the same shape."""

    triage_url: str
    triage_token: str
    knowledge_url: str
    knowledge_token: str
    triage_timeout_s: float = 60.0
    knowledge_timeout_s: float = 45.0

    async def triage(self, request: TriageRequest) -> TriageResult:
        return await send_data(
            self.triage_url,
            self.triage_token,
            TRIAGE_KEY,
            request,
            TriageResult,
            self.triage_timeout_s,
            "triage",
        )

    async def list_procedures(self, request: ProcedureListRequest) -> ProcedureList:
        return await send_data(
            self.knowledge_url,
            self.knowledge_token,
            LIST_PROCEDURES_KEY,
            request,
            ProcedureList,
            self.knowledge_timeout_s,
            "knowledge",
        )

    async def find_procedures(self, request: KnowledgeRequest) -> KnowledgeResult:
        return await send_data(
            self.knowledge_url,
            self.knowledge_token,
            FIND_PROCEDURES_KEY,
            request,
            KnowledgeResult,
            self.knowledge_timeout_s,
            "knowledge",
        )

    async def triage_health(self) -> ComponentHealth:
        try:
            status, data = await admin_call(self.triage_url, self.triage_token, "GET", "/v1/health")
            if status != 200:
                return ComponentHealth(ok=False, detail=f"triage /v1/health answered HTTP {status}")
            return ComponentHealth.model_validate(data)
        except RemoteError as e:
            return ComponentHealth(ok=False, detail=f"triage: {e}")
        except ValidationError:
            return ComponentHealth(ok=False, detail="triage /v1/health answered an unknown shape")

    async def grimoire_status(self) -> GrimoireStatus:
        """Raises RemoteError when the knowledge agent cannot say."""
        status, data = await self.grimoire("GET", "/v1/grimoire/status")
        if status != 200:
            raise RemoteError(f"knowledge /v1/grimoire/status answered HTTP {status}")
        try:
            return GrimoireStatus.model_validate(data)
        except ValidationError:
            raise RemoteError("knowledge /v1/grimoire/status answered an unknown shape") from None

    async def grimoire(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return await admin_call(
            self.knowledge_url, self.knowledge_token, method, path, body, timeout=self.knowledge_timeout_s
        )
