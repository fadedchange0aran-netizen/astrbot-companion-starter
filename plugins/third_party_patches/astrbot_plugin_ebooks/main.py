import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Union

from astrbot.api.all import *
from astrbot.api.event.filter import *
from data.plugins.astrbot_plugin_ebooks.annas_source import AnnasSource
from data.plugins.astrbot_plugin_ebooks.archive_source import ArchiveSource
from data.plugins.astrbot_plugin_ebooks.calibre_source import CalibreSource
from data.plugins.astrbot_plugin_ebooks.liber3_source import Liber3Source
from data.plugins.astrbot_plugin_ebooks.utils import (
    is_valid_annas_book_id,
    is_valid_archive_book_url,
    is_valid_calibre_book_url,
    is_valid_liber3_book_id,
    normalize_limit,
    to_event_results,
)
from data.plugins.astrbot_plugin_ebooks.zlib_source import ZlibSource


@register("ebooks", "Companion Starter Maintainers", "多源电子书搜索和下载插件", "2.0.1", "https://github.com/zouyonghe/astrbot_plugin_ebooks")
class ebooks(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.proxy = os.environ.get("https_proxy")
        self.TEMP_PATH = os.path.abspath("data/temp")
        os.makedirs(self.TEMP_PATH, exist_ok=True)
        self.enable_file_vault_copy = bool(self.config.get("enable_file_vault_copy", True))
        configured_file_vault_root = str(self.config.get("file_vault_root", "") or "").strip()
        self.FILE_VAULT_ROOT = Path(configured_file_vault_root or os.path.abspath("data/file_vault"))
        self.FILE_VAULT_FILES_ROOT = self.FILE_VAULT_ROOT / "files"
        self.FILE_VAULT_ITEMS_ROOT = self.FILE_VAULT_ROOT / "items"
        if self.enable_file_vault_copy:
            self._ensure_file_vault_dirs()
        self.max_results = self.config.get("max_results", 20)
        if not isinstance(self.max_results, int) or not (1 <= self.max_results <= 100):
            logger.warning("[ebooks] max_results 配置无效，已重置为 20")
            self.max_results = 20

        if self.config.get("enable_calibre", False) and not self.config.get("calibre_web_url", "").strip():
            self.config["enable_calibre"] = False
            self.config.save_config()
            logger.info("[ebooks] 未设置 Calibre-Web URL，禁用该平台。")

        self.calibre_source = CalibreSource(self.config, self.proxy, self.max_results)
        self.liber3_source = Liber3Source(self.config, self.proxy, self.max_results)
        self.archive_source = ArchiveSource(self.config, self.proxy, self.max_results, self.TEMP_PATH)
        self.zlib_source = ZlibSource(self.config, self.proxy, self.max_results, self.TEMP_PATH)
        self.annas_source = AnnasSource(self.config, self.proxy, self.max_results)

    @staticmethod
    def _shared_notice_path() -> Path:
        return Path(os.path.abspath("data/plugin_data/companion_system_notices.jsonl"))

    @staticmethod
    def _event_session_key(event: AstrMessageEvent) -> str:
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if unified_msg_origin:
            return unified_msg_origin
        try:
            session = getattr(event, "session", None)
            if session:
                unified_msg_origin = str(
                    getattr(session, "unified_msg_origin", "") or ""
                ).strip()
        except Exception:
            unified_msg_origin = ""
        if unified_msg_origin:
            return unified_msg_origin
        session_id = ""
        for attr_name in ("session_id", "conversation_id", "session_name"):
            try:
                value = str(getattr(event, attr_name, "") or "").strip()
            except Exception:
                value = ""
            if value:
                session_id = value
                break
        sender_id = ""
        try:
            sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        except Exception:
            sender_id = ""
        platform = ""
        for attr_name in ("platform_name", "client_platform", "platform", "source"):
            try:
                value = str(getattr(event, attr_name, "") or "").strip()
            except Exception:
                value = ""
            if value:
                platform = value
                break
        if session_id:
            return f"{platform or 'platform'}::{session_id}"
        if sender_id:
            return sender_id
        return ""

    def _append_system_notice(
        self,
        event: AstrMessageEvent,
        *,
        summary: str,
        source: str,
        tool_name: str = "",
    ) -> None:
        session_key = self._event_session_key(event)
        clean_summary = str(summary or "").strip()
        if not session_key or not clean_summary:
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

    @staticmethod
    def _limit_text(text: str, limit: int = 280) -> str:
        clean = " ".join(str(text or "").split())
        if len(clean) <= limit:
            return clean
        return clean[: limit - 3].rstrip() + "..."

    def _build_search_tasks(self, event: AstrMessageEvent, query: str, limit: int):
        tasks = []
        if self.config.get("enable_calibre", False):
            tasks.append(("Calibre-Web", self.calibre_source.search_nodes(event, query, limit)))
        if self.config.get("enable_liber3", False):
            tasks.append(("Liber3", self.liber3_source.search_nodes(event, query, limit)))
        if self.config.get("enable_archive", False):
            tasks.append(("archive.org", self.archive_source.search_nodes(event, query, limit)))
        if self.config.get("enable_zlib", False):
            tasks.append(("Z-Library", self.zlib_source.search_nodes(event, query, limit)))
        if self.config.get("enable_annas", False):
            tasks.append(("Anna's Archive", self.annas_source.search_nodes(event, query, limit)))
        return tasks

    async def _collect_named_search_results(self, event: AstrMessageEvent, query: str, limit: int):
        tasks = self._build_search_tasks(event, query, limit)
        if not tasks:
            return []
        search_results = await asyncio.gather(*[task for _, task in tasks])
        return list(zip([name for name, _ in tasks], search_results))

    async def _send_named_search_results(
        self, event: AstrMessageEvent, named_results: list[tuple[str, Any]]
    ) -> None:
        if self.config.get("enable_merge_forward", False):
            try:
                ns = Nodes([])
                for _, platform_results in named_results:
                    if isinstance(platform_results, str):
                        node = Node(
                            uin=event.get_self_id(),
                            name="ebooks",
                            content=[Plain(platform_results)],
                        )
                        ns.nodes.append(node)
                        continue
                    for i in range(0, len(platform_results), 30):
                        chunk_results = platform_results[i:i + 30]
                        node = Node(
                            uin=event.get_self_id(),
                            name="ebooks",
                            content=chunk_results,
                        )
                        ns.nodes.append(node)
                if ns.nodes:
                    await event.send(event.chain_result([ns]))
                    return
            except Exception as e:
                logger.error(f"[ebooks] Forward delivery failed, falling back to plain text: {e}")

        for platform_name, platform_results in named_results:
            message = self._format_platform_plain_message(platform_name, platform_results)
            if message:
                await event.send(event.plain_result(message))

    @staticmethod
    def _node_content_text(node: Any) -> str:
        content = getattr(node, "content", None) or []
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()

    def _node_summary_line(self, node: Any) -> str:
        raw = self._node_content_text(node)
        if not raw:
            return ""
        title = ""
        author = ""
        download_ref = ""
        for line in [part.strip() for part in raw.splitlines() if part.strip()]:
            if line.startswith("书名:") and not title:
                title = line.replace("书名:", "", 1).strip()
            elif line.startswith("作者:") and not author:
                author = line.replace("作者:", "", 1).strip()
            elif (
                line.startswith("ID(用于下载):")
                or line.startswith("Hash(用于下载):")
                or line.startswith("链接(用于下载):")
            ):
                if download_ref:
                    download_ref = f"{download_ref} | {line}"
                else:
                    download_ref = line
        pieces = [piece for piece in (title, author, download_ref) if piece]
        if pieces:
            return " | ".join(pieces)
        return self._limit_text(" ".join(raw.split()), 180)

    def _format_named_search_results_for_llm(
        self, query: str, named_results: list[tuple[str, Any]]
    ) -> str:
        lines = [f"[ebooks-summary] 已完成电子书搜索“{query}”。"]
        found_any = False
        for platform_name, platform_results in named_results:
            if isinstance(platform_results, str):
                lines.append(f"- {platform_name}: {self._limit_text(platform_results, 180)}")
                continue
            summary_lines: list[str] = []
            for node in list(platform_results)[:3]:
                summary = self._node_summary_line(node)
                if summary:
                    summary_lines.append(summary)
            if summary_lines:
                found_any = True
                lines.append(f"- {platform_name}:")
                for index, summary in enumerate(summary_lines, start=1):
                    lines.append(f"  {index}. {summary}")
            else:
                lines.append(f"- {platform_name}: 未返回可用结果。")
        if found_any:
            lines.append("如果接着要下载，优先沿用上面已有的平台、ID、Hash 或下载链接，不要重新泛化搜索。")
        else:
            lines.append("这次没有拿到稳定可用的搜索结果；如果要继续，可以换关键词、换平台，或稍后重试。")
        return "\n".join(lines)

    def _format_platform_plain_message(self, platform_name: str, platform_results: Any) -> str:
        if isinstance(platform_results, str):
            return f"[{platform_name}]\n{self._limit_text(platform_results, 1200)}"

        summary_lines: list[str] = []
        for index, node in enumerate(list(platform_results)[:8], start=1):
            text = self._node_content_text(node)
            if not text:
                continue
            summary_lines.append(f"{index}.\n{text}")

        if not summary_lines:
            return f"[{platform_name}]\n未返回可用结果。"

        header = f"[{platform_name}]"
        body = "\n\n".join(summary_lines)
        tail = "\n\n继续下载时，直接沿用这里的 ID、Hash 或下载链接。"
        return self._limit_text(f"{header}\n{body}{tail}", 1800)

    def _ensure_file_vault_dirs(self):
        self.FILE_VAULT_ROOT.mkdir(parents=True, exist_ok=True)
        self.FILE_VAULT_FILES_ROOT.mkdir(parents=True, exist_ok=True)
        self.FILE_VAULT_ITEMS_ROOT.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_file_vault_name(name: str) -> str:
        raw = str(name or "").strip() or "ebook"
        raw = raw.replace("\\", "_").replace("/", "_")
        cleaned = re.sub(r"[^0-9A-Za-z._() \-\u4e00-\u9fff]+", "_", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned or "ebook"

    @staticmethod
    def _safe_file_vault_item_id() -> str:
        return f"ebook_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _compute_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _store_local_file_in_vault(self, source_path: str, *, title: str = "") -> dict | None:
        if not self.enable_file_vault_copy:
            return None
        source_file = Path(str(source_path or "")).expanduser().resolve()
        if not source_file.is_file():
            return None
        self._ensure_file_vault_dirs()
        item_id = self._safe_file_vault_item_id()
        stored_dir = self.FILE_VAULT_FILES_ROOT / item_id
        stored_dir.mkdir(parents=True, exist_ok=True)
        stored_name = self._safe_file_vault_name(source_file.name)
        stored_file = stored_dir / stored_name
        shutil.copy2(source_file, stored_file)
        mime_type = mimetypes.guess_type(stored_file.name)[0] or "application/octet-stream"
        payload = {
            "item_id": item_id,
            "title": (title or "").strip() or Path(source_file.name).stem,
            "original_name": source_file.name,
            "stored_name": stored_name,
            "stored_rel_path": str(stored_file.relative_to(self.FILE_VAULT_ROOT)),
            "stored_path": str(stored_file),
            "source_path": str(source_file),
            "source_kind": "ebooks_temp",
            "mime_type": mime_type,
            "bytes": stored_file.stat().st_size,
            "sha256": self._compute_sha256(stored_file),
            "tags": ["ebook", "ebooks"],
            "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        manifest_path = self.FILE_VAULT_ITEMS_ROOT / f"{item_id}.json"
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload

    def _iter_local_file_components(self, item):
        chain = getattr(item, "chain", None) or []
        for comp in chain:
            local_path = ""
            for attr_name in ("file", "path", "file_path"):
                value = getattr(comp, attr_name, None)
                if isinstance(value, str) and value.strip():
                    local_path = value.strip()
                    break
            if not local_path:
                continue
            yield {
                "component": comp,
                "name": str(getattr(comp, "name", "") or Path(local_path).name).strip(),
                "local_path": local_path,
            }

    def _has_local_file_components(self, item) -> bool:
        return any(True for _ in self._iter_local_file_components(item))

    def _build_file_vault_feedback(self, item) -> tuple[list[dict[str, Any]], list[str], bool]:
        stored_items: list[dict[str, Any]] = []
        feedbacks: list[str] = []
        store_failed = False
        for comp in self._iter_local_file_components(item):
            local_path = comp["local_path"]
            try:
                stored_item = self._store_local_file_in_vault(
                    local_path,
                    title=Path(comp["name"]).stem,
                )
            except Exception as exc:
                logger.warning(f"[ebooks] 转存 file_vault 失败: {exc}")
                feedbacks.append(
                    f"[ebooks] 转存到 file_vault 失败：{exc}"
                )
                store_failed = True
                continue
            if not stored_item:
                feedbacks.append("[ebooks] 转存到 file_vault 失败：未生成可用条目。")
                store_failed = True
                continue
            stored_items.append(stored_item)
            component = comp.get("component")
            stored_path = str(stored_item.get("stored_path") or "").strip()
            if component is not None and stored_path:
                for attr_name in ("file", "path", "file_path"):
                    if hasattr(component, attr_name):
                        try:
                            setattr(component, attr_name, stored_path)
                        except Exception:
                            continue
            feedbacks.append(
                "[ebooks] 已把《{original_name}》转存到 file_vault，"
                "编号：{item_id}，路径：{stored_rel_path}。"
                "后续如果要再发给你，可以直接基于这份受管副本继续处理。".format(
                    original_name=stored_item["original_name"],
                    item_id=stored_item["item_id"],
                    stored_rel_path=stored_item["stored_rel_path"],
                )
            )
        return stored_items, feedbacks, store_failed

    @staticmethod
    def _download_no_direct_send_notice() -> str:
        return (
            "[ebooks] 本地临时下载文件已不再直接走 QQ 发附件，"
            "避免临时文件被清理后出现 ENOENT。"
            "如需继续发送，请基于 file_vault 里的受管副本处理。"
        )

    @staticmethod
    def _download_fallback_send_notice() -> str:
        return (
            "[ebooks] 转存到 file_vault 失败，已回退为直接发送当前下载文件。"
            "这次不会生成可继续转发的受管副本。"
        )

    def _build_llm_download_payload(
        self,
        arg1: str,
        arg2: str | None,
        stored_items: list[dict[str, Any]],
        *,
        sent_directly: bool,
        store_failed: bool = False,
    ) -> dict[str, Any]:
        requested = str(arg1 or "").strip()
        if arg2:
            requested = f"{requested}:{str(arg2).strip()}"
        payload: dict[str, Any] = {
            "success": True,
            "requested": requested,
            "sent_directly": sent_directly,
            "file_vault_items": stored_items,
            "item_count": len(stored_items),
            "store_failed": store_failed,
        }
        if stored_items:
            first_item = stored_items[0]
            message = (
                "电子书已下载并转存到 file_vault。"
                f"item_id={str(first_item.get('item_id') or '')}，"
                f"文件名={str(first_item.get('original_name') or '')}。"
                "如需继续发给其他 QQ，请调用 send_file_vault_item_to_qq。"
                "如需导入共读，请调用 import_file_vault_item_to_bookshelf。"
            )
            if store_failed:
                message += "本次有部分文件转存失败，已直接发送给当前用户。"
            payload.update(
                {
                    "item_id": str(first_item.get("item_id") or ""),
                    "original_name": str(first_item.get("original_name") or ""),
                    "stored_rel_path": str(first_item.get("stored_rel_path") or ""),
                    "message": message,
                }
            )
            return payload
        if sent_directly:
            payload["message"] = "电子书已处理并直接发送给当前用户，但未生成可继续转发的 file_vault 条目。"
        else:
            payload["message"] = "电子书下载已处理，但未生成可继续转发的 file_vault 条目。"
        return payload

    async def _run_llm_download(
        self,
        event: AstrMessageEvent,
        arg1: str,
        arg2: str | None = None,
    ) -> dict[str, Any]:
        results = []
        if arg1 and arg2:
            logger.info("[ebooks] 检测到 Z-Library ID 和 Hash，开始下载...")
            results = await self.zlib_source.download(event, arg1, arg2)
        elif is_valid_calibre_book_url(arg1):
            logger.info("[ebooks] 检测到 Calibre-Web 链接，开始下载...")
            results = await self.calibre_source.download(event, arg1)
        elif is_valid_archive_book_url(arg1):
            logger.info("[ebooks] 检测到 archive.org 链接，开始下载...")
            results = await self.archive_source.download(event, arg1)
        elif is_valid_liber3_book_id(arg1):
            logger.info("[ebooks] ⏳ 检测到 Liber3 ID，开始下载...")
            results = await self.liber3_source.download(event, arg1)
        elif is_valid_annas_book_id(arg1):
            logger.info("[ebooks] ⏳ 检测到 Annas Archive ID，开始下载...")
            results = await self.annas_source.download(event, arg1)
        else:
            return {
                "success": False,
                "requested": str(arg1 or "").strip(),
                "message": (
                    "未识别的输入格式，请提供以下格式之一："
                    "Calibre-Web 下载链接、archive.org 下载链接、"
                    "Liber3/Annas Archive 32位 ID，或 Z-Library 的 ID 和 Hash。"
                ),
            }

        stored_items: list[dict[str, Any]] = []
        sent_directly = False
        store_failed = False
        for item in results:
            item_stored, feedbacks, item_store_failed = self._build_file_vault_feedback(item)
            stored_items.extend(item_stored)
            has_local_files = self._has_local_file_components(item)
            store_failed = store_failed or item_store_failed
            if not has_local_files or item_store_failed:
                await event.send(item)
                sent_directly = True
            for feedback in feedbacks:
                self._append_system_notice(
                    event,
                    summary=feedback.replace("[ebooks]", "", 1).strip(),
                    source="ebooks_download",
                    tool_name="download_ebook",
                )
                await event.send(event.plain_result(feedback))
            if item_store_failed and has_local_files:
                await event.send(event.plain_result(self._download_fallback_send_notice()))
            elif feedbacks and has_local_files:
                await event.send(event.plain_result(self._download_no_direct_send_notice()))

        return self._build_llm_download_payload(
            arg1,
            arg2,
            stored_items,
            sent_directly=sent_directly,
            store_failed=store_failed,
        )

    async def terminate(self):
        await asyncio.gather(
            self.calibre_source.close(),
            self.liber3_source.close(),
            self.archive_source.close(),
            self.zlib_source.terminate(),
        )

    async def _yield_download_results(self, event: AstrMessageEvent, results):
        for item in results:
            _stored_items, feedbacks, store_failed = self._build_file_vault_feedback(item)
            has_local_files = self._has_local_file_components(item)
            if has_local_files:
                # Command-mode downloads should still deliver the actual file,
                # but now prefer the rewritten managed copy path when available.
                yield item
            elif not has_local_files:
                yield item
            for feedback in feedbacks:
                self._append_system_notice(
                    event,
                    summary=feedback.replace("[ebooks]", "", 1).strip(),
                    source="ebooks_download",
                    tool_name="download_ebook",
                )
                yield event.plain_result(feedback)
            if has_local_files and store_failed:
                yield event.plain_result(self._download_fallback_send_notice())

    @command_group("calibre")
    def calibre(self):
        pass

    @calibre.command("search")
    async def search_calibre(self, event: AstrMessageEvent, query: str, limit: str = ""):
        limit_value, err = normalize_limit(limit, self.max_results, 1, 100)
        if err:
            yield event.plain_result(f"[Calibre-Web] {err}")
            return
        result = await self.calibre_source.search_nodes(event, query, limit_value)
        for response in to_event_results(event, "Calibre-Web", result):
            yield response

    @calibre.command("download")
    async def download_calibre(self, event: AstrMessageEvent, book_url: str = None):
        results = await self.calibre_source.download(event, book_url)
        async for response in self._yield_download_results(event, results):
            yield response

    @calibre.command("recommend")
    async def recommend_calibre(self, event: AstrMessageEvent, n: int):
        results = await self.calibre_source.recommend(event, n)
        async for response in self._yield_download_results(event, results):
            yield response

    async def search_calibre_books(self, event: AstrMessageEvent, query: str):
        async for result in self.search_calibre(event, query):
            yield result

    async def download_calibre_book(self, event: AstrMessageEvent, book_url: str):
        async for result in self.download_calibre(event, book_url):
            yield result

    @command_group("liber3")
    def liber3(self):
        pass

    @liber3.command("search")
    async def search_liber3(self, event: AstrMessageEvent, query: str = None, limit: str = ""):
        limit_value, err = normalize_limit(limit, self.max_results, 1, 100)
        if err:
            yield event.plain_result(f"[Liber3] {err}")
            return
        result = await self.liber3_source.search_nodes(event, query, limit_value)
        for response in to_event_results(event, "Liber3", result):
            yield response

    @liber3.command("download")
    async def download_liber3(self, event: AstrMessageEvent, book_id: str = None):
        results = await self.liber3_source.download(event, book_id)
        async for response in self._yield_download_results(event, results):
            yield response

    # @llm_tool("search_liber3_books")
    async def search_liber3_books(self, event: AstrMessageEvent, query: str):
        async for result in self.search_liber3(event, query):
            yield result

    async def download_liber3_book(self, event: AstrMessageEvent, book_id: str):
        async for result in self.download_liber3(event, book_id):
            yield result

    @command_group("archive")
    def archive(self):
        pass

    @archive.command("search")
    async def search_archive(self, event: AstrMessageEvent, query: str = None, limit: str = ""):
        limit_value, err = normalize_limit(limit, self.max_results, 1, 60, clamp_max=True)
        if err:
            yield event.plain_result(f"[archive.org] {err}")
            return
        result = await self.archive_source.search_nodes(event, query, limit_value)
        for response in to_event_results(event, "archive.org", result):
            yield response

    @archive.command("download")
    async def download_archive(self, event: AstrMessageEvent, book_url: str = None):
        results = await self.archive_source.download(event, book_url)
        async for response in self._yield_download_results(event, results):
            yield response

    async def search_archive_books(self, event: AstrMessageEvent, query: str):

        async for result in self.search_archive(event, query):
            yield result

    # @llm_tool("download_archive_book")
    async def download_archive_book(self, event: AstrMessageEvent, download_url: str):

        async for result in self.download_archive(event, download_url):
            yield result

    @command_group("zlib")
    def zlib(self):
        pass

    @zlib.command("search")
    async def search_zlib(self, event: AstrMessageEvent, query: str = None, limit: str = ""):
        limit_value, err = normalize_limit(limit, self.max_results, 1, 60, clamp_max=True)
        if err:
            yield event.plain_result(f"[Z-Library] {err}")
            return
        result = await self.zlib_source.search_nodes(event, query, limit_value)
        for response in to_event_results(event, "Z-Library", result):
            yield response

    @zlib.command("download")
    async def download_zlib(self, event: AstrMessageEvent, book_id: str = None, book_hash: Union[str, int] = None):
        results = await self.zlib_source.download(event, book_id, book_hash)
        async for response in self._yield_download_results(event, results):
            yield response

    # @llm_tool("search_zlib_books")
    async def search_zlib_books(self, event: AstrMessageEvent, query: str):

        async for result in self.search_zlib(event, query):
            yield result

    async def download_zlib_book(self, event: AstrMessageEvent, book_id: str, book_hash: str):

        async for result in self.download_zlib(event, book_id, book_hash):
            yield result

    @command_group("annas")
    def annas(self):
        pass

    @annas.command("search")
    async def search_annas(self, event: AstrMessageEvent, query: str, limit: str = ""):
        limit_value, err = normalize_limit(limit, self.max_results, 1, 60, clamp_max=True)
        if err:
            yield event.plain_result(f"[Anna's Archive] {err}")
            return
        result = await self.annas_source.search_nodes(event, query, limit_value)
        for response in to_event_results(event, "anna's archive", result):
            yield response

    @annas.command("download")
    async def download_annas(self, event: AstrMessageEvent, book_id: str = None):
        results = await self.annas_source.download(event, book_id)
        async for response in self._yield_download_results(event, results):
            yield response

    @command_group("ebooks")
    def ebooks(self):
        pass

    @ebooks.command("help")
    async def show_help(self, event: AstrMessageEvent):
        help_msg = [
            "📚 **ebooks 插件使用指南**",
            "",
            "支持通过多平台（Calibre-Web、Liber3、Z-Library、archive.org）搜索、下载电子书。",
            "",
            "---",
            "🔧 **命令列表**:",
            "",
            "- **Calibre-Web**:",
            "  - `/calibre search <关键词> [数量]`：搜索 Calibre-Web 中的电子书。例如：`/calibre search Python 20`。",
            "  - `/calibre download <下载链接/书名>`：通过 Calibre-Web 下载电子书。例如：`/calibre download <URL>`。",
            "  - `/calibre recommend <数量>`：随机推荐指定数量的电子书。",
            "",
            "- **archive.org**:",
            "  - `/archive search <关键词> [数量]`：搜索 archive.org 电子书。例如：`/archive search Python 20`。",
            "  - `/archive download <下载链接>`：通过 archive.org 平台下载电子书。",
            "",
            "- **Z-Library**:",
            "  - `/zlib search <关键词> [数量]`：搜索 Z-Library 的电子书。例如：`/zlib search Python 20`。",
            "  - `/zlib download <ID> <Hash>`：通过 Z-Library 平台下载电子书。",
            "",
            "- **Liber3**:",
            "  - `/liber3 search <关键词> [数量]`：搜索 Liber3 平台上的电子书。例如：`/liber3 search Python 20`。",
            "  - `/liber3 download <ID>`：通过 Liber3 平台下载电子书。",
            "",
            "- **Anna's Archive**:",
            "  - `/annas search <关键词> [数量]`：搜索 Anna's Archive 平台上的电子书。例如：`/annas search Python 20`。",
            "  - `/annas download <ID>`：获取 Anna's Archive 电子书下载链接。",
            "",
            "- **通用命令**:",
            "  - `/ebooks help`：显示当前插件的帮助信息。",
            "  - `/ebooks search <关键词> [数量]`：在所有支持的平台中同时搜索电子书。例如：`/ebooks search Python 20`。",
            "  - `/ebooks download <URL/ID> [Hash]`：通用的电子书下载方式。",
            "",
            "---",
            "📒 **注意事项**:",
            "- `数量` 为可选参数，默认为20，用于限制搜索结果的返回数量，数量超过30会分多个转发发送。",
            "- 下载指令要根据搜索结果，提供有效的 URL、ID 和 Hash 值。",
            "- 推荐功能会从现有书目中随机选择书籍进行展示（目前仅支持Calibre-Web)。",
            "- 目前无法直接从 Anna's Archive 下载电子书。",
            "",
            "---",
            "🌐 **支持平台**:",
            "- Calibre-Web",
            "- Liber3",
            "- Z-Library",
            "- archive.org",
        ]
        yield event.plain_result("\n".join(help_msg))

    @ebooks.command("search")
    async def search_all_platforms(self, event: AstrMessageEvent, query: str = None, limit: str = ""):
        limit, err = normalize_limit(limit, self.max_results, 1, 50)
        if not query:
            yield event.plain_result("[ebooks] 请提供电子书关键词以进行搜索。")
            return

        if err:
            yield event.plain_result(f"[ebooks] {err}")
            return

        try:
            named_results = await self._collect_named_search_results(event, query, limit)
            if self.config.get("enable_merge_forward", False):
                ns = Nodes([])
                for _, platform_results in named_results:
                    if isinstance(platform_results, str):
                        node = Node(
                            uin=event.get_self_id(),
                            name="ebooks",
                            content=[Plain(platform_results)],
                        )
                        ns.nodes.append(node)
                        continue
                    for i in range(0, len(platform_results), 30):
                        chunk_results = platform_results[i:i + 30]
                        node = Node(
                            uin=event.get_self_id(),
                            name="ebooks",
                            content=chunk_results,
                        )
                        ns.nodes.append(node)
                yield event.chain_result([ns])
            else:
                for platform_name, platform_results in named_results:
                    for response in to_event_results(event, platform_name, platform_results):
                        yield response
        except Exception as e:
            logger.error(f"[ebooks] Error during multi-platform search: {e}")
            yield event.plain_result(f"[ebooks] 搜索电子书时发生错误，请稍后再试。")

    @ebooks.command("download")
    async def download_all_platforms(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        if not arg1:
            yield event.plain_result("[ebooks] 请提供有效的下载链接、ID 或参数！")
            return

        try:
            if arg1 and arg2:
                logger.info("[ebooks] 检测到 Z-Library ID 和 Hash，开始下载...")
                async for result in self.download_zlib(event, arg1, arg2):
                    yield result
                return

            if is_valid_calibre_book_url(arg1):
                logger.info("[ebooks] 检测到 Calibre-Web 链接，开始下载...")
                async for result in self.download_calibre(event, arg1):
                    yield result
                return

            if is_valid_archive_book_url(arg1):
                logger.info("[ebooks] 检测到 archive.org 链接，开始下载...")
                async for result in self.download_archive(event, arg1):
                    yield result
                return

            if is_valid_liber3_book_id(arg1):
                logger.info("[ebooks] ⏳ 检测到 Liber3 ID，开始下载...")
                async for result in self.download_liber3(event, arg1):
                    yield result
                return

            if is_valid_annas_book_id(arg1):
                logger.info("[ebooks] ⏳ 检测到 Annas Archive ID，开始下载...")
                async for result in self.download_annas(event, arg1):
                    yield result
                return

            yield event.plain_result(
                "[ebooks] 未识别的输入格式，请提供以下格式之一：\n"
                "- Calibre-Web 下载链接\n"
                "- archive.org 下载链接\n"
                "- Liber3/Annas Archive 32位 ID\n"
                "- Z-Library 的 ID 和 Hash"
            )
        except Exception:
            yield event.plain_result(f"[ebooks] 下载电子书时发生错误，请稍后再试。")

    @llm_tool("search_ebooks")
    async def search_ebooks(self, event: AstrMessageEvent, query: str):
        """Search for eBooks across all supported platforms.

        When to use:
            This method performs a unified search across multiple platforms supported by this plugin,
            allowing users to find ebooks by title or keyword.
            Unless a specific platform is explicitly mentioned, this function should be used as the default means for searching books.


        Args:
            query (string): The keyword or book title for searching.
        """
        limit, err = normalize_limit("20", self.max_results, 1, 50)
        if err:
            return f"[ebooks] {err}"
        try:
            named_results = await self._collect_named_search_results(event, query, limit)
        except Exception as e:
            logger.error(f"[ebooks] Error during llm search: {e}")
            return "[ebooks] 搜索电子书时发生错误，请稍后再试。"
        summary_text = self._format_named_search_results_for_llm(query, named_results)
        try:
            await self._send_named_search_results(event, named_results)
        except Exception as e:
            logger.error(f"[ebooks] Error sending llm search results: {e}")
        self._append_system_notice(
            event,
            summary=self._limit_text(
                f"刚刚已搜索电子书《{query}》。如果继续下载，优先沿用刚才结果里的平台、ID、Hash 或下载链接。"
            ),
            source="ebooks",
            tool_name="search_ebooks",
        )
        return summary_text

    @llm_tool("download_ebook")
    async def download_ebook(self, event: AstrMessageEvent, arg1: str, arg2: str = None):
        """Download eBooks by dispatching to the appropriate platform's download method.

        When to use:
            This method facilitates downloading of ebooks by automatically identifying the platform
            from the provided identifier (ID, URL, or Hash) and then calling the corresponding platform's download function.
            Unless the platform is specifically mentioned, this function serves as the default for downloading ebooks.

        Args:
            arg1 (string): Primary identifier, such as a URL or book ID.
            arg2 (string): Secondary input, such as a hash, required for Z-Library downloads.
        """
        return await self._run_llm_download(event, arg1, arg2)
