from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backends.base import BackendContext
from app.config import Settings
from app.schemas import ChatCompletionRequest


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_part(raw: str, fallback: str = "unknown") -> str:
    text = (raw or "").strip()
    if not text:
        text = fallback
    safe_chars = []
    for char in text:
        if char.isalnum() or char in {"-", "_", ".", "@"}:
            safe_chars.append(char)
        else:
            safe_chars.append("_")
    compact = "".join(safe_chars).strip("._")
    return compact or fallback


_SYSTEM_REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL)
_DATE_KEY_RE = re.compile(r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})")
_DATE_COMPACT_RE = re.compile(r"(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})")
_QQ_HEADER_RE = re.compile(
    r"^\[发送时间:\s*(?P<sent_at>[^|\]]+)(?:\s*\|.*?)*\s*\| 平台:\s*(?P<platform>[^\]]+)\]\s*\n?",
    re.DOTALL,
)


@dataclass(frozen=True)
class TranscriptArchive:
    settings: Settings

    async def append_chat_completion(
        self,
        *,
        payload: ChatCompletionRequest,
        context: BackendContext,
        response_payload: dict[str, Any],
    ) -> Path | None:
        if not self.settings.transcript_enabled:
            return None

        record = {
            "timestamp": _now_iso(),
            "request_id": context.request_id,
            "owner_id": context.owner_id,
            "session_id": context.session_id,
            "platform": context.client_platform,
            "client_ip": context.client_ip,
            "path": context.raw_path,
            "model": payload.model,
            "request": payload.model_dump(mode="python", exclude_none=True),
            "response": response_payload,
        }
        target_path = self._session_file_path(context)
        await asyncio.to_thread(self._append_jsonl, target_path, record)
        return target_path

    async def create_backup_bundle(self, label: str | None = None) -> dict[str, Any]:
        backup_root = self.settings.manual_backup_root
        backup_root.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = _safe_part(label or "", fallback="manual")
        archive_name = f"aran-backup-{suffix}-{timestamp}.tar.gz"
        archive_path = backup_root / archive_name

        include_paths: list[tuple[Path, str]] = []
        if self.settings.transcript_enabled and self.settings.transcript_root.exists():
            include_paths.append((self.settings.transcript_root, "transcripts"))
        qq_chat_export: dict[str, Any] | None = None
        if self.settings.qq_chat_backup_sessions:
            qq_chat_export = await asyncio.to_thread(self._export_qq_chat_snapshots)
            if qq_chat_export["session_count"] > 0:
                include_paths.append((self.settings.qq_chat_backup_root, "qq_chat_backups"))
        for extra_path in self.settings.manual_backup_extra_paths:
            if extra_path.exists():
                include_paths.append((extra_path, f"extra/{_safe_part(extra_path.name, 'path')}"))

        if not include_paths:
            raise RuntimeError("No backup source paths exist yet")

        await asyncio.to_thread(self._write_backup_tarball, archive_path, include_paths)
        return {
            "backup_path": str(archive_path),
            "included_paths": [str(path) for path, _ in include_paths],
            "created_at": _now_iso(),
            "qq_chat_export": qq_chat_export,
        }

    def _session_file_path(self, context: BackendContext) -> Path:
        owner_dir = self.settings.transcript_root / _safe_part(context.owner_id, "bia")
        session_name = _safe_part(context.session_id, "session")
        return owner_dir / f"{session_name}.jsonl"

    @staticmethod
    def _append_jsonl(target_path: Path, payload: dict[str, Any]) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _write_backup_tarball(archive_path: Path, include_paths: list[tuple[Path, str]]) -> None:
        with tarfile.open(archive_path, "w:gz") as tar:
            for source_path, arcname in include_paths:
                tar.add(str(source_path), arcname=arcname)

    def _export_qq_chat_snapshots(self) -> dict[str, Any]:
        db_path = self.settings.astrbot_data_db_path
        if not db_path.exists():
            raise RuntimeError(f"AstrBot data db not found: {db_path}")

        snapshot_root = self.settings.qq_chat_backup_root
        sessions_dir = snapshot_root / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        requested_targets = {
            item.strip()
            for item in self.settings.qq_chat_backup_sessions
            if item.strip()
        }
        if not requested_targets:
            return {
                "exported_at": _now_iso(),
                "root": str(snapshot_root),
                "session_count": 0,
                "items": [],
            }

        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT conversation_id, user_id, created_at, updated_at, title, content
                FROM conversations
                WHERE user_id LIKE 'napcat_qq:FriendMessage:%'
                ORDER BY updated_at DESC
                """
            ).fetchall()

        exported_at = _now_iso()
        exported_items: list[dict[str, Any]] = []
        seen_targets: set[str] = set()

        for row in rows:
            target_id = self._match_qq_backup_target(str(row["user_id"]), requested_targets)
            if target_id is None or target_id in seen_targets:
                continue

            snapshot_payload = self._build_qq_snapshot_payload(
                row=row,
                exported_at=exported_at,
                requested_target=target_id,
            )
            target_path = sessions_dir / f"{_safe_part(target_id, 'session')}.json"
            target_path.write_text(
                json.dumps(snapshot_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            day_slice_items = self._write_qq_daily_slices(
                snapshot_root=snapshot_root,
                snapshot_payload=snapshot_payload,
            )

            exported_items.append(
                {
                    "requested_session": target_id,
                    "platform_session_id": snapshot_payload["platform_session_id"],
                    "conversation_id": snapshot_payload["conversation_id"],
                    "snapshot_path": str(target_path),
                    "message_count": snapshot_payload["stats"]["exported_messages"],
                    "skipped_count": snapshot_payload["stats"]["skipped_messages"],
                    "day_count": len(day_slice_items),
                    "days": [
                        {
                            "day": item["day"],
                            "message_count": item["message_count"],
                        }
                        for item in day_slice_items
                    ],
                    "updated_at": snapshot_payload["updated_at"],
                    "last_message_preview": snapshot_payload["stats"]["last_message_preview"],
                }
            )
            seen_targets.add(target_id)

        manifest = {
            "exported_at": exported_at,
            "root": str(snapshot_root),
            "source_db_path": str(db_path),
            "requested_sessions": list(self.settings.qq_chat_backup_sessions),
            "session_count": len(exported_items),
            "missing_sessions": sorted(requested_targets - seen_targets),
            "items": exported_items,
        }
        (snapshot_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest

    @staticmethod
    def _match_qq_backup_target(platform_session_id: str, requested_targets: set[str]) -> str | None:
        normalized_value = platform_session_id.strip()
        raw_session_id = normalized_value.rsplit(":", 1)[-1]
        if normalized_value in requested_targets:
            return normalized_value
        if raw_session_id in requested_targets:
            return raw_session_id
        return None

    def _build_qq_snapshot_payload(
        self,
        *,
        row: sqlite3.Row,
        exported_at: str,
        requested_target: str,
    ) -> dict[str, Any]:
        try:
            raw_messages = json.loads(str(row["content"] or "[]"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Conversation content is not valid JSON for {row['user_id']}"
            ) from exc

        if not isinstance(raw_messages, list):
            raise RuntimeError(f"Conversation content must be a JSON array for {row['user_id']}")

        cleaned_messages: list[dict[str, Any]] = []
        skipped_messages = 0
        fallback_day = self._extract_day_key(str(row["updated_at"] or row["created_at"] or "")) or exported_at[:10]
        rolling_day = fallback_day

        for index, item in enumerate(raw_messages, start=1):
            cleaned_item = self._clean_conversation_message(index=index, payload=item)
            if cleaned_item is None:
                skipped_messages += 1
                continue
            explicit_day = self._extract_day_key(str(cleaned_item.get("sent_at") or ""))
            if explicit_day:
                rolling_day = explicit_day
            cleaned_item["day"] = rolling_day
            cleaned_messages.append(cleaned_item)

        last_preview = ""
        if cleaned_messages:
            last_preview = cleaned_messages[-1]["text"][:120]

        platform_session_id = str(row["user_id"])
        return {
            "exported_at": exported_at,
            "requested_session": requested_target,
            "platform_session_id": platform_session_id,
            "session_id": platform_session_id.rsplit(":", 1)[-1],
            "conversation_id": row["conversation_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "source_db_path": str(self.settings.astrbot_data_db_path),
            "stats": {
                "raw_message_count": len(raw_messages),
                "exported_messages": len(cleaned_messages),
                "skipped_messages": skipped_messages,
                "last_message_preview": last_preview,
                "day_count": len({item["day"] for item in cleaned_messages}),
            },
            "days": self._summarize_days(cleaned_messages),
            "messages": cleaned_messages,
        }

    @staticmethod
    def _extract_day_key(raw_value: str) -> str | None:
        text = (raw_value or "").strip()
        if not text:
            return None
        match = _DATE_KEY_RE.search(text)
        if match is None:
            match = _DATE_COMPACT_RE.search(text)
        if match is None:
            return None
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"

    @staticmethod
    def _summarize_days(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for item in messages:
            day = str(item.get("day") or "").strip()
            if not day:
                continue
            counts[day] = counts.get(day, 0) + 1
        return [
            {
                "day": day,
                "message_count": counts[day],
            }
            for day in sorted(counts)
        ]

    def _write_qq_daily_slices(
        self,
        *,
        snapshot_root: Path,
        snapshot_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        session_part = _safe_part(str(snapshot_payload.get("requested_session") or snapshot_payload.get("session_id") or "session"), "session")
        day_dir = snapshot_root / "days" / session_part
        day_dir.mkdir(parents=True, exist_ok=True)

        for existing in day_dir.glob("*.jsonl"):
            existing.unlink()

        grouped_messages: dict[str, list[dict[str, Any]]] = {}
        for item in snapshot_payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            day = str(item.get("day") or "").strip()
            if not day:
                continue
            grouped_messages.setdefault(day, []).append(item)

        day_items: list[dict[str, Any]] = []
        for day in sorted(grouped_messages):
            day_path = day_dir / f"{day}.jsonl"
            with day_path.open("w", encoding="utf-8") as handle:
                for message in grouped_messages[day]:
                    handle.write(json.dumps(message, ensure_ascii=False) + "\n")
            day_items.append(
                {
                    "day": day,
                    "message_count": len(grouped_messages[day]),
                    "slice_path": str(day_path),
                }
            )
        return day_items

    def _clean_conversation_message(
        self,
        *,
        index: int,
        payload: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None

        role = str(payload.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            return None

        text = self._extract_visible_text(payload.get("content"))
        text = _SYSTEM_REMINDER_RE.sub("", text).strip()
        if not text:
            return None

        sent_at: str | None = None
        platform_hint: str | None = None
        header_match = _QQ_HEADER_RE.match(text)
        if header_match is not None:
            sent_at = header_match.group("sent_at").strip()
            platform_hint = header_match.group("platform").strip()
            text = text[header_match.end() :].strip()

        if not text:
            return None

        return {
            "index": index,
            "role": role,
            "sent_at": sent_at,
            "platform_hint": platform_hint,
            "text": text,
        }

    def _extract_visible_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [self._extract_visible_text(item) for item in content]
            return "\n".join(part for part in parts if part.strip()).strip()
        if isinstance(content, dict):
            block_type = str(content.get("type") or "").strip().lower()
            if block_type == "think":
                return ""
            if block_type == "text":
                return str(content.get("text") or "").strip()
            if block_type in {"image", "image_url"}:
                image_url = ""
                image_payload = content.get("image_url")
                if isinstance(image_payload, dict):
                    image_url = str(image_payload.get("url") or "").strip()
                elif image_payload is not None:
                    image_url = str(image_payload).strip()
                if not image_url:
                    image_url = str(content.get("url") or "").strip()
                return f"[图片] {image_url}".strip()
            if "text" in content:
                return str(content.get("text") or "").strip()
            if "content" in content:
                return self._extract_visible_text(content.get("content"))
        return ""
