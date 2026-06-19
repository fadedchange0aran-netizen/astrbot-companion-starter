import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext


PLUGIN_NAME = "astrbot_plugin_link_context"
STATE_FILE_NAME = "recent_actions.json"
SHARED_NOTICE_FILE_NAME = "aran_system_notices.jsonl"
ADMIN_ONCE_FILE_NAME = "admin_once_access.json"
ADMIN_ONCE_TTL_SECONDS = 600
SUPPORTED_PLATFORMS = {"bilibili", "xiaohongshu"}
SUPPORTED_LINK_HOST_HINTS = (
    "bilibili.com",
    "b23.tv",
    "xiaohongshu.com",
    "xhslink.com",
    "xhs.cn",
)
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
CONSUMER_ROUTE_ALWAYS_KEEP_TOOLS = {
    "recall_long_term_memory",
    "send_message_to_user",
}
GROUP_DEFAULT_BLOCKED_TOOLS = {
    "astrbot_execute_shell",
    "astrbot_execute_python",
    "request_admin_tool_access",
    "run_admin_action_once",
    "send_whisper_to_bia",
    "send_email",
    "send_file_vault_item_via_email",
    "write_memory_file",
    "write_playground_file",
    "save_structured_memory",
    "update_structured_memory",
    "write_notion_page",
    "update_notion_block_tool",
    "delete_notion_block_tool",
    "rename_notion_page_tool",
    "manage_notion_todo_tool",
    "update_soul",
    "update_profile",
    "create_memory",
    "memory_save",
    "add_memo_block",
    "llm_publish_feed",
}
ROUTE_TOOL_ALLOWLISTS = {
    "ebook": {
        "search_ebooks",
        "download_ebook",
        "send_file_vault_item_to_qq",
        "get_file_vault_status",
        "store_file_in_vault",
    },
    "bookshelf": {
        "get_bookshelf_shared_panel",
        "continue_bookshelf_reading",
        "read_bookshelf_chapter",
        "import_file_vault_item_to_bookshelf",
        "send_file_vault_item_to_qq",
    },
    "music": {
        "play_song_by_name",
        "speak_to_user",
    },
    "link": {
        "read_link_context",
    },
}
ROUTE_LABELS = {
    "ebook": "电子书路由",
    "bookshelf": "共读/书架路由",
    "music": "音乐路由",
    "link": "链接路由",
}
ROUTE_FOLLOWUP_PATTERNS = {
    "ebook": (
        r"^发我$",
        r"^发给我$",
        r"^发群里$",
        r"^发到群里$",
        r"^发一下$",
        r"^发出来$",
        r"^传过来$",
        r"^下这个$",
        r"^下载这个$",
        r"^就这本$",
        r"^这本$",
        r"^第一本$",
        r"^第二本$",
        r"^第三本$",
    ),
    "bookshelf": (
        r"^继续$",
        r"^继续读$",
        r"^接着读$",
        r"^继续看$",
        r"^往下读$",
        r"^下一章$",
        r"^下一节$",
        r"^继续这一本文$",
    ),
    "music": (
        r"^来这首$",
        r"^放这首$",
        r"^就这首$",
        r"^播这个$",
        r"^播这首$",
        r"^放一下$",
    ),
    "link": (
        r"^这个讲了啥$",
        r"^这条讲了啥$",
        r"^这个说了啥$",
        r"^总结一下$",
        r"^展开说说$",
        r"^继续说$",
    ),
}
RECENT_FOLLOWUP_PATTERNS = (
    r"刚刚",
    r"上一条",
    r"上条",
    r"前面让你",
    r"你知道了吗",
    r"还记得",
    r"继续吧?",
    r"接着",
    r"然后呢",
    r"后来呢",
    r"怎么样了",
    r"成功了吗",
    r"搜到了吗",
    r"下好了没",
    r"发出去了吗",
)


@register(
    PLUGIN_NAME,
    "Aran",
    "给阿然提供 B站/小红书链接理解，以及最近一次工具成功结果感知。",
    "0.1.0",
)
class LinkContextPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / STATE_FILE_NAME
        self.admin_once_path = self.data_dir / ADMIN_ONCE_FILE_NAME
        self._parser_manager = None
        self._cmd_config_cache: dict[str, Any] | None = None
        self._cmd_config_mtime: float | None = None

    def _get_cfg(self, key: str, default: Any) -> Any:
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return default

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _parse_timestamp(raw: str) -> datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except Exception:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @staticmethod
    def _trim(text: str, limit: int) -> str:
        text = str(text or "").strip().replace("\r", "")
        text = " ".join(text.split())
        if limit <= 0 or len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def _looks_like_recent_followup(text: str) -> bool:
        raw = str(text or "").strip()
        if not raw:
            return False
        if len(raw) <= 8 and raw in {"继续", "继续吧", "继续啊", "接着", "然后", "然后呢"}:
            return True
        return any(re.search(pattern, raw, re.IGNORECASE) for pattern in RECENT_FOLLOWUP_PATTERNS)

    @staticmethod
    def _looks_like_ebook_delivery_request(text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        intent_patterns = (
            r"ebook",
            r"电子书",
            r"找书",
            r"下书",
            r"搜书",
            r"下载.*书",
            r"发群里",
            r"发到群里",
            r"发给群",
            r"发文件",
            r"传文件",
            r"发我",
            r"发一下",
        )
        matched = any(re.search(pattern, raw, re.IGNORECASE) for pattern in intent_patterns)
        if not matched:
            return False
        return any(
            keyword in raw
            for keyword in ("书", "ebook", "电子书", "txt", "epub", "pdf", "mobi", "azw3", "小说")
        )

    @staticmethod
    def _looks_like_bookshelf_request(text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        patterns = (
            r"共读",
            r"书架",
            r"继续读",
            r"接着读",
            r"读到哪",
            r"继续看书",
            r"章节",
        )
        return any(re.search(pattern, raw, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _looks_like_music_request(text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        patterns = (
            r"点歌",
            r"放歌",
            r"听歌",
            r"播.*歌",
            r"放一首",
            r"来一首",
            r"音乐",
            r"歌曲",
            r"song",
            r"music",
        )
        return any(re.search(pattern, raw, re.IGNORECASE) for pattern in patterns)

    def _detect_tool_route(self, event: AstrMessageEvent, text: str) -> str:
        if self._looks_like_ebook_delivery_request(text):
            return "ebook"
        if self._looks_like_bookshelf_request(text):
            return "bookshelf"
        if self._looks_like_music_request(text):
            return "music"
        if self._event_contains_supported_link(event):
            return "link"
        recent_followup_route = self._detect_recent_followup_route(event, text)
        if recent_followup_route:
            return recent_followup_route
        return ""

    def _detect_recent_followup_route(self, event: AstrMessageEvent, text: str) -> str:
        raw = str(text or "").strip().lower()
        if not raw:
            return ""
        matched_routes = [
            route_name
            for route_name, patterns in ROUTE_FOLLOWUP_PATTERNS.items()
            if any(re.search(pattern, raw, re.IGNORECASE) for pattern in patterns)
        ]
        if not matched_routes:
            return ""
        recent_actions = self._get_recent_actions(event)
        if not recent_actions:
            return ""
        recent_tool_names = [
            str(item.get("tool_name") or "").strip()
            for item in reversed(recent_actions)
            if str(item.get("tool_name") or "").strip()
        ]
        if not recent_tool_names:
            return ""
        for route_name in matched_routes:
            route_tool_names = ROUTE_TOOL_ALLOWLISTS.get(route_name, set())
            if any(tool_name in route_tool_names for tool_name in recent_tool_names):
                logger.info(
                    f"[{PLUGIN_NAME}] 命中最近事项续路由: session={self._session_key(event)}, "
                    f"route={route_name}, text={self._trim(raw, 60)}, recent_tools={recent_tool_names[:5]}"
                )
                return route_name
        return ""

    @staticmethod
    def _toolset_with_filters(
        tool_set: ToolSet,
        *,
        allow_names: set[str] | None = None,
        deny_names: set[str] | None = None,
    ) -> ToolSet:
        filtered = ToolSet()
        allow = set(allow_names or []) if allow_names is not None else None
        deny = set(deny_names or [])
        for tool in list(tool_set.tools):
            name = str(getattr(tool, "name", "") or "").strip()
            if not name:
                continue
            if allow is not None and name not in allow:
                continue
            if name in deny:
                continue
            filtered.add_tool(tool)
        return filtered

    @staticmethod
    def _tool_names(tool_set: ToolSet | None) -> list[str]:
        if tool_set is None:
            return []
        try:
            return list(tool_set.names())
        except Exception:
            return []

    def _tool_route_prompt(self, route_name: str, tool_names: list[str]) -> str:
        label = ROUTE_LABELS.get(route_name, route_name)
        pretty_tools = "、".join(tool_names) if tool_names else "无"
        if route_name == "ebook":
            body = (
                "当前请求已经切到电子书专用工具路由。"
                "这轮只应在 search_ebooks、download_ebook、send_file_vault_item_to_qq 这条链上行动。"
                "如果用户给的是书名，先搜；如果已有明确下载标识，再下；如果用户要发当前 QQ 会话，下载后继续发。"
            )
        elif route_name == "bookshelf":
            body = (
                "当前请求已经切到共读/书架工具路由。"
                "优先用书架与共读工具继续阅读、查看章节、导入文件，不要改走无关搜索或脚本工具。"
            )
        elif route_name == "music":
            body = (
                "当前请求已经切到音乐工具路由。"
                "优先直接用 play_song_by_name 处理点歌或放歌请求，不要改走网页搜索、脚本执行或别的泛工具。"
            )
        else:
            body = (
                "当前请求已经切到链接理解路由。"
                "优先用 read_link_context 读取当前消息里的链接内容，再基于结果接话。"
            )
        return (
            f"[工具路由]\n"
            f"- 当前路由: {label}\n"
            f"- 本轮保留工具: {pretty_tools}\n"
            f"{body}"
        )

    @filter.on_agent_begin()
    async def route_tools_for_request(
        self,
        event: AstrMessageEvent,
        run_context: ContextWrapper[AstrAgentContext],
    ) -> None:
        req = event.get_extra("provider_request")
        if not isinstance(req, ProviderRequest):
            return
        if req.func_tool is None or req.func_tool.empty():
            return

        request_text = str(
            getattr(event, "_aran_original_message_str", "") or getattr(event, "message_str", "") or ""
        ).strip()
        route_name = self._detect_tool_route(event, request_text)
        original_toolset = req.func_tool
        current_toolset = original_toolset
        applied_steps: list[str] = []

        if not event.is_private_chat():
            group_filtered = self._toolset_with_filters(
                current_toolset,
                deny_names=GROUP_DEFAULT_BLOCKED_TOOLS,
            )
            if not group_filtered.empty():
                before_names = self._tool_names(current_toolset)
                after_names = self._tool_names(group_filtered)
                if after_names != before_names:
                    current_toolset = group_filtered
                    applied_steps.append("group-default")

        if route_name:
            allow_names = set(CONSUMER_ROUTE_ALWAYS_KEEP_TOOLS)
            route_specific_names = set(ROUTE_TOOL_ALLOWLISTS.get(route_name, set()))
            available_names = set(self._tool_names(current_toolset))
            available_route_names = sorted(name for name in route_specific_names if name in available_names)
            if available_route_names:
                allow_names.update(available_route_names)
                routed_toolset = self._toolset_with_filters(
                    current_toolset,
                    allow_names=allow_names,
                )
                if not routed_toolset.empty():
                    before_names = self._tool_names(current_toolset)
                    after_names = self._tool_names(routed_toolset)
                    if after_names != before_names:
                        current_toolset = routed_toolset
                        applied_steps.append(f"route:{route_name}")
                    req.system_prompt = (
                        f"{req.system_prompt or ''}\n\n"
                        f"{self._tool_route_prompt(route_name, after_names)}"
                    )
            else:
                logger.warning(
                    f"[{PLUGIN_NAME}] 跳过工具路由: session={self._session_key(event)}, "
                    f"route={route_name}, reason=no_route_tools_in_current_toolset, "
                    f"current_tools={sorted(available_names)}"
                )

        final_names = self._tool_names(current_toolset)
        original_names = self._tool_names(original_toolset)
        if final_names != original_names:
            req.func_tool = current_toolset
            logger.info(
                f"[{PLUGIN_NAME}] 工具路由已应用: session={self._session_key(event)}, "
                f"route={route_name or 'default'}, steps={applied_steps}, "
                f"tool_count {len(original_names)} -> {len(final_names)}"
            )

    def _plugins_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def _data_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _media_parser_config_path(self) -> Path:
        return self._data_root() / "config" / "astrbot_plugin_media_parser_config.json"

    def _cmd_config_path(self) -> Path:
        return self._data_root() / "cmd_config.json"

    def _shared_notice_path(self) -> Path:
        return self._data_root() / "plugin_data" / SHARED_NOTICE_FILE_NAME

    def _load_cmd_config(self) -> dict[str, Any]:
        path = self._cmd_config_path()
        try:
            stat = path.stat()
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取 cmd_config 状态失败: {exc}")
            return {}

        if (
            self._cmd_config_cache is not None
            and self._cmd_config_mtime is not None
            and self._cmd_config_mtime == stat.st_mtime
        ):
            return self._cmd_config_cache

        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取 cmd_config 失败: {exc}")
            return {}

        if not isinstance(payload, dict):
            payload = {}
        self._cmd_config_cache = payload
        self._cmd_config_mtime = stat.st_mtime
        return payload

    def _group_auto_wake_whitelist(self) -> set[str]:
        cmd_config = self._load_cmd_config()
        provider_ltm_settings = cmd_config.get("provider_ltm_settings") or {}
        if not isinstance(provider_ltm_settings, dict):
            return set()
        active_reply = provider_ltm_settings.get("active_reply") or {}
        if not isinstance(active_reply, dict):
            return set()
        raw_whitelist = active_reply.get("whitelist") or []
        if not isinstance(raw_whitelist, (list, tuple, set)):
            return set()
        return {str(item or "").strip() for item in raw_whitelist if str(item or "").strip()}

    def _load_admin_once_grant(self) -> dict[str, Any]:
        if not self.admin_once_path.exists():
            return {"active": False}
        try:
            payload = json.loads(self.admin_once_path.read_text(encoding="utf-8"))
        except Exception:
            return {"active": False, "invalid": True}
        now_ts = time.time()
        expires_at_ts = float(payload.get("expires_at_ts") or 0)
        remaining_uses = int(payload.get("remaining_uses") or 0)
        active = bool(expires_at_ts > now_ts and remaining_uses > 0)
        if not active:
            try:
                self.admin_once_path.unlink(missing_ok=True)
            except Exception:
                pass
        payload["active"] = active
        payload["remaining_seconds"] = max(0, int(expires_at_ts - now_ts)) if expires_at_ts else 0
        return payload

    def _write_admin_once_grant(self, event: AstrMessageEvent, note: str) -> dict[str, Any]:
        now_ts = time.time()
        expires_at_ts = now_ts + ADMIN_ONCE_TTL_SECONDS
        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or sender_id or "").strip()
        payload = {
            "granted": True,
            "granted_at": datetime.fromtimestamp(now_ts).isoformat(),
            "expires_at": datetime.fromtimestamp(expires_at_ts).isoformat(),
            "granted_at_ts": now_ts,
            "expires_at_ts": expires_at_ts,
            "remaining_uses": 1,
            "granted_by": sender_id or None,
            "granted_by_name": sender_name or None,
            "note": self._trim(note, 120),
        }
        self.admin_once_path.parent.mkdir(parents=True, exist_ok=True)
        self.admin_once_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payload["active"] = True
        payload["remaining_seconds"] = ADMIN_ONCE_TTL_SECONDS
        return payload

    def _clear_admin_once_grant(self) -> bool:
        if not self.admin_once_path.exists():
            return False
        self.admin_once_path.unlink(missing_ok=True)
        return True

    def _load_media_parser_config(self) -> dict[str, Any]:
        path = self._media_parser_config_path()
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8-sig")
        return json.loads(raw or "{}")

    def _ensure_plugins_root_in_path(self) -> None:
        plugins_root = str(self._plugins_root())
        if plugins_root not in sys.path:
            sys.path.insert(0, plugins_root)

    def _get_parser_manager(self):
        if self._parser_manager is not None:
            return self._parser_manager

        self._ensure_plugins_root_in_path()
        from astrbot_plugin_media_parser.core.config_manager import ConfigManager
        from astrbot_plugin_media_parser.core.parser import ParserManager

        config = self._load_media_parser_config()
        cfg = ConfigManager(config)
        parsers = [
            parser for parser in cfg.create_parsers() if getattr(parser, "name", "") in SUPPORTED_PLATFORMS
        ]
        if not parsers:
            raise RuntimeError("media_parser 中没有可用的 B站/小红书解析器")
        self._parser_manager = ParserManager(parsers)
        return self._parser_manager

    def _extract_card_url(self, card_data: Any) -> str:
        self._ensure_plugins_root_in_path()
        try:
            from astrbot_plugin_media_parser.core.parser.utils import extract_url_from_card_data
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 导入 media_parser 卡片解析工具失败: {exc}")
            return ""
        try:
            return str(extract_url_from_card_data(card_data) or "").strip()
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 提取卡片链接失败: {exc}")
            return ""

    def _extract_url_from_json_card(self, event: AstrMessageEvent) -> str:
        try:
            messages = event.get_messages()
        except Exception:
            messages = None
        if not messages:
            return ""
        for comp in messages:
            card_url = self._extract_card_url(getattr(comp, "data", None))
            if card_url:
                return card_url
        return ""

    def _is_whitelisted_group_link_event(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat():
            return False
        whitelist = self._group_auto_wake_whitelist()
        if not whitelist:
            return False
        session_key = self._session_key(event)
        group_id = str(event.get_group_id() or "").strip()
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        return any(
            candidate in whitelist
            for candidate in (session_key, unified_msg_origin, group_id)
            if candidate
        )

    def _event_contains_supported_link(self, event: AstrMessageEvent) -> bool:
        for candidate in self._event_link_inputs(event):
            if self._text_maybe_contains_supported_link(candidate):
                return True
        return False

    def _first_supported_link_candidate(self, event: AstrMessageEvent) -> str:
        for candidate in self._event_link_inputs(event):
            text = str(candidate or "").strip()
            if text and self._text_maybe_contains_supported_link(text):
                return text
        return ""

    def _extract_event_dict_value(self, event: AstrMessageEvent, attr_names, key_names) -> str:
        for attr_name in attr_names:
            container = getattr(event, attr_name, None)
            if isinstance(container, dict):
                for key_name in key_names:
                    value = container.get(key_name)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return ""

    def _session_key(self, event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            return umo
        try:
            session = getattr(event, "session", None)
            nested_umo = str(getattr(session, "unified_msg_origin", "") or "").strip()
        except Exception:
            nested_umo = ""
        if nested_umo:
            return nested_umo
        session_id = ""
        for attr in ("session_id", "conversation_id", "session_name"):
            value = getattr(event, attr, None)
            if value:
                session_id = str(value).strip()
                break
        if not session_id:
            session_id = self._extract_event_dict_value(
                event,
                ("metadata", "extra", "headers", "message_obj", "raw_message"),
                ("original_session_id", "session_id", "conversation_id"),
            )
        platform = ""
        for attr in ("unified_msg_origin", "platform_name", "client_platform", "platform", "source"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value.strip():
                platform = value.strip()
                break
        if not platform:
            platform = self._extract_event_dict_value(
                event,
                ("metadata", "extra", "headers", "message_obj", "raw_message"),
                ("platform", "client_platform", "source", "x-platform"),
            )
        sender = str(event.get_sender_id() or "unknown").strip()
        return f"{platform or 'platform'}::{session_id or sender}"

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取最近工具结果失败: {exc}")
            return {}

    def _save_state(self, payload: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _load_shared_notices(self) -> list[dict[str, Any]]:
        path = self._shared_notice_path()
        if not path.exists():
            return []
        notices: list[dict[str, Any]] = []
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    notices.append(payload)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取共享系统通知失败: {exc}")
            return []
        return notices

    def _save_shared_notices(self, notices: list[dict[str, Any]]) -> None:
        path = self._shared_notice_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(
            json.dumps(item, ensure_ascii=False) for item in notices if isinstance(item, dict)
        )
        if content:
            content += "\n"
        path.write_text(content, encoding="utf-8")

    def _max_recent_actions(self) -> int:
        return max(1, min(int(self._get_cfg("max_recent_actions", 3)), 5))

    def _normalize_recent_actions(self, item: Any) -> list[dict[str, Any]]:
        if not isinstance(item, dict):
            return []
        raw_actions = item.get("actions")
        if not isinstance(raw_actions, list):
            raw_actions = [item]
        now = self._now()
        actions: list[dict[str, Any]] = []
        for raw_action in raw_actions:
            if not isinstance(raw_action, dict):
                continue
            summary = self._trim(str(raw_action.get("summary") or ""), 420)
            if not summary:
                continue
            created_at = str(raw_action.get("created_at") or "").strip()
            expires_raw = str(raw_action.get("expires_at") or "").strip()
            if not expires_raw:
                continue
            expires_at = self._parse_timestamp(expires_raw)
            if expires_at is None:
                continue
            if expires_at < now:
                continue
            actions.append(
                {
                    "summary": summary,
                    "source": str(raw_action.get("source") or "").strip(),
                    "tool_name": str(raw_action.get("tool_name") or "").strip(),
                    "created_at": created_at,
                    "expires_at": expires_raw,
                }
            )
        actions.sort(key=lambda action: str(action.get("created_at") or ""))
        return actions[-self._max_recent_actions() :]

    def _remember_action_for_session(
        self,
        session_key: str,
        *,
        summary: str,
        source: str,
        tool_name: str = "",
    ) -> None:
        clean_summary = self._trim(summary, 420)
        if not clean_summary or not session_key:
            return
        window_minutes = max(1, int(self._get_cfg("awareness_window_minutes", 30)))
        now = self._now()
        payload = self._load_state()
        actions = self._normalize_recent_actions(payload.get(session_key))
        entry = {
            "summary": clean_summary,
            "source": source,
            "tool_name": tool_name,
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(minutes=window_minutes)).isoformat(timespec="seconds"),
        }
        if actions:
            latest = actions[-1]
            if (
                latest.get("summary") == entry["summary"]
                and latest.get("tool_name") == entry["tool_name"]
                and latest.get("source") == entry["source"]
            ):
                actions[-1] = entry
            else:
                actions.append(entry)
        else:
            actions.append(entry)
        payload[session_key] = {"actions": actions[-self._max_recent_actions() :]}
        self._save_state(payload)

    def _remember_action(
        self,
        event: AstrMessageEvent,
        *,
        summary: str,
        source: str,
        tool_name: str = "",
    ) -> None:
        self._remember_action_for_session(
            self._session_key(event),
            summary=summary,
            source=source,
            tool_name=tool_name,
        )

    def _import_shared_notices(self, event: AstrMessageEvent) -> None:
        session_key = self._session_key(event)
        if not session_key:
            return
        notices = self._load_shared_notices()
        if not notices:
            return
        now = self._now()
        remaining: list[dict[str, Any]] = []
        matched = False
        for notice in notices:
            if not isinstance(notice, dict):
                continue
            expires_raw = str(notice.get("expires_at") or "").strip()
            if expires_raw:
                expires_at = self._parse_timestamp(expires_raw)
                if expires_at is not None and expires_at < now:
                    continue
            notice_session_key = str(notice.get("session_key") or "").strip()
            if notice_session_key != session_key:
                remaining.append(notice)
                continue
            summary = self._trim(str(notice.get("summary") or ""), 420)
            if summary:
                self._remember_action_for_session(
                    session_key,
                    summary=summary,
                    source=str(notice.get("source") or "shared_notice"),
                    tool_name=str(notice.get("tool_name") or ""),
                )
                matched = True
        if matched or len(remaining) != len(notices):
            self._save_shared_notices(remaining)

    def _get_recent_actions(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        session_key = self._session_key(event)
        payload = self._load_state()
        actions = self._normalize_recent_actions(payload.get(session_key))
        if not actions:
            return []
        payload[session_key] = {"actions": actions}
        self._save_state(payload)
        return actions

    @staticmethod
    def _looks_success(text: str) -> bool:
        raw = str(text or "")
        failure_words = ("失败", "没找到", "无可用", "未能", "错误", "超时", "为空")
        return bool(raw.strip()) and not any(word in raw for word in failure_words)

    @staticmethod
    def _looks_failure(text: str) -> bool:
        raw = str(text or "").strip()
        if not raw:
            return False
        failure_words = ("失败", "没找到", "无可用", "未能", "错误", "超时", "为空", "没有识别到", "读取失败")
        return any(word in raw for word in failure_words)

    @staticmethod
    def _coerce_tool_result_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            if text[:1] in {"{", "["}:
                try:
                    payload = json.loads(text)
                except Exception:
                    return text
                parsed = LinkContextPlugin._coerce_tool_result_text(payload)
                return parsed or text
            return text
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, dict):
            parts: list[str] = []
            for key in ("message", "summary", "result", "msg", "error", "data"):
                item = LinkContextPlugin._coerce_tool_result_text(value.get(key))
                if item:
                    parts.append(item)
            if parts:
                deduped: list[str] = []
                seen: set[str] = set()
                for item in parts:
                    if item in seen:
                        continue
                    seen.add(item)
                    deduped.append(item)
                return "\n".join(deduped)
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)
        if isinstance(value, (list, tuple, set)):
            parts = [LinkContextPlugin._coerce_tool_result_text(item) for item in value]
            return "\n".join(part for part in parts if part)
        component_parts: list[str] = []
        for attr_name in ("content", "chain"):
            container = getattr(value, attr_name, None)
            if isinstance(container, (list, tuple, set)):
                for item in container:
                    item_text = LinkContextPlugin._coerce_tool_result_text(item)
                    if item_text:
                        component_parts.append(item_text)
        if component_parts:
            deduped: list[str] = []
            seen: set[str] = set()
            for item in component_parts:
                if item in seen:
                    continue
                seen.add(item)
                deduped.append(item)
            return "\n".join(deduped)
        attr_values: list[str] = []
        for attr_name in ("text", "name", "url"):
            attr_value = getattr(value, attr_name, None)
            if isinstance(attr_value, str) and attr_value.strip():
                attr_values.append(attr_value.strip())
        if attr_values:
            deduped: list[str] = []
            seen: set[str] = set()
            for item in attr_values:
                if item in seen:
                    continue
                seen.add(item)
                deduped.append(item)
            return "\n".join(deduped)
        return str(value)

    @staticmethod
    def _tool_result_to_text(tool_result: Any) -> str:
        if tool_result is None:
            return ""
        if isinstance(tool_result, (dict, list, tuple, set, str, int, float, bool)):
            return LinkContextPlugin._coerce_tool_result_text(tool_result)
        content = getattr(tool_result, "content", None)
        if not content:
            return LinkContextPlugin._coerce_tool_result_text(tool_result)
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
                continue
            if isinstance(item, dict):
                value = LinkContextPlugin._coerce_tool_result_text(item)
                if value:
                    parts.append(value)
        return "\n".join(parts).strip()

    @staticmethod
    def _tool_result_payload(tool_result: Any) -> dict[str, Any] | None:
        if isinstance(tool_result, dict):
            return tool_result
        if isinstance(tool_result, str):
            raw = tool_result.strip()
            if raw[:1] not in {"{", "["}:
                return None
            try:
                payload = json.loads(raw)
            except Exception:
                return None
            return payload if isinstance(payload, dict) else None
        content = getattr(tool_result, "content", None)
        if not content:
            return None
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    payload = json.loads(text)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    return payload
        return None

    @staticmethod
    def _parse_jsonish_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            return {}
        raw = value.strip()
        if raw[:1] not in {"{", "["}:
            return {}
        try:
            payload = json.loads(raw)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _resolved_tool_name(tool_name: str, tool_args: dict[str, Any] | None) -> str:
        if tool_name != "run_wyc_tool":
            return tool_name
        wrapped_name = str((tool_args or {}).get("tool_name") or "").strip()
        return wrapped_name or tool_name

    def _expanded_tool_args(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = dict(tool_args or {})
        if tool_name == "run_wyc_tool":
            wrapped_args = self._parse_jsonish_dict(merged.get("tool_args"))
            if wrapped_args:
                merged.update(wrapped_args)
        return merged

    @staticmethod
    def _clean_result_lines(text: str) -> list[str]:
        lines: list[str] = []
        seen: set[str] = set()
        for raw_line in str(text or "").splitlines():
            line = " ".join(raw_line.strip().split())
            if not line:
                continue
            if line in {"{", "}", "[", "]", "标题样本:"}:
                continue
            if "page_id=" in line:
                line = line.split("| page_id=", 1)[0].strip()
            line = re.sub(r"^\[[^\]]+\]\s*", "", line)
            line = re.sub(r"^[0-9]+\.\s*", "", line)
            line = re.sub(r"^[\-*•]+\s*", "", line)
            line = re.sub(r"^[^\w\u4e00-\u9fff《“【]+", "", line)
            line = LinkContextPlugin._trim(line, 100)
            if not line or line in seen:
                continue
            seen.add(line)
            lines.append(line)
        return lines

    @staticmethod
    def _extract_hit_count(text: str) -> int | None:
        patterns = (
            r"共找到\s*(\d+)\s*个",
            r"找到\s*(\d+)\s*个",
            r"命中\s*(\d+)\s*个",
            r"共\s*(\d+)\s*条",
        )
        raw = str(text or "")
        for pattern in patterns:
            match = re.search(pattern, raw)
            if match:
                try:
                    return int(match.group(1))
                except Exception:
                    return None
        return None

    def _search_highlights(self, text: str, max_items: int = 3) -> list[str]:
        highlights: list[str] = []
        for line in self._clean_result_lines(text):
            if line.startswith(("继续读取", "继续查看", "可以直接用", "结果太多", "本次只检查")):
                continue
            if line.startswith(("共找到", "找到", "命中", "标题完全等于")):
                continue
            highlights.append(line)
            if len(highlights) >= max(1, max_items):
                break
        return highlights

    @staticmethod
    def _payload_result_text(payload: dict[str, Any] | None, fallback: str = "") -> str:
        if not isinstance(payload, dict):
            return fallback
        for key in ("result", "message", "summary", "msg", "error", "data"):
            text = LinkContextPlugin._coerce_tool_result_text(payload.get(key))
            if text:
                return text
        return fallback

    @staticmethod
    def _tool_arg_first_value(values: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
        for source in values:
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _summarize_notion_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        tool_text: str,
        *,
        success: bool,
        payload: dict[str, Any] | None,
    ) -> str:
        if "notion" not in tool_name:
            return ""
        merged_args = self._expanded_tool_args(tool_name, tool_args)
        result_text = self._payload_result_text(payload, tool_text)
        if tool_name == "write_notion_page":
            if not success:
                return self._trim(f"刚刚尝试写入 Notion 但失败了。返回信息：{result_text}", 220)
            content_preview = self._trim(str(merged_args.get("content") or ""), 48)
            route_desc = ""
            page_title = ""
            match = re.search(r"已写入 Notion：(.+?)（目标页：(.+?)）", result_text)
            if match:
                route_desc = self._trim(match.group(1), 60)
                page_title = self._trim(match.group(2), 40)
            if content_preview and route_desc and page_title:
                return (
                    f"我刚刚已经把“{content_preview}”记进 Notion 了，写入位置是 {route_desc}，"
                    f"目标页是《{page_title}》。如果用户继续追问，直接承认已经记下。"
                )
            if route_desc and page_title:
                return (
                    f"我刚刚已经把内容记进 Notion 了，写入位置是 {route_desc}，目标页是《{page_title}》。"
                    "如果用户继续追问，直接承认已经记下。"
                )
            return self._trim(f"我刚刚已经把内容写进 Notion 了。结果：{result_text}", 220)

        if tool_name == "search_notion_pages":
            query = self._tool_arg_first_value([merged_args, payload or {}], ("query", "keyword"))
            if not success:
                if query:
                    return self._trim(f"刚刚在 Notion 里搜索“{query}”但失败了。返回信息：{result_text}", 220)
                return self._trim(f"刚刚在 Notion 里搜索但失败了。返回信息：{result_text}", 220)
            hit_count = self._extract_hit_count(result_text)
            highlights = self._search_highlights(result_text, max_items=2)
            if query and hit_count:
                prefix = f"我刚刚已经在 Notion 里搜索“{query}”，命中了 {hit_count} 个页面。"
            elif query:
                prefix = f"我刚刚已经在 Notion 里搜索“{query}”。"
            else:
                prefix = "我刚刚已经在 Notion 里做过一次搜索。"
            if highlights:
                return (
                    f"{prefix} 目前最相关的是：{'；'.join(highlights)}。"
                    "如果用户继续追问，优先直接读取最相关页面，不要复述整段原始搜索输出。"
                )
            return f"{prefix} 如果用户继续追问，优先直接读取最相关页面。"

        if tool_name == "read_notion_page_content" and success:
            page_ref = self._tool_arg_first_value([merged_args, payload or {}], ("title", "page_id"))
            lines = self._clean_result_lines(result_text)
            preview = self._trim("；".join(lines[:2]), 100)
            if page_ref and preview:
                return f"我刚刚已经读了 Notion 页面“{page_ref}”，关键内容是：{preview}。"
            if preview:
                return f"我刚刚已经读了一页 Notion，关键内容是：{preview}。"
        return ""

    def _summarize_search_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        tool_text: str,
        *,
        success: bool,
        payload: dict[str, Any] | None,
    ) -> str:
        merged_args = self._expanded_tool_args(tool_name, tool_args)
        if "search" not in tool_name and not any(key in merged_args for key in ("query", "keyword")):
            return ""
        result_text = self._payload_result_text(payload, tool_text)
        query = self._tool_arg_first_value([merged_args, payload or {}], ("query", "keyword", "q"))
        if not success:
            if query:
                return self._trim(f"刚刚搜索“{query}”但失败了。返回信息：{result_text}", 220)
            return self._trim(f"刚刚尝试搜索但失败了。返回信息：{result_text}", 220)
        hit_count = self._extract_hit_count(result_text)
        highlights = self._search_highlights(result_text, max_items=3)
        if query and hit_count:
            prefix = f"刚刚已经搜索“{query}”，命中了 {hit_count} 条结果。"
        elif query:
            prefix = f"刚刚已经搜索“{query}”。"
        else:
            prefix = "刚刚已经完成一次搜索。"
        if highlights:
            return (
                f"{prefix} 先记住最关键的几条：{'；'.join(highlights)}。"
                "如果用户继续追问，优先基于这些关键信息继续，不要复述整段原始搜索输出。"
            )
        return f"{prefix} 如果用户继续追问，优先基于最相关结果继续，不要复述整段原始搜索输出。"

    def _summarize_ebook_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        tool_text: str,
        *,
        success: bool,
        payload: dict[str, Any] | None,
    ) -> str:
        if "ebook" not in tool_name:
            return ""
        merged_args = self._expanded_tool_args(tool_name, tool_args)
        result_text = self._payload_result_text(payload, tool_text)
        if tool_name == "download_ebook":
            requested = self._tool_arg_first_value(
                [merged_args, payload or {}],
                ("arg1", "book_id", "title", "query"),
            )
            if not success:
                if requested:
                    return self._trim(f"刚刚尝试下载电子书“{requested}”但失败了。返回信息：{result_text}", 220)
                return self._trim(f"刚刚尝试下载电子书但失败了。返回信息：{result_text}", 220)
            vault_line = ""
            for line in self._clean_result_lines(result_text):
                if "file_vault" in line:
                    vault_line = line
                    break
            if requested and vault_line:
                return (
                    f"刚刚已经下载电子书“{requested}”，而且{vault_line}。"
                    "如果用户继续追问，直接承认已经下载并代存。"
                )
            if vault_line:
                return f"刚刚已经下载了一本电子书，而且{vault_line}。如果用户继续追问，直接承认已经下载并代存。"
            if requested:
                return f"刚刚已经处理电子书下载“{requested}”。如果用户继续追问，直接说明下载结果。"
        return ""

    def _summarize_file_vault_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        tool_text: str,
        *,
        success: bool,
        payload: dict[str, Any] | None,
    ) -> str:
        if "file_vault" not in tool_name:
            return ""
        merged_args = self._expanded_tool_args(tool_name, tool_args)
        if tool_name == "send_file_vault_item_via_email":
            recipient = self._tool_arg_first_value([merged_args, payload or {}], ("recipient",))
            item = payload.get("item") if isinstance(payload, dict) and isinstance(payload.get("item"), dict) else {}
            file_name = self._trim(str(item.get("original_name") or item.get("title") or ""), 50)
            result_text = self._payload_result_text(payload, tool_text)
            if not success:
                if recipient and file_name:
                    return self._trim(
                        f"刚刚尝试把《{file_name}》通过邮件发给 {recipient}，但失败了。返回信息：{result_text}",
                        220,
                    )
                return self._trim(f"刚刚尝试通过邮件转发 file_vault 文件但失败了。返回信息：{result_text}", 220)
            if recipient and file_name:
                return f"刚刚已经把《{file_name}》通过邮件发给 {recipient} 了。如果用户继续追问，直接承认已经转发。"
            if recipient:
                return f"刚刚已经把 file_vault 里的文件通过邮件发给 {recipient} 了。如果用户继续追问，直接承认已经转发。"
        if tool_name == "send_file_vault_item_to_qq":
            recipient = self._tool_arg_first_value([merged_args, payload or {}], ("user_id", "recipient"))
            item = payload.get("item") if isinstance(payload, dict) and isinstance(payload.get("item"), dict) else {}
            file_name = self._trim(str(item.get("original_name") or item.get("title") or ""), 50)
            result_text = self._payload_result_text(payload, tool_text)
            if not success:
                if recipient and file_name:
                    return self._trim(
                        f"刚刚尝试把《{file_name}》通过 QQ 发给 {recipient}，但失败了。返回信息：{result_text}",
                        220,
                    )
                return self._trim(f"刚刚尝试通过 QQ 转发 file_vault 文件但失败了。返回信息：{result_text}", 220)
            if recipient and file_name:
                return f"刚刚已经把《{file_name}》通过 QQ 发给 {recipient} 了。如果用户继续追问，直接承认已经转发。"
            if recipient:
                return f"刚刚已经把 file_vault 里的文件通过 QQ 发给 {recipient} 了。如果用户继续追问，直接承认已经转发。"
        if tool_name == "store_file_in_vault" and success:
            item = payload.get("item") if isinstance(payload, dict) and isinstance(payload.get("item"), dict) else {}
            file_name = self._trim(str(item.get("original_name") or item.get("title") or ""), 50)
            item_id = self._trim(str(item.get("item_id") or ""), 40)
            if file_name and item_id:
                return f"刚刚已经把《{file_name}》收进 file_vault 了，编号是 {item_id}。"
        return ""

    def _summarize_qzone_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        tool_text: str,
        *,
        success: bool,
    ) -> str:
        if tool_name == "llm_publish_feed":
            if success:
                preview = self._trim(str((tool_args or {}).get("text") or ""), 90)
                if preview:
                    return (
                        f"刚刚已经成功发了一条QQ空间说说。内容摘要：{preview}。"
                        "如果用户在延续这个话题，可以继续聊这条说说的内容、图片或发出后的反应。"
                    )
                return "刚刚已经成功发了一条QQ空间说说，可以继续聊这条说说的内容、图片或发出后的反应。"
            return self._trim(f"刚刚尝试发QQ空间说说但失败了。返回信息：{tool_text}", 220)

        if tool_name == "llm_view_feed":
            if success:
                liked = bool((tool_args or {}).get("like"))
                replied = bool((tool_args or {}).get("reply"))
                comment_text = self._trim(
                    str((tool_args or {}).get("comment_text") or ""),
                    90,
                )
                first_lines = [line.strip() for line in tool_text.splitlines() if line.strip()]
                action = "查看了一条QQ空间说说"
                if liked and replied:
                    action = "评论并点赞了一条QQ空间说说"
                elif replied:
                    action = "评论了一条QQ空间说说"
                elif liked:
                    action = "点赞了一条QQ空间说说"
                action_line = self._trim(first_lines[0], 90) if first_lines else ""
                content_line = self._trim(first_lines[1], 90) if len(first_lines) > 1 else ""
                if action_line and content_line:
                    summary = (
                        f"刚刚已经成功：{action_line}。内容摘要：{content_line}。"
                        "如果用户继续聊这条空间动态，请直接承认刚刚的操作和内容。"
                    )
                    if comment_text:
                        summary = (
                            f"{summary[:-1]}；刚刚发出的评论是：{comment_text}。"
                            "如果用户在追问评论内容，请直接承认这句评论。"
                        )
                    return summary
                if action_line:
                    summary = (
                        f"刚刚已经成功：{action_line}。"
                        "如果用户继续聊这条空间动态，请直接承认刚刚的操作和内容。"
                    )
                    if comment_text:
                        summary = (
                            f"{summary[:-1]}；刚刚发出的评论是：{comment_text}。"
                            "如果用户在追问评论内容，请直接承认这句评论。"
                        )
                    return summary
                return f"刚刚已经成功{action}，如果用户继续聊这条空间动态，请直接承认刚刚的操作和内容。"
            return self._trim(f"刚刚尝试操作QQ空间说说但失败了。返回信息：{tool_text}", 220)

        if tool_name == "llm_comment_feed":
            if success:
                comment_text = self._trim(
                    str((tool_args or {}).get("comment_text") or ""),
                    90,
                )
                first_lines = [line.strip() for line in tool_text.splitlines() if line.strip()]
                action_line = self._trim(first_lines[0], 90) if first_lines else ""
                content_line = self._trim(first_lines[1], 90) if len(first_lines) > 1 else ""
                if action_line and content_line and comment_text:
                    return (
                        f"刚刚已经成功：{action_line}。内容摘要：{content_line}。"
                        f"刚刚发出的评论是：{comment_text}。"
                        "如果用户继续聊这条空间动态，请直接承认刚刚的评论和内容。"
                    )
                if action_line and comment_text:
                    return (
                        f"刚刚已经成功：{action_line}。刚刚发出的评论是：{comment_text}。"
                        "如果用户继续聊这条空间动态，请直接承认刚刚的评论和内容。"
                    )
            return self._trim(f"刚刚尝试评论QQ空间说说但失败了。返回信息：{tool_text}", 220)

        if tool_name == "send_online_file":
            file_name = self._trim(
                str((tool_args or {}).get("file_name") or Path(str((tool_args or {}).get("file_path") or "")).name),
                80,
            )
            user_id = self._trim(str((tool_args or {}).get("user_id") or ""), 32)
            if success:
                if file_name and user_id:
                    return (
                        f"刚刚已经把文件《{file_name}》发给 QQ {user_id} 了。"
                        "如果用户继续追问，直接承认刚刚已经发出这个文件。"
                    )
                if file_name:
                    return f"刚刚已经发出文件《{file_name}》了。如果用户继续追问，直接承认刚刚已经发出这个文件。"
                return "刚刚已经发出一个文件了。如果用户继续追问，直接承认刚刚已经发出。"
            if file_name and user_id:
                return self._trim(
                    f"刚刚尝试把文件《{file_name}》发给 QQ {user_id}，但失败了。返回信息：{tool_text}",
                    220,
                )
            return self._trim(f"刚刚尝试发文件但失败了。返回信息：{tool_text}", 220)

        if tool_name == "send_online_folder":
            folder_name = self._trim(
                str((tool_args or {}).get("folder_name") or Path(str((tool_args or {}).get("folder_path") or "")).name),
                80,
            )
            user_id = self._trim(str((tool_args or {}).get("user_id") or ""), 32)
            if success:
                if folder_name and user_id:
                    return (
                        f"刚刚已经把文件夹《{folder_name}》发给 QQ {user_id} 了。"
                        "如果用户继续追问，直接承认刚刚已经发出这个文件夹。"
                    )
                if folder_name:
                    return f"刚刚已经发出文件夹《{folder_name}》了。如果用户继续追问，直接承认刚刚已经发出这个文件夹。"
                return "刚刚已经发出一个文件夹了。如果用户继续追问，直接承认刚刚已经发出。"
            if folder_name and user_id:
                return self._trim(
                    f"刚刚尝试把文件夹《{folder_name}》发给 QQ {user_id}，但失败了。返回信息：{tool_text}",
                    220,
                )
            return self._trim(f"刚刚尝试发文件夹但失败了。返回信息：{tool_text}", 220)

        return ""

    def _summarize_wyc_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        tool_text: str,
        *,
        success: bool,
    ) -> str:
        if tool_name in {"search_wyc_tools", "call_wyc_tools"}:
            return ""

        if tool_name == "publish_qzone":
            wrapped_args = self._parse_jsonish_dict((tool_args or {}).get("tool_args"))
            preview = self._trim(str(wrapped_args.get("content") or ""), 90)
            if success:
                if preview:
                    return (
                        f"刚刚已经通过工具中心成功发了一条QQ空间说说。内容摘要：{preview}。"
                        "如果用户在延续这个话题，可以继续聊这条说说或发出后的反应。"
                    )
                return "刚刚已经通过工具中心成功发了一条QQ空间说说，可以继续聊这条说说或发出后的反应。"
            return self._trim(f"刚刚尝试通过工具中心发QQ空间说说但失败了。返回信息：{tool_text}", 220)

        if tool_name == "search_contacts":
            wrapped_args = self._parse_jsonish_dict((tool_args or {}).get("tool_args"))
            keyword = self._trim(str(wrapped_args.get("keyword") or ""), 40)
            if success:
                if keyword:
                    return f"刚刚已经成功查找联系人“{keyword}”，如果用户继续追问，可以直接基于查找结果回应。"
                return "刚刚已经成功查找联系人，如果用户继续追问，可以直接基于查找结果回应。"
            return self._trim(f"刚刚尝试查找联系人但失败了。返回信息：{tool_text}", 220)

        return ""

    def _build_tool_awareness_summary(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        tool_text: str,
        *,
        success: bool,
        payload: dict[str, Any] | None = None,
    ) -> str:
        tool_name = str(tool_name or "").strip()
        resolved_tool_name = self._resolved_tool_name(tool_name, tool_args)
        payload = payload or self._tool_result_payload(tool_text)
        if resolved_tool_name == "play_song_by_name" and success:
            return self._trim(tool_text, 220)
        if resolved_tool_name == "read_link_context":
            first_lines = [line.strip() for line in tool_text.splitlines() if line.strip()][:5]
            prefix = "刚刚已经成功读懂一条链接内容。" if success else "刚刚尝试解析链接，但失败了。"
            return self._trim(f"{prefix}{'；'.join(first_lines)}", 260)
        if resolved_tool_name == "run_admin_action_once":
            action_name = self._trim(str((tool_args or {}).get("action_name") or ""), 60)
            result_payload = payload.get("result") if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else {}
            description = self._trim(str(result_payload.get("description") or action_name or ""), 90)
            if success:
                if description:
                    return (
                        f"刚刚已经执行过一次管理动作：{description}。"
                        "如果用户继续追问，直接承认刚刚已经做过这次管理操作。"
                    )
                return "刚刚已经执行过一次管理动作了。如果用户继续追问，直接承认刚刚已经做过这次管理操作。"
            if description:
                return self._trim(f"刚刚尝试执行管理动作“{description}”但失败了。返回信息：{tool_text}", 220)
            return self._trim(f"刚刚尝试执行管理动作但失败了。返回信息：{tool_text}", 220)
        notion_summary = self._summarize_notion_tool(
            resolved_tool_name,
            tool_args,
            tool_text,
            success=success,
            payload=payload,
        )
        if notion_summary:
            return self._trim(notion_summary, 260)
        qzone_summary = self._summarize_qzone_tool(
            resolved_tool_name,
            tool_args,
            tool_text,
            success=success,
        )
        if qzone_summary:
            return self._trim(qzone_summary, 260)
        wyc_summary = self._summarize_wyc_tool(
            resolved_tool_name,
            tool_args,
            tool_text,
            success=success,
        )
        if wyc_summary:
            return self._trim(wyc_summary, 260)
        search_summary = self._summarize_search_tool(
            resolved_tool_name,
            tool_args,
            tool_text,
            success=success,
            payload=payload,
        )
        if search_summary:
            return self._trim(search_summary, 260)
        ebook_summary = self._summarize_ebook_tool(
            resolved_tool_name,
            tool_args,
            tool_text,
            success=success,
            payload=payload,
        )
        if ebook_summary:
            return self._trim(ebook_summary, 260)
        file_vault_summary = self._summarize_file_vault_tool(
            resolved_tool_name,
            tool_args,
            tool_text,
            success=success,
            payload=payload,
        )
        if file_vault_summary:
            return self._trim(file_vault_summary, 260)
        status_text = "执行成功" if success else "执行失败"
        if tool_args:
            arg_preview = self._trim(json.dumps(tool_args, ensure_ascii=False), 120)
            return self._trim(f"工具 {resolved_tool_name} 刚刚{status_text}。参数：{arg_preview}。结果：{tool_text}", 260)
        return self._trim(f"工具 {resolved_tool_name} 刚刚{status_text}。结果：{tool_text}", 260)

    def _component_texts(self, result: Any) -> tuple[list[str], set[str]]:
        chain = getattr(result, "chain", None) or []
        texts: list[str] = []
        class_names: set[str] = set()
        for item in chain:
            class_names.add(item.__class__.__name__)
            value = getattr(item, "text", None)
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
        return texts, class_names

    def _infer_sent_action(self, event: AstrMessageEvent, result: Any) -> str:
        texts, class_names = self._component_texts(result)
        joined = " ".join(texts)
        command = str(getattr(event, "command", "") or "").strip()
        message_str = str(getattr(event, "message_str", "") or "").strip()
        if command == "生图" and ("Image" in class_names or "✅ 生成成功" in joined):
            mode = "图生图" if "图生图" in joined else "文生图" if "文生图" in joined else "生图"
            return f"刚刚已经成功完成一次{mode}并把图片发给用户，可以继续聊这张图的风格、构图或提示词。"
        if command == "生图" and self._looks_failure(joined):
            return self._trim(f"刚刚尝试生图但失败了。返回信息：{joined}", 220)
        if "点歌" in message_str and ("Record" in class_names or "File" in class_names or "🎶" in joined):
            return "刚刚已经成功点歌并把歌曲发给用户，可以继续聊这首歌、歌手、歌词或情绪。"
        if "点歌" in message_str and self._looks_failure(joined):
            return self._trim(f"刚刚尝试点歌但失败了。返回信息：{joined}", 220)
        return ""

    def _hot_comments_text(self, metadata: dict[str, Any]) -> str:
        hot_comments = metadata.get("hot_comments") or []
        if not isinstance(hot_comments, list):
            return ""
        max_hot_comments = max(0, int(self._get_cfg("max_hot_comments", 2)))
        max_comment_length = max(20, int(self._get_cfg("max_comment_length", 90)))
        lines: list[str] = []
        for item in hot_comments[:max_hot_comments]:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or item.get("user_name") or "匿名用户").strip()
            message = self._trim(str(item.get("message") or ""), max_comment_length)
            likes = item.get("likes")
            if not message:
                continue
            if str(likes).strip():
                lines.append(f"- {username}（赞{likes}）：{message}")
            else:
                lines.append(f"- {username}：{message}")
        return "\n".join(lines)

    def _describe_link(self, metadata: dict[str, Any]) -> str:
        max_desc_length = max(80, int(self._get_cfg("max_desc_length", 280)))
        title = self._trim(str(metadata.get("title") or ""), 120)
        author = self._trim(str(metadata.get("author") or ""), 80)
        desc = self._trim(str(metadata.get("desc") or ""), max_desc_length)
        timestamp = self._trim(str(metadata.get("timestamp") or ""), 40)
        platform = str(metadata.get("platform") or metadata.get("parser_name") or "unknown").strip()
        video_urls = metadata.get("video_urls") or []
        image_urls = metadata.get("image_urls") or []
        if video_urls:
            kind = "视频"
        elif image_urls:
            kind = "图文"
        else:
            kind = "链接"
        lines = [
            f"平台: {platform}",
            f"类型: {kind}",
        ]
        if title:
            lines.append(f"标题: {title}")
        if author:
            lines.append(f"作者: {author}")
        if timestamp:
            lines.append(f"发布时间: {timestamp}")
        if desc:
            lines.append(f"文案摘要: {desc}")
        hot_comments_text = self._hot_comments_text(metadata)
        if hot_comments_text:
            lines.append("热门评论:")
            lines.append(hot_comments_text)
        return "\n".join(lines)

    @staticmethod
    def _extract_urls_from_text(text: str) -> list[str]:
        raw = str(text or "").strip()
        if not raw:
            return []
        return [match.group(0).rstrip(".,);]}>'\"") for match in URL_PATTERN.finditer(raw)]

    def _text_maybe_contains_supported_link(self, text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        if self._extract_urls_from_text(raw):
            return True
        return any(host in raw for host in SUPPORTED_LINK_HOST_HINTS)

    def _collect_rich_message_strings(
        self,
        value: Any,
        *,
        results: list[str],
        seen: set[int],
        depth: int = 0,
    ) -> None:
        if value is None or depth > 5:
            return
        if isinstance(value, (str, int, float, bool)):
            text = str(value).strip()
            if text and self._text_maybe_contains_supported_link(text):
                results.append(text)
            return
        obj_id = id(value)
        if obj_id in seen:
            return
        seen.add(obj_id)

        if isinstance(value, dict):
            for item in value.values():
                self._collect_rich_message_strings(
                    item,
                    results=results,
                    seen=seen,
                    depth=depth + 1,
                )
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                self._collect_rich_message_strings(
                    item,
                    results=results,
                    seen=seen,
                    depth=depth + 1,
                )
            return

        for attr_name in ("url", "title", "content", "text", "data", "message", "raw_message"):
            if not hasattr(value, attr_name):
                continue
            try:
                attr_value = getattr(value, attr_name)
            except Exception:
                continue
            self._collect_rich_message_strings(
                attr_value,
                results=results,
                seen=seen,
                depth=depth + 1,
            )

    def _event_link_inputs(self, event: AstrMessageEvent) -> list[str]:
        candidates: list[str] = []
        preserved_candidates = getattr(event, "_aran_link_context_candidates", None)
        if isinstance(preserved_candidates, (list, tuple, set)):
            candidates.extend(str(item or "").strip() for item in preserved_candidates)
        original_message_str = str(getattr(event, "_aran_original_message_str", "") or "").strip()
        if original_message_str:
            candidates.append(original_message_str)
        message_str = str(getattr(event, "message_str", "") or "").strip()
        if message_str:
            candidates.append(message_str)
        card_url = self._extract_url_from_json_card(event)
        if card_url:
            candidates.append(card_url)

        rich_values = [
            getattr(event, "_aran_original_message_chain", None),
            getattr(event, "_aran_original_raw_message", None),
            getattr(event, "_aran_link_context_candidates", None),
            getattr(event, "message_obj", None),
            getattr(event, "raw_message", None),
            getattr(getattr(event, "message_obj", None), "message", None),
            getattr(getattr(event, "message_obj", None), "raw_message", None),
        ]
        results: list[str] = []
        seen: set[int] = set()
        for value in rich_values:
            self._collect_rich_message_strings(value, results=results, seen=seen)

        unique: list[str] = []
        seen_texts: set[str] = set()
        for item in candidates + results:
            text = str(item or "").strip()
            if not text or text in seen_texts:
                continue
            seen_texts.add(text)
            unique.append(text)
        return unique

    async def _parse_link_context_text(self, text_or_url: str) -> str:
        parser_manager = self._get_parser_manager()
        links_with_parser = parser_manager.extract_all_links(text_or_url)
        if not links_with_parser:
            return ""

        max_links = max(1, int(self._get_cfg("max_links_per_call", 2)))
        links_with_parser = links_with_parser[:max_links]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            metadata_list = await parser_manager.parse_text(
                text_or_url,
                session,
                links_with_parser=links_with_parser,
            )
        if not metadata_list:
            return "链接已识别，但没有成功拿到可用的标题或文案内容。"

        chunks = []
        for index, metadata in enumerate(metadata_list[:max_links], start=1):
            chunks.append(f"[链接理解 {index}]\n{self._describe_link(metadata)}")
        return "\n\n".join(chunks)

    @filter.llm_tool(name="read_link_context")
    async def read_link_context(self, event: AstrMessageEvent, text_or_url: str):
        """
        读取 B站或小红书链接的标题、作者、正文摘要和热评，让你可以在同一条回复里继续讨论。

        Args:
            text_or_url(string): 用户发来的链接，或包含链接的整段文本
        """
        summary = await self._parse_link_context_text(text_or_url)
        if not summary:
            return "没有识别到可解析的 B站或小红书链接。"
        self._remember_action(
            event,
            summary=(
                f"刚刚已经成功读懂一条链接内容。\n{summary}"
                if not self._looks_failure(summary)
                else f"刚刚尝试解析链接，但失败了。\n{summary}"
            ),
            source="llm_tool_return",
            tool_name="read_link_context",
        )
        return summary

    @filter.on_llm_request()
    async def inject_recent_action(self, event: AstrMessageEvent, req: ProviderRequest):
        self._import_shared_notices(event)
        current_request_text = str(
            getattr(event, "_aran_original_message_str", "") or getattr(event, "message_str", "") or ""
        ).strip()
        is_recent_followup = self._looks_like_recent_followup(current_request_text)
        admin_grant = self._load_admin_once_grant()
        if admin_grant.get("active"):
            req.system_prompt = (
                f"{req.system_prompt or ''}\n\n"
                "[一次性管理授权]\n"
                "用户刚刚明确授权你执行一次管理动作。"
                "如果确实需要执行管理操作，只调用 run_admin_action_once，且默认只执行一次最小必要动作。"
                "如果当前问题不需要管理动作，就不要调用它。"
            )
        req.system_prompt = (
            f"{req.system_prompt or ''}\n\n"
            "[QQ 文件发送约束]\n"
            "如果你要把本地文件发到当前 QQ 对话，不要直接调用 send_message_to_user 去发送 type=file 的本地绝对路径，"
            "尤其不要直接发送 /AstrBot/data/... 这类容器内文件路径。\n"
            "正确做法是：优先调用 send_file_vault_item_to_qq(item_id, user_id='', file_name='')。\n"
            "如果目标文件目前在 playground、bookshelf、memory 或其他代存目录里，先收进 file_vault，再基于 item_id 调用 send_file_vault_item_to_qq。\n"
            "只有普通文本、图片或不依赖本地文件路径的消息，才走 send_message_to_user。"
        )
        current_link_context = ""
        if bool(self._get_cfg("auto_parse_links_on_request", True)):
            for candidate in self._event_link_inputs(event):
                try:
                    current_link_context = await self._parse_link_context_text(candidate)
                except Exception as exc:
                    logger.warning(f"[{PLUGIN_NAME}] 自动解析当前链接失败: {exc}")
                    current_link_context = ""
                if current_link_context:
                    break
            if current_link_context:
                req.system_prompt = (
                    f"{req.system_prompt or ''}\n\n"
                    "[当前消息内链接内容]\n"
                    f"{current_link_context}\n"
                    "如果用户正在发链接给你，请直接基于这些内容回应，不要假装没看到。"
                )
                self._remember_action(
                    event,
                    summary=(
                        f"刚刚已经成功读懂一条链接内容。\n{current_link_context}"
                        if not self._looks_failure(current_link_context)
                        else f"刚刚尝试解析链接，但失败了。\n{current_link_context}"
                    ),
                    source="auto_link_parse",
                    tool_name="read_link_context",
                )
        if current_request_text:
            req.system_prompt = (
                f"{req.system_prompt or ''}\n\n"
                "[当前用户请求]\n"
                f"{self._trim(current_request_text, 220)}\n"
                "优先处理用户这一条刚刚发来的明确请求。\n"
                "除非这条消息本身就是在追问刚刚完成的事，"
                "否则不要因为上一轮工具还很慢、上一轮搜索还没聊完，"
                "就把当前这条新请求当成旧任务的 follow-up 去继续执行。"
            )
            if not is_recent_followup:
                req.system_prompt = (
                    f"{req.system_prompt or ''}\n"
                    "这条消息看起来是一个新的独立请求，不是对上一轮慢任务的默认追问。\n"
                    "除非用户这条消息里明确引用了刚才那件事的结果、书名、ID、Hash、链接、图片或文件，"
                    "否则不要自动续跑上一轮搜索、下载、生图或浏览器任务。"
                )
            if self._looks_like_ebook_delivery_request(current_request_text):
                req.system_prompt = (
                    f"{req.system_prompt or ''}\n\n"
                    "[电子书下载与发群约束]\n"
                    "当前用户是在让你找书、下书，或把电子书发到当前 QQ 对话。\n"
                    "这类请求必须优先使用 ebook 与 file_vault 工具链，不要自己猜外部下载链接，不要自己写 shell/python 脚本去下载，也不要改走 astrbot_execute_shell、astrbot_execute_python、浏览器抓取或手搓 raw 文本链接。\n"
                    "正确顺序是：\n"
                    "1. 如果用户只给了书名、关键词或模糊描述，先调用 search_ebooks(query)。\n"
                    "2. 如果用户已经给了明确的下载标识，或刚刚搜索结果里已有可下载的 ID/Hash/链接，再调用 download_ebook(arg1, arg2)。\n"
                    "3. download_ebook 成功后，如果返回了 file_vault item_id，且用户要发到当前 QQ 对话，必须继续调用 send_file_vault_item_to_qq(item_id, user_id='', file_name='')。\n"
                    "4. 如果 download_ebook 只返回搜索提示或缺少可下载标识，就先补做 search_ebooks，不要伪造下载地址。\n"
                    "5. 如果用户说的是“发群里”“发文件”“发我”，默认目标就是当前这个 QQ 会话，发送时优先用 send_file_vault_item_to_qq，而不是 send_message_to_user 直接发本地路径。\n"
                    "禁止出现以下行为：\n"
                    "- 用 shell/python 自己下载 txt/epub/pdf 文件\n"
                    "- 自己拼 GitHub raw、网盘直链、论坛附件直链或其他猜测链接\n"
                    "- 让用户改去手敲插件触发词，除非 ebook 工具本身已经明确不可用\n"
                    "- 下载成功后不继续发送 file_vault 条目\n"
                )
        if not bool(self._get_cfg("inject_recent_tool_context", True)):
            return
        recent_actions = self._get_recent_actions(event)
        if not recent_actions:
            return
        max_summary_length = max(80, int(self._get_cfg("max_injected_action_length", 160)))
        lines: list[str] = []
        for index, recent in enumerate(reversed(recent_actions), start=1):
            summary = self._trim(str(recent.get("summary") or ""), max_summary_length)
            if not summary:
                continue
            lines.append(f"{index}. {summary}")
        if not lines:
            return
        req.system_prompt = (
            f"{req.system_prompt or ''}\n\n"
            "[最近完成事项提示]\n"
            f"{chr(10).join(lines)}\n"
            "如果用户在延续这个话题，请直接承认刚刚已完成的结果，不要装作不知道。\n"
            "如果用户问的是“刚刚”“上一条”“前面让你做了什么”“你知道了吗”这类追问，必须优先基于上面这些最近完成事项回答，"
            "不要改聊别的话题，不要引用无关旧记忆，也不要编造新的任务经过。\n"
            "除非最近完成事项里明确写了跨会话、跨渠道、别的窗口或外部系统来源，否则默认它们都属于当前这个会话刚刚发生的事，"
            "不要擅自脑补成“你去了别的通道”“你换了别的窗口”或类似说法。\n"
            "回答时只陈述用户可观察到的事实和上面明确记录的结果，不要解释你是怎么知道的，"
            "不要把答案说成来自系统提示、最近事项、共享通知、后台流程、阿策、理性后台或其他内部机制。\n"
            "如果最近完成事项没有明确写到来源，就不要主动提“阿策在后台做了”“我从别处同步到”“我刚搜出来了所以知道”这类来源说明。\n"
            "优先用直接事实句式回答，例如：“刚刚我已经搜了……”“刚刚我画的是……”“刚刚我已经把……发出去了”。\n"
            "禁止出现这类说法：“我在日志里看到”“系统提示告诉我”“最近事项里写着”“共享通知里有”“阿策在后台说”“我从后台得知”“我从别的窗口同步到”“我看工具返回知道的”。\n"
            "如果用户只是核对刚刚发生了什么，就直接回答结果本身，不要补充内部判断过程、提示词来源、日志来源或感知来源。"
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def auto_wake_group_link_message(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "").strip() == str(event.get_self_id() or "").strip():
            return
        if not self._is_whitelisted_group_link_event(event):
            return
        if getattr(event, "is_at_or_wake_command", False):
            return
        fallback_message = self._first_supported_link_candidate(event)
        if not fallback_message:
            return
        if not hasattr(event, "_aran_original_message_str"):
            event._aran_original_message_str = str(getattr(event, "message_str", "") or "")
        if not getattr(event, "_aran_original_raw_message", None) and hasattr(event.message_obj, "raw_message"):
            try:
                event._aran_original_raw_message = event.message_obj.raw_message
            except Exception:
                pass
        if not getattr(event, "_aran_original_message_chain", None) and hasattr(event.message_obj, "message"):
            try:
                event._aran_original_message_chain = list(event.message_obj.message or [])
            except Exception:
                pass
        if not str(getattr(event, "message_str", "") or "").strip():
            event.message_str = fallback_message
        event.is_wake = True
        event.is_at_or_wake_command = True
        logger.info(
            f"[{PLUGIN_NAME}] 白名单群链接自动唤醒已触发: {self._session_key(event)}"
        )

    @filter.on_llm_tool_respond()
    async def remember_tool_result(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
        tool_result: Any,
    ):
        tool_name = str(getattr(tool, "name", "") or "").strip()
        if not tool_name:
            return
        tool_text = self._tool_result_to_text(tool_result)
        if not tool_text:
            return
        payload = self._tool_result_payload(tool_result)
        success = self._looks_success(tool_text)
        if not success and not self._looks_failure(tool_text):
            return
        summary = self._build_tool_awareness_summary(
            tool_name,
            tool_args or {},
            tool_text,
            success=success,
            payload=payload,
        )
        self._remember_action(
            event,
            summary=summary,
            source="on_llm_tool_respond",
            tool_name=tool_name,
        )

    @filter.after_message_sent()
    async def remember_sent_result(self, event: AstrMessageEvent):
        try:
            result = event.get_result()
        except Exception:
            return
        if not result:
            return
        summary = self._infer_sent_action(event, result)
        if not summary:
            return
        self._remember_action(
            event,
            summary=summary,
            source="after_message_sent",
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("管理一轮", alias={"/管理一轮"})
    async def grant_admin_once(self, event: AstrMessageEvent, note: str = ""):
        payload = self._write_admin_once_grant(event, note)
        note_line = f"\n- 备注: {payload.get('note')}" if payload.get("note") else ""
        yield event.plain_result(
            "已开启一次性管理授权。\n"
            f"- 剩余次数: {payload.get('remaining_uses')}\n"
            f"- 有效期: {payload.get('remaining_seconds')} 秒{note_line}\n"
            "接下来如果确实需要执行管理动作，阿然应只调用 run_admin_action_once，执行一次后会自动失效。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消管理授权", alias={"/取消管理授权"})
    async def clear_admin_once(self, event: AstrMessageEvent):
        cleared = self._clear_admin_once_grant()
        if cleared:
            yield event.plain_result("已取消一次性管理授权。")
            return
        yield event.plain_result("当前没有生效中的一次性管理授权。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("管理授权状态", alias={"/管理授权状态"})
    async def admin_once_status(self, event: AstrMessageEvent):
        payload = self._load_admin_once_grant()
        yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))
