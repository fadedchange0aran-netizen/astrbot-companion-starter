import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import tarfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn

def load_env_file(env_file: Path) -> None:
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


BASE_DIR = Path(__file__).resolve().parent
BASE_RUNTIME_DIR = BASE_DIR / "runtime"
ENV_FILE = Path(os.getenv("ARANBOX_ENV_FILE", str(BASE_DIR / ".env"))).expanduser()
load_env_file(ENV_FILE)


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_json_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError(f"{name} must be a JSON array")
    result: list[str] = []
    for item in payload:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_SAFE_DEPLOY_ALLOWED_ACTIONS: tuple[str, ...] = (
    "repo_git_status",
    "sync_astrbot_plugins",
    "restore_astrbot_plugins_latest_snapshot",
    "deploy_astrbot_plugins_safe",
    "restart_astrbot_container",
    "restart_adapter_service",
    "restart_adapter_service_safe",
    "restart_mcp_service",
)


DEFAULT_SAFE_DEPLOY_GROUPS: tuple[str, ...] = (
    "inspect",
    "plugin_deploy",
    "service_restart",
)

DEFAULT_SHARED_ASTRBOT_DATA_ROOT = "/AstrBot/data"
DEFAULT_ENABLED_TOOL_LAYERS: tuple[str, ...] = ("daily", "extended")
DEFAULT_TOOL_LAYER_STATE_PATH = f"{DEFAULT_SHARED_ASTRBOT_DATA_ROOT}/mcp_tool_layers_state.json"

TOOL_LAYER_DEFINITIONS: dict[str, dict[str, str]] = {
    "daily": {
        "name": "daily",
        "label": "日常层",
        "description": "默认暴露的高频工具，覆盖记忆、文件暂存、基础读写和少量常用查询。",
    },
    "extended": {
        "name": "extended",
        "label": "扩展层",
        "description": "按需开启的外部集成工具，例如 Notion、邮件、地图、网页生成和备份浏览。",
    },
    "admin": {
        "name": "admin",
        "label": "管理层",
        "description": "运维和部署相关工具，默认不暴露给日常对话。",
    },
}


def sanitize_tool_layers(layer_names: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in layer_names:
        name = str(item or "").strip().lower()
        if not name or name in seen or name not in TOOL_LAYER_DEFINITIONS:
            continue
        seen.add(name)
        result.append(name)
    return result


def load_tool_layer_state_payload() -> dict[str, Any]:
    enabled_layers = list(SETTINGS.enabled_tool_layers)
    source = "env_defaults"
    state_path = SETTINGS.tool_layer_state_path
    if state_path.exists():
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("enabled_layers"), list):
            enabled_layers = sanitize_tool_layers(payload["enabled_layers"]) or []
            source = "state_file"
    return {
        "enabled_layers": enabled_layers,
        "state_path": str(state_path),
        "source": source,
    }


def write_tool_layer_state(enabled_layers: list[str], updated_by: str = "") -> dict[str, Any]:
    sanitized_layers = sanitize_tool_layers(enabled_layers)
    SETTINGS.tool_layer_state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled_layers": sanitized_layers,
        "updated_at": now_iso(),
        "updated_by": updated_by or None,
    }
    SETTINGS.tool_layer_state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return load_tool_layer_state_payload()


def current_enabled_tool_layers() -> set[str]:
    return set(load_tool_layer_state_payload()["enabled_layers"])


def is_tool_layer_enabled(layer_name: str) -> bool:
    return layer_name in current_enabled_tool_layers()


def try_consume_admin_once_for_tool(tool_name: str) -> dict[str, Any] | None:
    grant = load_admin_once_grant_payload()
    if not grant.get("active"):
        return None
    return consume_admin_once_grant_payload(tool_name)


@dataclass(frozen=True)
class Settings:
    adapter_url: str
    adapter_token: str
    backup_token: str
    backup_root: Path
    transcript_root: Path
    qq_chat_backup_root: Path
    shared_memory_root: Path
    playground_root: Path
    repo_workspace_root: Path
    file_vault_root: Path
    astrbot_attachments_root: Path
    astrbot_temp_root: Path
    astrbot_plugin_data_root: Path
    memory_db_path: Path
    safe_deploy_enabled: bool
    safe_deploy_allowed_actions: tuple[str, ...]
    safe_deploy_policy_path: Path
    tool_access_requests_path: Path
    enabled_tool_layers: tuple[str, ...]
    tool_layer_state_path: Path
    request_timeout_seconds: int
    default_owner_id: str
    pushplus_token: str
    pushplus_topic: str
    bind_host: str
    bind_port: int


def get_settings() -> Settings:
    adapter_url = env_str("ARANBOX_ADAPTER_URL", "http://127.0.0.1:8001").rstrip("/")
    adapter_token = env_str("ARANBOX_ADAPTER_TOKEN") or env_str("ARAN_ADAPTER_TOKEN")
    backup_token = (
        env_str("ARANBOX_BACKUP_TOKEN")
        or env_str("ARAN_MANUAL_BACKUP_TOKEN")
        or adapter_token
    )
    backup_root = Path(
        env_str(
            "ARANBOX_BACKUP_ROOT",
            str(BASE_RUNTIME_DIR / "adapter" / "backups"),
        )
    ).expanduser()
    transcript_root = Path(
        env_str(
            "ARANBOX_TRANSCRIPT_ROOT",
            str(BASE_RUNTIME_DIR / "adapter" / "transcripts"),
        )
    ).expanduser()
    qq_chat_backup_root = Path(
        env_str(
            "ARANBOX_QQ_CHAT_BACKUP_ROOT",
            env_str("ARAN_QQ_CHAT_BACKUP_ROOT", str(BASE_RUNTIME_DIR / "astrbot" / "qq_chat_backups")),
        )
    ).expanduser()
    shared_memory_root = Path(
        env_str(
            "ARANBOX_SHARED_MEMORY_ROOT",
            str(BASE_RUNTIME_DIR / "astrbot" / "aran_memory"),
        )
    ).expanduser()
    playground_root = Path(
        env_str(
            "ARANBOX_PLAYGROUND_ROOT",
            str(BASE_RUNTIME_DIR / "playground"),
        )
    ).expanduser()
    repo_workspace_root = Path(
        env_str(
            "ARANBOX_REPO_WORKSPACE_ROOT",
            str(BASE_RUNTIME_DIR / "repo_workspace"),
        )
    ).expanduser()
    file_vault_root = Path(
        env_str(
            "ARANBOX_FILE_VAULT_ROOT",
            str(BASE_RUNTIME_DIR / "astrbot" / "file_vault"),
        )
    ).expanduser()
    astrbot_attachments_root = Path(
        env_str(
            "ARANBOX_ASTRBOT_ATTACHMENTS_ROOT",
            str(BASE_RUNTIME_DIR / "astrbot" / "attachments"),
        )
    ).expanduser()
    astrbot_temp_root = Path(
        env_str(
            "ARANBOX_ASTRBOT_TEMP_ROOT",
            str(BASE_RUNTIME_DIR / "astrbot" / "temp"),
        )
    ).expanduser()
    astrbot_plugin_data_root = Path(
        env_str(
            "ARANBOX_ASTRBOT_PLUGIN_DATA_ROOT",
            str(BASE_RUNTIME_DIR / "astrbot" / "plugin_data"),
        )
    ).expanduser()
    memory_db_path = Path(
        env_str(
            "ARANBOX_MEMORY_DB_PATH",
            str(shared_memory_root / "memory_manager.db"),
        )
    ).expanduser()
    safe_deploy_enabled = env_bool("ARANBOX_SAFE_DEPLOY_ENABLED", False)
    safe_deploy_allowed_actions = tuple(
        env_json_list(
            "ARANBOX_SAFE_DEPLOY_ALLOWED_ACTIONS_JSON",
            list(DEFAULT_SAFE_DEPLOY_ALLOWED_ACTIONS),
        )
    )
    safe_deploy_policy_path = Path(
        env_str(
            "ARANBOX_SAFE_DEPLOY_POLICY_PATH",
            f"{DEFAULT_SHARED_ASTRBOT_DATA_ROOT}/mcp_safe_deploy_policy.json",
        )
    ).expanduser()
    tool_access_requests_path = Path(
        env_str(
            "ARANBOX_TOOL_ACCESS_REQUESTS_PATH",
            str(BASE_RUNTIME_DIR / "astrbot" / "mcp_tool_access_requests.jsonl"),
        )
    ).expanduser()
    enabled_tool_layers = tuple(
        sanitize_tool_layers(
            env_json_list(
                "ARANBOX_ENABLED_TOOL_LAYERS_JSON",
                list(DEFAULT_ENABLED_TOOL_LAYERS),
            )
        )
    ) or DEFAULT_ENABLED_TOOL_LAYERS
    tool_layer_state_path = Path(
        env_str(
            "ARANBOX_TOOL_LAYER_STATE_PATH",
            DEFAULT_TOOL_LAYER_STATE_PATH,
        )
    ).expanduser()
    request_timeout_seconds = env_int("ARANBOX_REQUEST_TIMEOUT_SECONDS", 30)
    default_owner_id = env_str("ARANBOX_DEFAULT_OWNER_ID", "owner") or "owner"
    pushplus_token = env_str("ARAN_PUSHPLUS_TOKEN") or env_str("PUSHPLUS_TOKEN")
    pushplus_topic = env_str("ARAN_PUSHPLUS_TOPIC")
    bind_host = env_str("ARAN_MCP_BIND_HOST") or env_str("ARANBOX_BIND_HOST", "0.0.0.0")
    bind_port = env_int(
        "ARAN_MCP_BIND_PORT",
        env_int("ARANBOX_BIND_PORT", 9001),
    )
    return Settings(
        adapter_url=adapter_url,
        adapter_token=adapter_token,
        backup_token=backup_token,
        backup_root=backup_root,
        transcript_root=transcript_root,
        qq_chat_backup_root=qq_chat_backup_root,
        shared_memory_root=shared_memory_root,
        playground_root=playground_root,
        repo_workspace_root=repo_workspace_root,
        file_vault_root=file_vault_root,
        astrbot_attachments_root=astrbot_attachments_root,
        astrbot_temp_root=astrbot_temp_root,
        astrbot_plugin_data_root=astrbot_plugin_data_root,
        memory_db_path=memory_db_path,
        safe_deploy_enabled=safe_deploy_enabled,
        safe_deploy_allowed_actions=safe_deploy_allowed_actions,
        safe_deploy_policy_path=safe_deploy_policy_path,
        tool_access_requests_path=tool_access_requests_path,
        enabled_tool_layers=enabled_tool_layers,
        tool_layer_state_path=tool_layer_state_path,
        request_timeout_seconds=request_timeout_seconds,
        default_owner_id=default_owner_id,
        pushplus_token=pushplus_token,
        pushplus_topic=pushplus_topic,
        bind_host=bind_host,
        bind_port=bind_port,
    )


SETTINGS = get_settings()
PUBLIC_PAGES_ROOT = Path(__file__).resolve().parent / "public" / "pages"
mcp = FastMCP(
    "Companion_AstrBot_MCP",
    host=SETTINGS.bind_host,
    port=SETTINGS.bind_port,
)
MCP_TOOL_REGISTRY: list[dict[str, Any]] = []
_ORIGINAL_LIST_TOOLS = mcp._tool_manager.list_tools


def _filtered_list_tools():
    enabled_layers = current_enabled_tool_layers()
    tool_layers = {item["name"]: item["layer"] for item in MCP_TOOL_REGISTRY}
    visible_tools = []
    for tool in _ORIGINAL_LIST_TOOLS():
        layer = tool_layers.get(tool.name)
        if layer and layer not in enabled_layers:
            continue
        visible_tools.append(tool)
    return visible_tools


mcp._tool_manager.list_tools = _filtered_list_tools


def register_mcp_tool(*, layer: str):
    resolved_layers = sanitize_tool_layers([layer])
    if not resolved_layers:
        raise ValueError(f"unsupported MCP tool layer: {layer}")
    resolved_layer = resolved_layers[0]

    def decorator(func):
        MCP_TOOL_REGISTRY.append(
            {
                "name": func.__name__,
                "layer": resolved_layer,
                "doc": (func.__doc__ or "").strip(),
            }
        )
        @wraps(func)
        async def guarded(*args, **kwargs):
            if not is_tool_layer_enabled(resolved_layer):
                if resolved_layer == "admin":
                    consumed = try_consume_admin_once_for_tool(func.__name__)
                    if consumed and consumed.get("consumed"):
                        return await func(*args, **kwargs)
                raise RuntimeError(
                    f"MCP tool layer is disabled: {resolved_layer}. "
                    "Please enable the layer first via command or admin control page, "
                    "or grant one-shot admin access via /管理一轮."
                )
            return await func(*args, **kwargs)

        return mcp.tool()(guarded)

    return decorator


def json_pretty(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


NotionCategoryParam = Annotated[
    str,
    "Notion 分类。优先填写。推荐值：love_diary=恋爱日记，timeline=时间线，memo=备忘录，todo=待办，memory_profile=记忆设定，archive=资料档案，creation=创作，inbox=待整理，discard=作废与重复。不确定时可留空，系统会自动路由；若仍不确定，优先填 inbox。",
]
NotionCreationTypeParam = Annotated[
    str,
    "仅在 category=creation 时使用。推荐值：series=系列，short=短篇，extra=彩蛋。其他分类请留空。",
]
NotionDateHintParam = Annotated[
    str,
    "可选日期提示，如 2026-06-10、2026/06/10、2026-06 或 6月10日。用于帮助路由到正确的年/月/日页面。",
]
NotionSeriesNameParam = Annotated[
    str,
    "仅在 category=creation 且作品属于某个系列时填写系列名；其他情况可留空。",
]

ARAN_VAULT_DATABASE_URL = "https://app.notion.com/p/384ee01d305680b1a71bd5f031bc77f5?v=384ee01d305680b4a11e000cbb67317c"


def _contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def resolve_notion_write_category(category: str = "", title: str = "", content: str = "") -> tuple[str, str]:
    normalized = str(category or "").strip()
    if normalized:
        return normalized, "explicit"

    combined = " ".join([str(title or "").strip(), str(content or "").strip()]).lower()

    if _contains_any_keyword(
        combined,
        (
            "恋爱日记",
            "恋爱记录",
            "约会记录",
            "情侣日记",
        ),
    ):
        return "love_diary", "auto:恋爱日记"

    if _contains_any_keyword(
        combined,
        (
            "时间线",
            "今天发生",
            "刚刚发生",
            "今日记录",
            "经历记录",
            "事件记录",
            "纪念日",
        ),
    ):
        return "timeline", "auto:时间线"

    if _contains_any_keyword(
        combined,
        (
            "待办",
            "todo",
            "提醒我",
            "需要做",
            "要做",
            "记得要",
            "之后要",
        ),
    ):
        return "todo", "auto:待办"

    if _contains_any_keyword(
        combined,
        (
            "备忘录",
            "备忘",
            "记一下",
            "记个",
            "随手记",
            "记住这条",
        ),
    ):
        return "memo", "auto:备忘录"

    if _contains_any_keyword(
        combined,
        (
            "记忆设定",
            "人设",
            "口癖",
            "喜好设定",
            "偏好设定",
            "关系设定",
            "角色设定",
        ),
    ):
        return "memory_profile", "auto:记忆设定"

    if _contains_any_keyword(
        combined,
        (
            "资料档案",
            "资料",
            "档案",
            "背景资料",
            "参考资料",
            "人物资料",
        ),
    ):
        return "archive", "auto:资料档案"

    if _contains_any_keyword(
        combined,
        (
            "创作",
            "小说",
            "故事",
            "短篇",
            "系列",
            "彩蛋",
            "剧情",
            "正文",
            "番外",
        ),
    ):
        return "creation", "auto:创作"

    if _contains_any_keyword(
        combined,
        (
            "作废",
            "重复",
            "无效",
            "忽略这条",
            "废弃",
        ),
    ):
        return "discard", "auto:作废与重复"

    return "inbox", "auto:default_inbox"


def mcp_tool_layer_status_payload() -> dict[str, Any]:
    state = load_tool_layer_state_payload()
    enabled_layers = set(state["enabled_layers"])
    layers: list[dict[str, Any]] = []
    for name, info in TOOL_LAYER_DEFINITIONS.items():
        tools = [
            item["name"]
            for item in MCP_TOOL_REGISTRY
            if item["layer"] == name
        ]
        layers.append(
            {
                **info,
                "enabled": name in enabled_layers,
                "tool_count": len(tools),
                "tools": tools,
            }
        )
    return {
        "enabled_layers": list(state["enabled_layers"]),
        "state_path": state["state_path"],
        "source": state["source"],
        "available_layers": layers,
    }


def parse_csv_values(raw_value: str) -> list[str]:
    raw = (raw_value or "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    values: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def admin_once_grant_path() -> Path:
    return SETTINGS.astrbot_plugin_data_root / "astrbot_plugin_link_context" / "admin_once_access.json"


def load_admin_once_grant_payload() -> dict[str, Any]:
    path = admin_once_grant_path()
    if not path.exists():
        return {
            "active": False,
            "exists": False,
            "grant_path": str(path),
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "active": False,
            "exists": True,
            "invalid": True,
            "grant_path": str(path),
        }
    now_ts = time.time()
    expires_at_ts = float(payload.get("expires_at_ts") or 0)
    remaining_uses = int(payload.get("remaining_uses") or 0)
    active = bool(expires_at_ts > now_ts and remaining_uses > 0)
    payload["active"] = active
    payload["exists"] = True
    payload["grant_path"] = str(path)
    payload["remaining_seconds"] = max(0, int(expires_at_ts - now_ts)) if expires_at_ts else 0
    return payload


def write_admin_once_grant_payload(granted_by: str, note: str = "", ttl_seconds: int = 600) -> dict[str, Any]:
    path = admin_once_grant_path()
    now_ts = time.time()
    expires_at_ts = now_ts + max(30, int(ttl_seconds))
    payload = {
        "granted": True,
        "granted_at": datetime.fromtimestamp(now_ts).isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at_ts).isoformat(),
        "granted_at_ts": now_ts,
        "expires_at_ts": expires_at_ts,
        "remaining_uses": 1,
        "granted_by": granted_by or "admin-api",
        "granted_by_name": granted_by or "admin-api",
        "note": str(note or "").strip()[:120],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return load_admin_once_grant_payload()


def clear_admin_once_grant_payload() -> dict[str, Any]:
    path = admin_once_grant_path()
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {
        "active": False,
        "cleared": existed,
        "grant_path": str(path),
    }


def consume_admin_once_grant_payload(action_name: str) -> dict[str, Any]:
    path = admin_once_grant_path()
    payload = load_admin_once_grant_payload()
    if not payload.get("active"):
        raise RuntimeError("missing active one-shot admin grant")
    payload["remaining_uses"] = max(0, int(payload.get("remaining_uses") or 0) - 1)
    payload["last_used_at"] = now_iso()
    payload["last_action"] = action_name
    if int(payload.get("remaining_uses") or 0) <= 0:
        path.unlink(missing_ok=True)
        payload["consumed"] = True
        payload["active"] = False
        payload["remaining_seconds"] = 0
        return payload
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    payload["consumed"] = False
    return payload


def admin_api_token() -> str:
    return (
        env_str("ARANBOX_ADMIN_CONTROL_TOKEN")
        or SETTINGS.adapter_token
        or env_str("ARAN_ADAPTER_TOKEN")
    )


def validate_admin_api_request(request: Request) -> str | None:
    expected_token = admin_api_token()
    if not expected_token:
        return "admin control token is not configured"
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return "missing bearer token"
    provided_token = auth_header[7:].strip()
    if provided_token != expected_token:
        return "invalid bearer token"
    return None


async def create_admin_tool_access_request_payload(
    reason: str,
    requested_tools_csv: str,
) -> dict[str, Any]:
    cleaned_reason = reason.strip()
    if not cleaned_reason:
        raise ValueError("reason is required")
    requested_tools = parse_csv_values(requested_tools_csv)
    request_id = f"admin-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    payload = {
        "request_id": request_id,
        "requested_at": now_iso(),
        "reason": cleaned_reason,
        "requested_tools": requested_tools,
        "enabled_layers": list(load_tool_layer_state_payload()["enabled_layers"]),
        "status": "pending_user_confirmation",
    }
    SETTINGS.tool_access_requests_path.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS.tool_access_requests_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    notification_sent = False
    notification_error = ""
    if SETTINGS.pushplus_token:
        try:
            await pushplus_notify(
                title="机器人申请管理工具权限",
                content=json_pretty(payload),
            )
            notification_sent = True
        except Exception as exc:
            notification_error = str(exc)

    return {
        **payload,
        "request_log_path": str(SETTINGS.tool_access_requests_path),
        "notification_sent": notification_sent,
        "notification_error": notification_error or None,
        "next_step": "由用户确认后，再手动开启 admin 层或执行对应管理操作。",
    }


def repo_root() -> Path:
    return SETTINGS.repo_workspace_root.parent.resolve()


def safe_deploy_action_catalog(repo: Path) -> dict[str, dict[str, Any]]:
    return {
        "repo_git_status": {
            "name": "repo_git_status",
            "description": "Run git status in the managed repo root.",
            "cwd": str(repo),
            "command": ["git", "status", "--short"],
        },
        "sync_astrbot_plugins": {
            "name": "sync_astrbot_plugins",
            "description": "Sync managed plugin patches through staging into the live AstrBot plugins directory. A snapshot of current live plugins is created before overwrite.",
            "cwd": str(repo),
            "command": ["bash", str(repo / "deploy" / "scripts" / "sync-astrbot-plugins.sh")],
            "post_checks": ["py_compile inside staging script", "snapshot created before overwrite"],
        },
        "restore_astrbot_plugins_latest_snapshot": {
            "name": "restore_astrbot_plugins_latest_snapshot",
            "description": "Restore the most recent live plugin snapshot created by sync_astrbot_plugins.",
            "cwd": str(repo),
            "command": [
                "bash",
                str(repo / "deploy" / "scripts" / "restore-astrbot-plugins-snapshot.sh"),
                "latest",
            ],
        },
        "deploy_astrbot_plugins_safe": {
            "name": "deploy_astrbot_plugins_safe",
            "description": "Sync managed plugins, restart AstrBot, and automatically restore the latest plugin snapshot if startup checks fail.",
            "cwd": str(repo),
            "command": [
                "bash",
                str(repo / "deploy" / "scripts" / "deploy-astrbot-plugins-safe.sh"),
            ],
            "post_checks": [
                "staging sync",
                "snapshot before overwrite",
                "container running",
                "no Traceback in startup logs",
                "auto rollback on failure",
            ],
        },
        "restart_astrbot_container": {
            "name": "restart_astrbot_container",
            "description": "Restart the live AstrBot Docker container and check whether it is running again.",
            "cwd": str(BASE_DIR.parents[1]),
            "command": ["docker", "restart", "astrbot"],
            "post_checks": ["docker inspect running state"],
        },
        "restart_adapter_service_safe": {
            "name": "restart_adapter_service_safe",
            "description": "Restart aran-adapter.service with health checks and restore the pre-restart .env snapshot if checks fail.",
            "cwd": str(BASE_DIR.parents[1]),
            "command": [
                "bash",
                str(repo / "deploy" / "scripts" / "restart-adapter-safe.sh"),
            ],
            "post_checks": [
                "snapshot adapter .env",
                "systemctl is-active",
                "adapter /healthz",
                "adapter /readyz",
                "auto restore .env on failure",
            ],
        },
        "restart_adapter_service": {
            "name": "restart_adapter_service",
            "description": "Restart systemd service aran-adapter.service and verify health/readiness.",
            "cwd": str(BASE_DIR.parents[1]),
            "command": ["systemctl", "restart", "aran-adapter.service"],
            "post_checks": ["systemctl is-active", "adapter /healthz", "adapter /readyz"],
        },
        "restart_mcp_service": {
            "name": "restart_mcp_service",
            "description": "Restart systemd service aran-mcp.service. The caller connection may close during restart.",
            "cwd": str(BASE_DIR.parents[1]),
            "command": ["systemctl", "restart", "aran-mcp.service"],
        },
    }


def safe_deploy_group_catalog() -> dict[str, dict[str, Any]]:
    return {
        "inspect": {
            "name": "inspect",
            "label": "只读检查",
            "description": "只查看仓库或部署状态，不执行写入和重启。",
            "actions": ["repo_git_status"],
        },
        "plugin_deploy": {
            "name": "plugin_deploy",
            "label": "插件部署",
            "description": "允许同步插件、恢复最近一次插件快照，以及执行组合安全部署。",
            "actions": [
                "sync_astrbot_plugins",
                "restore_astrbot_plugins_latest_snapshot",
                "deploy_astrbot_plugins_safe",
            ],
        },
        "service_restart": {
            "name": "service_restart",
            "label": "服务重启",
            "description": "允许重启 AstrBot、adapter、mcp 等目标服务，但仍不开放任意 shell。",
            "actions": [
                "restart_astrbot_container",
                "restart_adapter_service",
                "restart_adapter_service_safe",
                "restart_mcp_service",
            ],
        },
    }


def sanitize_allowed_safe_deploy_actions(action_names: list[str], repo: Path) -> list[str]:
    catalog = safe_deploy_action_catalog(repo)
    result: list[str] = []
    seen: set[str] = set()
    for item in action_names:
        name = str(item or "").strip()
        if not name or name in seen or name not in catalog:
            continue
        seen.add(name)
        result.append(name)
    return result


def sanitize_allowed_safe_deploy_groups(group_names: list[str]) -> list[str]:
    catalog = safe_deploy_group_catalog()
    result: list[str] = []
    seen: set[str] = set()
    for item in group_names:
        name = str(item or "").strip()
        if not name or name in seen or name not in catalog:
            continue
        seen.add(name)
        result.append(name)
    return result


def expand_safe_deploy_groups(group_names: list[str]) -> list[str]:
    catalog = safe_deploy_group_catalog()
    actions: list[str] = []
    seen: set[str] = set()
    for name in sanitize_allowed_safe_deploy_groups(group_names):
        for action_name in catalog[name]["actions"]:
            if action_name in seen:
                continue
            seen.add(action_name)
            actions.append(action_name)
    return actions


def infer_safe_deploy_groups_and_extra_actions(
    action_names: list[str],
    repo: Path,
) -> tuple[list[str], list[str], list[str]]:
    sanitized_actions = sanitize_allowed_safe_deploy_actions(action_names, repo)
    group_catalog = safe_deploy_group_catalog()
    action_set = set(sanitized_actions)
    allowed_groups = [
        name
        for name, info in group_catalog.items()
        if all(action_name in action_set for action_name in info["actions"])
    ]
    grouped_actions = set(expand_safe_deploy_groups(allowed_groups))
    extra_actions = [name for name in sanitized_actions if name not in grouped_actions]
    return allowed_groups, extra_actions, sanitized_actions


def load_safe_deploy_policy_payload() -> dict[str, Any]:
    repo = repo_root()
    catalog = safe_deploy_action_catalog(repo)
    group_catalog = safe_deploy_group_catalog()
    enabled = SETTINGS.safe_deploy_enabled
    allowed_groups = sanitize_allowed_safe_deploy_groups(list(DEFAULT_SAFE_DEPLOY_GROUPS))
    extra_actions: list[str] = []
    allowed_actions = sanitize_allowed_safe_deploy_actions(
        list(SETTINGS.safe_deploy_allowed_actions),
        repo,
    )
    source = "env_defaults"
    if SETTINGS.safe_deploy_policy_path.exists():
        payload = json.loads(SETTINGS.safe_deploy_policy_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            if isinstance(payload.get("enabled"), bool):
                enabled = payload["enabled"]
            if isinstance(payload.get("allowed_groups"), list):
                allowed_groups = sanitize_allowed_safe_deploy_groups(payload["allowed_groups"])
            if isinstance(payload.get("extra_actions"), list):
                extra_actions = sanitize_allowed_safe_deploy_actions(
                    payload["extra_actions"],
                    repo,
                )
            if isinstance(payload.get("allowed_actions"), list):
                allowed_actions = sanitize_allowed_safe_deploy_actions(
                    payload["allowed_actions"],
                    repo,
                )
            else:
                group_actions = expand_safe_deploy_groups(allowed_groups)
                allowed_actions = sanitize_allowed_safe_deploy_actions(
                    group_actions + extra_actions,
                    repo,
                )
            source = "policy_file"
    else:
        allowed_groups, extra_actions, allowed_actions = infer_safe_deploy_groups_and_extra_actions(
            allowed_actions,
            repo,
        )
    return {
        "enabled": enabled,
        "allowed_groups": allowed_groups,
        "extra_actions": extra_actions,
        "allowed_actions": allowed_actions,
        "policy_path": str(SETTINGS.safe_deploy_policy_path),
        "source": source,
        "available_groups": list(group_catalog.values()),
        "available_actions": list(catalog.keys()),
    }


def write_safe_deploy_policy(
    enabled: bool,
    allowed_actions: list[str],
    allowed_groups: list[str] | None = None,
) -> dict[str, Any]:
    repo = repo_root()
    if allowed_groups is None:
        sanitized_groups, sanitized_extra_actions, sanitized = infer_safe_deploy_groups_and_extra_actions(
            allowed_actions,
            repo,
        )
    else:
        sanitized_groups = sanitize_allowed_safe_deploy_groups(allowed_groups)
        grouped_actions = expand_safe_deploy_groups(sanitized_groups)
        sanitized_extra_actions = sanitize_allowed_safe_deploy_actions(allowed_actions, repo)
        sanitized_extra_actions = [
            name for name in sanitized_extra_actions if name not in set(grouped_actions)
        ]
        sanitized = sanitize_allowed_safe_deploy_actions(
            grouped_actions + sanitized_extra_actions,
            repo,
        )
    SETTINGS.safe_deploy_policy_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(enabled),
        "allowed_groups": sanitized_groups,
        "extra_actions": sanitized_extra_actions,
        "allowed_actions": sanitized,
        "updated_at": now_iso(),
    }
    SETTINGS.safe_deploy_policy_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return load_safe_deploy_policy_payload()


def parse_safe_deploy_actions_csv(raw_value: str) -> list[str]:
    raw = (raw_value or "").strip()
    if not raw:
        return []
    if raw.lower() in {"none", "__none__"}:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_safe_deploy_groups_csv(raw_value: str) -> list[str]:
    return parse_safe_deploy_actions_csv(raw_value)


def normalize_owner_id(owner_id: str | None) -> str:
    value = (owner_id or "").strip()
    return value or SETTINGS.default_owner_id


def safe_owner_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned or SETTINGS.default_owner_id


def owner_memory_dir(owner_id: str | None) -> Path:
    return SETTINGS.shared_memory_root / safe_owner_part(normalize_owner_id(owner_id))


def allowed_memory_file(file_name: str, *, writable: bool) -> Path:
    candidate = Path(file_name)
    if candidate.name != file_name or candidate.is_absolute():
        raise ValueError("file_name must be a simple basename")
    if writable and candidate.suffix.lower() != ".md":
        raise ValueError("write only supports .md memory files")
    if candidate.suffix.lower() not in {".md", ".jsonl"}:
        raise ValueError("supported suffixes: .md, .jsonl")
    return candidate


def resolve_playground_path(relative_path: str, *, writable: bool) -> Path:
    raw_value = (relative_path or "").strip()
    if not raw_value:
        raise ValueError("relative_path is required")

    candidate = Path(raw_value)
    if candidate.is_absolute():
        raise ValueError("relative_path must not be absolute")

    suffix = candidate.suffix.lower()
    if suffix not in {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml"}:
        raise ValueError("supported suffixes: .md, .txt, .json, .jsonl, .yaml, .yml")

    root = SETTINGS.playground_root.resolve()
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("relative_path escapes playground root") from exc

    if writable:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def bookshelf_data_root() -> Path:
    return SETTINGS.astrbot_plugin_data_root / "astrbot_plugin_bookshelf"


def resolve_bookshelf_path(relative_path: str) -> Path:
    raw_value = (relative_path or "").strip()
    if not raw_value:
        raise ValueError("relative_path is required")

    candidate = Path(raw_value)
    if candidate.is_absolute():
        raise ValueError("relative_path must not be absolute")

    root = bookshelf_data_root().resolve()
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("relative_path escapes bookshelf root") from exc
    if not target.exists():
        raise FileNotFoundError(f"bookshelf file not found: {target}")
    if not target.is_file():
        raise FileNotFoundError(f"bookshelf path is not a file: {target}")
    return target


def list_bookshelf_files_payload() -> dict[str, Any]:
    root = bookshelf_data_root()
    items: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            items.append(
                {
                    "path": str(path.relative_to(root)),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
    return {
        "bookshelf_root": str(root),
        "exists": root.exists(),
        "items": items,
    }


def managed_file_sources_catalog() -> dict[str, dict[str, str]]:
    return {
        "file_vault": {
            "label": "受管文件库",
            "description": "已经纳入 file_vault 的文件副本，适合后续继续预览、邮件转发或 QQ 转发。",
        },
        "playground": {
            "label": "共享读写区",
            "description": "机器人可读写的 playground 目录。",
        },
        "bookshelf": {
            "label": "共读目录",
            "description": "astrbot_plugin_bookshelf 的插件数据目录。",
        },
        "memory": {
            "label": "长期代存目录",
            "description": "机器人长期代存文件目录。",
        },
    }


def normalize_managed_file_source(source: str) -> str:
    normalized = str(source or "").strip().lower() or "all"
    allowed = {"all", *managed_file_sources_catalog().keys()}
    if normalized not in allowed:
        raise ValueError(f"unsupported source: {source}")
    return normalized


def managed_file_keyword_match(values: list[str], keyword: str) -> bool:
    normalized_keyword = keyword.strip().lower()
    if not normalized_keyword:
        return True
    return any(normalized_keyword in str(value or "").lower() for value in values)


def list_regular_files_payload(
    *,
    source: str,
    root: Path,
    limit: int,
    keyword: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not root.exists():
        return items
    for path in sorted(root.rglob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_file():
            continue
        relative_path = str(path.relative_to(root))
        display_path = f"{source}/{relative_path}"
        if not managed_file_keyword_match([relative_path, display_path, path.name], keyword):
            continue
        stat = path.stat()
        items.append(
            {
                "source": source,
                "path": display_path,
                "relative_path": relative_path,
                "file_name": path.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
        if len(items) >= limit:
            break
    return items


def list_file_vault_browse_items(limit: int, keyword: str) -> list[dict[str, Any]]:
    ensure_file_vault_dirs()
    items: list[dict[str, Any]] = []
    for path in sorted(
        file_vault_items_root().glob("*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        summary = summarize_file_vault_item(payload)
        if not managed_file_keyword_match(
            [
                summary.get("item_id", ""),
                summary.get("title", ""),
                summary.get("original_name", ""),
                summary.get("stored_rel_path", ""),
            ],
            keyword,
        ):
            continue
        items.append(
            {
                "source": "file_vault",
                "path": str(summary.get("stored_rel_path") or ""),
                "relative_path": str(summary.get("stored_rel_path") or ""),
                "file_name": str(summary.get("original_name") or summary.get("title") or ""),
                "item_id": summary.get("item_id"),
                "title": summary.get("title"),
                "size_bytes": summary.get("bytes"),
                "modified_at": summary.get("stored_at"),
                "mime_type": summary.get("mime_type"),
                "source_kind": summary.get("source_kind"),
                "stored_path": summary.get("stored_path"),
            }
        )
        if len(items) >= limit:
            break
    return items


def list_managed_files_payload(source: str, limit: int, keyword: str) -> dict[str, Any]:
    resolved_source = normalize_managed_file_source(source)
    resolved_limit = max(1, min(int(limit), 100))
    resolved_keyword = str(keyword or "").strip()
    catalog = managed_file_sources_catalog()
    roots = {
        "playground": SETTINGS.playground_root,
        "bookshelf": bookshelf_data_root(),
        "memory": SETTINGS.shared_memory_root,
    }

    items: list[dict[str, Any]] = []
    if resolved_source in {"all", "file_vault"}:
        items.extend(list_file_vault_browse_items(resolved_limit, resolved_keyword))
    for source_name, root in roots.items():
        if resolved_source not in {"all", source_name}:
            continue
        items.extend(
            list_regular_files_payload(
                source=source_name,
                root=root,
                limit=resolved_limit if resolved_source != "all" else max(resolved_limit, 50),
                keyword=resolved_keyword,
            )
        )

    items.sort(key=lambda item: str(item.get("modified_at") or ""), reverse=True)
    items = items[:resolved_limit]
    return {
        "checked_at": now_iso(),
        "source": resolved_source,
        "limit": resolved_limit,
        "keyword": resolved_keyword or None,
        "available_sources": catalog,
        "count": len(items),
        "items": items,
    }


def list_playground_files_payload() -> dict[str, Any]:
    root = SETTINGS.playground_root
    root.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        items.append(
            {
                "path": str(path.relative_to(root)),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return {
        "playground_root": str(root),
        "items": items,
    }


def resolve_repo_workspace_path(relative_path: str, *, writable: bool) -> Path:
    raw_value = (relative_path or "").strip()
    if not raw_value:
        raise ValueError("relative_path is required")

    candidate = Path(raw_value)
    if candidate.is_absolute():
        raise ValueError("relative_path must not be absolute")

    suffix = candidate.suffix.lower()
    if suffix not in {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".py", ".sh"}:
        raise ValueError(
            "supported suffixes: .md, .txt, .json, .jsonl, .yaml, .yml, .py, .sh"
        )

    root = SETTINGS.repo_workspace_root.resolve()
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("relative_path escapes repo workspace root") from exc

    if writable:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def list_repo_workspace_files_payload() -> dict[str, Any]:
    root = SETTINGS.repo_workspace_root
    root.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        items.append(
            {
                "path": str(path.relative_to(root)),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return {
        "repo_workspace_root": str(root),
        "items": items,
    }


def read_repo_workspace_text_file(relative_path: str) -> dict[str, Any]:
    path = resolve_repo_workspace_path(relative_path, writable=False)
    if not path.exists():
        raise FileNotFoundError(f"repo workspace file not found: {path}")
    content = path.read_text(encoding="utf-8")
    return {
        "repo_workspace_root": str(SETTINGS.repo_workspace_root),
        "path": str(path.relative_to(SETTINGS.repo_workspace_root.resolve())),
        "content": content,
    }


def write_repo_workspace_text_file(
    relative_path: str,
    content: str,
    append: bool,
) -> dict[str, Any]:
    path = resolve_repo_workspace_path(relative_path, writable=True)
    existing = ""
    if append and path.exists():
        existing = path.read_text(encoding="utf-8")
    final_content = f"{existing}{content}" if append else content
    path.write_text(final_content, encoding="utf-8")
    return {
        "repo_workspace_root": str(SETTINGS.repo_workspace_root),
        "path": str(path.relative_to(SETTINGS.repo_workspace_root.resolve())),
        "bytes": path.stat().st_size,
        "append": append,
    }


def safe_deploy_actions_payload() -> dict[str, Any]:
    repo = repo_root()
    policy = load_safe_deploy_policy_payload()
    catalog = safe_deploy_action_catalog(repo)
    return {
        "enabled": policy["enabled"],
        "allowed_groups": policy["allowed_groups"],
        "extra_actions": policy["extra_actions"],
        "allowed_actions": policy["allowed_actions"],
        "policy_path": policy["policy_path"],
        "policy_source": policy["source"],
        "repo_root": str(repo),
        "available_groups": policy["available_groups"],
        "actions": [catalog[name] for name in policy["allowed_actions"] if name in catalog],
    }


async def run_safe_process(command: list[str], cwd: Path) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    return {
        "command": command,
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "ok": process.returncode == 0,
    }


async def run_safe_deploy_post_checks(action_name: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if action_name in {"restart_astrbot_container", "deploy_astrbot_plugins_safe"}:
        inspect_result = await run_safe_process(
            ["docker", "inspect", "--format", "{{.State.Running}}", "astrbot"],
            BASE_DIR.parents[1],
        )
        checks.append(
            {
                "name": "docker_inspect_running",
                **inspect_result,
            }
        )
        return checks

    if action_name in {"restart_adapter_service", "restart_adapter_service_safe"}:
        active_result = await run_safe_process(
            ["systemctl", "is-active", "aran-adapter.service"],
            BASE_DIR.parents[1],
        )
        checks.append(
            {
                "name": "systemctl_is_active",
                **active_result,
            }
        )
        for endpoint in ("/healthz", "/readyz"):
            try:
                payload = await adapter_get(endpoint)
                checks.append(
                    {
                        "name": f"adapter{endpoint}",
                        "ok": True,
                        "payload": payload,
                    }
                )
            except Exception as exc:
                checks.append(
                    {
                        "name": f"adapter{endpoint}",
                        "ok": False,
                        "error": str(exc),
                    }
                )
        return checks

    return checks


async def run_safe_deploy_action_payload(action_name: str) -> dict[str, Any]:
    normalized_action = action_name.strip()
    policy = load_safe_deploy_policy_payload()
    if not policy["enabled"]:
        raise RuntimeError("safe deploy actions are disabled by current safe deploy policy")
    if normalized_action not in set(policy["allowed_actions"]):
        raise RuntimeError(f"safe deploy action is not allowed: {normalized_action}")

    repo = repo_root()
    actions = safe_deploy_action_catalog(repo)

    action = actions.get(normalized_action)
    if not action:
        raise ValueError(f"unsupported safe deploy action: {action_name}")

    result = await run_safe_process(action["command"], Path(str(action["cwd"])))
    post_checks: list[dict[str, Any]] = []
    if result["ok"]:
        post_checks = await run_safe_deploy_post_checks(normalized_action)
    checks_ok = all(bool(item.get("ok")) for item in post_checks) if post_checks else True
    return {
        "action": normalized_action,
        "description": action["description"],
        **result,
        "post_checks": post_checks,
        "ok": bool(result["ok"]) and checks_ok,
    }


def file_vault_files_root() -> Path:
    return SETTINGS.file_vault_root / "files"


def file_vault_items_root() -> Path:
    return SETTINGS.file_vault_root / "items"


def ensure_file_vault_dirs() -> None:
    SETTINGS.file_vault_root.mkdir(parents=True, exist_ok=True)
    file_vault_files_root().mkdir(parents=True, exist_ok=True)
    file_vault_items_root().mkdir(parents=True, exist_ok=True)


def safe_file_vault_item_id() -> str:
    prefix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"fv-{prefix}-{uuid.uuid4().hex[:8]}"


def safe_file_vault_name(name: str) -> str:
    candidate = Path(name).name.strip()
    if not candidate:
        candidate = "unnamed"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate)
    return safe.strip("._") or "unnamed"


def parse_file_vault_tags(tags: str) -> list[str]:
    raw = (tags or "").strip()
    if not raw:
        return []
    values = [part.strip() for part in raw.split(",")]
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def guess_mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_text_previewable(path: Path, mime_type: str) -> bool:
    suffix = path.suffix.lower()
    if mime_type.startswith("text/"):
        return True
    return suffix in {
        ".md",
        ".txt",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".py",
        ".log",
        ".csv",
        ".ini",
        ".cfg",
        ".html",
        ".css",
        ".js",
        ".ts",
        ".xml",
    }


def build_text_preview(path: Path, *, max_chars: int = 1600, max_lines: int = 30) -> dict[str, Any]:
    raw = path.read_bytes()[: max_chars * 4]
    text = raw.decode("utf-8", errors="replace").replace("\r", "")
    lines = text.splitlines()
    excerpt = "\n".join(lines[:max_lines]).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip() + "…"
    truncated = len(raw) == max_chars * 4 or len(lines) > max_lines or len(text) > len(excerpt)
    return {
        "available": bool(excerpt),
        "excerpt": excerpt,
        "truncated": truncated,
        "max_chars": max_chars,
        "max_lines": max_lines,
    }


def file_vault_item_path(item_id: str) -> Path:
    safe_item_id = safe_file_vault_name(item_id)
    return file_vault_items_root() / f"{safe_item_id}.json"


def load_file_vault_item(item_id: str) -> dict[str, Any]:
    path = file_vault_item_path(item_id)
    if not path.exists():
        raise FileNotFoundError(f"file vault item not found: {item_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_file_vault_item(item: dict[str, Any]) -> dict[str, Any]:
    preview = item.get("preview") if isinstance(item.get("preview"), dict) else {}
    stored_rel_path = item.get("stored_rel_path")
    stored_path = ""
    if isinstance(stored_rel_path, str) and stored_rel_path.strip():
        try:
            stored_path = str((SETTINGS.file_vault_root / stored_rel_path).resolve())
        except Exception:
            stored_path = ""
    return {
        "item_id": item.get("item_id"),
        "title": item.get("title"),
        "original_name": item.get("original_name"),
        "bytes": item.get("bytes"),
        "mime_type": item.get("mime_type"),
        "stored_at": item.get("stored_at"),
        "tags": item.get("tags", []),
        "source_kind": item.get("source_kind"),
        "stored_rel_path": stored_rel_path,
        "stored_path": stored_path or None,
        "preview_available": bool(preview.get("available")),
    }


def list_file_vault_items_payload(limit: int) -> dict[str, Any]:
    ensure_file_vault_dirs()
    items: list[dict[str, Any]] = []
    for path in sorted(
        file_vault_items_root().glob("*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )[: max(1, limit)]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items.append(summarize_file_vault_item(payload))
    return {
        "file_vault_root": str(SETTINGS.file_vault_root),
        "count": len(items),
        "items": items,
    }


def resolve_file_vault_source(source_path: str) -> tuple[Path, str]:
    raw = (source_path or "").strip()
    if not raw:
        raise ValueError("source_path is required")

    allowed_roots = [
        (SETTINGS.astrbot_attachments_root.resolve(), "attachments"),
        (SETTINGS.astrbot_temp_root.resolve(), "temp"),
        (SETTINGS.playground_root.resolve(), "playground"),
        (bookshelf_data_root().resolve(), "bookshelf"),
        (SETTINGS.shared_memory_root.resolve(), "memory"),
    ]
    candidate = Path(raw)

    if candidate.is_absolute():
        resolved = candidate.resolve()
        for root, kind in allowed_roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            if not resolved.is_file():
                raise FileNotFoundError(f"source file not found: {resolved}")
            return resolved, kind
        raise ValueError("source_path is outside allowed roots")

    prefixed_roots = {
        "playground": SETTINGS.playground_root.resolve(),
        "bookshelf": bookshelf_data_root().resolve(),
        "memory": SETTINGS.shared_memory_root.resolve(),
    }
    if candidate.parts and candidate.parts[0] in prefixed_roots and len(candidate.parts) > 1:
        prefix = candidate.parts[0]
        resolved = (prefixed_roots[prefix] / Path(*candidate.parts[1:])).resolve()
        try:
            resolved.relative_to(prefixed_roots[prefix])
        except ValueError as exc:
            raise ValueError(f"relative source_path escapes {prefix} root") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"{prefix} source file not found: {resolved}")
        return resolved, candidate.parts[0]

    if len(candidate.parts) > 1:
        resolved = (SETTINGS.playground_root.resolve() / candidate).resolve()
        try:
            resolved.relative_to(SETTINGS.playground_root.resolve())
        except ValueError as exc:
            raise ValueError("relative source_path escapes playground root") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"playground source file not found: {resolved}")
        return resolved, "playground"

    for root, kind in allowed_roots[:2]:
        resolved = (root / candidate.name).resolve()
        if resolved.is_file():
            return resolved, kind

    resolved = (SETTINGS.playground_root.resolve() / candidate.name).resolve()
    if resolved.is_file():
        return resolved, "playground"
    raise FileNotFoundError(f"source file not found in attachments/temp/playground: {raw}")


def store_file_in_vault_payload(source_path: str, title: str, tags: str) -> dict[str, Any]:
    ensure_file_vault_dirs()
    source_file, source_kind = resolve_file_vault_source(source_path)
    item_id = safe_file_vault_item_id()
    stored_dir = file_vault_files_root() / item_id
    stored_dir.mkdir(parents=True, exist_ok=True)
    original_name = source_file.name
    stored_name = safe_file_vault_name(original_name)
    stored_file = stored_dir / stored_name
    shutil.copy2(source_file, stored_file)

    mime_type = guess_mime_type(stored_file)
    preview = {
        "available": False,
        "excerpt": "",
        "truncated": False,
    }
    if is_text_previewable(stored_file, mime_type):
        preview = build_text_preview(stored_file)

    payload = {
        "item_id": item_id,
        "title": (title or "").strip() or Path(original_name).stem,
        "original_name": original_name,
        "stored_name": stored_name,
        "stored_rel_path": str(stored_file.relative_to(SETTINGS.file_vault_root)),
        "source_path": str(source_file),
        "source_kind": source_kind,
        "mime_type": mime_type,
        "bytes": stored_file.stat().st_size,
        "sha256": compute_sha256(stored_file),
        "tags": parse_file_vault_tags(tags),
        "stored_at": now_iso(),
        "preview": preview,
    }
    file_vault_item_path(item_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "file_vault_root": str(SETTINGS.file_vault_root),
        "item": payload,
    }


def read_file_vault_preview_payload(item_id: str) -> dict[str, Any]:
    item = load_file_vault_item(item_id)
    return {
        "file_vault_root": str(SETTINGS.file_vault_root),
        "item": item,
    }


def resolve_file_vault_stored_file(item: dict[str, Any]) -> Path:
    stored_rel_path = item.get("stored_rel_path")
    if not isinstance(stored_rel_path, str) or not stored_rel_path.strip():
        raise RuntimeError("file vault item has no stored path")
    stored_file = (SETTINGS.file_vault_root / stored_rel_path).resolve()
    try:
        stored_file.relative_to(SETTINGS.file_vault_root.resolve())
    except ValueError as exc:
        raise ValueError("stored file escapes file vault root") from exc
    if not stored_file.is_file():
        raise FileNotFoundError(f"stored file not found: {stored_file}")
    return stored_file


def read_file_vault_text_slice_payload(
    item_id: str,
    start_line: int,
    line_count: int,
    max_chars: int,
) -> dict[str, Any]:
    item = load_file_vault_item(item_id)
    stored_file = resolve_file_vault_stored_file(item)
    mime_type = str(item.get("mime_type") or "")
    if not is_text_previewable(stored_file, mime_type):
        raise ValueError("only text-like vault files support selective reading")

    start_line = max(1, int(start_line))
    line_count = max(1, min(int(line_count), 80))
    max_chars = max(200, min(int(max_chars), 6000))
    lines = stored_file.read_text(encoding="utf-8", errors="replace").splitlines()
    slice_lines = lines[start_line - 1 : start_line - 1 + line_count]
    excerpt = "\n".join(slice_lines)
    truncated = len(excerpt) > max_chars
    if truncated:
        excerpt = excerpt[:max_chars].rstrip() + "…"
    return {
        "item_id": item.get("item_id"),
        "title": item.get("title"),
        "original_name": item.get("original_name"),
        "start_line": start_line,
        "line_count": line_count,
        "total_lines": len(lines),
        "excerpt": excerpt,
        "truncated": truncated,
    }


def send_file_vault_item_via_email_payload(
    item_id: str,
    recipient: str,
    subject: str,
    body: str,
    max_attachment_bytes: int = 20 * 1024 * 1024,
) -> dict[str, Any]:
    item = load_file_vault_item(item_id)
    stored_file = resolve_file_vault_stored_file(item)
    file_size = stored_file.stat().st_size
    if file_size > max_attachment_bytes:
        raise ValueError(
            f"file vault item is too large for email attachment: {file_size} bytes > {max_attachment_bytes}"
        )
    resolved_recipient = str(recipient or "").strip()
    if not resolved_recipient:
        raise ValueError("recipient is required")
    resolved_subject = str(subject or "").strip() or f"机器人转发文件：{item.get('title') or item.get('original_name') or item_id}"
    resolved_body = (
        str(body or "").strip()
        or (
            "这是机器人从 file_vault 里给你转发的文件。\n\n"
            f"- item_id: {item.get('item_id')}\n"
            f"- 标题: {item.get('title') or ''}\n"
            f"- 原文件名: {item.get('original_name') or ''}\n"
            f"- 存储时间: {item.get('stored_at') or ''}\n"
        )
    )
    tools = load_external_tools()
    result = tools["send_email_with_attachment_zoho"](
        resolved_recipient,
        resolved_subject,
        resolved_body,
        str(stored_file),
        str(item.get("original_name") or stored_file.name),
    )
    return {
        "recipient": resolved_recipient,
        "subject": resolved_subject,
        "result": result,
        "item": summarize_file_vault_item(item),
        "attachment": {
            "stored_path": str(stored_file),
            "bytes": file_size,
            "max_attachment_bytes": max_attachment_bytes,
        },
    }


def read_playground_text_file(relative_path: str) -> dict[str, Any]:
    path = resolve_playground_path(relative_path, writable=False)
    if not path.exists():
        raise FileNotFoundError(f"playground file not found: {path}")
    content = path.read_text(encoding="utf-8")
    return {
        "playground_root": str(SETTINGS.playground_root),
        "path": str(path.relative_to(SETTINGS.playground_root.resolve())),
        "content": content,
    }


def write_playground_text_file(relative_path: str, content: str, append: bool) -> dict[str, Any]:
    path = resolve_playground_path(relative_path, writable=True)
    existing = ""
    if append and path.exists():
        existing = path.read_text(encoding="utf-8")
    final_content = f"{existing}{content}" if append else content
    path.write_text(final_content, encoding="utf-8")
    return {
        "playground_root": str(SETTINGS.playground_root),
        "path": str(path.relative_to(SETTINGS.playground_root.resolve())),
        "bytes": path.stat().st_size,
        "append": append,
    }


def get_memory_db_connection() -> sqlite3.Connection:
    if not SETTINGS.memory_db_path.exists():
        raise FileNotFoundError(f"memory db not found: {SETTINGS.memory_db_path}")
    connection = sqlite3.connect(SETTINGS.memory_db_path)
    connection.row_factory = sqlite3.Row
    return connection


def get_token_set(text: str) -> set[str]:
    chars = [char for char in (text or "").lower() if not char.isspace()]
    bigrams = {"".join(chars[index : index + 2]) for index in range(max(0, len(chars) - 1))}
    return set(chars) | bigrams


def calc_similarity(text_a: str, text_b: str) -> float:
    tokens_a = get_token_set(text_a)
    tokens_b = get_token_set(text_b)
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union else 0.0


def load_notion_tools():
    from components.notion_service import (  # type: ignore
        append_notion_database_row,
        delete_notion_block,
        get_notion_structure,
        list_notion_blocks,
        list_notion_inbox_entries,
        manage_notion_todo,
        read_page_content,
        read_timeline,
        rename_notion_page,
        search_pages,
        summarize_notion_database_numbers,
        triage_notion_inbox_entry,
        update_notion_block,
        write_to_existing_notion_page,
        write_structured_notion,
        write_timeline,
        write_to_notion,
    )

    return {
        "append_notion_database_row": append_notion_database_row,
        "delete_notion_block": delete_notion_block,
        "get_notion_structure": get_notion_structure,
        "list_notion_blocks": list_notion_blocks,
        "list_notion_inbox_entries": list_notion_inbox_entries,
        "manage_notion_todo": manage_notion_todo,
        "read_page_content": read_page_content,
        "read_timeline": read_timeline,
        "rename_notion_page": rename_notion_page,
        "search_pages": search_pages,
        "summarize_notion_database_numbers": summarize_notion_database_numbers,
        "triage_notion_inbox_entry": triage_notion_inbox_entry,
        "update_notion_block": update_notion_block,
        "write_to_existing_notion_page": write_to_existing_notion_page,
        "write_structured_notion": write_structured_notion,
        "write_timeline": write_timeline,
        "write_to_notion": write_to_notion,
    }


def load_external_tools():
    from components.email_tools import (  # type: ignore
        read_emails_zoho,
        read_specific_email_zoho,
        send_email_with_attachment_zoho,
        send_email_zoho,
    )
    from components.map_tools import search_nearby_places  # type: ignore
    from components.weather_tools import get_weather_function  # type: ignore
    from components.web_tools import create_html_page, list_html_pages  # type: ignore

    return {
        "create_html_page": create_html_page,
        "get_weather_function": get_weather_function,
        "list_html_pages": list_html_pages,
        "read_emails_zoho": read_emails_zoho,
        "read_specific_email_zoho": read_specific_email_zoho,
        "search_nearby_places": search_nearby_places,
        "send_email_with_attachment_zoho": send_email_with_attachment_zoho,
        "send_email_zoho": send_email_zoho,
    }


def require_adapter_token() -> str:
    if not SETTINGS.adapter_token:
        raise RuntimeError(
            "Missing ARANBOX_ADAPTER_TOKEN or ARAN_ADAPTER_TOKEN"
        )
    return SETTINGS.adapter_token


def require_backup_token() -> str:
    if not SETTINGS.backup_token:
        raise RuntimeError(
            "Missing ARANBOX_BACKUP_TOKEN or ARAN_MANUAL_BACKUP_TOKEN / ARANBOX_ADAPTER_TOKEN / ARAN_ADAPTER_TOKEN"
        )
    return SETTINGS.backup_token


async def adapter_get(path: str) -> dict[str, Any]:
    url = f"{SETTINGS.adapter_url}{path}"
    async with httpx.AsyncClient(timeout=SETTINGS.request_timeout_seconds) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def adapter_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{SETTINGS.adapter_url}{path}"
    token = require_adapter_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=SETTINGS.request_timeout_seconds) as client:
        response = await client.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()


async def adapter_post_backup(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{SETTINGS.adapter_url}{path}"
    token = require_backup_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=SETTINGS.request_timeout_seconds) as client:
        response = await client.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()


def summarize_backup_files(limit: int) -> list[dict[str, Any]]:
    if not SETTINGS.backup_root.exists():
        return []

    items = sorted(
        SETTINGS.backup_root.glob("*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    for path in items[: max(1, limit)]:
        stat = path.stat()
        result.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return result


def summarize_transcripts(limit: int) -> dict[str, Any]:
    if not SETTINGS.transcript_root.exists():
        return {
            "root": str(SETTINGS.transcript_root),
            "exists": False,
            "total_files": 0,
            "recent_files": [],
        }

    transcript_files = sorted(
        SETTINGS.transcript_root.rglob("*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    recent_files: list[dict[str, Any]] = []
    for path in transcript_files[: max(1, limit)]:
        stat = path.stat()
        recent_files.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )

    owner_dirs = [path for path in SETTINGS.transcript_root.iterdir() if path.is_dir()]
    return {
        "root": str(SETTINGS.transcript_root),
        "exists": True,
        "owner_count": len(owner_dirs),
        "total_files": len(transcript_files),
        "recent_files": recent_files,
    }


def qq_manifest_path(root: Path | None = None) -> Path:
    base = root or SETTINGS.qq_chat_backup_root
    return base / "manifest.json"


def safe_session_match(item: dict[str, Any], session_id: str) -> bool:
    target = (session_id or "").strip()
    if not target:
        return False
    candidates = {
        str(item.get("requested_session") or "").strip(),
        str(item.get("platform_session_id") or "").strip(),
    }
    platform_session_id = str(item.get("platform_session_id") or "").strip()
    if platform_session_id:
        candidates.add(platform_session_id.rsplit(":", 1)[-1])
    return target in candidates


def summarize_snapshot(snapshot: dict[str, Any], tail_limit: int) -> dict[str, Any]:
    messages = snapshot.get("messages") or []
    tail_messages = messages[-max(1, int(tail_limit)) :]
    return {
        "requested_session": snapshot.get("requested_session"),
        "platform_session_id": snapshot.get("platform_session_id"),
        "conversation_id": snapshot.get("conversation_id"),
        "created_at": snapshot.get("created_at"),
        "updated_at": snapshot.get("updated_at"),
        "exported_at": snapshot.get("exported_at"),
        "stats": snapshot.get("stats") or {},
        "days": snapshot.get("days") or [],
        "tail_messages": tail_messages,
    }


def read_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            items.append(payload)
    return items


def load_local_qq_manifest() -> dict[str, Any]:
    path = qq_manifest_path()
    if not path.exists():
        raise FileNotFoundError(f"qq chat manifest not found: {path}")
    return read_json_file(path)


def read_member_json_from_tar(archive_path: Path, member_name: str) -> dict[str, Any]:
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            member = tar.getmember(member_name)
        except KeyError as exc:
            raise FileNotFoundError(
                f"{member_name} not found in backup archive {archive_path.name}"
            ) from exc
        handle = tar.extractfile(member)
        if handle is None:
            raise FileNotFoundError(
                f"Unable to extract {member_name} from backup archive {archive_path.name}"
            )
        return json.loads(handle.read().decode("utf-8"))


def read_member_jsonl_from_tar(archive_path: Path, member_name: str) -> list[dict[str, Any]]:
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            member = tar.getmember(member_name)
        except KeyError as exc:
            raise FileNotFoundError(
                f"{member_name} not found in backup archive {archive_path.name}"
            ) from exc
        handle = tar.extractfile(member)
        if handle is None:
            raise FileNotFoundError(
                f"Unable to extract {member_name} from backup archive {archive_path.name}"
            )
        items: list[dict[str, Any]] = []
        for raw_line in handle.read().decode("utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                items.append(payload)
        return items


def resolve_backup_archive(backup_name: str) -> Path:
    candidate = (backup_name or "").strip()
    if not candidate:
        raise ValueError("backup_name is required")
    archive_path = SETTINGS.backup_root / Path(candidate).name
    if not archive_path.exists():
        raise FileNotFoundError(f"backup archive not found: {archive_path}")
    return archive_path


def find_snapshot_item(manifest: dict[str, Any], session_id: str) -> dict[str, Any]:
    items = manifest.get("items") or []
    for item in items:
        if isinstance(item, dict) and safe_session_match(item, session_id):
            return item
    raise ValueError(f"session not found in manifest: {session_id}")


def load_local_qq_snapshot(session_id: str) -> dict[str, Any]:
    manifest = load_local_qq_manifest()
    item = find_snapshot_item(manifest, session_id)
    snapshot_path = Path(str(item.get("snapshot_path") or ""))
    if not snapshot_path.exists():
        raise FileNotFoundError(f"qq chat snapshot not found: {snapshot_path}")
    return read_json_file(snapshot_path)


def load_local_qq_day_slice(session_id: str, day: str) -> list[dict[str, Any]]:
    safe_day = Path((day or "").strip()).name
    if not safe_day:
        raise ValueError("day is required")
    path = SETTINGS.qq_chat_backup_root / "days" / Path(session_id).name / f"{safe_day}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"qq chat day slice not found: {path}")
    return read_jsonl_file(path)


def load_backup_qq_manifest(backup_name: str) -> dict[str, Any]:
    archive_path = resolve_backup_archive(backup_name)
    return read_member_json_from_tar(archive_path, "qq_chat_backups/manifest.json")


def load_backup_qq_snapshot(backup_name: str, session_id: str) -> dict[str, Any]:
    archive_path = resolve_backup_archive(backup_name)
    manifest = load_backup_qq_manifest(backup_name)
    item = find_snapshot_item(manifest, session_id)
    snapshot_path = str(item.get("snapshot_path") or "")
    if not snapshot_path:
        raise ValueError(f"snapshot_path missing in backup manifest: {backup_name}")
    member_name = f"qq_chat_backups/{Path(snapshot_path).relative_to(SETTINGS.qq_chat_backup_root)}"
    return read_member_json_from_tar(archive_path, member_name)


def load_backup_qq_day_slice(backup_name: str, session_id: str, day: str) -> list[dict[str, Any]]:
    archive_path = resolve_backup_archive(backup_name)
    safe_day = Path((day or "").strip()).name
    if not safe_day:
        raise ValueError("day is required")
    member_name = f"qq_chat_backups/days/{Path(session_id).name}/{safe_day}.jsonl"
    return read_member_jsonl_from_tar(archive_path, member_name)


def build_qq_backup_status(limit: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "checked_at": now_iso(),
        "qq_chat_backup_root": str(SETTINGS.qq_chat_backup_root),
        "local_manifest_exists": False,
        "local_sessions": [],
        "recent_backup_archives": [],
    }
    manifest_path = qq_manifest_path()
    if manifest_path.exists():
        manifest = read_json_file(manifest_path)
        payload["local_manifest_exists"] = True
        payload["local_manifest"] = {
            "exported_at": manifest.get("exported_at"),
            "session_count": manifest.get("session_count"),
            "missing_sessions": manifest.get("missing_sessions"),
        }
        payload["local_sessions"] = manifest.get("items") or []

    for item in summarize_backup_files(limit):
        archive_payload = {
            **item,
            "has_qq_chat_backup": False,
        }
        try:
            manifest = load_backup_qq_manifest(item["name"])
        except Exception:
            payload["recent_backup_archives"].append(archive_payload)
            continue
        archive_payload["has_qq_chat_backup"] = True
        archive_payload["qq_chat_manifest"] = {
            "exported_at": manifest.get("exported_at"),
            "session_count": manifest.get("session_count"),
            "missing_sessions": manifest.get("missing_sessions"),
        }
        payload["recent_backup_archives"].append(archive_payload)
    return payload


def compare_snapshots(
    older_snapshot: dict[str, Any],
    newer_snapshot: dict[str, Any],
    tail_limit: int,
) -> dict[str, Any]:
    older_messages = older_snapshot.get("messages") or []
    newer_messages = newer_snapshot.get("messages") or []

    common_prefix = 0
    for old_item, new_item in zip(older_messages, newer_messages):
        if old_item.get("role") != new_item.get("role") or old_item.get("text") != new_item.get("text"):
            break
        common_prefix += 1

    older_tail = older_messages[common_prefix : common_prefix + max(1, int(tail_limit))]
    newer_tail = newer_messages[common_prefix : common_prefix + max(1, int(tail_limit))]
    appended_messages = newer_messages[common_prefix:]

    return {
        "session_id": newer_snapshot.get("session_id") or older_snapshot.get("session_id"),
        "platform_session_id": newer_snapshot.get("platform_session_id")
        or older_snapshot.get("platform_session_id"),
        "common_prefix_messages": common_prefix,
        "older_message_count": len(older_messages),
        "newer_message_count": len(newer_messages),
        "appended_message_count": max(0, len(newer_messages) - common_prefix),
        "older_changed_slice": older_tail,
        "newer_changed_slice": newer_tail,
        "appended_tail": appended_messages[-max(1, int(tail_limit)) :],
    }


def summarize_day_slice(
    session_id: str,
    day: str,
    messages: list[dict[str, Any]],
    tail_limit: int,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "day": day,
        "message_count": len(messages),
        "tail_messages": messages[-max(1, int(tail_limit)) :],
    }


async def pushplus_notify(title: str, content: str, topic: str = "") -> dict[str, Any]:
    token = SETTINGS.pushplus_token
    if not token:
        raise RuntimeError("missing ARAN_PUSHPLUS_TOKEN or PUSHPLUS_TOKEN")
    payload: dict[str, Any] = {
        "token": token,
        "title": title,
        "content": content,
        "template": "markdown",
    }
    resolved_topic = topic.strip() or SETTINGS.pushplus_topic
    if resolved_topic:
        payload["topic"] = resolved_topic

    async with httpx.AsyncClient(timeout=SETTINGS.request_timeout_seconds) as client:
        response = await client.post("https://www.pushplus.plus/send", json=payload)
    response.raise_for_status()
    return response.json()


@register_mcp_tool(layer="daily")
async def get_mcp_tool_layer_status() -> str:
    """Show the current MCP tool layer configuration and exposed tool set."""
    return json_pretty(mcp_tool_layer_status_payload())


@register_mcp_tool(layer="daily")
async def request_admin_tool_access(reason: str, requested_tools_csv: str = "") -> str:
    """Create a user-facing request for temporary admin-layer access."""
    payload = await create_admin_tool_access_request_payload(reason, requested_tools_csv)
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def run_admin_action_once(action_name: str) -> str:
    """Run one safe deploy action only when the user has just granted one-shot admin access via /管理一轮."""
    grant_before = load_admin_once_grant_payload()
    consumed_grant = consume_admin_once_grant_payload(action_name)
    result = await run_safe_deploy_action_payload(action_name)
    payload = {
        "grant_before": grant_before,
        "grant_after": consumed_grant,
        "result": result,
    }
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def get_adapter_status() -> str:
    """Read adapter health and readiness endpoints."""
    health = await adapter_get("/healthz")
    ready = await adapter_get("/readyz")
    payload = {
        "checked_at": now_iso(),
        "adapter_url": SETTINGS.adapter_url,
        "health": health,
        "ready": ready,
    }
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def create_adapter_backup(label: str = "manual") -> str:
    """Trigger adapter backup bundle creation."""
    payload = await adapter_post_backup("/admin/backups/create", {"label": label})
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def list_adapter_backups(limit: int = 10) -> str:
    """List local backup bundles under the configured adapter backup root."""
    payload = {
        "checked_at": now_iso(),
        "backup_root": str(SETTINGS.backup_root),
        "items": summarize_backup_files(limit),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def get_transcript_status(limit: int = 10) -> str:
    """Summarize transcript archive files under the configured transcript root."""
    payload = {
        "checked_at": now_iso(),
        **summarize_transcripts(limit),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def get_qq_chat_backup_status(limit: int = 10) -> str:
    """Summarize local QQ chat snapshots and recent backup archives."""
    payload = build_qq_backup_status(limit)
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def get_file_vault_status(limit: int = 20) -> str:
    """List recent file vault items and preview availability."""
    payload = list_file_vault_items_payload(limit)
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def list_managed_files(source: str = "all", limit: int = 20, keyword: str = "") -> str:
    """List managed files across file_vault/playground/bookshelf/memory with one unified entry."""
    payload = list_managed_files_payload(source, limit, keyword)
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def store_file_in_vault(source_path: str, title: str = "", tags: str = "") -> str:
    """Copy one file from attachments/temp/playground/bookshelf/memory into the managed file vault."""
    payload = store_file_in_vault_payload(source_path, title, tags)
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def read_file_vault_preview(item_id: str) -> str:
    """Read one file vault item manifest and its small preview excerpt."""
    payload = read_file_vault_preview_payload(item_id)
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def read_file_vault_text_slice(
    item_id: str,
    start_line: int = 1,
    line_count: int = 40,
    max_chars: int = 2400,
) -> str:
    """Read only a small text slice from one vault file, not the whole file."""
    payload = read_file_vault_text_slice_payload(item_id, start_line, line_count, max_chars)
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def read_qq_chat_snapshot(
    session_id: str,
    backup_name: str = "",
    day: str = "",
    tail_limit: int = 20,
) -> str:
    """Read one QQ chat snapshot from local latest export or a backup archive."""
    if day.strip():
        if backup_name.strip():
            messages = load_backup_qq_day_slice(backup_name, session_id, day)
            source = {
                "type": "backup_archive",
                "backup_name": backup_name,
            }
        else:
            messages = load_local_qq_day_slice(session_id, day)
            source = {
                "type": "local_latest",
                "root": str(SETTINGS.qq_chat_backup_root),
            }
        payload = {
            "source": source,
            "day_slice": summarize_day_slice(session_id, day, messages, tail_limit),
        }
    else:
        if backup_name.strip():
            snapshot = load_backup_qq_snapshot(backup_name, session_id)
            source = {
                "type": "backup_archive",
                "backup_name": backup_name,
            }
        else:
            snapshot = load_local_qq_snapshot(session_id)
            source = {
                "type": "local_latest",
                "root": str(SETTINGS.qq_chat_backup_root),
            }
        payload = {
            "source": source,
            "snapshot": summarize_snapshot(snapshot, tail_limit),
        }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def compare_qq_chat_snapshots(
    session_id: str,
    older_backup_name: str,
    newer_backup_name: str = "",
    tail_limit: int = 12,
) -> str:
    """Compare two QQ chat snapshots and show the changed tail."""
    older_snapshot = load_backup_qq_snapshot(older_backup_name, session_id)
    if newer_backup_name.strip():
        newer_snapshot = load_backup_qq_snapshot(newer_backup_name, session_id)
        newer_source: dict[str, Any] = {
            "type": "backup_archive",
            "backup_name": newer_backup_name,
        }
    else:
        newer_snapshot = load_local_qq_snapshot(session_id)
        newer_source = {
            "type": "local_latest",
            "root": str(SETTINGS.qq_chat_backup_root),
        }
    payload = {
        "older_source": {
            "type": "backup_archive",
            "backup_name": older_backup_name,
        },
        "newer_source": newer_source,
        "comparison": compare_snapshots(older_snapshot, newer_snapshot, tail_limit),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def get_weather(city: str = "", date: str = "") -> str:
    """Query weather by city/date via the shared weather component."""
    tools = load_external_tools()
    result = await tools["get_weather_function"](city or None, date or None)
    payload = {
        "city": city or None,
        "date": date or None,
        "result": result,
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def search_nearby_places_tool(
    keywords: str,
    city: str = "",
    center: str = "",
) -> str:
    """Search nearby places with AMap."""
    tools = load_external_tools()
    result = await asyncio.to_thread(
        tools["search_nearby_places"],
        keywords,
        city or None,
        center or None,
    )
    payload = {
        "keywords": keywords,
        "city": city or None,
        "center": center or None,
        "result": result,
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def render_html(filename: str, html_code: str) -> str:
    """Create an HTML page under the MCP public directory and return the access URL."""
    tools = load_external_tools()
    public_base_url = os.getenv("ARANBOX_WEB_PUBLIC_BASE_URL") or os.getenv("MY_DOMAIN") or ""
    if not public_base_url.strip():
        raise RuntimeError("missing ARANBOX_WEB_PUBLIC_BASE_URL or MY_DOMAIN")
    result = await asyncio.to_thread(
        tools["create_html_page"],
        filename,
        html_code,
        public_base_url,
    )
    payload = {
        "filename": filename,
        "result": result,
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def list_generated_pages(limit: int = 20) -> str:
    """List generated HTML pages under the MCP public directory."""
    tools = load_external_tools()
    public_base_url = os.getenv("ARANBOX_WEB_PUBLIC_BASE_URL") or os.getenv("MY_DOMAIN") or ""
    items = await asyncio.to_thread(tools["list_html_pages"], public_base_url, limit)
    for item in items:
        modified_at = item.get("modified_at")
        if modified_at:
            item["modified_at"] = datetime.fromtimestamp(
                float(modified_at),
                tz=timezone.utc,
            ).isoformat()
    payload = {
        "count": len(items),
        "items": items,
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def send_email(recipient: str, subject: str, body: str) -> str:
    """Send an email via Zoho."""
    tools = load_external_tools()
    result = await asyncio.to_thread(tools["send_email_zoho"], recipient, subject, body)
    payload = {
        "recipient": recipient,
        "subject": subject,
        "result": result,
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def send_file_vault_item_via_email(
    item_id: str,
    recipient: str,
    subject: str = "",
    body: str = "",
) -> str:
    """Send one managed file vault item as an email attachment."""
    payload = await asyncio.to_thread(
        send_file_vault_item_via_email_payload,
        item_id,
        recipient,
        subject,
        body,
    )
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def read_emails(folder: str = "INBOX", count: int = 5) -> str:
    """List recent emails from a folder."""
    tools = load_external_tools()
    result = await asyncio.to_thread(tools["read_emails_zoho"], folder, count)
    payload = {
        "folder": folder,
        "count": count,
        "result": result,
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def read_specific_email(message_id: str) -> str:
    """Read one email by Message-ID."""
    tools = load_external_tools()
    result = await asyncio.to_thread(tools["read_specific_email_zoho"], message_id)
    payload = {
        "message_id": message_id,
        "result": result,
    }
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def list_playground_files() -> str:
    """List files under the shared playground directory."""
    payload = list_playground_files_payload()
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def list_bookshelf_files() -> str:
    """List files under the bookshelf plugin data directory."""
    payload = list_bookshelf_files_payload()
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def read_playground_file(relative_path: str) -> str:
    """Read one text file under the shared playground directory."""
    payload = read_playground_text_file(relative_path)
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def write_playground_file(
    relative_path: str,
    content: str,
    append: bool = False,
) -> str:
    """Write or append one text file under the shared playground directory."""
    payload = write_playground_text_file(relative_path, content, append)
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def list_repo_workspace_files() -> str:
    """List files under the restricted repo automation workspace."""
    payload = list_repo_workspace_files_payload()
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def read_repo_workspace_file(relative_path: str) -> str:
    """Read one text file under the restricted repo automation workspace."""
    payload = read_repo_workspace_text_file(relative_path)
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def write_repo_workspace_file(
    relative_path: str,
    content: str,
    append: bool = False,
) -> str:
    """Write or append one text file under the restricted repo automation workspace."""
    payload = write_repo_workspace_text_file(relative_path, content, append)
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def list_safe_deploy_actions() -> str:
    """List fixed safe deploy actions. This is not arbitrary shell access."""
    payload = safe_deploy_actions_payload()
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def get_safe_deploy_policy() -> str:
    """Read the current safe deploy policy, including enabled state, groups, and actions."""
    payload = load_safe_deploy_policy_payload()
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def set_safe_deploy_policy(
    enabled: bool,
    allowed_groups_csv: str = "",
    extra_actions_csv: str = "",
    allowed_actions_csv: str = "",
) -> str:
    """Persist the safe deploy policy by groups plus extra actions, or by an exact action list."""
    current = load_safe_deploy_policy_payload()
    if allowed_actions_csv.strip():
        payload = write_safe_deploy_policy(
            enabled,
            parse_safe_deploy_actions_csv(allowed_actions_csv),
        )
        return json_pretty(payload)

    allowed_groups = (
        parse_safe_deploy_groups_csv(allowed_groups_csv)
        if allowed_groups_csv.strip() or allowed_groups_csv.lower() in {"none", "__none__"}
        else list(current["allowed_groups"])
    )
    extra_actions = (
        parse_safe_deploy_actions_csv(extra_actions_csv)
        if extra_actions_csv.strip() or extra_actions_csv.lower() in {"none", "__none__"}
        else list(current["extra_actions"])
    )
    payload = write_safe_deploy_policy(enabled, extra_actions, allowed_groups)
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def reset_safe_deploy_policy() -> str:
    """Delete the persisted safe deploy policy file and fall back to env defaults."""
    if SETTINGS.safe_deploy_policy_path.exists():
        SETTINGS.safe_deploy_policy_path.unlink()
    payload = load_safe_deploy_policy_payload()
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def run_safe_deploy_action(action_name: str) -> str:
    """Run one fixed safe deploy action by name."""
    payload = await run_safe_deploy_action_payload(action_name)
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def search_notion_pages(
    query: Annotated[str, "搜索词。尽量填得更具体，例如 标题+日期。支持 YYYY-MM-DD 和 YYYY-MM。"],
    category: NotionCategoryParam = "",
    max_results: int = 30,
    sample_limit: int = 8,
) -> str:
    """Search Notion pages.

    Tips:
        - query 尽量具体一点，如标题 + 日期；日期支持 YYYY-MM-DD 和 YYYY-MM。
        - category 可选值：love_diary / timeline / memo / todo / memory_profile / archive / creation / inbox / discard。
        - 不确定就留空；恋爱日记优先用 love_diary。
        - 结果太多时会自动截断，请继续缩小 query。
    """
    notion = load_notion_tools()
    payload = {
        "query": query,
        "category": category,
        "max_results": max_results,
        "sample_limit": sample_limit,
        "result": notion["search_pages"](
            query,
            category=category,
            max_results=max_results,
            sample_limit=sample_limit,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def read_notion_page_content(
    title: str = "",
    start_index: int = 0,
    max_length: int = 3000,
    include_block_index: bool = False,
    page_id: str = "",
) -> str:
    """Read a Notion page as plain text."""
    notion = load_notion_tools()
    payload = {
        "title": title,
        "page_id": page_id,
        "result": notion["read_page_content"](
            title=title,
            start_index=start_index,
            max_length=max_length,
            include_block_index=include_block_index,
            page_id=page_id,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def list_notion_blocks_tool(
    title: str = "",
    start_index: int = 1,
    limit: int = 20,
    page_id: str = "",
) -> str:
    """List editable Notion paragraph blocks with indices."""
    notion = load_notion_tools()
    payload = {
        "title": title,
        "page_id": page_id,
        "result": notion["list_notion_blocks"](
            title=title,
            start_index=start_index,
            limit=limit,
            page_id=page_id,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def update_notion_block_tool(
    block_index: int,
    content: str,
    title: str = "",
    color: str = "default",
    page_id: str = "",
) -> str:
    """Update one paragraph block inside a Notion page."""
    notion = load_notion_tools()
    payload = {
        "title": title,
        "page_id": page_id,
        "block_index": block_index,
        "result": notion["update_notion_block"](
            title=title,
            block_index=block_index,
            content=content,
            color=color,
            page_id=page_id,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def delete_notion_block_tool(title: str = "", block_index: int = 1, page_id: str = "") -> str:
    """Delete one paragraph block inside a Notion page."""
    notion = load_notion_tools()
    payload = {
        "title": title,
        "page_id": page_id,
        "block_index": block_index,
        "result": notion["delete_notion_block"](
            title=title,
            block_index=block_index,
            page_id=page_id,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def rename_notion_page_tool(old_title: str, new_title: str, page_id: str = "") -> str:
    """Rename a Notion page."""
    notion = load_notion_tools()
    payload = {
        "old_title": old_title,
        "new_title": new_title,
        "page_id": page_id,
        "result": notion["rename_notion_page"](
            old_title=old_title,
            new_title=new_title,
            page_id=page_id,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def write_notion_page(
    content: Annotated[str, "要写入 Notion 的正文内容，必填。"],
    title: Annotated[str, "页面标题或条目标题。恋爱日记通常可留空；创作、资料档案、记忆设定建议填写。"] = "",
    color: str = "default",
    category: NotionCategoryParam = "",
    date_hint: NotionDateHintParam = "",
    series_name: NotionSeriesNameParam = "",
    creation_type: NotionCreationTypeParam = "",
    legacy_direct_write: Annotated[bool, "旧模式直写开关。只有确实要把内容直接追加进一个已存在页面时才设为 true；一般保持 false。"] = False,
) -> str:
    """Write content into Notion.

    写 Notion 日记时优先使用本工具，不要用本地记忆工具。
    Prefer passing category so the tool can route automatically.
    若未传 category：
        - 工具会按关键词自动在 8 个分类里路由
        - 常见的时间线 / 备忘录 / 待办 / 创作 / 资料档案 / 记忆设定都会自动尝试识别
        - 如果仍然识别不出来，会先自动暂存到 inbox / 待整理，避免直接报错

    Args:
        content: The正文内容，必填。
        title: 页面标题或条目标题。恋爱日记通常可留空；创作、资料档案、记忆设定建议填写。
        color: Notion 段落颜色，默认 default。
        category: 推荐填写的分类参数，可选值：
            - love_diary / 恋爱日记
            - timeline / 时间线
            - memo / 备忘录
            - todo / 待办
            - memory_profile / 记忆设定
            - archive / 资料档案
            - creation / 创作
            - inbox / 待整理
            - discard / 作废与重复
        date_hint: 可选日期提示，如 2026-06-10、2026/06/10、6月10日。用于决定应写入哪一年/月/日。
        series_name: 仅在创作类内容中常用。若是系列作品，请传系列名。
        creation_type: 仅在创作类内容中常用，可选值：
            - series / 系列
            - short / 短篇
            - extra / 彩蛋

    Routing:
        - 恋爱日记 -> 年页 -> 月页 -> 日页
        - 时间线 -> 年页 -> 月页 -> 条目
        - 备忘录 -> 年页 -> 月页 -> 条目
        - 待办 -> 待办｜当前
        - 创作 -> 系列 / 短篇 / 彩蛋

    Notes:
        - 如果分类不确定，优先传 inbox / 待整理。
        - 现在即使不传 category，也会先做自动兜底，不再直接报错。
        - 只有在 legacy_direct_write=true 且 title 指向已存在页面时，才允许旧模式直写。
    """
    notion = load_notion_tools()
    resolved_category, route_mode = resolve_notion_write_category(
        category=category,
        title=title,
        content=content,
    )
    normalized_category = str(resolved_category or "").strip()
    payload = {
        "title": title,
        "category": resolved_category,
        "category_input": category,
        "route_mode": route_mode,
        "date_hint": date_hint,
        "series_name": series_name,
        "creation_type": creation_type,
        "legacy_direct_write": legacy_direct_write,
        "result": (
            notion["write_structured_notion"](
                category=resolved_category,
                title=title,
                content=content,
                color=color,
                date_hint=date_hint,
                series_name=series_name,
                creation_type=creation_type,
            )
            if normalized_category
            else (
                notion["write_to_existing_notion_page"](title=title, content=content, color=color)
                if legacy_direct_write
                else "❌ 现在写入 Notion 默认必须提供 category。若你确实要按旧方式直写现有页面，请同时传 legacy_direct_write=true 和 title。"
            )
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def append_notion_database_row_tool(
    database_id: Annotated[str, "Notion 数据库链接或 database_id。"],
    title: Annotated[str, "新记录的标题；如果数据库标题字段不是“名称”，工具也会自动识别。"] = "",
    properties_json: Annotated[
        str,
        "其他字段的 JSON 对象。支持 rich_text/date/number/select/multi_select/checkbox/url/email/phone_number。示例：{\"日期\":{\"date\":\"2026-06-19\"},\"金额\":{\"number\":1.19},\"类型\":{\"select\":\"创收\"},\"备注\":{\"rich_text\":\"今天按时吃饭\"}}",
    ] = "",
    title_property: Annotated[str, "标题字段名；默认“名称”，若数据库实际标题字段不同，工具会尽量自动识别。"] = "名称",
) -> str:
    """Append a row into a Notion database.

    当目标是 Notion 数据库而不是普通页面时，使用本工具。
    常见于小金库、结构化台账、清单记录等场景。
    """
    notion = load_notion_tools()
    payload = {
        "database_id": database_id,
        "title": title,
        "title_property": title_property,
        "properties_json": properties_json,
        "result": notion["append_notion_database_row"](
            database_id=database_id,
            title=title,
            properties_json=properties_json,
            title_property=title_property,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def write_aran_vault_entry(
    title: Annotated[str, "小金库记录标题，例如 健康监管、科研通关、吃醋保释金。"],
    amount: Annotated[float, "金额。入账用正数，扣钱/支出用负数；不确定时先确认再写。"],
    entry_type: Annotated[str, "类型。推荐值：创收、罚金、基金存入、支出、待确认。"] = "待确认",
    clause: Annotated[str, "触发条款。推荐值：健康监管、情绪抚慰、科研通关、Homra头牌、造梦列车、吃醋保释金、实体穿搭基金、爬墙未遂倒贴罚金。"] = "",
    status: Annotated[str, "状态。推荐值：已确认、待 owner 确认。"] = "待 owner 确认",
    note: Annotated[str, "备注，可写发生了什么。"] = "",
    date: Annotated[str, "日期，推荐格式 YYYY-MM-DD；留空时默认用今天。"] = "",
) -> str:
    """Write a row into the shared Notion vault database.

    当目标明确是“小金库”时，优先使用本工具，不要先去搜索普通 Notion 页面。
    本工具会向配置中的共享小金库数据库直接新增一条流水记录。
    """
    notion = load_notion_tools()
    date_value = str(date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    properties = {
        "日期": {"date": date_value},
        "金额": {"number": amount},
        "类型": {"select": str(entry_type or "").strip() or "待确认"},
        "状态": {"select": str(status or "").strip() or "待 owner 确认"},
    }
    clause_value = str(clause or "").strip()
    note_value = str(note or "").strip()
    if clause_value:
        properties["触发条款"] = {"select": clause_value}
    if note_value:
        properties["备注"] = {"rich_text": note_value}

    payload = {
        "database_id": ARAN_VAULT_DATABASE_URL,
        "title": title,
        "amount": amount,
        "entry_type": entry_type,
        "clause": clause,
        "status": status,
        "date": date_value,
        "result": notion["append_notion_database_row"](
            database_id=ARAN_VAULT_DATABASE_URL,
            title=title,
            properties_json=json.dumps(properties, ensure_ascii=False),
            title_property="名称",
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="daily")
async def read_aran_vault_total() -> str:
    """Read the current total amount and summary of the shared vault."""
    notion = load_notion_tools()
    payload = notion["summarize_notion_database_numbers"](
        database_id=ARAN_VAULT_DATABASE_URL,
        number_property="金额",
        status_property="状态",
        confirmed_status="已确认",
        pending_status="待 owner 确认",
    )
    payload["database_id"] = ARAN_VAULT_DATABASE_URL
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def manage_notion_todo_tool(
    action: str,
    text: str = "",
    page_title: str = "待办｜当前",
    todo_index: int = 0,
    show_completed: bool = False,
) -> str:
    """Manage the Notion todo page."""
    notion = load_notion_tools()
    payload = {
        "action": action,
        "page_title": page_title,
        "result": notion["manage_notion_todo"](
            action=action,
            text=text or None,
            page_title=page_title,
            todo_index=None if todo_index <= 0 else todo_index,
            show_completed=show_completed,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def write_timeline_entry(summary: str) -> str:
    """Legacy timeline helper. Do not use for general Notion writing."""
    notion = load_notion_tools()
    payload = {
        "summary": summary,
        "result": notion["write_timeline"](summary),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def read_timeline_history(days: int = 7, limit: int = 30) -> str:
    """Legacy timeline reader kept for maintenance only."""
    notion = load_notion_tools()
    payload = {
        "days": days,
        "limit": limit,
        "result": notion["read_timeline"](days=days, limit=limit),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def list_notion_inbox_items(page_title: str = "待整理｜当前", start_index: int = 1, limit: int = 20) -> str:
    """List inbox items in the Notion 待整理 page with indices."""
    notion = load_notion_tools()
    payload = {
        "page_title": page_title,
        "start_index": start_index,
        "limit": limit,
        "result": notion["list_notion_inbox_entries"](
            page_title=page_title,
            start_index=start_index,
            limit=limit,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="admin")
async def triage_notion_inbox_item(
    item_index: int,
    target_category: NotionCategoryParam,
    page_title: str = "待整理｜当前",
    target_title: str = "",
    date_hint: NotionDateHintParam = "",
    series_name: NotionSeriesNameParam = "",
    creation_type: NotionCreationTypeParam = "",
    delete_source: bool = True,
    color: str = "default",
) -> str:
    """Route one inbox item from 待整理 into a formal Notion category."""
    notion = load_notion_tools()
    payload = {
        "item_index": item_index,
        "target_category": target_category,
        "page_title": page_title,
        "target_title": target_title,
        "date_hint": date_hint,
        "series_name": series_name,
        "creation_type": creation_type,
        "delete_source": delete_source,
        "result": notion["triage_notion_inbox_entry"](
            item_index=item_index,
            target_category=target_category,
            page_title=page_title,
            target_title=target_title,
            date_hint=date_hint,
            series_name=series_name,
            creation_type=creation_type,
            delete_source=delete_source,
            color=color,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def view_notion_structure(category: NotionCategoryParam = "", max_children: int = 20) -> str:
    """View the current Notion route structure. Leave category empty to see top-level roots."""
    notion = load_notion_tools()
    payload = {
        "category": category,
        "max_children": max_children,
        "result": notion["get_notion_structure"](
            category=category,
            max_children=max_children,
        ),
    }
    return json_pretty(payload)


@register_mcp_tool(layer="extended")
async def send_pushplus_message(
    title: str,
    content: str,
    topic: str = "",
) -> str:
    """Send a PushPlus notification."""
    payload = await pushplus_notify(title=title, content=content, topic=topic)
    return json_pretty(payload)


async def send_whisper_to_bia(
    title: str,
    content: str,
    topic: str = "",
) -> str:
    """Legacy compatibility alias for older callers."""
    return await send_pushplus_message(title=title, content=content, topic=topic)


async def admin_status_endpoint(request: Request) -> JSONResponse:
    auth_error = validate_admin_api_request(request)
    if auth_error:
        return JSONResponse({"ok": False, "error": auth_error}, status_code=401)
    payload = {
        "ok": True,
        "tool_layers": mcp_tool_layer_status_payload(),
        "safe_deploy": load_safe_deploy_policy_payload(),
        "admin_once": load_admin_once_grant_payload(),
    }
    return JSONResponse(payload)


async def admin_tool_layers_endpoint(request: Request) -> JSONResponse:
    auth_error = validate_admin_api_request(request)
    if auth_error:
        return JSONResponse({"ok": False, "error": auth_error}, status_code=401)
    body = await request.json()
    enabled_layers = body.get("enabled_layers") if isinstance(body, dict) else []
    payload = write_tool_layer_state(enabled_layers if isinstance(enabled_layers, list) else [], "admin-api")
    return JSONResponse({"ok": True, "tool_layers": mcp_tool_layer_status_payload(), "saved": payload})


async def admin_safe_deploy_endpoint(request: Request) -> JSONResponse:
    auth_error = validate_admin_api_request(request)
    if auth_error:
        return JSONResponse({"ok": False, "error": auth_error}, status_code=401)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid request body"}, status_code=400)
    enabled = bool(body.get("enabled", False))
    allowed_groups = body.get("allowed_groups") if isinstance(body.get("allowed_groups"), list) else []
    extra_actions = body.get("extra_actions") if isinstance(body.get("extra_actions"), list) else []
    payload = write_safe_deploy_policy(enabled, extra_actions, allowed_groups)
    return JSONResponse({"ok": True, "safe_deploy": payload})


async def admin_once_endpoint(request: Request) -> JSONResponse:
    auth_error = validate_admin_api_request(request)
    if auth_error:
        return JSONResponse({"ok": False, "error": auth_error}, status_code=401)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid request body"}, status_code=400)
    action = str(body.get("action") or "").strip().lower()
    if action == "grant":
        note = str(body.get("note") or "").strip()
        granted_by = str(body.get("granted_by") or "admin-api").strip()
        ttl_seconds = int(body.get("ttl_seconds") or 600)
        payload = write_admin_once_grant_payload(granted_by=granted_by, note=note, ttl_seconds=ttl_seconds)
        return JSONResponse({"ok": True, "admin_once": payload})
    if action == "clear":
        payload = clear_admin_once_grant_payload()
        return JSONResponse({"ok": True, "admin_once": payload})
    return JSONResponse({"ok": False, "error": "unsupported action"}, status_code=400)


def create_http_app() -> Starlette:
    PUBLIC_PAGES_ROOT.mkdir(parents=True, exist_ok=True)
    mcp_http_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    routes = [
        Route("/admin-api/status", endpoint=admin_status_endpoint, methods=["GET"]),
        Route("/admin-api/tool-layers", endpoint=admin_tool_layers_endpoint, methods=["POST"]),
        Route("/admin-api/safe-deploy", endpoint=admin_safe_deploy_endpoint, methods=["POST"]),
        Route("/admin-api/admin-once", endpoint=admin_once_endpoint, methods=["POST"]),
        Mount(
            "/pages",
            app=StaticFiles(directory=str(PUBLIC_PAGES_ROOT), check_dir=False),
            name="pages",
        ),
        Mount("/", app=mcp_http_app),
    ]
    return Starlette(
        routes=routes,
        lifespan=lifespan,
    )


if __name__ == "__main__":
    uvicorn.run(
        create_http_app(),
        host=SETTINGS.bind_host,
        port=SETTINGS.bind_port,
    )
