from __future__ import annotations

import logging

from typing import Mapping

import httpx

from app.backends.base import AdapterBackend, BackendContext
from app.config import Settings
from app.schemas import ChatCompletionRequest

logger = logging.getLogger("aran-adapter")

_PASSTHROUGH_HEADERS = (
    "x-session-id",
    "x-platform",
    "x-client-platform",
    "x-vision-mode",
    "x-skip-conversation-log",
)


class OpenAIProxyBackend(AdapterBackend):
    supports_stream = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def chat_complete(
        self,
        payload: ChatCompletionRequest,
        headers: Mapping[str, str],
        context: BackendContext,
    ) -> dict:
        if not self.settings.upstream_chat_url:
            raise RuntimeError("ARAN_UPSTREAM_CHAT_URL is not configured")

        body = payload.model_dump(mode="python", exclude_none=True)
        proxy_headers = self._build_headers(headers, context)

        logger.info(
            "Proxy chat request request_id=%s session_id=%s path=%s upstream=%s",
            context.request_id,
            context.session_id,
            context.raw_path,
            self.settings.upstream_chat_url,
        )

        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
            response = await client.post(
                self.settings.upstream_chat_url,
                json=body,
                headers=proxy_headers,
            )

        if response.status_code >= 400:
            text = response.text[:1000]
            raise RuntimeError(
                f"Upstream returned {response.status_code}: {text}"
            )

        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Upstream returned a non-object JSON response")
        return data

    def _build_headers(self, headers: Mapping[str, str], context: BackendContext) -> dict[str, str]:
        proxy_headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Request-Id": context.request_id,
        }

        if self.settings.upstream_api_key:
            proxy_headers["Authorization"] = f"Bearer {self.settings.upstream_api_key}"
        elif self.settings.upstream_forward_auth and headers.get("authorization"):
            proxy_headers["Authorization"] = headers["authorization"]

        for header_name in _PASSTHROUGH_HEADERS:
            value = headers.get(header_name)
            if value:
                proxy_headers[header_name] = value

        for header_name, value in self.settings.upstream_extra_headers.items():
            proxy_headers[header_name] = value

        return proxy_headers
