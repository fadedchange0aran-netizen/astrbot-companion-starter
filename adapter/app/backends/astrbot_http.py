from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import time
import uuid
from pathlib import Path

from typing import Any, Mapping
from urllib.parse import quote, urlparse

import httpx

from app.backends.base import AdapterBackend, BackendContext, StreamingChatSession
from app.config import Settings
from app.schemas import (
    AdapterMetadata,
    AttachmentInfo,
    ChatCompletionRequest,
    ToolEvent,
    build_chat_response,
)

logger = logging.getLogger("aran-adapter")
IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,]+)?(?P<base64>;base64)?,(?P<data>.*)$", re.IGNORECASE)
IMAGE_PLACEHOLDER_RE = re.compile(r"\[IMAGE\]([^\s]+)")


class AstrBotHttpBackend(AdapterBackend):
    supports_stream = True
    PLATFORM_SESSION_PREFIX = "pf::"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def chat_complete(
        self,
        payload: ChatCompletionRequest,
        headers: Mapping[str, str],
        context: BackendContext,
    ) -> dict[str, Any]:
        del headers
        if not self.settings.astrbot_target_url:
            raise RuntimeError("ARAN_ASTRBOT_TARGET_URL is not configured")

        target_url = self._resolve_target_url(self.settings.astrbot_target_url)
        file_upload_url = self._resolve_file_upload_url(self.settings.astrbot_target_url)
        capture = self._new_capture()

        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
            body = await self._build_request_body(payload, context, client, file_upload_url)
            proxy_headers = self._build_headers(context, stream_enabled=True)
            logger.info(
                "AstrBot chat request request_id=%s session_id=%s upstream_session_id=%s username=%s target=%s",
                context.request_id,
                context.session_id,
                body["session_id"],
                body["username"],
                target_url,
            )
            async with client.stream(
                "POST",
                target_url,
                json=body,
                headers=proxy_headers,
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", errors="ignore")[:1000]
                    raise RuntimeError(f"AstrBot HTTP API returned {response.status_code}: {text}")

                await self._consume_astrbot_stream(response, capture)

        text, adapter_metadata, message_extra = self._build_final_response_parts(capture)
        return build_chat_response(
            text,
            payload.model,
            adapter_metadata=adapter_metadata,
            message_extra=message_extra,
        ).model_dump(mode="python")

    async def chat_complete_stream(
        self,
        payload: ChatCompletionRequest,
        headers: Mapping[str, str],
        context: BackendContext,
    ) -> StreamingChatSession:
        del headers
        if not self.settings.astrbot_target_url:
            raise RuntimeError("ARAN_ASTRBOT_TARGET_URL is not configured")

        target_url = self._resolve_target_url(self.settings.astrbot_target_url)
        file_upload_url = self._resolve_file_upload_url(self.settings.astrbot_target_url)
        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        resolved_model = payload.model or "aran-main"
        capture = self._new_capture()
        done_event = asyncio.Event()
        finalized_payload: dict[str, Any] = {}
        finalize_error: Exception | None = None

        async def body_iterator():
            nonlocal finalized_payload, finalize_error
            async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                body = await self._build_request_body(payload, context, client, file_upload_url)
                proxy_headers = self._build_headers(context, stream_enabled=True)
                async with client.stream(
                    "POST",
                    target_url,
                    json=body,
                    headers=proxy_headers,
                ) as response:
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="ignore")[:1000]
                        raise RuntimeError(f"AstrBot HTTP API returned {response.status_code}: {text}")

                    yield self._encode_openai_chunk(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": resolved_model,
                            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                        }
                    )

                    async for event in self._iter_sse_events(response):
                        chunks = self._event_to_openai_chunks(
                            event,
                            capture,
                            response_id=response_id,
                            created=created,
                            model=resolved_model,
                        )
                        for chunk in chunks:
                            yield self._encode_openai_chunk(chunk)

            try:
                text, adapter_metadata, message_extra = self._build_final_response_parts(capture)
                finalized_payload = build_chat_response(
                    text,
                    payload.model,
                    response_id=response_id,
                    adapter_metadata=adapter_metadata,
                    message_extra=message_extra,
                ).model_dump(mode="python")
                yield self._encode_openai_chunk(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": resolved_model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
                yield b"data: [DONE]\n\n"
            except Exception as exc:
                finalize_error = exc
                raise
            finally:
                done_event.set()

        async def finalize() -> dict[str, Any]:
            await done_event.wait()
            if finalize_error is not None:
                raise finalize_error
            return finalized_payload

        return StreamingChatSession(body_iterator=body_iterator(), finalize=finalize)

    @staticmethod
    def _resolve_target_url(raw_url: str) -> str:
        parsed = urlparse(raw_url)
        path = parsed.path.rstrip("/")
        if path.endswith("/api/v1/chat"):
            return raw_url
        if not path:
            return raw_url.rstrip("/") + "/api/v1/chat"
        return raw_url

    @staticmethod
    def _resolve_file_upload_url(raw_url: str) -> str:
        parsed = urlparse(raw_url)
        path = parsed.path.rstrip("/")
        if path.endswith("/api/v1/chat"):
            return raw_url[: -len("/api/v1/chat")] + "/api/v1/file"
        if not path:
            return raw_url.rstrip("/") + "/api/v1/file"
        return raw_url.rstrip("/") + "/api/v1/file"

    def _build_headers(self, context: BackendContext, *, stream_enabled: bool) -> dict[str, str]:
        upstream_session_id = self._build_upstream_session_id(context)
        proxy_headers = {
            "Accept": "text/event-stream" if stream_enabled else "application/json",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Request-Id": context.request_id,
            "X-Platform": context.client_platform,
            "X-Client-Platform": context.client_platform,
            "X-Session-Id": upstream_session_id,
            "X-Original-Session-Id": context.session_id,
        }
        if self.settings.astrbot_api_key:
            proxy_headers["Authorization"] = f"Bearer {self.settings.astrbot_api_key}"
            proxy_headers["X-API-Key"] = self.settings.astrbot_api_key
        return proxy_headers

    async def _consume_astrbot_stream(
        self,
        response: httpx.Response,
        capture: dict[str, Any],
    ) -> None:
        async for event in self._iter_sse_events(response):
            self._event_to_openai_chunks(
                event,
                capture,
                response_id=None,
                created=0,
                model=None,
            )

    @staticmethod
    def _new_capture() -> dict[str, Any]:
        return {
            "delta_parts": [],
            "final_text": "",
            "reasoning_parts": [],
            "tool_events": [],
            "tool_calls_by_index": {},
            "attachments": [],
            "event_types": [],
        }

    def _build_final_response_parts(
        self,
        capture: dict[str, Any],
    ) -> tuple[str, AdapterMetadata | None, dict[str, Any] | None]:
        resolved = str(capture["final_text"]).strip() or "".join(capture["delta_parts"]).strip()
        if not resolved:
            raise RuntimeError("AstrBot HTTP API stream ended without reply text")
        resolved, placeholder_attachments = self._replace_image_placeholders(resolved)

        reasoning = "".join(capture["reasoning_parts"]).strip() or None
        tool_events = self._filter_tool_events(capture["tool_events"])
        attachments = list(capture["attachments"])
        for item in placeholder_attachments:
            if item not in attachments:
                attachments.append(item)
        event_types = capture["event_types"]

        adapter_metadata = None
        if reasoning or tool_events or attachments or event_types:
            adapter_metadata = AdapterMetadata(
                reasoning=reasoning,
                tool_events=[ToolEvent(**item) for item in tool_events],
                attachments=[AttachmentInfo(**item) for item in attachments],
                event_types=event_types,
            )

        message_extra: dict[str, Any] = {}
        if reasoning and self.settings.astrbot_expose_reasoning:
            message_extra.update(self._build_reasoning_message_extra(reasoning))
        if self.settings.astrbot_expose_tool_calls:
            finalized_tool_calls = self._finalize_tool_calls(capture.get("tool_calls_by_index"))
            filtered_tool_calls = self._filter_tool_calls(finalized_tool_calls)
            if filtered_tool_calls:
                message_extra["tool_calls"] = filtered_tool_calls


        return resolved, adapter_metadata, message_extra or None

    def _event_to_openai_chunks(
        self,
        event: dict[str, str],
        capture: dict[str, Any],
        *,
        response_id: str | None,
        created: int,
        model: str | None,
    ) -> list[dict[str, Any]]:
        event_name = event.get("event") or "message"
        if event_name not in capture["event_types"]:
            capture["event_types"].append(event_name)

        data_str = str(event.get("data") or "").strip()
        if not data_str or data_str == "[DONE]":
            return []

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            logger.debug("Ignoring non-JSON SSE line from AstrBot: %s", data_str[:200])
            return []

        self._capture_event_payload(event_name, data, capture)
        if not response_id or not model:
            return []

        chunks: list[dict[str, Any]] = []
        for text in self._extract_delta_texts(data, include_complete=False):
            text, placeholder_attachments = self._replace_image_placeholders(text)
            chunks.append(
                self._build_chunk(
                    response_id=response_id,
                    created=created,
                    model=model,
                    delta={"content": text},
                )
            )
            if placeholder_attachments:
                chunks.append(
                    self._build_chunk(
                        response_id=response_id,
                        created=created,
                        model=model,
                        delta={"attachments": placeholder_attachments},
                    )
                )

        reasoning = self._extract_reasoning_text(data)
        if reasoning and self.settings.astrbot_expose_reasoning:
            chunks.append(
                self._build_chunk(
                    response_id=response_id,
                    created=created,
                    model=model,
                    delta=self._build_reasoning_message_extra(reasoning),
                )
            )

        if self.settings.astrbot_expose_tool_calls:
            tool_calls = self._normalize_tool_calls_list(
                self._first_non_empty(
                    self._deep_get(data, "choices", 0, "delta", "tool_calls"),
                    self._deep_get(data, "choices", 0, "message", "tool_calls"),
                    self._deep_get(data, "choices", 0, "tool_calls"),
                    data.get("tool_calls"),
                )
            )
            filtered_tool_calls = self._filter_tool_calls(tool_calls)
            if filtered_tool_calls:
                chunks.append(
                    self._build_chunk(
                        response_id=response_id,
                        created=created,
                        model=model,
                        delta={"tool_calls": filtered_tool_calls},
                    )
                )

        attachments = self._extract_attachments(data)
        if attachments:
            chunks.append(
                self._build_chunk(
                    response_id=response_id,
                    created=created,
                    model=model,
                    delta={"attachments": attachments},
                )
            )


        return chunks

    @staticmethod
    def _build_chunk(
        *,
        response_id: str,
        created: int,
        model: str,
        delta: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }

    @staticmethod
    def _encode_openai_chunk(payload: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    async def _build_request_body(
        self,
        payload: ChatCompletionRequest,
        context: BackendContext,
        client: httpx.AsyncClient,
        file_upload_url: str,
    ) -> dict[str, Any]:
        username = (
            str(payload.owner_id or "").strip()
            or str(payload.user or "").strip()
            or self.settings.astrbot_username
        )
        latest_user_message = payload.latest_user_message()
        latest_user_text = payload.latest_user_text()
        if latest_user_message is None or not latest_user_text:
            raise RuntimeError("Adapter request does not contain a user message")

        metadata = payload.metadata or {}
        upstream_session_id = self._build_upstream_session_id(context)
        body = {
            "message": await self._build_astrbot_message(
                latest_user_message.content,
                client=client,
                file_upload_url=file_upload_url,
                context=context,
            ),
            "username": username,
            "session_id": upstream_session_id,
            "enable_streaming": True,
            "platform": context.client_platform,
            "client_platform": context.client_platform,
            "metadata": {
                "client_platform": context.client_platform,
                "platform": context.client_platform,
                "source": context.client_platform,
                "original_session_id": context.session_id,
                "request_id": context.request_id,
            },
        }
        selected_model = str(payload.model or "").strip()
        bot_aliases = {
            str(self.settings.astrbot_bot_id or "").strip(),
            "aran-main",
        }
        if selected_model and selected_model not in bot_aliases:
            body["selected_model"] = selected_model
        selected_provider = str(metadata.get("astrbot_selected_provider") or "").strip()
        if selected_provider:
            body["selected_provider"] = selected_provider
        config_id = str(metadata.get("astrbot_config_id") or self.settings.astrbot_config_id).strip()
        if config_id:
            body["config_id"] = config_id
        return body

    async def _iter_sse_events(self, response: httpx.Response):
        block_lines: list[str] = []

        async for raw_line in response.aiter_lines():
            line = raw_line if raw_line is not None else ""
            if line == "":
                event = self._parse_sse_block(block_lines)
                block_lines = []
                if event is not None:
                    yield event
                continue
            block_lines.append(line)

        if block_lines:
            event = self._parse_sse_block(block_lines)
            if event is not None:
                yield event

    @staticmethod
    def _parse_sse_block(lines: list[str]) -> dict[str, str] | None:
        if not lines:
            return None

        event_name = "message"
        data_lines: list[str] = []
        for raw_line in lines:
            if not raw_line:
                continue
            if raw_line.startswith(":"):
                continue
            if raw_line.startswith("event:"):
                event_name = raw_line[6:].strip() or "message"
                continue
            if raw_line.startswith("data:"):
                data_lines.append(raw_line[5:].lstrip())
                continue
        if not data_lines:
            return None
        return {"event": event_name, "data": "\n".join(data_lines)}

    def _capture_event_payload(self, event_name: str, data: dict[str, Any], capture: dict[str, Any]) -> None:
        for part in self._extract_delta_texts(data, include_complete=False):
            part, placeholder_attachments = self._replace_image_placeholders(part)
            capture["delta_parts"].append(part)
            for item in placeholder_attachments:
                if item not in capture["attachments"]:
                    capture["attachments"].append(item)

        final_text = self._extract_final_text(data)
        if final_text:
            final_text, placeholder_attachments = self._replace_image_placeholders(final_text)
            capture["final_text"] = final_text
            for item in placeholder_attachments:
                if item not in capture["attachments"]:
                    capture["attachments"].append(item)

        reasoning = self._extract_reasoning_text(data)
        if reasoning:
            capture["reasoning_parts"].append(reasoning)

        tool_events = self._extract_tool_events(event_name, data)
        for item in tool_events:
            capture["tool_events"].append(item)

        tool_calls = self._normalize_tool_calls_list(
            self._first_non_empty(
                self._deep_get(data, "choices", 0, "delta", "tool_calls"),
                self._deep_get(data, "choices", 0, "message", "tool_calls"),
                self._deep_get(data, "choices", 0, "tool_calls"),
                data.get("tool_calls"),
            )
        )
        if tool_calls:
            self._merge_tool_calls(capture["tool_calls_by_index"], tool_calls)

        attachments = self._extract_attachments(data)
        for item in attachments:
            if item not in capture["attachments"]:
                capture["attachments"].append(item)

        for url in self._extract_markdown_image_urls(final_text):
            payload = {
                "type": "image",
                "attachment_id": None,
                "url": url,
                "mime_type": None,
                "name": None,
                "text": None,
            }
            if payload not in capture["attachments"]:
                capture["attachments"].append(payload)


    async def _build_astrbot_message(
        self,
        content: Any,
        *,
        client: httpx.AsyncClient,
        file_upload_url: str,
        context: BackendContext,
    ) -> Any:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            segments: list[dict[str, Any]] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    segments.append({"type": "plain", "text": item.strip()})
                    continue
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        segments.append({"type": "plain", "text": text.strip()})
                    continue
                if item_type in {"image", "file", "record", "video"}:
                    attachment_id = str(item.get("attachment_id") or "").strip()
                    if attachment_id:
                        segments.append({"type": item_type, "attachment_id": attachment_id})
                        continue
                    uploaded_attachment_id = await self._upload_attachment_from_block(
                        item,
                        expected_type=item_type,
                        client=client,
                        file_upload_url=file_upload_url,
                        context=context,
                    )
                    if uploaded_attachment_id:
                        segments.append({"type": item_type, "attachment_id": uploaded_attachment_id})
                    continue
                if item_type in {"image_url", "input_image"}:
                    uploaded_attachment_id = await self._upload_attachment_from_block(
                        item,
                        expected_type="image",
                        client=client,
                        file_upload_url=file_upload_url,
                        context=context,
                    )
                    if uploaded_attachment_id:
                        segments.append({"type": "image", "attachment_id": uploaded_attachment_id})
                    continue
                if item_type in {"input_file"}:
                    uploaded_attachment_id = await self._upload_attachment_from_block(
                        item,
                        expected_type="file",
                        client=client,
                        file_upload_url=file_upload_url,
                        context=context,
                    )
                    if uploaded_attachment_id:
                        segments.append({"type": "file", "attachment_id": uploaded_attachment_id})
                    continue
                if item_type == "reply":
                    message_id = str(item.get("message_id") or "").strip()
                    if message_id:
                        segment: dict[str, Any] = {"type": "reply", "message_id": message_id}
                        selected_text = str(item.get("selected_text") or "").strip()
                        if selected_text:
                            segment["selected_text"] = selected_text
                        segments.append(segment)
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    segments.append({"type": "plain", "text": text.strip()})
            if segments:
                return segments
        return str(content or "").strip()

    async def _upload_attachment_from_block(
        self,
        item: dict[str, Any],
        *,
        expected_type: str,
        client: httpx.AsyncClient,
        file_upload_url: str,
        context: BackendContext,
    ) -> str | None:
        inline_payload = self._extract_inline_media_payload(item, expected_type=expected_type)
        if inline_payload is not None:
            binary, resolved_filename, mime_type = inline_payload
            return await self._upload_attachment_bytes(
                binary,
                filename=resolved_filename,
                mime_type=mime_type,
                client=client,
                file_upload_url=file_upload_url,
                context=context,
            )

        url, filename, mime_type_hint = self._extract_input_media_url_and_name(
            item,
            expected_type=expected_type,
        )
        if url:
            binary, resolved_filename, mime_type = await self._download_or_decode_input_media(
                url,
                suggested_name=filename,
                client=client,
                mime_type_hint=mime_type_hint,
            )
            if binary is None:
                return None
            return await self._upload_attachment_bytes(
                binary,
                filename=resolved_filename,
                mime_type=mime_type,
                client=client,
                file_upload_url=file_upload_url,
                context=context,
            )
        return None

    async def _download_or_decode_input_media(
        self,
        raw_url: str,
        *,
        suggested_name: str | None,
        client: httpx.AsyncClient,
        mime_type_hint: str | None = None,
    ) -> tuple[bytes | None, str, str | None]:
        data_match = DATA_URL_RE.match(raw_url)
        if data_match:
            mime_type = (data_match.group("mime") or "").strip() or mime_type_hint or None
            encoded_data = data_match.group("data") or ""
            if data_match.group("base64"):
                binary = base64.b64decode(encoded_data)
            else:
                binary = encoded_data.encode("utf-8")
            filename = suggested_name or self._guess_filename(mime_type=mime_type, source_name="upload")
            return binary, filename, mime_type

        response = await client.get(raw_url, follow_redirects=True)
        if response.status_code >= 400:
            raise RuntimeError(f"Failed to download attachment source: {response.status_code}")
        mime_type = response.headers.get("Content-Type") or mime_type_hint
        filename = suggested_name or self._guess_filename(
            mime_type=mime_type,
            source_name=self._filename_from_url(raw_url) or "upload",
        )
        return response.content, filename, mime_type

    async def _upload_attachment_bytes(
        self,
        binary: bytes,
        *,
        filename: str,
        mime_type: str | None,
        client: httpx.AsyncClient,
        file_upload_url: str,
        context: BackendContext,
    ) -> str | None:
        files = {
            "file": (
                filename,
                binary,
                mime_type or "application/octet-stream",
            )
        }
        response = await client.post(
            file_upload_url,
            files=files,
            headers=self._build_upload_headers(context),
        )
        if response.status_code >= 400:
            text = response.text[:500]
            raise RuntimeError(f"AstrBot file upload failed {response.status_code}: {text}")

        payload = response.json()
        attachment_id = self._deep_get(payload, "data", "attachment_id")
        if isinstance(attachment_id, str) and attachment_id.strip():
            return attachment_id.strip()
        raise RuntimeError("AstrBot file upload response did not include attachment_id")

    def _build_upload_headers(self, context: BackendContext) -> dict[str, str]:
        upstream_session_id = self._build_upstream_session_id(context)
        headers = {
            "Accept": "application/json",
            "X-Request-Id": context.request_id,
            "X-Platform": context.client_platform,
            "X-Client-Platform": context.client_platform,
            "X-Session-Id": upstream_session_id,
            "X-Original-Session-Id": context.session_id,
        }
        if self.settings.astrbot_api_key:
            headers["Authorization"] = f"Bearer {self.settings.astrbot_api_key}"
            headers["X-API-Key"] = self.settings.astrbot_api_key
        return headers

    def _build_upstream_session_id(self, context: BackendContext) -> str:
        session_id = str(context.session_id or "").strip()
        platform = self._normalize_platform_name(context.client_platform)
        if not session_id or platform == "unknown":
            return session_id
        parsed_platform, _ = self._parse_platform_session_id(session_id)
        if parsed_platform:
            return session_id
        return f"{self.PLATFORM_SESSION_PREFIX}{platform}::{session_id}"

    @classmethod
    def _parse_platform_session_id(cls, session_id: str) -> tuple[str | None, str]:
        raw_value = str(session_id or "").strip()
        if not raw_value.startswith(cls.PLATFORM_SESSION_PREFIX):
            return None, raw_value
        remainder = raw_value[len(cls.PLATFORM_SESSION_PREFIX) :]
        platform, separator, original_session_id = remainder.partition("::")
        if not separator or not platform.strip() or not original_session_id.strip():
            return None, raw_value
        return platform.strip(), original_session_id.strip()

    @staticmethod
    def _normalize_platform_name(raw_value: str) -> str:
        normalized = re.sub(r"[^a-z0-9_.-]+", "-", str(raw_value or "").strip().lower()).strip("-")
        return normalized or "unknown"

    @staticmethod
    def _extract_input_media_url_and_name(
        item: dict[str, Any],
        *,
        expected_type: str,
    ) -> tuple[str | None, str | None, str | None]:
        filename_hint = AstrBotHttpBackend._extract_filename_hint(item)
        mime_type_hint = AstrBotHttpBackend._extract_mime_type_hint(item)
        possible_sources: list[Any] = [
            item,
            item.get("file"),
            item.get("image_url"),
            item.get("input_image"),
            item.get("input_file"),
            item.get("file_data"),
            item.get("image"),
        ]
        if expected_type == "image":
            possible_sources.insert(0, item.get("image_url"))
        if expected_type == "file":
            possible_sources.insert(0, item.get("file"))

        for candidate in possible_sources:
            normalized_url = AstrBotHttpBackend._extract_media_url_from_candidate(candidate)
            if normalized_url:
                return normalized_url, filename_hint, mime_type_hint
        return None, filename_hint, mime_type_hint

    @classmethod
    def _extract_inline_media_payload(
        cls,
        item: dict[str, Any],
        *,
        expected_type: str,
    ) -> tuple[bytes, str, str | None] | None:
        filename = cls._extract_filename_hint(item) or "upload"
        mime_type = cls._extract_mime_type_hint(item) or cls._default_media_mime_type(expected_type)
        for candidate in cls._iter_media_source_candidates(item, expected_type=expected_type):
            if not isinstance(candidate, dict):
                continue
            for key in ("b64_json", "base64", "data_base64", "image_base64", "file_base64"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    binary = base64.b64decode(value.strip())
                    resolved_filename = cls._guess_filename(
                        mime_type=mime_type,
                        source_name=filename,
                    )
                    return binary, resolved_filename, mime_type
            for key in ("data_url",):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    binary, resolved_filename, resolved_mime_type = cls._decode_data_url_value(
                        value.strip(),
                        suggested_name=filename,
                        mime_type_hint=mime_type,
                    )
                    return binary, resolved_filename, resolved_mime_type
            for key in ("data", "content"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip().startswith("data:"):
                    binary, resolved_filename, resolved_mime_type = cls._decode_data_url_value(
                        value.strip(),
                        suggested_name=filename,
                        mime_type_hint=mime_type,
                    )
                    return binary, resolved_filename, resolved_mime_type
        return None

    @classmethod
    def _iter_media_source_candidates(cls, item: dict[str, Any], *, expected_type: str) -> list[Any]:
        candidates: list[Any] = [
            item,
            item.get("file"),
            item.get("image_url"),
            item.get("input_image"),
            item.get("input_file"),
            item.get("file_data"),
            item.get("image"),
        ]
        if expected_type == "image":
            candidates.insert(0, item.get("image_url"))
        if expected_type == "file":
            candidates.insert(0, item.get("file"))
        return [candidate for candidate in candidates if candidate is not None]

    @staticmethod
    def _extract_media_url_from_candidate(candidate: Any) -> str | None:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if not isinstance(candidate, dict):
            return None
        for key in ("url", "file_url", "image_url", "download_url", "content_url", "src", "href"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("url") or value.get("src") or value.get("href")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return None

    @staticmethod
    def _extract_filename_hint(item: dict[str, Any]) -> str | None:
        for candidate in (
            item,
            item.get("file"),
            item.get("image_url"),
            item.get("input_image"),
            item.get("input_file"),
            item.get("file_data"),
            item.get("image"),
        ):
            if not isinstance(candidate, dict):
                continue
            for key in ("filename", "file_name", "name"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            detail = candidate.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
        return None

    @staticmethod
    def _extract_mime_type_hint(item: dict[str, Any]) -> str | None:
        for candidate in (
            item,
            item.get("file"),
            item.get("image_url"),
            item.get("input_image"),
            item.get("input_file"),
            item.get("file_data"),
            item.get("image"),
        ):
            if not isinstance(candidate, dict):
                continue
            for key in ("mime_type", "content_type", "mime"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @classmethod
    def _decode_data_url_value(
        cls,
        raw_value: str,
        *,
        suggested_name: str,
        mime_type_hint: str | None,
    ) -> tuple[bytes, str, str | None]:
        data_match = DATA_URL_RE.match(raw_value)
        if not data_match:
            raise RuntimeError("Unsupported inline media format")
        mime_type = (data_match.group("mime") or "").strip() or mime_type_hint or None
        encoded_data = data_match.group("data") or ""
        if data_match.group("base64"):
            binary = base64.b64decode(encoded_data)
        else:
            binary = encoded_data.encode("utf-8")
        filename = cls._guess_filename(
            mime_type=mime_type,
            source_name=suggested_name,
        )
        return binary, filename, mime_type

    @staticmethod
    def _default_media_mime_type(expected_type: str) -> str:
        if expected_type == "image":
            return "image/png"
        return "application/octet-stream"

    @staticmethod
    def _filename_from_url(raw_url: str) -> str | None:
        parsed = urlparse(raw_url)
        name = parsed.path.rsplit("/", 1)[-1].strip()
        return name or None

    @staticmethod
    def _guess_filename(*, mime_type: str | None, source_name: str) -> str:
        source = source_name.strip() or "upload"
        if "." in source.rsplit("/", 1)[-1]:
            return source
        extension = mimetypes.guess_extension((mime_type or "").split(";", 1)[0].strip()) or ""
        return f"{source}{extension}"

    @staticmethod
    def _extract_text_from_content_block(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").lower()
                if item_type in ("text", "output_text", "plain"):
                    text_value = item.get("text") or item.get("content")
                    if isinstance(text_value, str):
                        parts.append(text_value)
                    elif isinstance(text_value, dict):
                        nested = text_value.get("value") or text_value.get("content")
                        if isinstance(nested, str):
                            parts.append(nested)
                elif item_type in ("reasoning", "thinking"):
                    reasoning_value = item.get("text") or item.get("content") or item.get("reasoning")
                    if isinstance(reasoning_value, str):
                        parts.append(reasoning_value)
                else:
                    text_value = item.get("text") or item.get("content")
                    if isinstance(text_value, str):
                        parts.append(text_value)
                    elif isinstance(text_value, dict):
                        nested = text_value.get("value") or text_value.get("content")
                        if isinstance(nested, str):
                            parts.append(nested)
            return "".join(parts)
        if isinstance(content, dict):
            for key in ("text", "content", "value", "output_text"):
                value = content.get(key)
                if isinstance(value, str):
                    return value
        return ""

    def _extract_delta_texts(
        self,
        data: dict[str, Any],
        *,
        include_complete: bool = True,
        user_visible_only: bool = True,
    ) -> list[str]:
        parts: list[str] = []
        data_type = str(data.get("type") or "").lower()
        candidates = [
            self._deep_get(data, "choices", 0, "delta", "content"),
            self._deep_get(data, "delta", "content"),
            data.get("data"),
            data.get("content"),
            data.get("text"),
            data.get("output_text"),
            self._deep_get(data, "choices", 0, "message", "content"),
        ]
        for candidate in candidates:
            text = self._extract_text_from_content_block(candidate).strip()
            if text:
                parts.append(text)

        if data_type in {"delta", "chunk", "token", "text", "message", "output_text"} and not parts:
            text = self._extract_text_from_content_block(data).strip()
            if text:
                parts.append(text)

        if data_type == "complete" and not include_complete:
            return []
        if not user_visible_only:
            return parts
        return [text for text in parts if self._is_user_visible_text(text)]

    def _extract_reasoning_text(self, data: dict[str, Any]) -> str:
        candidates = [
            self._deep_get(data, "choices", 0, "delta", "reasoning_content"),
            self._deep_get(data, "choices", 0, "delta", "reasoning"),
            self._deep_get(data, "choices", 0, "message", "reasoning_content"),
            self._deep_get(data, "choices", 0, "message", "reasoning"),
            self._deep_get(data, "choices", 0, "reasoning_content"),
            self._deep_get(data, "choices", 0, "reasoning"),
            data.get("reasoning_content"),
            data.get("reasoning"),
            data.get("thinking"),
        ]
        for candidate in candidates:
            text = self._extract_text_from_content_block(candidate).strip()
            if text:
                return text
        return ""

    def _extract_final_text(self, data: dict[str, Any]) -> str:
        for candidate in (
            self._deep_get(data, "choices", 0, "message", "content"),
            data.get("data"),
            data.get("message"),
            data.get("content"),
            data.get("text"),
            data.get("output_text"),
        ):
            text = self._extract_text_from_content_block(candidate).strip()
            if text and self._is_user_visible_text(text):
                return text
        return ""

    @staticmethod
    def _looks_like_tool_call_payload(text: str) -> bool:
        stripped = text.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return False
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        call_id = str(payload.get("id") or "").strip()
        if call_id.startswith("call_") and any(key in payload for key in ("name", "args", "result")):
            return True
        return False

    @classmethod
    def _is_user_visible_text(cls, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        if stripped.startswith("[RECORD]"):
            return False
        if cls._looks_like_tool_call_payload(stripped):
            return False
        return True

    def _extract_tool_events(self, event_name: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        normalized_calls = self._normalize_tool_calls_list(
            self._first_non_empty(
                self._deep_get(data, "choices", 0, "delta", "tool_calls"),
                self._deep_get(data, "choices", 0, "message", "tool_calls"),
                self._deep_get(data, "choices", 0, "tool_calls"),
                data.get("tool_calls"),
            )
        )
        if normalized_calls:
            for tool_call in normalized_calls:
                function_info = tool_call.get("function") if isinstance(tool_call, dict) else None
                events.append(
                    {
                        "kind": "tool_call",
                        "name": function_info.get("name") if isinstance(function_info, dict) else None,
                        "call_id": str(tool_call.get("id") or "") or None,
                        "status": self._infer_tool_status(event_name, data, default="called"),
                        "arguments": function_info.get("arguments") if isinstance(function_info, dict) else None,
                        "raw_type": str(data.get("type") or event_name or ""),
                    }
                )

        tool_result_payloads = []
        for key in ("tool_result", "tool_results", "result", "results"):
            value = data.get(key)
            if value:
                tool_result_payloads.append(value)
        if "tool" in event_name.lower() and any(token in event_name.lower() for token in ("result", "status", "done", "finish")):
            tool_result_payloads.append(data)

        for payload in tool_result_payloads:
            if isinstance(payload, list):
                iterable = payload
            else:
                iterable = [payload]
            for item in iterable:
                if not isinstance(item, dict):
                    continue
                events.append(
                    {
                        "kind": "tool_result",
                        "name": str(item.get("name") or item.get("tool_name") or "").strip() or None,
                        "call_id": str(item.get("id") or item.get("tool_call_id") or "").strip() or None,
                        "status": self._infer_tool_status(event_name, item, default="completed"),
                        "output": item.get("output") or item.get("content") or item.get("result") or item,
                        "raw_type": str(item.get("type") or event_name or ""),
                    }
                )
        return events

    def _extract_attachments(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []

        def add_attachment(item: dict[str, Any], default_type: str = "file") -> None:
            attachment_type = str(item.get("type") or default_type or "file").strip() or "file"
            payload = {
                "type": attachment_type,
                "attachment_id": str(item.get("attachment_id") or "").strip() or None,
                "url": self._extract_attachment_url(item),
                "mime_type": str(item.get("mime_type") or item.get("content_type") or "").strip() or None,
                "name": str(item.get("name") or item.get("filename") or "").strip() or None,
                "text": self._extract_text_from_content_block(item.get("text") or item.get("content")).strip() or None,
            }
            if payload not in attachments:
                attachments.append(payload)

        possible_lists = [
            data.get("attachments"),
            data.get("files"),
            data.get("images"),
            self._deep_get(data, "message", "attachments"),
            self._deep_get(data, "choices", 0, "message", "attachments"),
        ]
        for items in possible_lists:
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        add_attachment(item, default_type=str(item.get("type") or "file"))

        for candidate in (
            data.get("message"),
            self._deep_get(data, "choices", 0, "message"),
            data,
        ):
            attachments.extend(self._extract_attachments_from_content(candidate))

        return attachments

    def _extract_attachments_from_content(self, candidate: Any) -> list[dict[str, Any]]:
        if not isinstance(candidate, dict):
            return []
        content = candidate.get("content")
        if not isinstance(content, list):
            return []

        attachments: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").lower()
            if block_type in {"image", "file", "record", "video"}:
                attachments.append(
                    {
                        "type": block_type,
                        "attachment_id": str(block.get("attachment_id") or "").strip() or None,
                        "url": self._extract_attachment_url(block),
                        "mime_type": str(block.get("mime_type") or "").strip() or None,
                        "name": str(block.get("name") or block.get("filename") or "").strip() or None,
                        "text": self._extract_text_from_content_block(block.get("text") or block.get("content")).strip() or None,
                    }
                )
            elif block_type in {"image_url", "input_image"}:
                attachments.append(
                    {
                        "type": "image",
                        "attachment_id": str(block.get("attachment_id") or "").strip() or None,
                        "url": self._extract_attachment_url(block),
                        "mime_type": str(block.get("mime_type") or "").strip() or None,
                        "name": None,
                        "text": None,
                    }
                )
        return attachments

    @staticmethod
    def _extract_markdown_image_urls(text: str) -> list[str]:
        if not text:
            return []
        return [match.strip() for match in IMAGE_MARKDOWN_RE.findall(text) if match.strip()]

    def _replace_image_placeholders(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        if not text or "[IMAGE]" not in text:
            return text, []

        attachments: list[dict[str, Any]] = []

        def repl(match: re.Match[str]) -> str:
            filename = Path(match.group(1)).name
            attachment = self._build_public_image_attachment(filename)
            if not attachment:
                return match.group(0)
            if attachment not in attachments:
                attachments.append(attachment)
            return f"![{filename}]({attachment['url']})"

        return IMAGE_PLACEHOLDER_RE.sub(repl, text), attachments

    def _build_public_image_attachment(self, filename: str) -> dict[str, Any] | None:
        public_base = str(self.settings.astrbot_public_base_url or "").strip().rstrip("/")
        if not public_base or not filename:
            return None

        file_path = (self.settings.astrbot_attachments_dir / Path(filename).name).resolve()
        try:
            file_path.relative_to(self.settings.astrbot_attachments_dir.resolve())
        except ValueError:
            return None
        if not file_path.exists() or not file_path.is_file():
            return None

        mime_type, _ = mimetypes.guess_type(file_path.name)
        return {
            "type": "image",
            "attachment_id": None,
            "url": f"{public_base}/attachments/{quote(file_path.name)}",
            "mime_type": mime_type or "application/octet-stream",
            "name": file_path.name,
            "text": None,
        }

    @staticmethod
    def _extract_attachment_url(item: dict[str, Any]) -> str | None:
        for key in ("url", "image_url", "file_url", "download_url"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("url")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return None

    @staticmethod
    def _normalize_tool_calls_list(tool_calls: Any) -> list[dict[str, Any]] | None:
        if not isinstance(tool_calls, list):
            return None

        normalized_calls: list[dict[str, Any]] = []
        for tc_idx, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            normalized_call = dict(tool_call)
            if normalized_call.get("index") is None:
                normalized_call["index"] = tc_idx
            fn = normalized_call.get("function")
            if isinstance(fn, dict):
                normalized_call["function"] = {
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments", ""),
                }
            normalized_calls.append(normalized_call)
        return normalized_calls or None

    @staticmethod
    def _merge_tool_calls(accumulated: dict[Any, dict[str, Any]], tool_calls: list[dict[str, Any]]) -> None:
        for tc in tool_calls:
            idx = tc.get("index", 0)
            if idx not in accumulated:
                accumulated[idx] = {
                    "index": idx,
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            entry = accumulated[idx]
            if tc.get("id"):
                entry["id"] = tc["id"]
            fn = tc.get("function")
            if isinstance(fn, dict):
                if fn.get("name"):
                    entry["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    entry["function"]["arguments"] += str(fn["arguments"])

    @staticmethod
    def _finalize_tool_calls(tool_calls_by_index: Any) -> list[dict[str, Any]] | None:
        if not isinstance(tool_calls_by_index, dict) or not tool_calls_by_index:
            return None
        return [
            tool_calls_by_index[key]
            for key in sorted(tool_calls_by_index.keys(), key=lambda item: int(item) if str(item).isdigit() else str(item))
        ]

    def _build_reasoning_message_extra(self, reasoning: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"reasoning_content": reasoning}
        if self.settings.astrbot_emit_reasoning_alias:
            payload["reasoning"] = reasoning
        return payload

    def _filter_tool_calls(self, tool_calls: Any) -> list[dict[str, Any]] | None:
        if not isinstance(tool_calls, list):
            return None

        filtered_calls: list[dict[str, Any]] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            entry = dict(tool_call)
            fn = entry.get("function")
            if isinstance(fn, dict):
                filtered_function = dict(fn)
                if not self.settings.astrbot_expose_tool_call_arguments:
                    filtered_function.pop("arguments", None)
                entry["function"] = filtered_function
            filtered_calls.append(entry)
        return filtered_calls or None

    def _filter_tool_events(self, tool_events: Any) -> list[dict[str, Any]]:
        if not isinstance(tool_events, list):
            return []

        filtered_events: list[dict[str, Any]] = []
        for event in tool_events:
            if not isinstance(event, dict):
                continue
            item = dict(event)
            if not self.settings.astrbot_expose_tool_call_arguments:
                item.pop("arguments", None)
            filtered_events.append(item)
        return filtered_events

    @staticmethod
    def _infer_tool_status(event_name: str, payload: dict[str, Any], *, default: str) -> str:
        for key in ("status", "state", "phase"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        lowered = str(event_name or "").lower()
        if "start" in lowered or "call" in lowered:
            return "called"
        if "result" in lowered or "done" in lowered or "finish" in lowered:
            return "completed"
        if "error" in lowered or "fail" in lowered:
            return "error"
        return default

    @staticmethod
    def _deep_get(value: Any, *path: Any) -> Any:
        current = value
        for part in path:
            if isinstance(part, int):
                if not isinstance(current, list) or part >= len(current):
                    return None
                current = current[part]
                continue
            if not isinstance(current, dict):
                return None
            current = current.get(part)
            if current is None:
                return None
        return current

    @staticmethod
    def _first_non_empty(*values: Any) -> Any:
        for value in values:
            if value is None:
                continue
            if isinstance(value, (str, list, dict)) and not value:
                continue
            return value
        return None
