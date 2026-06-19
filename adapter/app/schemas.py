from __future__ import annotations

import time
import uuid

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = ""
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None
    user: str | None = None
    session_id: str | None = None
    owner_id: str | None = None

    def latest_user_message(self) -> ChatMessage | None:
        for message in reversed(self.messages):
            if message.role == "user":
                return message
        return None

    def latest_user_text(self) -> str:
        message = self.latest_user_message()
        if message is None:
            return ""
        return stringify_content(message.content)


class ChatChoiceMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = "assistant"
    content: str


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatChoiceMessage
    finish_reason: str = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ToolEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str
    name: str | None = None
    call_id: str | None = None
    status: str | None = None
    arguments: Any = None
    output: Any = None
    raw_type: str | None = None


class AttachmentInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = "file"
    attachment_id: str | None = None
    url: str | None = None
    mime_type: str | None = None
    name: str | None = None
    text: str | None = None


class AdapterMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    reasoning: str | None = None
    tool_events: list[ToolEvent] = Field(default_factory=list)
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    raw_events: list[dict[str, Any]] | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)
    adapter_metadata: AdapterMetadata | None = None


def stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                    continue
                if "text" in item and isinstance(item["text"], str):
                    parts.append(item["text"])
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return str(content).strip()


def build_chat_response(
    text: str,
    model: str | None = None,
    *,
    response_id: str | None = None,
    adapter_metadata: AdapterMetadata | None = None,
    message_extra: dict[str, Any] | None = None,
) -> ChatCompletionResponse:
    resolved_id = response_id or f"chatcmpl-{uuid.uuid4().hex}"
    resolved_model = model or "aran-main"
    message_payload: dict[str, Any] = {"content": text}
    if message_extra:
        message_payload.update(message_extra)
    return ChatCompletionResponse(
        id=resolved_id,
        created=int(time.time()),
        model=resolved_model,
        choices=[ChatChoice(message=ChatChoiceMessage(**message_payload))],
        adapter_metadata=adapter_metadata,
    )
