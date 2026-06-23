import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


PLUGIN_NAME = "astrbot_plugin_file_delivery"
CONTAINER_ASTRBOT_DATA_ROOT = "/AstrBot/data"
HOST_ASTRBOT_DATA_ROOT = os.environ.get("ASTRBOT_HOST_DATA_ROOT", "/var/lib/astrbot/data")
DEFAULT_FILE_VAULT_ROOTS = (
    f"{CONTAINER_ASTRBOT_DATA_ROOT}/file_vault",
    f"{HOST_ASTRBOT_DATA_ROOT.rstrip('/')}\/file_vault",
    os.path.abspath("data/file_vault"),
)
DEFAULT_FLASH_TRANSFER_DIR = "/tmp/astrbot_flash"
SHARED_NOTICE_FILE_NAME = "system_notices.jsonl"


def _safe_name(value: str) -> str:
    candidate = Path(str(value or "")).name.strip()
    if not candidate:
        return "unnamed"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate)
    return safe.strip("._") or "unnamed"


@register(
    PLUGIN_NAME,
    "Companion Starter Maintainers",
    "把 file_vault 里的受管文件直接发回 QQ，默认只发给当前用户。",
    "0.1.0",
)
class FileDeliveryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._client = None

    def _get_cfg(self, key: str, default: Any) -> Any:
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return default

    def _file_vault_root(self) -> Path:
        raw = str(self._get_cfg("file_vault_root", "") or "").strip()
        candidates: list[Path] = []
        if raw:
            candidates.append(Path(raw))
            remapped = self._remap_host_data_path(raw)
            if remapped and remapped != raw:
                candidates.append(Path(remapped))
        for candidate in DEFAULT_FILE_VAULT_ROOTS:
            candidates.append(Path(candidate))
        seen: set[str] = set()
        for path in candidates:
            normalized = str(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            if path.exists():
                if raw and normalized != raw:
                    logger.warning(
                        "[%s] configured file_vault_root %s not found, fallback to %s",
                        PLUGIN_NAME,
                        raw,
                        normalized,
                    )
                return path
        if raw:
            logger.warning(
                "[%s] configured file_vault_root %s not found, using as-is",
                PLUGIN_NAME,
                raw,
            )
            return Path(raw)
        return Path(DEFAULT_FILE_VAULT_ROOTS[0])

    @staticmethod
    def _remap_host_data_path(path: str) -> str:
        raw = str(path or "").strip()
        if not raw or not Path("/.dockerenv").exists():
            return raw
        if raw == HOST_ASTRBOT_DATA_ROOT:
            return CONTAINER_ASTRBOT_DATA_ROOT
        host_prefix = HOST_ASTRBOT_DATA_ROOT.rstrip("/") + "/"
        if raw.startswith(host_prefix):
            suffix = raw[len(host_prefix):].lstrip("/")
            return f"{CONTAINER_ASTRBOT_DATA_ROOT.rstrip('/')}/{suffix}"
        return raw

    def _flash_transfer_dir(self) -> Path:
        raw = str(
            self._get_cfg("flash_transfer_dir", DEFAULT_FLASH_TRANSFER_DIR) or ""
        ).strip()
        return Path(raw or DEFAULT_FLASH_TRANSFER_DIR)

    def _napcat_container_name(self) -> str:
        return str(self._get_cfg("napcat_container_name", "napcat") or "napcat").strip() or "napcat"

    def _allow_anyone(self) -> bool:
        return bool(self._get_cfg("allow_anyone", False))

    def _allow_cross_user_delivery(self) -> bool:
        return bool(self._get_cfg("allow_cross_user_delivery", False))

    def _allowed_sender_ids(self) -> set[str]:
        raw = str(self._get_cfg("allowed_sender_ids", "") or "").strip()
        if not raw:
            return set()
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _sender_id(self, event: AstrMessageEvent) -> str:
        try:
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:
            sender_id = ""
        return sender_id or "unknown"

    def _session_key(self, event: AstrMessageEvent) -> str:
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if unified_msg_origin:
            return unified_msg_origin
        try:
            session = getattr(event, "session", None)
            nested_umo = str(
                getattr(session, "unified_msg_origin", "") or ""
            ).strip()
        except Exception:
            nested_umo = ""
        if nested_umo:
            return nested_umo
        try:
            session_id = str(getattr(event, "session_id", "") or "").strip()
        except Exception:
            session_id = ""
        if session_id:
            platform = ""
            for attr_name in ("platform_name", "client_platform", "platform", "source"):
                try:
                    value = str(getattr(event, attr_name, "") or "").strip()
                except Exception:
                    value = ""
                if value:
                    platform = value
                    break
            return f"{platform or 'platform'}::{session_id}"
        return self._sender_id(event)

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        if self._allow_anyone():
            return True
        allowed_sender_ids = self._allowed_sender_ids()
        if not allowed_sender_ids:
            return False
        return self._sender_id(event) in allowed_sender_ids

    def _unauthorized_message(self, event: AstrMessageEvent) -> str:
        sender_id = self._sender_id(event)
        return (
            "当前未授权使用文件回传命令。\n"
            f"- 你的 sender_id: {sender_id}\n"
            "- 需要在插件配置里把这个 sender_id 加进 allowed_sender_ids，"
            "或临时打开 allow_anyone。"
        )

    async def _get_client(self, event: AstrMessageEvent | None = None):
        if event:
            client = getattr(event, "bot", None)
            if client and hasattr(client, "call_action"):
                self._client = client
                return client
        if self._client and hasattr(self._client, "call_action"):
            return self._client
        try:
            platform_manager = getattr(self.context, "platform_manager", None)
            if platform_manager is None:
                return None
            if hasattr(platform_manager, "get_insts"):
                platforms = platform_manager.get_insts()
            elif hasattr(platform_manager, "platform_insts"):
                platforms = getattr(platform_manager, "platform_insts", [])
            else:
                platforms = getattr(platform_manager, "_platforms", {}).values()
            for platform in platforms:
                client = None
                if hasattr(platform, "get_client"):
                    try:
                        client = platform.get_client()
                    except Exception:
                        client = None
                elif hasattr(platform, "client"):
                    client = platform.client
                if client and hasattr(client, "call_action"):
                    self._client = client
                    return client
        except Exception as exc:
            logger.debug("[%s] get client failed: %s", PLUGIN_NAME, exc)
        return None

    def _item_path(self, item_id: str) -> Path:
        safe_item_id = _safe_name(item_id)
        return self._file_vault_root() / "items" / f"{safe_item_id}.json"

    def _load_item(self, item_id: str) -> dict[str, Any]:
        path = self._item_path(item_id)
        if not path.exists():
            fallback = self._recover_item_from_files(item_id)
            if fallback is None:
                raise FileNotFoundError(f"file vault item not found: {item_id}")
            return fallback
        return json.loads(path.read_text(encoding="utf-8"))

    def _recover_item_from_files(self, item_id: str) -> dict[str, Any] | None:
        file_dir = self._file_vault_root() / "files" / _safe_name(item_id)
        if not file_dir.is_dir():
            return None
        files = [candidate for candidate in file_dir.iterdir() if candidate.is_file()]
        if not files:
            return None
        files.sort(key=lambda candidate: candidate.stat().st_mtime, reverse=True)
        stored_file = files[0]
        stored_rel_path = stored_file.relative_to(self._file_vault_root()).as_posix()
        stat = stored_file.stat()
        guessed_item = {
            "item_id": _safe_name(item_id),
            "title": stored_file.stem,
            "original_name": stored_file.name,
            "stored_rel_path": stored_rel_path,
            "stored_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "bytes": stat.st_size,
            "source_kind": "recovered_file_vault_item",
        }
        logger.warning(
            "[%s] file vault item manifest missing, recovered from files dir: %s",
            PLUGIN_NAME,
            item_id,
        )
        return guessed_item

    def _resolve_stored_file(self, item: dict[str, Any]) -> Path:
        stored_rel_path = str(item.get("stored_rel_path") or "").strip()
        if not stored_rel_path:
            raise ValueError("file vault item has no stored_rel_path")
        file_vault_root = self._file_vault_root().resolve()
        stored_file = (file_vault_root / stored_rel_path).resolve()
        try:
            stored_file.relative_to(file_vault_root)
        except ValueError as exc:
            raise ValueError("stored file escapes file vault root") from exc
        if not stored_file.is_file():
            raise FileNotFoundError(f"stored file not found: {stored_file}")
        return stored_file

    def _summarize_item(self, item: dict[str, Any], stored_file: Path) -> dict[str, Any]:
        return {
            "item_id": str(item.get("item_id") or ""),
            "title": str(item.get("title") or ""),
            "original_name": str(item.get("original_name") or ""),
            "stored_rel_path": str(item.get("stored_rel_path") or ""),
            "stored_path": str(stored_file),
            "stored_at": str(item.get("stored_at") or ""),
            "bytes": int(item.get("bytes") or stored_file.stat().st_size),
            "source_kind": str(item.get("source_kind") or ""),
        }

    def _shared_notice_path(self) -> Path:
        return Path(os.path.abspath(f"data/plugin_data/{SHARED_NOTICE_FILE_NAME}"))

    def _append_system_notice(
        self,
        event: AstrMessageEvent,
        *,
        summary: str,
        source: str,
        tool_name: str = "",
    ) -> None:
        clean_summary = str(summary or "").strip()
        session_key = self._session_key(event)
        if not clean_summary or not session_key:
            return
        now = datetime.now(timezone.utc)
        payload = {
            "session_key": session_key,
            "source": source,
            "tool_name": tool_name,
            "summary": clean_summary,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
        }
        path = self._shared_notice_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _copy_to_flash_transfer_dir(self, stored_file: Path) -> str:
        flash_dir = self._flash_transfer_dir()
        flash_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_name(stored_file.name)
        target_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        target_path = flash_dir / target_name
        shutil.copy2(stored_file, target_path)
        return str(target_path)

    def _copy_file_to_napcat(self, stored_file: Path) -> str:
        if shutil.which("docker") is None:
            raise RuntimeError("docker binary not found in AstrBot runtime")
        safe_name = _safe_name(stored_file.name)
        napcat_dest = f"/tmp/{uuid.uuid4().hex[:8]}_{safe_name}"
        cmd = ["docker", "cp", str(stored_file), f"{self._napcat_container_name()}:{napcat_dest}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"docker cp 到 NapCat 失败: {error_text or 'unknown error'}")
        return napcat_dest

    def _candidate_delivery_paths(self, stored_file: Path) -> list[tuple[str, callable]]:
        candidates: list[tuple[str, callable]] = []
        flash_dir = str(self._get_cfg("flash_transfer_dir", DEFAULT_FLASH_TRANSFER_DIR) or "").strip()
        if flash_dir:
            candidates.append(("flash_transfer_dir", lambda: self._copy_to_flash_transfer_dir(stored_file)))
        if Path("/.dockerenv").exists():
            candidates.append(("docker_cp", lambda: self._copy_file_to_napcat(stored_file)))
        candidates.append(("direct", lambda: str(stored_file)))
        return candidates

    @staticmethod
    def _is_path_access_error(exc: Exception) -> bool:
        text = str(exc or "")
        lowered = text.lower()
        return (
            "enoent" in lowered
            or "no such file or directory" in lowered
            or "file_path" in lowered and "不存在" in text
            or "open '" in lowered
        )

    async def _send_online_file_with_fallback(
        self,
        client,
        *,
        target_user_id: str,
        stored_file: Path,
        resolved_file_name: str,
    ) -> tuple[str, str]:
        attempts = self._candidate_delivery_paths(stored_file)
        last_exc: Exception | None = None
        tried_modes: list[str] = []
        skipped_modes: list[str] = []
        for delivery_mode, path_factory in attempts:
            tried_modes.append(delivery_mode)
            try:
                delivery_file_path = path_factory()
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "[%s] prepare send path via %s failed: %s",
                    PLUGIN_NAME,
                    delivery_mode,
                    exc,
                )
                skipped_modes.append(f"{delivery_mode}({exc})")
                continue
            try:
                await client.call_action(
                    "send_online_file",
                    user_id=target_user_id,
                    file_path=delivery_file_path,
                    file_name=resolved_file_name,
                )
                return delivery_file_path, delivery_mode
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "[%s] send_online_file via %s failed: %s",
                    PLUGIN_NAME,
                    delivery_mode,
                    exc,
                )
                if not self._is_path_access_error(exc):
                    raise
                continue
        extra_hint = ""
        if any(mode.startswith("docker_cp(") for mode in skipped_modes):
            extra_hint = "；当前 docker_cp 不可用，请优先给 AstrBot 和 NapCat 挂同一个 /tmp/astrbot_flash 共享目录。"
        raise RuntimeError(
            "发送路径全部失败。已尝试模式：{modes}。跳过模式：{skipped}。最后错误：{error}{hint}".format(
                modes=", ".join(tried_modes) or "(无)",
                skipped=", ".join(skipped_modes) or "(无)",
                error=last_exc or "unknown error",
                hint=extra_hint,
            )
        )

    def _resolve_target_user_id(self, event: AstrMessageEvent, user_id: str | None) -> str:
        sender_id = self._sender_id(event)
        requested_user_id = str(user_id or "").strip()
        target_user_id = requested_user_id or sender_id
        if not target_user_id or target_user_id == "unknown":
            raise ValueError("无法确定目标 QQ 号，请显式提供 user_id")
        if requested_user_id and requested_user_id != sender_id:
            if not self._allow_cross_user_delivery() and not self._is_authorized(event):
                raise PermissionError("默认只允许发回当前对话用户；如需跨用户转发，请在插件配置中授权。")
        return target_user_id

    async def _send_item_to_qq(
        self,
        event: AstrMessageEvent,
        *,
        item_id: str,
        user_id: str = "",
        file_name: str = "",
    ) -> dict[str, Any]:
        client = await self._get_client(event)
        if not client:
            raise RuntimeError("无法获取 QQ 客户端")
        item = self._load_item(item_id)
        stored_file = self._resolve_stored_file(item)
        target_user_id = self._resolve_target_user_id(event, user_id)
        resolved_file_name = str(file_name or "").strip() or str(item.get("original_name") or "") or stored_file.name
        delivery_file_path, delivery_mode = await self._send_online_file_with_fallback(
            client,
            target_user_id=target_user_id,
            stored_file=stored_file,
            resolved_file_name=resolved_file_name,
        )
        message = f"已把《{resolved_file_name}》发给 QQ {target_user_id}。"
        self._append_system_notice(
            event,
            summary=message,
            source="file_delivery",
            tool_name="send_file_vault_item_to_qq",
        )
        return {
            "status": "success",
            "message": message,
            "recipient": target_user_id,
            "item": self._summarize_item(item, stored_file),
            "delivery": {
                "channel": "qq_online_file",
                "file_name": resolved_file_name,
                "file_path": delivery_file_path,
                "mode": delivery_mode,
            },
        }

    @filter.llm_tool(name="send_file_vault_item_to_qq")
    async def send_file_vault_item_to_qq(
        self,
        event: AstrMessageEvent,
        item_id: str,
        user_id: str = "",
        file_name: str = "",
    ) -> dict[str, Any]:
        """
        把 file_vault 里的受管文件发回 QQ。
        不填 user_id 时，默认发给当前正在和机器人聊天的这个用户。

        Args:
            item_id(string): file_vault 条目编号，例如 fv-20260612T000000Z-abcd1234。
            user_id(string): 目标 QQ 号。留空时默认发给当前用户。
            file_name(string): 发送时显示的文件名。留空时沿用原文件名。
        """
        try:
            return await self._send_item_to_qq(
                event,
                item_id=str(item_id or "").strip(),
                user_id=user_id,
                file_name=file_name,
            )
        except Exception as exc:
            logger.error("[%s] send_file_vault_item_to_qq failed: %s", PLUGIN_NAME, exc, exc_info=True)
            return {
                "status": "error",
                "message": f"发送 file_vault 文件失败: {exc}",
                "item_id": str(item_id or "").strip(),
                "recipient": str(user_id or "").strip(),
            }

    @filter.command("发代存文件", alias={"/发代存文件"})
    async def send_file_vault_item_command(self, event: AstrMessageEvent):
        """发代存文件 <item_id> [qq号]"""
        if not self._is_authorized(event):
            yield event.plain_result(self._unauthorized_message(event))
            return
        raw_text = (getattr(event, "message_str", "") or "").strip()
        cleaned = re.sub(r"^/?发代存文件\s*", "", raw_text).strip()
        if not cleaned:
            yield event.plain_result("用法：/发代存文件 <item_id> [qq号]")
            return
        parts = cleaned.split()
        item_id = parts[0]
        target_user_id = parts[1] if len(parts) > 1 else ""
        result = await self._send_item_to_qq(
            event,
            item_id=item_id,
            user_id=target_user_id,
        )
        yield event.plain_result(str(result.get("message") or "操作完成"))

    @filter.command("代存文件回传状态", alias={"/代存文件回传状态"})
    async def file_delivery_status(self, event: AstrMessageEvent):
        """查看 file delivery 插件当前状态。"""
        root = self._file_vault_root()
        yield event.plain_result(
            "\n".join(
                [
                    "file delivery 当前状态：",
                    f"- file_vault_root: {root}",
                    f"- flash_transfer_dir: {self._flash_transfer_dir()}",
                    f"- napcat_container_name: {self._napcat_container_name()}",
                    f"- allow_anyone: {self._allow_anyone()}",
                    f"- allow_cross_user_delivery: {self._allow_cross_user_delivery()}",
                    f"- allowed_sender_ids: {', '.join(sorted(self._allowed_sender_ids())) or '(未配置)'}",
                    f"- 当前 sender_id: {self._sender_id(event)}",
                ]
            )
        )
