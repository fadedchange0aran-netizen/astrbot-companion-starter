import asyncio
import base64
import difflib
import json
import logging
import math
import os
import re
import secrets
import shutil
import struct
import time
import uuid
from pathlib import Path
from datetime import datetime
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Deque, Dict, Literal

import edge_tts
import httpx
import uvicorn
import websockets
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket as FastAPIWebSocket
from fastapi.responses import Response
from starlette.routing import Mount
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport, TransportSecuritySettings

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("STACKCHAN_ENV_FILE", str(BASE_DIR / ".env"))).expanduser()
LEGACY_ENV_FILE = BASE_DIR / "aran.env"


def _llmperception_config_candidates() -> tuple[Path, ...]:
    explicit_path = str(os.getenv("STACKCHAN_LLMPERCEPTION_CONFIG", "") or "").strip()
    if explicit_path:
        return (Path(explicit_path).expanduser(),)

    roots: list[Path] = []
    configured_root = str(os.getenv("STACKCHAN_ASTRBOT_CONFIG_DIR", "") or "").strip()
    for raw_root in (
        configured_root,
        "/AstrBot/data/config",
        str(BASE_DIR / "config"),
    ):
        if not raw_root:
            continue
        roots.append(Path(raw_root).expanduser())

    candidates: list[Path] = []
    for root in roots:
        candidates.append(root / "astrbot_plugin_llmperception_config.json")
        candidates.append(root / "astrbot_plugin_llmperception.json")
    return tuple(candidates)


LLMPERCEPTION_CONFIG_CANDIDATES = _llmperception_config_candidates()


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


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


load_env_file(ENV_FILE)
if not ENV_FILE.exists() and LEGACY_ENV_FILE != ENV_FILE:
    load_env_file(LEGACY_ENV_FILE)


def load_stackchan_frontend_settings() -> dict:
    for config_path in LLMPERCEPTION_CONFIG_CANDIDATES:
        if not config_path.exists():
            continue
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def get_stackchan_selected_provider_id() -> str:
    frontend_settings = load_stackchan_frontend_settings()
    if bool(frontend_settings.get("enable_stackchan_light_mode", False)):
        provider_id = str(frontend_settings.get("stackchan_selected_provider") or "").strip()
        if provider_id:
            return provider_id
    return CONFIG["CHAT_UPSTREAM_PROVIDER_ID"]


def get_stackchan_selected_model_name() -> str:
    frontend_settings = load_stackchan_frontend_settings()
    if bool(frontend_settings.get("enable_stackchan_light_mode", False)):
        model_name = str(frontend_settings.get("stackchan_selected_model") or "").strip()
        if model_name:
            return model_name
    return CONFIG["ROBOT_MODEL_NAME"]


# --- 1. 核心配置清单 ---
CONFIG = {
    "APP_HOST": env_str("STACKCHAN_APP_HOST", "0.0.0.0"),
    "APP_PORT": env_int("STACKCHAN_APP_PORT", 3000),
    "CONTROL_WS_HOST": env_str("STACKCHAN_CONTROL_WS_HOST", "0.0.0.0"),
    "CONTROL_WS_PORT": env_int("STACKCHAN_CONTROL_WS_PORT", 8765),
    "DEFAULT_ROBOT_ID": env_str("STACKCHAN_DEFAULT_ROBOT_ID"),
    "CHAT_UPSTREAM_URL": env_str("STACKCHAN_ADAPTER_URL").strip() or env_str("STACKCHAN_GATEWAY_URL"),
    "CHAT_UPSTREAM_KEY": env_str("STACKCHAN_ADAPTER_KEY").strip() or env_str("STACKCHAN_GATEWAY_KEY"),
    "CHAT_UPSTREAM_PROVIDER_ID": env_str("STACKCHAN_ASTRBOT_PROVIDER_ID").strip(),
    "GROQ_API_KEY": env_str("STACKCHAN_GROQ_API_KEY"),
    "STT_API_URL": env_str("STACKCHAN_STT_API_URL", "https://api.groq.com/openai/v1/audio/transcriptions"),
    "VISION_API_URL": env_str("STACKCHAN_VISION_API_URL", "https://api.groq.com/openai/v1/chat/completions"),
    "ROBOT_MODEL_NAME": env_str("STACKCHAN_MODEL_NAME"),
    "VISION_MODEL_NAME": env_str("STACKCHAN_VISION_MODEL_NAME", "meta-llama/llama-4-scout-17b-16e-instruct"),
    "VOICE_NAME": env_str("STACKCHAN_VOICE_NAME", "zh-CN-YunxiNeural"),
    "VOICE_WS_URL": env_str("STACKCHAN_VOICE_WS_URL"),
    "VOICE_WS_TOKEN": env_str("STACKCHAN_VOICE_WS_TOKEN"),
    "CONTROL_WS_TOKEN": env_str("STACKCHAN_CONTROL_WS_TOKEN").strip() or env_str("STACKCHAN_VOICE_WS_TOKEN"),
    "VOICE_PROTOCOL_VERSION": env_int("STACKCHAN_VOICE_PROTOCOL_VERSION", 1),
    "VOICE_FINALIZE_SILENCE_SECONDS": env_float("STACKCHAN_VOICE_FINALIZE_SILENCE_SECONDS", 0.25),
    "VOICE_LISTENING_GUARD_MS": env_int("STACKCHAN_VOICE_LISTENING_GUARD_MS", 350),
    "VOICE_MAX_LISTEN_SECONDS": env_float("STACKCHAN_VOICE_MAX_LISTEN_SECONDS", 8.0),
    "VOICE_ACTIVITY_WINDOW_FRAMES": env_int("STACKCHAN_VOICE_ACTIVITY_WINDOW_FRAMES", 12),
    "VOICE_ACTIVITY_CHECK_INTERVAL_SECONDS": env_float("STACKCHAN_VOICE_ACTIVITY_CHECK_INTERVAL_SECONDS", 0.35),
    "VOICE_ACTIVITY_MIN_PCM_RMS": env_float("STACKCHAN_VOICE_ACTIVITY_MIN_PCM_RMS", 120.0),
    "VOICE_ACTIVITY_MIN_PCM_PEAK": env_float("STACKCHAN_VOICE_ACTIVITY_MIN_PCM_PEAK", 600.0),
    "STT_HIGH_NO_SPEECH_PROB": env_float("STACKCHAN_STT_HIGH_NO_SPEECH_PROB", 0.72),
    "STT_LOW_CONFIDENCE_NO_SPEECH_PROB": env_float("STACKCHAN_STT_LOW_CONFIDENCE_NO_SPEECH_PROB", 0.45),
    "STT_LOW_CONFIDENCE_AVG_LOGPROB": env_float("STACKCHAN_STT_LOW_CONFIDENCE_AVG_LOGPROB", -0.9),
    "STT_VERY_LOW_AVG_LOGPROB": env_float("STACKCHAN_STT_VERY_LOW_AVG_LOGPROB", -1.2),
    "STT_HIGH_COMPRESSION_RATIO": env_float("STACKCHAN_STT_HIGH_COMPRESSION_RATIO", 2.2),
    "STT_MIN_PCM_RMS": env_float("STACKCHAN_STT_MIN_PCM_RMS", 220.0),
    "STT_MIN_PCM_PEAK": env_float("STACKCHAN_STT_MIN_PCM_PEAK", 900.0),
    "STT_WEAK_AUDIO_MAX_RMS_DBFS": env_float("STACKCHAN_STT_WEAK_AUDIO_MAX_RMS_DBFS", -45.0),
    "STT_SPIKE_AUDIO_MAX_RMS": env_float("STACKCHAN_STT_SPIKE_AUDIO_MAX_RMS", 180.0),
    "STT_SPIKE_AUDIO_MAX_RMS_DBFS": env_float("STACKCHAN_STT_SPIKE_AUDIO_MAX_RMS_DBFS", -44.0),
    "STT_SPIKE_AUDIO_MIN_PEAK_RATIO": env_float("STACKCHAN_STT_SPIKE_AUDIO_MIN_PEAK_RATIO", 12.0),
    "STT_FEEDBACK_COOLDOWN_SECONDS": env_float("STACKCHAN_STT_FEEDBACK_COOLDOWN_SECONDS", 6.0),
    "TTS_SAMPLE_RATE": env_int("STACKCHAN_TTS_SAMPLE_RATE", 24000),
    "TTS_FRAME_DURATION_MS": env_int("STACKCHAN_TTS_FRAME_MS", 60),
    "TTS_OPUS_BITRATE": env_str("STACKCHAN_TTS_OPUS_BITRATE", "48k"),
    "FFMPEG_PATH": env_str("STACKCHAN_FFMPEG_PATH", "ffmpeg"),
    "PHOTO_SAVE_DIR": env_str("STACKCHAN_PHOTO_DIR", "photos"),
    "SHORT_HISTORY_TURNS": env_int("STACKCHAN_SHORT_HISTORY_TURNS", 15),
    "HTTP_TIMEOUT_SECONDS": env_float("STACKCHAN_HTTP_TIMEOUT_SECONDS", 45.0),
}

REQUIRED_CONFIG_KEYS = (
    "CHAT_UPSTREAM_URL",
    "CHAT_UPSTREAM_KEY",
    "GROQ_API_KEY",
    "ROBOT_MODEL_NAME",
    "VOICE_WS_TOKEN",
)


def validate_config() -> None:
    missing = [key for key in REQUIRED_CONFIG_KEYS if not CONFIG[key]]
    if missing:
        env_name_map = {
            "CHAT_UPSTREAM_URL": "STACKCHAN_ADAPTER_URL (or legacy STACKCHAN_GATEWAY_URL)",
            "CHAT_UPSTREAM_KEY": "STACKCHAN_ADAPTER_KEY (or legacy STACKCHAN_GATEWAY_KEY)",
            "GROQ_API_KEY": "STACKCHAN_GROQ_API_KEY",
            "ROBOT_MODEL_NAME": "STACKCHAN_MODEL_NAME",
            "VOICE_WS_TOKEN": "STACKCHAN_VOICE_WS_TOKEN",
        }
        missing_names = ", ".join(env_name_map.get(key, f"STACKCHAN_{key}") for key in missing)
        raise RuntimeError(
            f"Missing required configuration in {ENV_FILE.name} or process env: {missing_names}"
        )


validate_config()

SUPPORTED_HW_ACTIONS = {
    "rotate_head": "左右转头，value 取 -90 到 90",
    "nod_head": "点头/俯仰，value 取 -90 到 90",
    "shake_head": "摇头，忽略 value",
    "dance": "跳舞，忽略 value",
    "reboot": "重启机器人，忽略 value",
    "look_up": "向上看，忽略 value",
    "look_down": "向下看，忽略 value",
    "look_left": "向左看，忽略 value",
    "look_right": "向右看，忽略 value",
    "look_center": "回正，忽略 value",
}

SUPPORTED_EMOTIONS = {
    "happy": 0,
    "sad": 1,
    "angry": 2,
    "surprised": 3,
    "sleepy": 4,
    "neutral": 5,
}

BUILTIN_VOICE_STYLE_PROMPT = (
    "你是方脑壳。回复必须适合直接语音播报。"
    "只输出自然、简洁的口语化纯文本。"
    "不要输出任何动作描写、表情描写、场景描写、心理描写、旁白、舞台说明。"
    "不要使用 emoji、颜文字、项目符号或角色扮演式括号文本。"
    "如果需要调用工具、查询信息或控制机器人，静默完成，不要向用户描述调用过程、思考过程或系统状态。"
    "不要说“我来查一下”“我帮你调用工具”“根据工具结果”这类过程性话术。"
    "默认直接回答问题本身，先给结论，再补一句即可。"
    "除非用户明确要求故事、创作或详细展开，否则控制在 1 句到 2 句，尽量不超过 30 个字。"
    "不要使用暧昧调情、训斥、占有欲、角色扮演式称呼。"
)

HwActionLiteral = Literal[
    "rotate_head",
    "nod_head",
    "shake_head",
    "dance",
    "reboot",
    "look_up",
    "look_down",
    "look_left",
    "look_right",
    "look_center",
]

EmotionLiteral = Literal["happy", "sad", "angry", "surprised", "sleepy", "neutral"]
SpeechEmotionLiteral = Literal["auto", "happy", "sad", "angry", "surprised", "sleepy", "neutral"]


def resolve_robot_id(robot_id: str = "") -> tuple[str | None, str | None]:
    resolved = canonicalize_robot_id(robot_id or CONFIG["DEFAULT_ROBOT_ID"])
    if not resolved:
        return None, "错误：未提供 robot_id，且未配置 STACKCHAN_DEFAULT_ROBOT_ID。"
    return resolved, None


def canonicalize_robot_id(raw_robot_id: str | None) -> str:
    value = (raw_robot_id or "").strip()
    if not value:
        return ""

    compact = re.sub(r"[:-]", "", value)
    if len(compact) == 12 and re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        return compact.upper()
    return value


def get_robot_id_aliases(robot_id: str | None) -> list[str]:
    canonical = canonicalize_robot_id(robot_id)
    if not canonical:
        return []

    aliases = [canonical]
    compact = re.sub(r"[:-]", "", canonical)
    if len(compact) == 12 and re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        colon_lower = ":".join(compact[i : i + 2] for i in range(0, 12, 2)).lower()
        for candidate in (compact.lower(), colon_lower, colon_lower.upper()):
            if candidate not in aliases:
                aliases.append(candidate)
    return aliases


def migrate_robot_mapping_alias(mapping: dict[str, object], robot_id: str) -> str | None:
    aliases = get_robot_id_aliases(robot_id)
    if not aliases:
        return None

    canonical = aliases[0]
    if canonical in mapping:
        return canonical

    for alias in aliases[1:]:
        if alias in mapping:
            mapping[canonical] = mapping.pop(alias)
            return canonical
    return None


def get_connected_robot_socket(robot_id: str) -> object | None:
    key = migrate_robot_mapping_alias(connected_robots, robot_id)
    if key is None:
        return None
    return connected_robots.get(key)


def get_robot_prompt(robot_id: str) -> str:
    key = migrate_robot_mapping_alias(robot_prompts, robot_id)
    if key is None:
        return ""
    return str(robot_prompts.get(key, ""))


def get_robot_history(robot_id: str, *, create: bool = False) -> Deque[dict]:
    key = migrate_robot_mapping_alias(robot_histories, robot_id)
    if key is not None:
        return robot_histories[key]

    canonical = canonicalize_robot_id(robot_id)
    if create and canonical:
        return robot_histories[canonical]
    return deque()


def get_robot_connection_state(robot_id: str) -> dict[str, bool]:
    session = get_active_voice_session(robot_id)
    control_online = get_connected_robot_socket(robot_id) is not None
    voice_online = session is not None and not session.closed
    return {
        "control_channel_online": control_online,
        "voice_session_online": voice_online,
        "robot_online": control_online or voice_online,
    }


def validate_robot_availability(
    robot_id: str,
    *,
    require_control_channel: bool = False,
    allow_voice_session: bool = False,
    capability_name: str = "操作",
) -> str | None:
    state = get_robot_connection_state(robot_id)

    if require_control_channel:
        if state["control_channel_online"]:
            return None
        if state["voice_session_online"]:
            return (
                f"错误：机器人 {robot_id} 当前只有语音会话在线，"
                f"控制通道未在线，暂时无法执行{capability_name}。"
            )
        return f"错误：机器人 {robot_id} 当前不在线，暂时无法执行{capability_name}。"

    if allow_voice_session:
        if state["robot_online"]:
            return None
        return f"错误：机器人 {robot_id} 当前不在线，暂时无法执行{capability_name}。"

    if not state["robot_online"]:
        return f"错误：机器人 {robot_id} 当前不在线，暂时无法执行{capability_name}。"
    return None


def parse_emotion_value(emotion: str) -> tuple[int | None, str | None]:
    value = emotion.strip().lower()
    if value.isdigit():
        idx = int(value)
        if idx in SUPPORTED_EMOTIONS.values():
            return idx, None
    if value in SUPPORTED_EMOTIONS:
        return SUPPORTED_EMOTIONS[value], None
    choices = ", ".join(f"{name}={idx}" for name, idx in SUPPORTED_EMOTIONS.items())
    return None, f"错误：不支持的表情 {emotion}。仅支持：{choices}"


def resolve_speech_emotion(emotion: str, text: str) -> tuple[int | None, str | None]:
    value = emotion.strip().lower()
    if not value or value == "auto":
        return guess_emotion(text), None
    return parse_emotion_value(value)


class PacketType:
    OPUS = 0x01
    JPEG = 0x02
    CONTROL_AVATAR = 0x03
    CONTROL_MOTION = 0x04
    TEXT_MESSAGE = 0x07
    HEARTBEAT_PING = 0x10
    HEARTBEAT_PONG = 0x11
    DANCE_SEQUENCE = 0x14
    START_AUDIO_STREAM = 0x18
    STOP_AUDIO_STREAM = 0x19


@dataclass
class VoiceSession:
    websocket: FastAPIWebSocket
    robot_id: str
    client_id: str
    protocol_version: int
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    listen_mode: str = "auto"
    listening: bool = False
    audio_frames: list[bytes] = field(default_factory=list)
    processing_task: asyncio.Task | None = None
    tts_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None
    finalize_task: asyncio.Task | None = None
    closed: bool = False
    audio_frame_count: int = 0
    audio_byte_count: int = 0
    listening_guard_until: float = 0.0
    playback_guard_until: float = 0.0
    listening_started_at: float = 0.0
    last_audio_frame_at: float = 0.0
    listening_guard_dropped_frames: int = 0
    last_stt_feedback_text: str = ""
    last_stt_feedback_at: float = 0.0


@dataclass
class SttResult:
    text: str = ""
    raw_text: str = ""
    language: str = ""
    duration_seconds: float = 0.0
    segment_count: int = 0
    avg_logprob: float | None = None
    mean_no_speech_prob: float | None = None
    max_no_speech_prob: float | None = None
    max_compression_ratio: float | None = None
    suppressed_hint: str = ""


@dataclass
class AudioEnergyStats:
    sample_count: int = 0
    rms: float = 0.0
    peak: int = 0
    rms_dbfs: float = -120.0
    peak_to_rms_ratio: float = 0.0


# --- 2. 初始化服务 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StackChan-Bridge")

mcp = FastMCP("stackchan-control-bridge")
connected_robots: Dict[str, object] = {}
robot_prompts: Dict[str, str] = {}
robot_histories: Dict[str, Deque[dict]] = defaultdict(
    lambda: deque(maxlen=CONFIG["SHORT_HISTORY_TURNS"] * 2)
)
voice_sessions: Dict[str, VoiceSession] = {}
photo_futures: Dict[str, asyncio.Future] = {}
interpret_futures: Dict[str, asyncio.Future] = {}
interpret_requests: Dict[str, dict] = {}
photo_uploads: Dict[str, dict] = {}


# --- 3. 语音引擎与协议辅助 ---

def build_binary_packet(packet_type: int, payload: bytes = b"") -> bytes:
    return bytes([packet_type]) + struct.pack(">I", len(payload)) + payload


def parse_binary_packet(data: bytes) -> tuple[int, bytes]:
    if len(data) < 5:
        raise ValueError("packet too short")
    payload_len = struct.unpack(">I", data[1:5])[0]
    payload = data[5 : 5 + payload_len]
    if len(payload) != payload_len:
        raise ValueError("packet payload length mismatch")
    return data[0], payload


_LEADING_STAGE_DIRECTION_RE = re.compile(r"^\s*[（(【\[].*?[）)】\]]\s*", re.S)
_FULL_LINE_STAGE_DIRECTION_RE = re.compile(r"^\s*[（(【\[].*?[）)】\]]\s*$", re.S)
_INLINE_STAGE_DIRECTION_RE = re.compile(r"[（(【\[].*?[）)】\]]", re.S)
_STAR_STAGE_DIRECTION_RE = re.compile(r"[*＊][^*＊\n]{1,80}[*＊]")
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    re.UNICODE,
)
_LEADING_MARKDOWN_RE = re.compile(r"^\s*(?:[-*•#>`]+|\d+\.)\s*")
_TOOL_NARRATION_PREFIX_RE = re.compile(
    r"^\s*(?:好|那|让我|我来|我先|我帮你|我来帮你|我先帮你|我现在|这就|稍等(?:一下)?[，,、 ]*)?"
    r"(?:查一下|看一下|查查看|看一眼|调用(?:一下)?工具|使用(?:一下)?工具|先去查|先帮你查|先帮你看)"
    r"[，,、 ]*",
    re.I,
)
_TOOL_NARRATION_SENTENCE_RE = re.compile(
    r"^\s*(?:好|那|让我|我来|我先|我帮你|我来帮你|我先帮你|我现在|这就|稍等(?:一下)?)?"
    r"(?:帮你)?(?:查一下|看一下|查查看|看一眼|调用(?:一下)?工具|使用(?:一下)?工具|先去查|先帮你查|先帮你看)"
    r"[^。！？!?]{0,24}[。！？!?]\s*",
    re.I,
)
_META_LINE_RE = re.compile(
    r"^\s*(?:"
    r"思考|旁白|动作|表情|场景|心理|系统|状态|工具(?:调用|结果)?|调用工具|使用工具|查询结果"
    r")\s*[:：]",
    re.I,
)
_TOOL_RESULT_LEAD_RE = re.compile(r"^\s*(?:根据|按照)(?:工具|查询)(?:结果)?[，,、 ]*")


def sanitize_voice_text(text: str) -> str:
    cleaned = text.strip()
    while True:
        updated = _LEADING_STAGE_DIRECTION_RE.sub("", cleaned, count=1)
        if updated == cleaned:
            break
        cleaned = updated.strip()

    lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _FULL_LINE_STAGE_DIRECTION_RE.fullmatch(line):
            continue
        if _META_LINE_RE.match(line):
            continue
        line = _INLINE_STAGE_DIRECTION_RE.sub("", line)
        line = _STAR_STAGE_DIRECTION_RE.sub("", line)
        line = _EMOJI_RE.sub("", line)
        line = line.replace("`", "")
        line = _LEADING_MARKDOWN_RE.sub("", line)
        line = _TOOL_NARRATION_SENTENCE_RE.sub("", line, count=1)
        line = _TOOL_NARRATION_PREFIX_RE.sub("", line, count=1)
        line = _TOOL_RESULT_LEAD_RE.sub("", line, count=1)
        line = re.sub(r"\s{2,}", " ", line)
        lines.append(line.strip())

    cleaned = "\n".join(lines).strip()
    cleaned = _STAR_STAGE_DIRECTION_RE.sub("", cleaned)
    cleaned = _INLINE_STAGE_DIRECTION_RE.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if not cleaned:
        return "我在听。"
    return cleaned


_PROMO_NOISE_PATTERNS = (
    "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
)
_ENDING_NOISE_PATTERNS = (
    "谢谢大家",
    "谢谢观看",
)
_NON_DIALOGUE_NOISE_PATTERNS = (
    "感谢观看",
    "谢谢观看",
    "字幕志愿者",
    "中文字幕志愿者",
    "字幕组",
    "杨栋梁",
)
_PROMO_NOISE_KEYWORDS = (
    "点赞",
    "订阅",
    "分享",
    "转发",
    "打赏",
    "关注",
)
_EMPTY_STT_HINT = "刚刚没听清，你再说一遍。"
_ECHO_STT_HINT = "刚才像是回声，你再说一遍。"
_NOISE_STT_HINT = "刚刚像是杂音，你再说一遍。"
_LOW_CONFIDENCE_STT_HINT = "刚刚这句不太确定，你再说一遍。"
_LOW_ENERGY_STT_HINT = "刚刚声音太轻或环境太安静，你再说一遍。"
_TTS_PREPARE_FAILED_HINT = "这句已经生成出来了，但语音播报失败了。"
_BUILTIN_STT_PROMPT = "这是中文口语对话。如果没有听清或只有杂音，请尽量返回空文本，不要脑补，不要补全。"


def sanitize_stt_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""

    for noise in _PROMO_NOISE_PATTERNS:
        cleaned = cleaned.replace(noise, " ")

    for noise in _ENDING_NOISE_PATTERNS:
        if cleaned == noise:
            cleaned = ""
        elif cleaned.startswith(noise + " "):
            cleaned = cleaned[len(noise) + 1 :]
        elif cleaned.endswith(" " + noise):
            cleaned = cleaned[: -(len(noise) + 1)]

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,，。!！?？")
    return cleaned


def normalize_compare_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text).strip().lower()


def has_non_dialogue_noise_signature(text: str) -> bool:
    normalized = normalize_compare_text(text)
    if not normalized:
        return False

    for pattern in _NON_DIALOGUE_NOISE_PATTERNS:
        signature = normalize_compare_text(pattern)
        if signature and signature in normalized:
            logger.info("[Voice] STT non-dialogue noise suppressed text=%s pattern=%s", text, pattern)
            return True

    keyword_hits = 0
    for keyword in _PROMO_NOISE_KEYWORDS:
        signature = normalize_compare_text(keyword)
        if signature and signature in normalized:
            keyword_hits += 1

    if keyword_hits >= 2:
        logger.info("[Voice] STT promo-like noise suppressed text=%s hits=%d", text, keyword_hits)
        return True

    return False


def _safe_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_stt_result(payload: object) -> SttResult:
    if not isinstance(payload, dict):
        return SttResult()

    text = sanitize_stt_text(str(payload.get("text", "")))
    segments = payload.get("segments")
    segment_list = segments if isinstance(segments, list) else []

    avg_logprobs: list[float] = []
    no_speech_probs: list[float] = []
    compression_ratios: list[float] = []
    for item in segment_list:
        if not isinstance(item, dict):
            continue
        avg_logprob = _safe_float(item.get("avg_logprob"))
        no_speech_prob = _safe_float(item.get("no_speech_prob"))
        compression_ratio = _safe_float(item.get("compression_ratio"))
        if avg_logprob is not None:
            avg_logprobs.append(avg_logprob)
        if no_speech_prob is not None:
            no_speech_probs.append(no_speech_prob)
        if compression_ratio is not None:
            compression_ratios.append(compression_ratio)

    return SttResult(
        text=text,
        raw_text=str(payload.get("text", "")),
        language=str(payload.get("language", "") or ""),
        duration_seconds=_safe_float(payload.get("duration")) or 0.0,
        segment_count=len(segment_list),
        avg_logprob=(sum(avg_logprobs) / len(avg_logprobs)) if avg_logprobs else None,
        mean_no_speech_prob=(sum(no_speech_probs) / len(no_speech_probs)) if no_speech_probs else None,
        max_no_speech_prob=max(no_speech_probs) if no_speech_probs else None,
        max_compression_ratio=max(compression_ratios) if compression_ratios else None,
    )


def is_low_confidence_stt(result: SttResult, frame_count: int, byte_count: int) -> bool:
    normalized = normalize_compare_text(result.text)
    if not normalized:
        return False

    avg_logprob = result.avg_logprob
    mean_no_speech_prob = result.mean_no_speech_prob
    max_no_speech_prob = result.max_no_speech_prob
    max_compression_ratio = result.max_compression_ratio
    text_len = len(normalized)

    reasons: list[str] = []
    if mean_no_speech_prob is not None and mean_no_speech_prob >= CONFIG["STT_HIGH_NO_SPEECH_PROB"]:
        reasons.append(f"mean_no_speech={mean_no_speech_prob:.2f}")

    if (
        mean_no_speech_prob is not None
        and avg_logprob is not None
        and mean_no_speech_prob >= CONFIG["STT_LOW_CONFIDENCE_NO_SPEECH_PROB"]
        and avg_logprob <= CONFIG["STT_LOW_CONFIDENCE_AVG_LOGPROB"]
    ):
        reasons.append(
            f"weak_audio mean_no_speech={mean_no_speech_prob:.2f} avg_logprob={avg_logprob:.2f}"
        )

    if (
        max_no_speech_prob is not None
        and avg_logprob is not None
        and max_no_speech_prob >= 0.85
        and avg_logprob <= CONFIG["STT_LOW_CONFIDENCE_AVG_LOGPROB"]
        and text_len <= 18
    ):
        reasons.append(
            f"peak_no_speech={max_no_speech_prob:.2f} avg_logprob={avg_logprob:.2f}"
        )

    if (
        max_compression_ratio is not None
        and avg_logprob is not None
        and max_compression_ratio >= CONFIG["STT_HIGH_COMPRESSION_RATIO"]
        and avg_logprob <= CONFIG["STT_VERY_LOW_AVG_LOGPROB"]
    ):
        reasons.append(
            f"compression={max_compression_ratio:.2f} avg_logprob={avg_logprob:.2f}"
        )

    if reasons:
        logger.info(
            "[Voice] STT low-confidence suppressed chars=%d frames=%d bytes=%d duration=%.2f segments=%d text=%s reasons=%s",
            text_len,
            frame_count,
            byte_count,
            result.duration_seconds,
            result.segment_count,
            result.text,
            "; ".join(reasons),
        )
        return True

    return False


def compute_pcm_energy_stats(pcm_s16le: bytes) -> AudioEnergyStats:
    if len(pcm_s16le) < 2:
        return AudioEnergyStats()

    sample_count = len(pcm_s16le) // 2
    total_squares = 0.0
    peak = 0
    for (sample,) in struct.iter_unpack("<h", pcm_s16le[: sample_count * 2]):
        amplitude = abs(sample)
        if amplitude > peak:
            peak = amplitude
        total_squares += float(sample) * float(sample)

    if sample_count == 0:
        return AudioEnergyStats()

    rms = math.sqrt(total_squares / sample_count)
    rms_dbfs = 20.0 * math.log10(max(rms, 1.0) / 32767.0)
    peak_to_rms_ratio = float(peak) / max(rms, 1.0)
    return AudioEnergyStats(
        sample_count=sample_count,
        rms=rms,
        peak=peak,
        rms_dbfs=rms_dbfs,
        peak_to_rms_ratio=peak_to_rms_ratio,
    )


async def ogg_opus_to_pcm_s16le_bytes(ogg_data: bytes, sample_rate: int = 16000) -> bytes:
    ffmpeg_path = CONFIG["FFMPEG_PATH"]
    if not shutil.which(ffmpeg_path):
        raise RuntimeError(f"{ffmpeg_path} not found in PATH")

    process = await asyncio.create_subprocess_exec(
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pcm_data, stderr = await process.communicate(ogg_data)
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="ignore").strip() or "ffmpeg pcm decode failed")
    return pcm_data


async def analyze_audio_energy(ogg_data: bytes) -> AudioEnergyStats | None:
    if not ogg_data:
        return None
    try:
        pcm_data = await ogg_opus_to_pcm_s16le_bytes(ogg_data)
        return compute_pcm_energy_stats(pcm_data)
    except Exception as exc:
        logger.warning("[Voice] Audio energy analysis skipped: %s", exc)
        return None


def get_low_energy_reasons(stats: AudioEnergyStats | None) -> list[str]:
    if stats is None or stats.sample_count == 0:
        return []

    reasons: list[str] = []
    if stats.rms < CONFIG["STT_MIN_PCM_RMS"] and stats.peak < CONFIG["STT_MIN_PCM_PEAK"]:
        reasons.append(
            f"low_rms_peak rms={stats.rms:.1f} peak={stats.peak}"
        )

    if stats.rms < CONFIG["STT_MIN_PCM_RMS"] and stats.rms_dbfs <= CONFIG["STT_WEAK_AUDIO_MAX_RMS_DBFS"]:
        reasons.append(
            f"low_rms_dbfs rms={stats.rms:.1f} rms_dbfs={stats.rms_dbfs:.1f}"
        )

    if (
        stats.rms < CONFIG["STT_SPIKE_AUDIO_MAX_RMS"]
        and stats.rms_dbfs <= CONFIG["STT_SPIKE_AUDIO_MAX_RMS_DBFS"]
        and stats.peak_to_rms_ratio >= CONFIG["STT_SPIKE_AUDIO_MIN_PEAK_RATIO"]
    ):
        reasons.append(
            f"spiky_noise rms={stats.rms:.1f} rms_dbfs={stats.rms_dbfs:.1f} peak_ratio={stats.peak_to_rms_ratio:.1f}"
        )

    return reasons


def is_low_energy_audio(stats: AudioEnergyStats | None) -> bool:
    return bool(get_low_energy_reasons(stats))


def has_voice_activity(stats: AudioEnergyStats | None) -> bool:
    if stats is None or stats.sample_count == 0:
        return False
    return (
        stats.rms >= CONFIG["VOICE_ACTIVITY_MIN_PCM_RMS"]
        or stats.peak >= CONFIG["VOICE_ACTIVITY_MIN_PCM_PEAK"]
    )


def is_probably_spurious_short_text(text: str, frame_count: int, byte_count: int) -> bool:
    normalized = normalize_compare_text(text)
    if not normalized:
        return True

    text_len = len(normalized)
    # 经验规则：非常短的识别如果只对应很短的一小段音频，更像杂音触发而不是用户真的说话。
    if text_len <= 2 and frame_count <= 8:
        logger.info(
            "[Voice] STT short-noise suppressed chars=%d frames=%d bytes=%d text=%s",
            text_len,
            frame_count,
            byte_count,
            text,
        )
        return True
    if text_len <= 6 and frame_count <= 4:
        logger.info(
            "[Voice] STT weak-short suppressed chars=%d frames=%d bytes=%d text=%s",
            text_len,
            frame_count,
            byte_count,
            text,
        )
        return True
    return False


def is_echo_like_text(robot_id: str, text: str) -> bool:
    candidate = normalize_compare_text(text)
    if len(candidate) < 6:
        return False

    history = get_robot_history(robot_id)
    if not history:
        return False

    assistant_messages = [
        normalize_compare_text(item.get("content", ""))
        for item in reversed(history)
        if item.get("role") == "assistant" and item.get("content")
    ]

    for previous in assistant_messages[:2]:
        if len(previous) < 6:
            continue
        ratio = difflib.SequenceMatcher(None, candidate, previous).ratio()
        if candidate in previous or previous in candidate or ratio >= 0.72:
            logger.info(
                "[Voice] STT echo suppressed robot=%s ratio=%.2f text=%s",
                robot_id,
                ratio,
                text,
            )
            return True
    return False


def normalize_auth_token(raw_token: str | None) -> str:
    if not raw_token:
        return ""
    token = raw_token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def validate_voice_auth(websocket: FastAPIWebSocket) -> bool:
    expected = normalize_auth_token(CONFIG["VOICE_WS_TOKEN"])
    if not expected:
        return True
    incoming = normalize_auth_token(websocket.headers.get("authorization"))
    return incoming == expected


def validate_control_auth_header(raw_header: str | None) -> bool:
    expected = normalize_auth_token(CONFIG["CONTROL_WS_TOKEN"])
    if not expected:
        return True
    incoming = normalize_auth_token(raw_header)
    return incoming == expected


def ensure_photo_dir() -> Path:
    photo_dir = Path(CONFIG["PHOTO_SAVE_DIR"]).expanduser()
    photo_dir.mkdir(parents=True, exist_ok=True)
    return photo_dir


def sanitize_photo_device_id(device_id: str) -> str:
    safe_device_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", device_id).strip("._")
    return safe_device_id or "unknown-device"


def build_photo_output_path(device_id: str) -> Path:
    photo_dir = ensure_photo_dir()
    safe_device_id = sanitize_photo_device_id(device_id)
    filename = f"photo_{safe_device_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
    return photo_dir / filename


def persist_photo_bytes(device_id: str, image_bytes: bytes) -> str:
    if not image_bytes:
        raise ValueError("empty image")
    file_path = build_photo_output_path(device_id)
    file_path.write_bytes(image_bytes)
    return str(file_path)


def decode_photo_b64_chunks(chunks: list[str]) -> bytes:
    if not chunks:
        raise ValueError("empty image")

    image_bytes = bytearray()
    for chunk in chunks:
        image_bytes.extend(base64.b64decode(chunk, validate=True))
    return bytes(image_bytes)


def persist_photo_b64(device_id: str, b64_data: str) -> str:
    image_bytes = base64.b64decode(b64_data, validate=True)
    return persist_photo_bytes(device_id, image_bytes)


async def interpret_uploaded_photo(device_id: str, image_bytes: bytes, prompt: str) -> tuple[str, str]:
    file_path = persist_photo_bytes(device_id, image_bytes)
    logger.info(
        "[Vision] Interpreting uploaded photo from %s file=%s prompt_len=%d",
        device_id,
        file_path,
        len(prompt),
    )
    b64_data = base64.b64encode(image_bytes).decode("ascii")
    result = await interpret_image(b64_data, prompt)
    return result, file_path


def unwrap_audio_payload(protocol_version: int, payload: bytes) -> bytes:
    if protocol_version == 2:
        if len(payload) < 12:
            return b""
        declared_size = struct.unpack(">I", payload[8:12])[0]
        data = payload[12 : 12 + declared_size]
        return data if len(data) == declared_size else b""
    if protocol_version == 3:
        if len(payload) < 4:
            return b""
        declared_size = struct.unpack(">H", payload[2:4])[0]
        data = payload[4 : 4 + declared_size]
        return data if len(data) == declared_size else b""
    return payload


def wrap_audio_payload(protocol_version: int, payload: bytes) -> bytes:
    if protocol_version == 2:
        # [server_sample_rate][reserved][payload_size][payload]
        return (
            struct.pack(">I", int(CONFIG["TTS_SAMPLE_RATE"]))
            + b"\x00\x00\x00\x00"
            + struct.pack(">I", len(payload))
            + payload
        )
    if protocol_version == 3:
        # [codec=0x00 opus][reserved][payload_size uint16][payload]
        return b"\x00\x00" + struct.pack(">H", len(payload)) + payload
    return payload


async def send_session_json(session: VoiceSession, payload: dict) -> None:
    if session.closed:
        raise RuntimeError("voice websocket is already closed")
    if "session_id" not in payload:
        payload = {"session_id": session.session_id, **payload}
    try:
        await session.websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except RuntimeError:
        session.closed = True
        raise


async def send_stt_feedback(session: VoiceSession, text: str) -> None:
    now = time.perf_counter()
    cooldown = CONFIG["STT_FEEDBACK_COOLDOWN_SECONDS"]
    if (
        session.last_stt_feedback_text == text
        and now - session.last_stt_feedback_at < cooldown
    ):
        logger.info(
            "[Voice] feedback throttled robot=%s text=%s cooldown=%.1fs",
            session.robot_id,
            text,
            cooldown,
        )
        return

    session.last_stt_feedback_text = text
    session.last_stt_feedback_at = now
    await send_session_json(session, {"type": "stt", "text": text})


async def send_session_audio(session: VoiceSession, payload: bytes) -> None:
    if session.closed:
        raise RuntimeError("voice websocket is already closed")
    try:
        await session.websocket.send_bytes(wrap_audio_payload(session.protocol_version, payload))
    except RuntimeError:
        session.closed = True
        raise


async def send_robot_json(robot_socket: object, payload: dict) -> None:
    message = json.dumps(payload, ensure_ascii=False)
    if hasattr(robot_socket, "send_text"):
        await robot_socket.send_text(message)
        return
    await robot_socket.send(message)


async def send_robot_message(robot_id: str, payload: dict) -> None:
    robot_socket = get_connected_robot_socket(robot_id)
    if robot_socket is None:
        raise KeyError(f"robot {robot_id} control channel is not online")
    await send_robot_json(robot_socket, payload)


def emotion_name_from_index(emotion: int) -> str:
    mapping = {
        0: "happy",
        1: "sad",
        2: "angry",
        3: "surprised",
        4: "sleepy",
        5: "neutral",
    }
    return mapping.get(emotion, "neutral")


def guess_emotion(text: str) -> int:
    if any(word in text for word in ("哈哈", "开心", "太棒", "高兴", "喜欢", "耶", "可爱", "好耶", "真棒")):
        return 0
    if any(word in text for word in ("难过", "抱歉", "伤心", "遗憾", "委屈", "呜呜", "可惜")):
        return 1
    if any(word in text for word in ("生气", "愤怒", "别这样", "烦死了", "讨厌", "不许")):
        return 2
    if any(word in text for word in ("哇", "居然", "真的吗", "惊讶", "天哪", "竟然", "诶", "欸")):
        return 3
    if any(word in text for word in ("困", "晚安", "睡", "被窝", "想睡", "休息")):
        return 4
    return 5


def guess_emotion_name(text: str) -> str:
    mapping = {
        0: "happy",
        1: "sad",
        2: "angry",
        3: "surprised",
        5: "neutral",
    }
    return mapping.get(guess_emotion(text), "neutral")


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def _build_ogg_page(packet: bytes, serial: int, sequence: int, granule_pos: int, header_type: int) -> bytes:
    segments = []
    remaining = len(packet)
    while remaining >= 255:
        segments.append(255)
        remaining -= 255
    segments.append(remaining)

    header = bytearray()
    header.extend(b"OggS")
    header.extend(b"\x00")
    header.extend(bytes([header_type]))
    header.extend(struct.pack("<Q", granule_pos))
    header.extend(struct.pack("<I", serial))
    header.extend(struct.pack("<I", sequence))
    header.extend(struct.pack("<I", 0))
    header.extend(bytes([len(segments)]))
    header.extend(bytes(segments))

    page = header + packet
    crc = _ogg_crc(page)
    page[22:26] = struct.pack("<I", crc)
    return bytes(page)


def build_ogg_opus(frames: list[bytes], sample_rate: int = 16000, channels: int = 1) -> bytes:
    if not frames:
        return b""

    serial = secrets.randbits(32)
    pages = []

    opus_head = (
        b"OpusHead"
        + bytes([1, channels])
        + struct.pack("<H", 312)
        + struct.pack("<I", sample_rate)
        + struct.pack("<h", 0)
        + b"\x00"
    )
    vendor = b"StackChan-Bridge"
    opus_tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)

    pages.append(_build_ogg_page(opus_head, serial, 0, 0, 0x02))
    pages.append(_build_ogg_page(opus_tags, serial, 1, 0, 0x00))

    granule_pos = 0
    samples_per_frame = 960
    sequence = 2
    for index, frame in enumerate(frames):
        granule_pos += samples_per_frame
        header_type = 0x04 if index == len(frames) - 1 else 0x00
        pages.append(_build_ogg_page(frame, serial, sequence, granule_pos, header_type))
        sequence += 1

    return b"".join(pages)


async def transcribe_audio_frames(opus_frames: list[bytes]) -> SttResult:
    if not opus_frames:
        return SttResult()
    if not CONFIG["GROQ_API_KEY"] or CONFIG["GROQ_API_KEY"] == "YOUR_GROQ_API_KEY_HERE":
        logger.warning("Groq API Key not configured, skip STT")
        return SttResult()

    logger.info("[Voice] STT prepare frames=%d", len(opus_frames))
    ogg_data = build_ogg_opus(opus_frames)
    if not ogg_data:
        logger.warning("[Voice] STT skipped because OGG payload is empty")
        return SttResult()
    logger.info("[Voice] STT ogg bytes=%d", len(ogg_data))

    energy_stats = await analyze_audio_energy(ogg_data)
    if energy_stats is not None:
        logger.info(
            "[Voice] audio energy samples=%d rms=%.1f peak=%d rms_dbfs=%.1f peak_ratio=%.1f",
            energy_stats.sample_count,
            energy_stats.rms,
            energy_stats.peak,
            energy_stats.rms_dbfs,
            energy_stats.peak_to_rms_ratio,
        )
        low_energy_reasons = get_low_energy_reasons(energy_stats)
        if low_energy_reasons:
            logger.info(
                "[Voice] STT skipped by low energy rms=%.1f peak=%d rms_dbfs=%.1f peak_ratio=%.1f reasons=%s",
                energy_stats.rms,
                energy_stats.peak,
                energy_stats.rms_dbfs,
                energy_stats.peak_to_rms_ratio,
                "; ".join(low_energy_reasons),
            )
            return SttResult(suppressed_hint=_LOW_ENERGY_STT_HINT)

    files = {"file": ("speech.ogg", ogg_data, "audio/ogg")}
    data = {
        "model": "whisper-large-v3",
        "language": "zh",
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
        "temperature": "0",
        "prompt": _BUILTIN_STT_PROMPT,
    }
    headers = {"Authorization": f"Bearer {CONFIG['GROQ_API_KEY']}"}

    async with httpx.AsyncClient(timeout=CONFIG["HTTP_TIMEOUT_SECONDS"]) as client:
        try:
            logger.info("[Voice] STT request -> %s", CONFIG["STT_API_URL"])
            response = await client.post(CONFIG["STT_API_URL"], files=files, data=data, headers=headers)
            response.raise_for_status()
            result = build_stt_result(response.json())
            logger.info(
                "[Voice] STT response ok chars=%d segments=%d duration=%.2f language=%s avg_logprob=%s mean_no_speech=%s max_no_speech=%s",
                len(result.text),
                result.segment_count,
                result.duration_seconds,
                result.language or "unknown",
                f"{result.avg_logprob:.2f}" if result.avg_logprob is not None else "n/a",
                f"{result.mean_no_speech_prob:.2f}" if result.mean_no_speech_prob is not None else "n/a",
                f"{result.max_no_speech_prob:.2f}" if result.max_no_speech_prob is not None else "n/a",
            )
            return result
        except Exception as exc:
            logger.error("STT Error: %s", exc)
            return SttResult()


async def text_to_speech_mp3_bytes(text: str) -> bytes:
    communicate = edge_tts.Communicate(text, CONFIG["VOICE_NAME"])
    audio_data = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data.extend(chunk["data"])
    return bytes(audio_data)


async def mp3_to_ogg_opus_bytes(mp3_bytes: bytes) -> bytes:
    ffmpeg_path = CONFIG["FFMPEG_PATH"]
    if not shutil.which(ffmpeg_path):
        raise RuntimeError(f"{ffmpeg_path} not found in PATH")

    process = await asyncio.create_subprocess_exec(
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-application",
        "voip",
        "-ar",
        str(CONFIG["TTS_SAMPLE_RATE"]),
        "-ac",
        "1",
        "-b:a",
        CONFIG["TTS_OPUS_BITRATE"],
        "-frame_duration",
        str(CONFIG["TTS_FRAME_DURATION_MS"]),
        "-f",
        "ogg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ogg_data, stderr = await process.communicate(mp3_bytes)
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="ignore").strip() or "ffmpeg opus transcode failed")
    return ogg_data


async def prepare_tts_audio(text: str) -> list[bytes]:
    mp3_bytes = await text_to_speech_mp3_bytes(text)
    ogg_data = await mp3_to_ogg_opus_bytes(mp3_bytes)
    return extract_ogg_opus_packets(ogg_data)


def extract_ogg_opus_packets(ogg_data: bytes) -> list[bytes]:
    packets: list[bytes] = []
    current_packet = bytearray()
    offset = 0

    while offset + 27 <= len(ogg_data):
        if ogg_data[offset : offset + 4] != b"OggS":
            raise ValueError("invalid ogg page")

        page_segments = ogg_data[offset + 26]
        segment_table_start = offset + 27
        segment_table_end = segment_table_start + page_segments
        if segment_table_end > len(ogg_data):
            raise ValueError("truncated ogg segment table")

        segment_table = ogg_data[segment_table_start:segment_table_end]
        payload_start = segment_table_end
        payload_end = payload_start + sum(segment_table)
        if payload_end > len(ogg_data):
            raise ValueError("truncated ogg payload")

        payload = ogg_data[payload_start:payload_end]
        payload_offset = 0
        for segment_size in segment_table:
            current_packet.extend(payload[payload_offset : payload_offset + segment_size])
            payload_offset += segment_size
            if segment_size < 255:
                packets.append(bytes(current_packet))
                current_packet.clear()

        offset = payload_end

    # 跳过 Ogg Opus 容器头，只保留真正的音频帧
    return [packet for packet in packets if not packet.startswith((b"OpusHead", b"OpusTags"))]


async def mp3_to_opus_packets(mp3_bytes: bytes) -> list[bytes]:
    ogg_data = await mp3_to_ogg_opus_bytes(mp3_bytes)
    packets = extract_ogg_opus_packets(ogg_data)
    if not packets:
        raise RuntimeError("no opus packets generated")
    return packets


def build_chat_upstream_messages(robot_id: str, user_text: str) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": BUILTIN_VOICE_STYLE_PROMPT}]
    prompt = get_robot_prompt(robot_id).strip()
    if prompt:
        messages.append({"role": "system", "content": prompt})
    messages.extend(list(get_robot_history(robot_id, create=True)))
    messages.append({"role": "user", "content": user_text})
    return messages


async def query_chat_upstream(robot_id: str, user_text: str) -> str:
    messages = build_chat_upstream_messages(robot_id, user_text)
    selected_provider_id = get_stackchan_selected_provider_id()
    selected_model_name = get_stackchan_selected_model_name()
    frontend_settings = load_stackchan_frontend_settings()
    headers = {
        "Authorization": f"Bearer {CONFIG['CHAT_UPSTREAM_KEY']}",
        "Content-Type": "application/json",
        "X-Session-Id": f"stackchan-{robot_id}",
        "X-Platform": "stackchan",
    }
    body = {
        "model": selected_model_name,
        "messages": messages,
        "metadata": {
            "source": "stackchan",
            "robot_id": robot_id,
        },
    }
    if selected_provider_id:
        body["metadata"]["astrbot_selected_provider"] = selected_provider_id

    logger.info(
        "[Chat] StackChan upstream request robot=%s provider=%s model=%s frontend_provider=%s frontend_model=%s",
        robot_id,
        selected_provider_id or "",
        selected_model_name,
        str(frontend_settings.get("stackchan_selected_provider") or "").strip(),
        str(frontend_settings.get("stackchan_selected_model") or "").strip(),
    )

    async with httpx.AsyncClient(timeout=CONFIG["HTTP_TIMEOUT_SECONDS"]) as client:
        response = await client.post(CONFIG["CHAT_UPSTREAM_URL"], json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        ai_text = sanitize_voice_text(data["choices"][0]["message"]["content"])

    history = get_robot_history(robot_id, create=True)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": ai_text})
    return ai_text


async def interpret_image(image_b64: str, prompt: str) -> str:
    """使用 Groq Vision 模型解读图片内容。"""
    headers = {
        "Authorization": f"Bearer {CONFIG['GROQ_API_KEY']}",
        "Content-Type": "application/json",
    }
    body = {
        "model": CONFIG["VISION_MODEL_NAME"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            }
        ],
    }
    async with httpx.AsyncClient(timeout=CONFIG["HTTP_TIMEOUT_SECONDS"]) as client:
        response = await client.post(CONFIG["VISION_API_URL"], json=body, headers=headers)
        if response.status_code != 200:
            logger.error("[Vision] Groq API error: %s", response.text)
            return f"视觉解读失败 (API 错误: {response.status_code})"
        
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()


def clear_session_audio_buffer(session: VoiceSession) -> None:
    session.audio_frames.clear()
    session.audio_frame_count = 0
    session.audio_byte_count = 0


def estimate_tts_playback_seconds(opus_packets: list[bytes]) -> float:
    if not opus_packets:
        return 1.0
    return max(1.0, len(opus_packets) * CONFIG["TTS_FRAME_DURATION_MS"] / 1000.0)


def arm_session_playback_guard(session: VoiceSession, duration_seconds: float) -> None:
    guard_until = time.perf_counter() + max(0.0, duration_seconds)
    session.playback_guard_until = max(session.playback_guard_until, guard_until)
    session.listening_guard_until = max(session.listening_guard_until, session.playback_guard_until)
    session.listening_guard_dropped_frames = 0


async def send_control_channel_tts(
    robot_id: str,
    text: str,
    emotion: int,
    opus_packets: list[bytes] | None = None,
) -> None:
    if get_connected_robot_socket(robot_id) is None:
        return

    if opus_packets is None:
        opus_packets = await prepare_tts_audio(text)

    await send_robot_message(
        robot_id,
        {
            "type": "tts_stream_start",
            "text": text,
            "emotion": emotion,
            "sample_rate": CONFIG["TTS_SAMPLE_RATE"],
            "frame_duration": CONFIG["TTS_FRAME_DURATION_MS"],
            "packet_count": len(opus_packets),
        },
    )

    for index, packet in enumerate(opus_packets):
        await send_robot_message(
            robot_id,
            {
                "type": "tts_stream_chunk",
                "index": index,
                "data": base64.b64encode(packet).decode("ascii"),
            },
        )

    await send_robot_message(robot_id, {"type": "tts_stream_end"})


async def send_control_channel_stop_tts(robot_id: str) -> None:
    if get_connected_robot_socket(robot_id) is None:
        return
    await send_robot_message(robot_id, {"type": "stop_tts"})


def get_active_voice_session(robot_id: str) -> VoiceSession | None:
    aliases = set(get_robot_id_aliases(robot_id))
    canonical = canonicalize_robot_id(robot_id)
    for session in voice_sessions.values():
        if canonicalize_robot_id(session.robot_id) in aliases:
            if canonical and session.robot_id != canonical:
                session.robot_id = canonical
            return session
    return None


async def cancel_task(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, RuntimeError):
        pass


async def cancel_session_tasks(session: VoiceSession) -> None:
    await cancel_task(session.processing_task)
    session.processing_task = None
    await cancel_task(session.tts_task)
    session.tts_task = None
    await cancel_task(session.heartbeat_task)
    session.heartbeat_task = None
    await cancel_task(session.finalize_task)
    session.finalize_task = None


async def keep_voice_session_alive(session: VoiceSession) -> None:
    while True:
        await asyncio.sleep(20)
        if session.closed:
            return
        await send_session_json(session, {"type": "ping"})


async def finalize_realtime_speech(session: VoiceSession, silence_seconds: float | None = None) -> None:
    if silence_seconds is None:
        silence_seconds = CONFIG["VOICE_FINALIZE_SILENCE_SECONDS"]
    check_interval = max(CONFIG["VOICE_ACTIVITY_CHECK_INTERVAL_SECONDS"], silence_seconds)

    while True:
        await asyncio.sleep(check_interval)
        if not session.listening or not session.audio_frames:
            return
        if session.processing_task is not None and not session.processing_task.done():
            return

        now = time.perf_counter()
        silence_gap = now - session.last_audio_frame_at if session.last_audio_frame_at else 0.0

        # In auto/manual mode, a real packet gap is still the most reliable natural end marker.
        if session.listen_mode != "realtime":
            if silence_gap < silence_seconds:
                continue
            logger.info(
                "[Voice] finalize robot=%s mode=%s frames=%d bytes=%d silence_gap=%.2fs",
                session.robot_id,
                session.listen_mode,
                session.audio_frame_count,
                session.audio_byte_count,
                silence_gap,
            )
            session.listening = False
            session.listening_started_at = 0.0
            session.last_audio_frame_at = 0.0
            session.processing_task = asyncio.create_task(run_processing_task(session))
            return

        # In realtime mode the robot may keep sending packets even during silence,
        # so inspect only the recent window instead of resetting finalize per frame.
        recent_limit = max(1, CONFIG["VOICE_ACTIVITY_WINDOW_FRAMES"])
        recent_frames = session.audio_frames[-recent_limit:]
        stats = await analyze_audio_energy(build_ogg_opus(recent_frames))
        active = has_voice_activity(stats)
        logger.info(
            "[Voice] realtime activity robot=%s active=%s frames=%d rms=%s peak=%s",
            session.robot_id,
            active,
            len(recent_frames),
            f"{stats.rms:.1f}" if stats is not None else "n/a",
            str(stats.peak) if stats is not None else "n/a",
        )
        if active:
            continue

        logger.info(
            "[Voice] finalize robot=%s mode=%s frames=%d bytes=%d recent_frames=%d",
            session.robot_id,
            session.listen_mode,
            session.audio_frame_count,
            session.audio_byte_count,
            len(recent_frames),
        )
        session.listening = False
        session.listening_started_at = 0.0
        session.last_audio_frame_at = 0.0
        session.processing_task = asyncio.create_task(run_processing_task(session))
        return


async def run_processing_task(session: VoiceSession) -> None:
    try:
        await process_voice_request(session)
    finally:
        session.processing_task = None


async def stream_native_tts_audio(session: VoiceSession, opus_packets: list[bytes]) -> None:
    frame_delay = CONFIG["TTS_FRAME_DURATION_MS"] / 1000.0

    for packet in opus_packets:
        await send_session_audio(session, packet)
        await asyncio.sleep(frame_delay)


async def speak_to_robot(robot_id: str, text: str, emotion: int) -> None:
    session = get_active_voice_session(robot_id)
    if session is not None:
        await cancel_task(session.tts_task)
        session.tts_task = asyncio.create_task(
            run_tts_response(session, text, emotion_name_from_index(emotion), emotion)
        )
        try:
            await session.tts_task
            return
        except Exception as exc:
            logger.warning("[Voice] Native TTS speak fallback for %s: %s", robot_id, exc)
        finally:
            session.tts_task = None

    await send_control_channel_tts(robot_id, text, emotion)


async def stop_robot_speaking(robot_id: str) -> None:
    session = get_active_voice_session(robot_id)
    if session is not None:
        session.listening = False
        session.playback_guard_until = 0.0
        clear_session_audio_buffer(session)
        await cancel_session_tasks(session)
        await send_session_json(session, {"type": "tts", "state": "stop"})
        await reset_session_emotion(session)
    await send_control_channel_stop_tts(robot_id)


async def reset_session_emotion(session: VoiceSession) -> None:
    if session.closed:
        return
    await send_session_json(session, {"type": "llm", "emotion": "neutral", "text": ""})


async def handle_tts_prepare_failure(
    session: VoiceSession,
    ai_text: str,
    emotion_name: str,
    exc: Exception,
) -> None:
    logger.exception("[Voice] TTS prepare failed for %s: %s", session.robot_id, exc)
    if session.closed:
        return

    try:
        message = ai_text.strip()
        if message:
            message = f"{message}\n{_TTS_PREPARE_FAILED_HINT}"
        else:
            message = _TTS_PREPARE_FAILED_HINT
        await send_session_json(session, {"type": "llm", "emotion": emotion_name, "text": ""})
        await send_session_json(session, {"type": "tts", "state": "sentence_start", "text": message})
        await send_session_json(session, {"type": "tts", "state": "stop"})
        await reset_session_emotion(session)
    except RuntimeError:
        logger.warning("[Voice] Session closed while reporting TTS prepare failure for %s", session.robot_id)


async def run_tts_response(session: VoiceSession, ai_text: str, emotion_name: str, emotion_index: int) -> None:
    opus_packets: list[bytes] = []
    used_control_fallback = False

    try:
        await cancel_task(session.finalize_task)
        session.finalize_task = None
        clear_session_audio_buffer(session)
        opus_packets = await prepare_tts_audio(ai_text)
    except Exception as exc:
        await handle_tts_prepare_failure(session, ai_text, emotion_name, exc)
        return

    try:
        await send_session_json(session, {"type": "llm", "emotion": emotion_name, "text": ""})
        await send_session_json(session, {"type": "tts", "state": "start"})
        await send_session_json(session, {"type": "tts", "state": "sentence_start", "text": ai_text})
    except RuntimeError as exc:
        logger.warning("[Voice] Session already closed for %s, fallback to control channel: %s", session.robot_id, exc)
        await send_control_channel_tts(session.robot_id, ai_text, emotion_index, opus_packets=opus_packets)
        return

    try:
        await stream_native_tts_audio(session, opus_packets)
    except Exception as exc:
        logger.warning("[Voice] Native TTS failed for %s, fallback to control channel: %s", session.robot_id, exc)
        used_control_fallback = True
        clear_session_audio_buffer(session)
        arm_session_playback_guard(session, estimate_tts_playback_seconds(opus_packets) + 0.8)
        await send_control_channel_tts(session.robot_id, ai_text, emotion_index, opus_packets=opus_packets)
    finally:
        if not session.closed:
            await send_session_json(session, {"type": "tts", "state": "stop"})
            if not used_control_fallback:
                await reset_session_emotion(session)


async def process_voice_request(session: VoiceSession) -> None:
    if not session.audio_frames:
        return

    try:
        frame_count = session.audio_frame_count
        byte_count = session.audio_byte_count
        logger.info(
            "[Voice] processing robot=%s frames=%d bytes=%d",
            session.robot_id,
            frame_count,
            byte_count,
        )
        started_at = time.perf_counter()
        stt_result = await transcribe_audio_frames(session.audio_frames)
        session.audio_frames.clear()
        session.audio_frame_count = 0
        session.audio_byte_count = 0
        stt_elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        user_text = stt_result.text

        if not user_text:
            user_text = stt_result.suppressed_hint or _EMPTY_STT_HINT
            logger.info("[Voice] %s STT empty (%d ms)", session.robot_id, stt_elapsed_ms)
            await send_stt_feedback(session, user_text)
            return

        if has_non_dialogue_noise_signature(user_text):
            logger.info("[Voice] %s non-dialogue STT ignored (%d ms)", session.robot_id, stt_elapsed_ms)
            await send_stt_feedback(session, _NOISE_STT_HINT)
            return

        if is_low_confidence_stt(stt_result, frame_count, byte_count):
            logger.info("[Voice] %s low-confidence STT ignored (%d ms)", session.robot_id, stt_elapsed_ms)
            await send_stt_feedback(session, _LOW_CONFIDENCE_STT_HINT)
            return

        if is_probably_spurious_short_text(user_text, frame_count, byte_count):
            logger.info("[Voice] %s likely spurious STT ignored (%d ms)", session.robot_id, stt_elapsed_ms)
            await send_stt_feedback(session, _NOISE_STT_HINT)
            return

        if is_echo_like_text(session.robot_id, user_text):
            logger.info("[Voice] %s echo-like STT ignored (%d ms)", session.robot_id, stt_elapsed_ms)
            await send_stt_feedback(session, _ECHO_STT_HINT)
            return

        logger.info("[Voice] %s -> %s (stt=%d ms)", session.robot_id, user_text, stt_elapsed_ms)
        await send_session_json(session, {"type": "stt", "text": user_text})

        try:
            upstream_started_at = time.perf_counter()
            ai_text = await query_chat_upstream(session.robot_id, user_text)
            upstream_elapsed_ms = int((time.perf_counter() - upstream_started_at) * 1000)
            logger.info("[Voice] %s chat_upstream=%d ms", session.robot_id, upstream_elapsed_ms)
        except Exception as exc:
            logger.error("[Voice] Gateway error: %s", exc)
            ai_text = "抱歉，我暂时连不上大脑服务。"

        emotion_name = guess_emotion_name(ai_text)
        emotion_index = guess_emotion(ai_text)
        session.tts_task = asyncio.create_task(run_tts_response(session, ai_text, emotion_name, emotion_index))
        try:
            await session.tts_task
        finally:
            session.tts_task = None
    except Exception:
        logger.exception("[Voice] process_voice_request crashed for %s", session.robot_id)
        session.audio_frames.clear()
        session.audio_frame_count = 0
        session.audio_byte_count = 0


# --- 4. MCP 工具定义 ---

@mcp.tool()
async def mcp__stackchan_action(
    action: Annotated[HwActionLiteral, "固件动作名。只允许使用固件已实现的枚举动作"],
    value: Annotated[int, "动作参数（角度-90~90，仅在 rotate_head/nod_head 时有效）"] = 0,
    robot_id: Annotated[str, "机器人的唯一标识符（MAC 地址）。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
) -> str:
    """控制机器人的物理动作。不要臆造新的 action，必须使用固件已实现的动作名。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    action = action.strip().lower()
    if action not in SUPPORTED_HW_ACTIONS:
        choices = "; ".join(f"{name}: {desc}" for name, desc in SUPPORTED_HW_ACTIONS.items())
        return f"错误：不支持的动作 {action}。仅支持：{choices}"
    error = validate_robot_availability(robot_id, require_control_channel=True, capability_name="动作控制")
    if error:
        return error
    try:
        logger.info("[MCP] hw_control robot=%s action=%s value=%d", robot_id, action, value)
        await send_robot_message(robot_id, {"type": "hw_control", "action": action, "value": value})
        return f"Success: Action {action} sent."
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def mcp__stackchan_capture_photo(robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "") -> str:
    """控制机器人拍照，将照片保存到 VPS 本地文件并返回路径。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    error = validate_robot_availability(robot_id, require_control_channel=True, capability_name="拍照")
    if error:
        return error

    # 清理旧的 Future 确保状态干净
    if robot_id in photo_futures:
        old_f = photo_futures.pop(robot_id)
        if not old_f.done(): old_f.cancel()

    future = asyncio.get_event_loop().create_future()
    photo_futures[robot_id] = future
    
    logger.info("[Vision] Requesting photo from %s", robot_id)
    await send_robot_message(robot_id, {"type": "capture_photo"})
    
    try:
        result = await asyncio.wait_for(future, timeout=60.0)
        if isinstance(result, dict):
            ok = bool(result.get("ok", False))
            if ok:
                path = str(result.get("path", ""))
                return f"成功拍照并保存！文件路径: {path}"
            message = str(result.get("message", "") or "拍照失败。")
            return f"错误：{message}"

        # 兼容旧固件：仍然接受 Base64 图片并在 VPS 侧落盘。
        b64_data = str(result)
        file_path = persist_photo_b64(robot_id, b64_data)
        file_size = os.path.getsize(file_path)
        return f"成功拍照并保存！文件路径: {file_path} (数据大小: {file_size} bytes)"
    except asyncio.TimeoutError:
        photo_futures.pop(robot_id, None)
        return "错误：拍照请求超时（机器人 60 秒内未回传图片数据）。"
    except Exception as e:
        photo_futures.pop(robot_id, None)
        return f"错误：保存照片失败 - {str(e)}"


@mcp.tool()
async def mcp__stackchan_interpret_photo(
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
    prompt: str = "请详细描述你看到的内容。",
) -> str:
    """让机器人拍照并使用 AI 进行视觉解读，解读结果将返回给 MCP。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    error = validate_robot_availability(robot_id, require_control_channel=True, capability_name="视觉解读")
    if error:
        return error

    if robot_id in interpret_futures:
        old_f = interpret_futures.pop(robot_id)
        if not old_f.done():
            old_f.cancel()
    interpret_requests.pop(robot_id, None)

    future = asyncio.get_event_loop().create_future()
    interpret_futures[robot_id] = future
    interpret_requests[robot_id] = {"prompt": prompt}
    await send_robot_message(robot_id, {"type": "interpret_photo", "prompt": prompt})
    
    try:
        result = await asyncio.wait_for(future, timeout=60.0)
        ok = bool(result.get("ok", False))
        text = str(result.get("text", ""))
        if ok:
            return f"视觉解读结果：\n{text}"
        return f"错误：{text or '视觉解读失败。'}"
    except asyncio.TimeoutError:
        interpret_futures.pop(robot_id, None)
        interpret_requests.pop(robot_id, None)
        return "错误：拍照请求超时。"
    except Exception as e:
        interpret_futures.pop(robot_id, None)
        interpret_requests.pop(robot_id, None)
        return f"错误：视觉解读失败 - {str(e)}"


@mcp.tool()
async def mcp__stackchan_switch_mode(
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
    xiaozhi_mode: bool = False,
) -> str:
    """切换机器人的工作模式（小智模式或原生模式）。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    error = validate_robot_availability(robot_id, require_control_channel=True, capability_name="模式切换")
    if error:
        return error

    await send_robot_message(robot_id, {"type": "set_mode", "xiaozhi": xiaozhi_mode})
    return f"已成功发送切换模式指令：{'小智模式' if xiaozhi_mode else '原生模式'}。"


@mcp.tool()
async def mcp__stackchan_speak(
    text: Annotated[str, "要显示或播报的文字内容"],
    emotion: Annotated[SpeechEmotionLiteral, "说话时使用的表情枚举。auto 表示根据文本自动推断"] = "auto",
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
) -> str:
    """让机器人通过语音播报一段文字。若 emotion 为 auto 或留空，会根据文本内容自动推断表情。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    error = validate_robot_availability(robot_id, allow_voice_session=True, capability_name="播报")
    if error:
        return error
    emotion_index, error = resolve_speech_emotion(emotion, text)
    if error:
        return error
    try:
        await speak_to_robot(robot_id, text, emotion_index)
        return f"Success: Robot {robot_id} is now speaking with emotion {emotion_name_from_index(emotion_index)}: {text}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def mcp__stackchan_set_emotion(
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
    emotion: Annotated[EmotionLiteral, "表情枚举。仅支持 happy/sad/angry/surprised/sleepy/neutral"] = "neutral",
) -> str:
    """只切换机器人表情，不播报文本。对应固件 `set_emotion` 指令。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    error = validate_robot_availability(robot_id, require_control_channel=True, capability_name="表情切换")
    if error:
        return error
    emotion_index, error = parse_emotion_value(emotion)
    if error:
        return error
    try:
        logger.info("[MCP] set_emotion robot=%s emotion=%s(%d)", robot_id, emotion, emotion_index)
        await send_robot_message(robot_id, {"type": "hw_control", "action": "set_emotion", "value": emotion_index})
        return f"成功：已将机器人 {robot_id} 的表情设置为 {emotion}({emotion_index})。"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def mcp__stackchan_stop_speaking(
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
) -> str:
    """强制打断机器人当前说话/播报状态。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    error = validate_robot_availability(robot_id, allow_voice_session=True, capability_name="停止播报")
    if error:
        return error
    try:
        await stop_robot_speaking(robot_id)
        return f"Success: Robot {robot_id} speaking task stopped."
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def mcp__stackchan_status(
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
) -> str:
    """查询机器人在线状态、主语音会话状态与关键依赖可用性。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    session = get_active_voice_session(robot_id)
    state = get_robot_connection_state(robot_id)
    status = {
        "robot_id": robot_id,
        "robot_online": state["robot_online"],
        "control_channel_online": state["control_channel_online"],
        "voice_session_online": state["voice_session_online"],
        "voice_session_id": session.session_id if session else "",
        "voice_listening": session.listening if session else False,
        "voice_protocol_version": session.protocol_version if session else 0,
        "ffmpeg_available": bool(shutil.which(CONFIG["FFMPEG_PATH"])),
        "has_robot_prompt": bool(get_robot_prompt(robot_id).strip()),
        "history_turns": len(get_robot_history(robot_id)) // 2,
    }
    return json.dumps(status, ensure_ascii=False)


@mcp.tool()
async def mcp__stackchan_set_prompt(
    prompt: Annotated[str, "要给该机器人的 system prompt / 提示词"],
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
) -> str:
    """设置机器人专属提示词。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    robot_prompts[robot_id] = prompt
    return f"Success: Prompt has been set for robot {robot_id}."


@mcp.tool()
async def mcp__stackchan_clear_prompt(
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
) -> str:
    """清除机器人专属提示词。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    key = migrate_robot_mapping_alias(robot_prompts, robot_id)
    if key and key in robot_prompts:
        del robot_prompts[key]
        return f"Success: Prompt cleared for robot {robot_id}."
    return f"Success: Robot {robot_id} had no custom prompt."


@mcp.tool()
async def mcp__stackchan_reset_dialogue(
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
) -> str:
    """清空 VPS 侧为该机器人维护的短会话上下文。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    history = get_robot_history(robot_id)
    if history:
        history.clear()
    return f"Success: Dialogue history cleared for robot {robot_id}."


@mcp.tool()
async def mcp__stackchan_switch_official(
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
) -> str:
    """一键切回小智官方模式。这会让机器人重启并连接到官方服务器。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    error = validate_robot_availability(robot_id, require_control_channel=True, capability_name="模式切换")
    if error:
        return error
    
    await send_robot_message(robot_id, {"type": "set_mode", "xiaozhi": True})
    return f"已成功向机器人 {robot_id} 发送切回官方模式的指令，机器人正在重启..."


@mcp.tool()
async def mcp__stackchan_list_photos(limit: int = 10) -> str:
    """查看 VPS 中保存过的机器人历史照片。不需要机器人在线。"""
    photo_dir = CONFIG["PHOTO_SAVE_DIR"]
    if not os.path.exists(photo_dir):
        return "VPS 中尚未创建照片目录。"
    
    files = [f for f in os.listdir(photo_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
    if not files:
        return "VPS 照片目录中还没有任何图片。"
    
    # 按修改时间倒序排列（最新的在前面）
    files.sort(key=lambda x: os.path.getmtime(os.path.join(photo_dir, x)), reverse=True)
    
    recent_files = files[:limit]
    result = [f"最近的 {len(recent_files)} 张照片："]
    for i, f in enumerate(recent_files, 1):
        file_path = os.path.join(photo_dir, f)
        mtime = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime('%Y-%m-%d %H:%M:%S')
        size_kb = os.path.getsize(file_path) / 1024
        result.append(f"{i}. {f} ({mtime}, {size_kb:.1f} KB)")
    
    result.append("\n你可以使用 mcp__stackchan_interpret_stored_photo(filename) 来让 AI 解读特定照片的内容。")
    return "\n".join(result)


@mcp.tool()
async def mcp__stackchan_interpret_stored_photo(filename: str, prompt: str = "请详细描述你看到的内容。") -> str:
    """让 AI 解读 VPS 中保存过的历史照片。不需要机器人在线。"""
    photo_dir = CONFIG["PHOTO_SAVE_DIR"]
    file_path = os.path.join(photo_dir, filename)
    
    # 安全性检查
    if not os.path.abspath(file_path).startswith(os.path.abspath(photo_dir)):
        return "错误：非法的文件路径。"
        
    if not os.path.exists(file_path):
        return f"错误：照片 {filename} 不存在。"
        
    try:
        # 1. 读取并转为 Base64
        with open(file_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
        
        # 2. 调用视觉模型解读
        logger.info("[Vision] Interpreting stored photo: %s", filename)
        result = await interpret_image(b64_data, prompt)
        
        return f"照片 {filename} 的解读结果：\n{result}"
    except Exception as e:
        return f"错误：解读历史照片失败 - {str(e)}"


@mcp.tool()
async def mcp__stackchan_get_photo_content(filename: str) -> str:
    """获取 VPS 中特定照片的 Base64 内容（用于 AI 解读或查看）。不需要机器人在线。"""
    photo_dir = CONFIG["PHOTO_SAVE_DIR"]
    file_path = os.path.join(photo_dir, filename)
    
    # 安全性检查：防止目录穿越
    if not os.path.abspath(file_path).startswith(os.path.abspath(photo_dir)):
        return "错误：非法的文件路径。"
        
    if not os.path.exists(file_path):
        return f"错误：文件 {filename} 不存在。"
        
    try:
        with open(file_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
        return f"照片 {filename} 的数据已提取。Base64 长度: {len(b64_data)}"
    except Exception as e:
        return f"错误：读取文件失败 - {str(e)}"


@mcp.tool()
async def mcp__stackchan_switch_native(
    robot_id: Annotated[str, "机器人的唯一标识符。不填时使用 STACKCHAN_DEFAULT_ROBOT_ID"] = "",
) -> str:
    """一键切到 VPS 主语音 WebSocket 代理模式。这会让机器人重启并连接到你的私有 VPS。"""
    robot_id, error = resolve_robot_id(robot_id)
    if error:
        return error
    error = validate_robot_availability(robot_id, require_control_channel=True, capability_name="模式切换")
    if error:
        return error
    
    await send_robot_message(robot_id, {"type": "set_mode", "xiaozhi": False})
    return f"已成功向机器人 {robot_id} 发送切换到原生模式的指令，机器人正在重启..."


# --- 5. 主语音 WebSocket 协议 (/ws) ---

app = FastAPI()


@app.post("/vision/explain")
async def vision_explain_handler(
    request: Request,
    file: UploadFile = File(...),
    question: str = Form("请详细描述你看到的内容。"),
):
    if not validate_control_auth_header(request.headers.get("authorization")):
        raise HTTPException(status_code=401, detail="unauthorized")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="empty image")

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    logger.info("[Vision] Explain endpoint file=%s size=%d question=%s", file.filename, len(image_bytes), question)
    result = await interpret_image(image_b64, question)
    return Response(content=result, media_type="text/plain; charset=utf-8")


@app.post("/capture")
async def capture_handler(
    request: Request,
    file: UploadFile = File(...),
    question: str = Form(""),
):
    if not validate_control_auth_header(request.headers.get("authorization")):
        raise HTTPException(status_code=401, detail="unauthorized")

    device_id = request.headers.get("device-id", "").strip() or "unknown-device"
    safe_device_id = sanitize_photo_device_id(device_id)
    file_path = build_photo_output_path(device_id)

    total_bytes = 0
    with file_path.open("wb") as output:
        while True:
            chunk = await file.read(8192)
            if not chunk:
                break
            output.write(chunk)
            total_bytes += len(chunk)

    await file.close()

    if total_bytes == 0:
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="empty image")

    logger.info(
        "[Vision] Capture endpoint saved device=%s file=%s size=%d ignored_question=%s",
        safe_device_id,
        file_path,
        total_bytes,
        bool(question),
    )
    return Response(content=str(file_path), media_type="text/plain; charset=utf-8")

@app.websocket("/ws")
async def main_voice_ws_handler(websocket: FastAPIWebSocket):
    if not validate_voice_auth(websocket):
        logger.warning(
            "[Voice] Auth failed: device_id=%s client_id=%s",
            websocket.headers.get("device-id") or websocket.query_params.get("device_id") or "unknown-device",
            websocket.headers.get("client-id") or "unknown-client",
        )
        await websocket.close(code=4401)
        return

    await websocket.accept()
    robot_id = canonicalize_robot_id(
        websocket.headers.get("device-id") or websocket.query_params.get("device_id") or "unknown-device"
    )
    client_id = websocket.headers.get("client-id") or uuid.uuid4().hex
    protocol_version = int(websocket.headers.get("protocol-version", "1") or "1")

    session = VoiceSession(
        websocket=websocket,
        robot_id=robot_id,
        client_id=client_id,
        protocol_version=protocol_version,
    )
    voice_sessions[client_id] = session
    logger.info("[Voice] Robot connected: robot_id=%s client_id=%s version=%s", robot_id, client_id, protocol_version)

    try:
        while True:
            message = await websocket.receive()

            if message.get("text") is not None:
                text = message["text"]
                try:
                    doc = json.loads(text)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "[Voice] Invalid JSON robot=%s client=%s error=%s raw=%r",
                        session.robot_id,
                        session.client_id,
                        exc,
                        text[:200],
                    )
                    continue
                msg_type = doc.get("type", "")

                if msg_type == "hello":
                    logger.info(
                        "[Voice] hello recv robot=%s client=%s version=%s transport=%s session_id=%s audio=%s",
                        session.robot_id,
                        session.client_id,
                        doc.get("version"),
                        doc.get("transport"),
                        doc.get("session_id"),
                        doc.get("audio_params"),
                    )
                    if doc.get("session_id"):
                        session.session_id = doc["session_id"]
                    hello = {
                        "type": "hello",
                        "version": 1,
                        "transport": "websocket",
                        "session_id": session.session_id,
                        "features": {"mcp": True},
                        "audio_params": {
                            "format": "opus",
                            "sample_rate": CONFIG["TTS_SAMPLE_RATE"],
                            "channels": 1,
                            "frame_duration": CONFIG["TTS_FRAME_DURATION_MS"],
                        },
                    }
                    logger.info(
                        "[Voice] hello send robot=%s client=%s session_id=%s audio=%s",
                        session.robot_id,
                        session.client_id,
                        hello["session_id"],
                        hello["audio_params"],
                    )
                    await send_session_json(session, hello)
                    await cancel_task(session.heartbeat_task)
                    session.heartbeat_task = asyncio.create_task(keep_voice_session_alive(session))
                    continue

                if msg_type == "listen":
                    if doc.get("session_id"):
                        session.session_id = doc["session_id"]
                    state = doc.get("state", "")
                    session.listen_mode = doc.get("mode", session.listen_mode)
                    logger.info(
                        "[Voice] listen event robot=%s state=%s mode=%s",
                        session.robot_id,
                        state,
                        session.listen_mode,
                    )
                    if state in {"start", "detect"}:
                        await cancel_task(session.processing_task)
                        session.processing_task = None
                        await cancel_task(session.finalize_task)
                        session.finalize_task = None
                        clear_session_audio_buffer(session)
                        now = time.perf_counter()
                        guard_until = now + CONFIG["VOICE_LISTENING_GUARD_MS"] / 1000.0
                        session.listening_guard_until = max(guard_until, session.playback_guard_until)
                        session.listening_started_at = now
                        session.last_audio_frame_at = 0.0
                        session.listening_guard_dropped_frames = 0
                        session.listening = True
                    elif state == "stop":
                        session.listening = False
                        session.listening_started_at = 0.0
                        session.last_audio_frame_at = 0.0
                        await cancel_task(session.finalize_task)
                        session.finalize_task = None
                        await cancel_task(session.processing_task)
                        session.processing_task = asyncio.create_task(process_voice_request(session))
                    continue

                if msg_type == "abort":
                    if doc.get("session_id"):
                        session.session_id = doc["session_id"]
                    session.playback_guard_until = 0.0
                    clear_session_audio_buffer(session)
                    session.listening = False
                    await cancel_session_tasks(session)
                    await send_session_json(session, {"type": "tts", "state": "stop"})
                    await send_control_channel_stop_tts(session.robot_id)
                    continue

                if msg_type == "ping":
                    await send_session_json(session, {"type": "pong"})
                    continue

                if msg_type == "pong":
                    continue

                continue

            if message.get("bytes") is not None:
                raw_audio = unwrap_audio_payload(session.protocol_version, message["bytes"])
                if raw_audio and session.listening:
                    now = time.perf_counter()
                    if now < session.listening_guard_until:
                        session.listening_guard_dropped_frames += 1
                        if session.listening_guard_dropped_frames == 1 or session.listening_guard_dropped_frames % 20 == 0:
                            logger.info(
                                "[Voice] guard drop robot=%s frame=%d chunk=%d",
                                session.robot_id,
                                session.listening_guard_dropped_frames,
                                len(raw_audio),
                            )
                        continue
                    session.audio_frames.append(raw_audio)
                    session.audio_frame_count += 1
                    session.audio_byte_count += len(raw_audio)
                    session.last_audio_frame_at = now
                    if session.audio_frame_count == 1 or session.audio_frame_count % 20 == 0:
                        logger.info(
                            "[Voice] audio frame robot=%s frame=%d chunk=%d total_bytes=%d",
                            session.robot_id,
                            session.audio_frame_count,
                            len(raw_audio),
                            session.audio_byte_count,
                        )
                    elapsed_listening = now - session.listening_started_at if session.listening_started_at else 0.0
                    if elapsed_listening >= CONFIG["VOICE_MAX_LISTEN_SECONDS"]:
                        logger.warning(
                            "[Voice] max listen reached robot=%s elapsed=%.2fs frames=%d bytes=%d; force processing",
                            session.robot_id,
                            elapsed_listening,
                            session.audio_frame_count,
                            session.audio_byte_count,
                        )
                        session.listening = False
                        session.listening_started_at = 0.0
                        session.last_audio_frame_at = 0.0
                        await cancel_task(session.finalize_task)
                        session.finalize_task = None
                        await cancel_task(session.processing_task)
                        session.processing_task = asyncio.create_task(process_voice_request(session))
                    else:
                        if session.finalize_task is None or session.finalize_task.done():
                            session.finalize_task = asyncio.create_task(finalize_realtime_speech(session))
                elif session.listening:
                    logger.warning(
                        "[Voice] empty audio payload robot=%s raw_len=%d version=%d",
                        session.robot_id,
                        len(message["bytes"]),
                        session.protocol_version,
                    )
                continue

            if message.get("type") == "websocket.disconnect":
                session.closed = True
                logger.info(
                    "[Voice] disconnect event robot=%s client=%s code=%s",
                    session.robot_id,
                    session.client_id,
                    message.get("code"),
                )
                break

    except Exception as exc:
        session.closed = True
        logger.error("[Voice] Main protocol error for %s: %s", robot_id, exc)
    finally:
        session.closed = True
        await cancel_session_tasks(session)
        voice_sessions.pop(client_id, None)
        logger.info(
            "[Voice] session closed robot=%s client=%s session_id=%s listening=%s frames=%d bytes=%d",
            session.robot_id,
            session.client_id,
            session.session_id,
            session.listening,
            session.audio_frame_count,
            session.audio_byte_count,
        )


# --- 6. StackChan App/Avatar WebSocket (/stackChan/ws) ---

@app.websocket("/stackChan/ws")
async def stackchan_avatar_proxy_handler(websocket: FastAPIWebSocket):
    await websocket.accept()
    logger.info("[Avatar] Robot connected to /stackChan/ws proxy")
    try:
        while True:
            message = await websocket.receive()
            if message.get("text") is not None:
                logger.info("[Avatar] Text frame: %s", message["text"])
                continue

            if message.get("bytes") is None:
                continue

            raw = message["bytes"]
            packet_type, payload = parse_binary_packet(raw)

            if packet_type == PacketType.HEARTBEAT_PING:
                await websocket.send_bytes(build_binary_packet(PacketType.HEARTBEAT_PONG))
                continue

            if packet_type == PacketType.TEXT_MESSAGE:
                logger.info("[Avatar] TextMessage size=%s", len(payload))
                continue
    except Exception as exc:
        logger.error("[Avatar] WebSocket Error: %s", exc)


@app.websocket("/xiaozhi/ws")
async def xiaozhi_proxy_handler_compat(websocket: FastAPIWebSocket):
    await main_voice_ws_handler(websocket)


# --- 7. 机器人控制通道 ---

async def process_robot_control_message(message: str, robot_socket: object) -> str | None:
    data = json.loads(message)
    msg_type = data.get("type")

    if msg_type == "register":
        device_id = canonicalize_robot_id(data.get("id"))
        token = data.get("token")
        if not device_id:
            return None
            
        # 校验 Token (可选，为了安全建议开启)
        if not validate_control_auth_header(token):
            logger.warning("[WSS] Robot registration failed: Invalid token from %s", device_id)
            return None

        connected_robots[device_id] = robot_socket
        logger.info("[WSS] Robot registered: %s", device_id)
        await send_robot_json(robot_socket, {"type": "reg_ack", "status": "ok"})
        return device_id

    if msg_type == "ping":
        await send_robot_json(robot_socket, {"type": "pong"})

    if msg_type == "photo_data":
        # 寻找对应的 robot_id
        robot_id = None
        for rid, sock in connected_robots.items():
            if sock is robot_socket:
                robot_id = rid
                break
        
        if robot_id and robot_id in photo_futures:
            data_len = len(data.get("data", ""))
            logger.info("[WSS] Received photo data from %s (size: %d bytes)", robot_id, data_len)
            future = photo_futures.pop(robot_id)
            if not future.done():
                future.set_result(data.get("data"))

    if msg_type == "photo_capture_result":
        robot_id = None
        for rid, sock in connected_robots.items():
            if sock is robot_socket:
                robot_id = rid
                break

        if robot_id and robot_id in photo_futures:
            result = {
                "ok": bool(data.get("ok", False)),
                "path": str(data.get("path", "")),
                "message": str(data.get("message", "")),
            }
            logger.info(
                "[WSS] Capture result from %s ok=%s path=%s message_len=%d",
                robot_id,
                result["ok"],
                result["path"],
                len(result["message"]),
            )
            future = photo_futures.pop(robot_id)
            if not future.done():
                future.set_result(result)

    if msg_type == "photo_begin":
        robot_id = None
        for rid, sock in connected_robots.items():
            if sock is robot_socket:
                robot_id = rid
                break

        if robot_id:
            total_chunks = int(data.get("total_chunks", 0) or 0)
            photo_uploads[robot_id] = {
                "chunks": [],
                "total_chunks": total_chunks,
            }
            logger.info("[WSS] Photo upload begin from %s chunks=%d size=%s", robot_id, total_chunks, data.get("size"))

    if msg_type == "photo_chunk":
        robot_id = None
        for rid, sock in connected_robots.items():
            if sock is robot_socket:
                robot_id = rid
                break

        if robot_id:
            upload = photo_uploads.setdefault(robot_id, {"chunks": [], "total_chunks": 0})
            chunk_data = str(data.get("data", ""))
            upload["chunks"].append(chunk_data)

    if msg_type == "photo_end":
        robot_id = None
        for rid, sock in connected_robots.items():
            if sock is robot_socket:
                robot_id = rid
                break

        if robot_id:
            upload = photo_uploads.pop(robot_id, None) or {"chunks": [], "total_chunks": 0}
            chunks = list(upload.get("chunks", []))
            chunk_count = len(chunks)
            expected_chunks = int(data.get("total_chunks", 0) or upload.get("total_chunks", 0) or 0)
            logger.info(
                "[WSS] Photo upload end from %s chunks=%d expected=%d total_b64=%d",
                robot_id,
                chunk_count,
                expected_chunks,
                sum(len(chunk) for chunk in chunks),
            )
            if expected_chunks and chunk_count != expected_chunks:
                logger.warning(
                    "[WSS] Photo chunk count mismatch from %s expected=%d got=%d",
                    robot_id,
                    expected_chunks,
                    chunk_count,
                )

            if robot_id in photo_futures:
                future = photo_futures.pop(robot_id)
                if not future.done():
                    try:
                        image_bytes = decode_photo_b64_chunks(chunks)
                        file_path = persist_photo_bytes(robot_id, image_bytes)
                        future.set_result({"ok": True, "path": file_path, "message": ""})
                    except Exception as exc:
                        logger.exception("[WSS] Failed to persist photo from %s: %s", robot_id, exc)
                        future.set_result({"ok": False, "path": "", "message": f"保存照片失败: {exc}"})
            elif robot_id in interpret_futures:
                future = interpret_futures.pop(robot_id)
                request = interpret_requests.pop(robot_id, None) or {}
                prompt = str(request.get("prompt", "") or "请详细描述你看到的内容。")
                if not future.done():
                    try:
                        image_bytes = decode_photo_b64_chunks(chunks)
                        text, file_path = await interpret_uploaded_photo(robot_id, image_bytes, prompt)
                        logger.info(
                            "[WSS] Interpret completed from %s file=%s text_len=%d",
                            robot_id,
                            file_path,
                            len(text),
                        )
                        future.set_result({"ok": True, "text": text})
                    except Exception as exc:
                        logger.exception("[WSS] Failed to interpret uploaded photo from %s: %s", robot_id, exc)
                        future.set_result({"ok": False, "text": f"视觉解读失败: {exc}"})

    if msg_type == "photo_interpret_result":
        robot_id = None
        for rid, sock in connected_robots.items():
            if sock is robot_socket:
                robot_id = rid
                break

        if robot_id and robot_id in interpret_futures:
            result = {
                "ok": bool(data.get("ok", False)),
                "text": str(data.get("text", "")),
            }
            interpret_requests.pop(robot_id, None)
            logger.info(
                "[WSS] Interpret result from %s ok=%s text_len=%d",
                robot_id,
                result["ok"],
                len(result["text"]),
            )
            future = interpret_futures.pop(robot_id)
            if not future.done():
                future.set_result(result)

    return None

async def robot_ws_handler(websocket):
    device_id = None
    try:
        async for message in websocket:
            new_device_id = await process_robot_control_message(message, websocket)
            if new_device_id:
                device_id = new_device_id
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if device_id in connected_robots and connected_robots.get(device_id) is websocket:
            del connected_robots[device_id]
        photo_uploads.pop(device_id, None)
        future = photo_futures.pop(device_id, None)
        if future and not future.done():
            future.cancel()
        future = interpret_futures.pop(device_id, None)
        if future and not future.done():
            future.cancel()
        interpret_requests.pop(device_id, None)


@app.websocket("/robot-wss")
async def robot_ws_handler_http(websocket: FastAPIWebSocket):
    await websocket.accept()
    device_id = None
    try:
        while True:
            message = await websocket.receive()
            if message.get("text") is None:
                if message.get("type") == "websocket.disconnect":
                    break
                continue

            new_device_id = await process_robot_control_message(message["text"], websocket)
            if new_device_id:
                device_id = new_device_id
    except Exception as exc:
        logger.error("[WSS] FastAPI control channel error: %s", exc)
    finally:
        if device_id in connected_robots and connected_robots.get(device_id) is websocket:
            del connected_robots[device_id]
        photo_uploads.pop(device_id, None)
        future = photo_futures.pop(device_id, None)
        if future and not future.done():
            future.cancel()
        future = interpret_futures.pop(device_id, None)
        if future and not future.done():
            future.cancel()
        interpret_requests.pop(device_id, None)


# --- 8. 系统启动 ---

security_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)
sse = SseServerTransport("/messages/", security_settings=security_settings)
app.router.routes.append(Mount("/messages", app=sse.handle_post_message))


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with websockets.serve(robot_ws_handler, CONFIG["CONTROL_WS_HOST"], CONFIG["CONTROL_WS_PORT"]):
        logger.info(
            "WSS Control Server started on %s:%s",
            CONFIG["CONTROL_WS_HOST"],
            CONFIG["CONTROL_WS_PORT"],
        )
        yield


app.router.lifespan_context = lifespan


@app.get("/sse")
async def handle_sse(request: Request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp._mcp_server.run(streams[0], streams[1], mcp._mcp_server.create_initialization_options())
    return Response()


@app.get("/messages", include_in_schema=False)
async def handle_messages_docs():
    return Response(status_code=204)


@app.get("/health")
async def health_check():
    return {
        "ok": True,
        "robots_online": len(connected_robots),
        "voice_sessions": len(voice_sessions),
        "ffmpeg_available": bool(shutil.which(CONFIG["FFMPEG_PATH"])),
        "groq_configured": CONFIG["GROQ_API_KEY"] != "YOUR_GROQ_API_KEY_HERE",
    }


if __name__ == "__main__":
    uvicorn.run(app, host=CONFIG["APP_HOST"], port=CONFIG["APP_PORT"])
