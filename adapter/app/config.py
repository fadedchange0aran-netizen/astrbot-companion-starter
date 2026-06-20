from __future__ import annotations

import json
import os

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_env_file(env_file: Path) -> None:
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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = BASE_DIR / ".env"
ENV_FILE = Path(os.getenv("ARAN_ADAPTER_ENV_FILE", str(DEFAULT_ENV_FILE))).expanduser()
_load_env_file(ENV_FILE)


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_version: str
    host: str
    port: int
    log_level: str
    adapter_token: str
    timeout_seconds: float
    backend_type: str
    upstream_chat_url: str
    upstream_api_key: str
    upstream_forward_auth: bool
    upstream_extra_headers: dict[str, str]
    astrbot_target_url: str
    astrbot_api_key: str
    astrbot_username: str
    astrbot_config_id: str
    astrbot_bot_id: str
    astrbot_platform: str
    astrbot_public_base_url: str
    astrbot_attachments_dir: Path
    astrbot_data_db_path: Path
    astrbot_expose_reasoning: bool
    astrbot_emit_reasoning_alias: bool
    astrbot_expose_tool_calls: bool
    astrbot_expose_tool_call_arguments: bool
    transcript_enabled: bool
    transcript_root: Path
    manual_backup_root: Path
    manual_backup_token: str
    manual_backup_extra_paths: tuple[Path, ...]
    qq_chat_backup_root: Path
    qq_chat_backup_sessions: tuple[str, ...]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    raw_headers = _env_str("ARAN_UPSTREAM_EXTRA_HEADERS_JSON", "{}").strip() or "{}"
    try:
        parsed_headers = json.loads(raw_headers)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ARAN_UPSTREAM_EXTRA_HEADERS_JSON must be valid JSON") from exc

    if not isinstance(parsed_headers, dict):
        raise RuntimeError("ARAN_UPSTREAM_EXTRA_HEADERS_JSON must be a JSON object")

    normalized_headers = {
        str(key): str(value)
        for key, value in parsed_headers.items()
        if value is not None
    }

    raw_backup_paths = _env_str("ARAN_MANUAL_BACKUP_EXTRA_PATHS_JSON", "[]").strip() or "[]"
    try:
        parsed_backup_paths = json.loads(raw_backup_paths)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ARAN_MANUAL_BACKUP_EXTRA_PATHS_JSON must be valid JSON") from exc

    if not isinstance(parsed_backup_paths, list):
        raise RuntimeError("ARAN_MANUAL_BACKUP_EXTRA_PATHS_JSON must be a JSON array")

    raw_qq_backup_sessions = _env_str("ARAN_QQ_CHAT_BACKUP_SESSIONS_JSON", "[]").strip() or "[]"
    try:
        parsed_qq_backup_sessions = json.loads(raw_qq_backup_sessions)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ARAN_QQ_CHAT_BACKUP_SESSIONS_JSON must be valid JSON") from exc

    if not isinstance(parsed_qq_backup_sessions, list):
        raise RuntimeError("ARAN_QQ_CHAT_BACKUP_SESSIONS_JSON must be a JSON array")

    transcript_root = Path(
        _env_str("ARAN_TRANSCRIPT_ROOT", str(BASE_DIR / "data" / "transcripts"))
    ).expanduser()
    manual_backup_root = Path(
        _env_str("ARAN_MANUAL_BACKUP_ROOT", str(BASE_DIR / "data" / "backups"))
    ).expanduser()
    astrbot_attachments_dir = Path(
        _env_str("ARAN_ASTRBOT_ATTACHMENTS_DIR", "/srv/aran/data/astrbot/attachments")
    ).expanduser()
    astrbot_data_db_path = Path(
        _env_str("ARAN_ASTRBOT_DATA_DB_PATH", "/srv/aran/data/astrbot/data_v4.db")
    ).expanduser()
    manual_backup_extra_paths = tuple(
        Path(str(item)).expanduser()
        for item in parsed_backup_paths
        if str(item).strip()
    )
    qq_chat_backup_root = Path(
        _env_str("ARAN_QQ_CHAT_BACKUP_ROOT", "/srv/aran/data/astrbot/qq_chat_backups")
    ).expanduser()
    qq_chat_backup_sessions = tuple(
        str(item).strip()
        for item in parsed_qq_backup_sessions
        if str(item).strip()
    )

    return Settings(
        app_name="aran-adapter",
        app_version="0.1.0",
        host=_env_str("ARAN_ADAPTER_HOST", "0.0.0.0"),
        port=_env_int("ARAN_ADAPTER_PORT", 8001),
        log_level=_env_str("ARAN_ADAPTER_LOG_LEVEL", "INFO").upper(),
        adapter_token=_env_str("ARAN_ADAPTER_TOKEN", "").strip(),
        timeout_seconds=_env_float("ARAN_ADAPTER_TIMEOUT_SECONDS", 45.0),
        backend_type=_env_str("ARAN_ADAPTER_BACKEND_TYPE", "openai_proxy").strip().lower(),
        upstream_chat_url=_env_str("ARAN_UPSTREAM_CHAT_URL", "").strip(),
        upstream_api_key=_env_str("ARAN_UPSTREAM_API_KEY", "").strip(),
        upstream_forward_auth=_env_bool("ARAN_UPSTREAM_FORWARD_AUTH", False),
        upstream_extra_headers=normalized_headers,
        astrbot_target_url=_env_str("ARAN_ASTRBOT_TARGET_URL", "").strip(),
        astrbot_api_key=_env_str("ARAN_ASTRBOT_API_KEY", "").strip(),
        astrbot_username=_env_str("ARAN_ASTRBOT_USERNAME", "bia").strip() or "bia",
        astrbot_config_id=_env_str("ARAN_ASTRBOT_CONFIG_ID", "").strip(),
        astrbot_bot_id=_env_str("ARAN_ASTRBOT_BOT_ID", "aran").strip() or "aran",
        astrbot_platform=_env_str("ARAN_ASTRBOT_PLATFORM", "aran_bridge").strip() or "aran_bridge",
        astrbot_public_base_url=_env_str("ARAN_ASTRBOT_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        astrbot_attachments_dir=astrbot_attachments_dir,
        astrbot_data_db_path=astrbot_data_db_path,
        astrbot_expose_reasoning=_env_bool("ARAN_ASTRBOT_EXPOSE_REASONING", True),
        astrbot_emit_reasoning_alias=_env_bool("ARAN_ASTRBOT_EMIT_REASONING_ALIAS", True),
        astrbot_expose_tool_calls=_env_bool("ARAN_ASTRBOT_EXPOSE_TOOL_CALLS", False),
        astrbot_expose_tool_call_arguments=_env_bool(
            "ARAN_ASTRBOT_EXPOSE_TOOL_CALL_ARGUMENTS", False
        ),
        transcript_enabled=_env_bool("ARAN_TRANSCRIPT_ENABLED", True),
        transcript_root=transcript_root,
        manual_backup_root=manual_backup_root,
        manual_backup_token=_env_str("ARAN_MANUAL_BACKUP_TOKEN", "").strip(),
        manual_backup_extra_paths=manual_backup_extra_paths,
        qq_chat_backup_root=qq_chat_backup_root,
        qq_chat_backup_sessions=qq_chat_backup_sessions,
    )
