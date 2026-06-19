from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from app.schemas import ChatCompletionRequest


@dataclass(frozen=True)
class BackendContext:
    request_id: str
    owner_id: str
    session_id: str
    client_platform: str
    client_ip: str
    raw_path: str


@dataclass(frozen=True)
class StreamingChatSession:
    body_iterator: AsyncIterator[bytes]
    finalize: Callable[[], Awaitable[dict[str, Any]]]


class AdapterBackend(ABC):
    supports_stream: bool = False

    @abstractmethod
    async def chat_complete(
        self,
        payload: ChatCompletionRequest,
        headers: Mapping[str, str],
        context: BackendContext,
    ) -> dict:
        raise NotImplementedError

    async def chat_complete_stream(
        self,
        payload: ChatCompletionRequest,
        headers: Mapping[str, str],
        context: BackendContext,
    ) -> StreamingChatSession:
        raise NotImplementedError(f"Streaming is not implemented for {self.__class__.__name__}")
