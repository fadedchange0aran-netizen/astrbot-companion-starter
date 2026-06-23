import base64
import json
import os
import posixpath
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, register, StarTools
from bs4 import BeautifulSoup
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSesion

try:
    from quart import request as _quart_request
except Exception:
    _quart_request = None


def _safe_name(name: str) -> str:
    """把书名转换成安全目录名，避免路径穿越。"""
    name = name.strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = name.replace("..", "_")
    return name[:80] or "未命名书籍"


# 预编译正则，避免每次调用重复编译
_RE_CHAPTER = re.compile(
    r"(?m)^\s*((?:第\s*[一二三四五六七八九十百千万零〇0-9]+\s*[章节卷回篇幕].*)|(?:CHAPTER\s+[0-9IVXLCDM]+.*))\s*$",
    re.IGNORECASE,
)
_RE_NORMALIZE = re.compile(r"\n{3,}")
_RE_READ_CHAPTER = re.compile(r"^/?读第\s+(.+?)\s+第?\s*(\d+)\s*章?$")
_RE_CONTINUE_READ = re.compile(r"^/?继续读\s+(.+?)\s*$")
_RE_NEXT_CHAPTER = re.compile(r"^/?下一章\s+(.+?)\s*$")
_RE_PREV_CHAPTER = re.compile(r"^/?上一章\s+(.+?)\s*$")
_RE_JUMP_CHAPTER = re.compile(r"^/?跳到\s+(.+?)\s+第?\s*(\d+)\s*章?$")
_RE_IMPORT_VAULT = re.compile(r"^/?导入代存书籍\s+(\S+)(?:\s+(.+))?$")
_RE_WRITE_NOTE = re.compile(r"^/?写笔记\s+(.+?)\s+第?\s*(\d+)\s*章\s+(.+)$", re.S)
_RE_READ_NOTES = re.compile(r"^/?看笔记\s+(.+?)\s+第?\s*(\d+)\s*章?$")
_RE_WRITE_THOUGHT = re.compile(r"^/?读后感\s+(.+?)\s+第?\s*(\d+)\s*章\s+(.+)$", re.S)
_RE_READ_THOUGHTS = re.compile(r"^/?看读后感\s+(.+?)(?:\s+第?\s*(\d+)\s*章?)?$")
_RE_TRAILING_BRACKETS = re.compile(r"\s*[（(][^()（）]{1,80}[)）]\s*$")
SUPPORTED_BOOK_SUFFIXES = (".txt", ".epub")
DEFAULT_FILE_VAULT_ROOTS = (
    "/AstrBot/data/file_vault",
    f"{os.environ.get("ASTRBOT_HOST_DATA_ROOT", "/var/lib/astrbot/data").rstrip("/")}/file_vault",
    os.path.abspath("data/file_vault"),
)
HOST_ASTRBOT_DATA_ROOT = os.environ.get("ASTRBOT_HOST_DATA_ROOT", "/var/lib/astrbot/data")
CONTAINER_ASTRBOT_DATA_ROOT = "/AstrBot/data"
DEFAULT_QQ_TARGET_KV_KEY = "default_qq_target_umo"
DEFAULT_CALL_CONTEXT_MODE_KV_KEY = "default_call_context_mode"
QQ_DISCUSSION_HISTORY_LIMIT = 6
CALL_CONTEXT_MODE_AUTO = "auto"
CALL_CONTEXT_MODE_FULL = "full"
CALL_CONTEXT_MODE_EXCERPT = "excerpt"
CALL_CONTEXT_MODES = {
    CALL_CONTEXT_MODE_AUTO,
    CALL_CONTEXT_MODE_FULL,
    CALL_CONTEXT_MODE_EXCERPT,
}


def _ensure_dir(data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)


def _book_dir(data_dir: str, book_name: str) -> str:
    return os.path.join(data_dir, _safe_name(book_name))


def _imports_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "imports")


def _index_path(data_dir: str, book_name: str) -> str:
    return os.path.join(_book_dir(data_dir, book_name), "index.json")


def _notes_path(data_dir: str, book_name: str) -> str:
    return os.path.join(_book_dir(data_dir, book_name), "notes.json")


def _thoughts_path(data_dir: str, book_name: str) -> str:
    return os.path.join(_book_dir(data_dir, book_name), "thoughts.json")


def _chapter_path(data_dir: str, book_name: str, chapter_no: int) -> str:
    return os.path.join(_book_dir(data_dir, book_name), f"chapter_{chapter_no:04d}.txt")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"bookshelf: failed to load json {path}: {exc}")
        return default


def _save_json(path: str, data: Any) -> None:
    """原子写入：先写临时文件再rename，防止写入中途崩溃导致数据损坏"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _RE_NORMALIZE.sub("\n\n", text)
    return text.strip()


def _trim_book_extension(name: str) -> str:
    value = str(name or "").strip()
    lower_value = value.lower()
    for suffix in SUPPORTED_BOOK_SUFFIXES:
        if lower_value.endswith(suffix):
            return value[: -len(suffix)].rstrip()
    return value


def _collapse_book_spaces(text: str) -> str:
    return " ".join(str(text or "").replace("_", " ").split()).strip()


def _simplify_book_title(name: str) -> str:
    value = _collapse_book_spaces(_trim_book_extension(name))
    if not value:
        return ""
    value = value.replace("<省略>", "").strip(" .-_")
    while True:
        updated = _RE_TRAILING_BRACKETS.sub("", value).strip()
        if updated == value:
            break
        value = updated
    for splitter in ("（", "("):
        if splitter in value:
            head = value.split(splitter, 1)[0].strip()
            if head:
                value = head
                break
    return value.strip(" .-_")


def _lookup_key(name: str) -> str:
    return re.sub(r"\s+", "", _collapse_book_spaces(_trim_book_extension(name))).casefold()


def _build_book_aliases(name: str, extra_aliases: Optional[List[str]] = None) -> List[str]:
    candidates = [str(name or "").strip(), _trim_book_extension(name), _simplify_book_title(name)]
    if extra_aliases:
        candidates.extend(str(item or "").strip() for item in extra_aliases)
    aliases: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _collapse_book_spaces(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(normalized)
    return aliases


def _read_text_file(path: str) -> str:
    encodings = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "utf-16-le", "utf-16-be")
    for encoding in encodings:
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _read_text_bytes(data: bytes) -> str:
    encodings = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "utf-16-le", "utf-16-be")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _html_to_text(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    return _normalize_text(soup.get_text("\n"))


def _find_epub_rootfile_path(archive: zipfile.ZipFile) -> str:
    try:
        container_xml = _read_text_bytes(archive.read("META-INF/container.xml"))
        root = ET.fromstring(container_xml)
    except Exception as exc:
        raise ValueError(f"EPUB 缺少有效的 container.xml：{exc}") from exc

    for node in root.findall(".//{*}rootfile"):
        full_path = str(node.attrib.get("full-path") or "").strip()
        if full_path:
            return full_path
    raise ValueError("EPUB 里没有找到 OPF 根文件。")


def _extract_epub_text(path: str) -> str:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"EPUB 文件损坏或格式不正确：{exc}") from exc

    with archive:
        names = set(archive.namelist())
        rootfile_path = _find_epub_rootfile_path(archive)
        if rootfile_path not in names:
            raise ValueError(f"EPUB 根文件不存在：{rootfile_path}")

        try:
            opf_root = ET.fromstring(_read_text_bytes(archive.read(rootfile_path)))
        except Exception as exc:
            raise ValueError(f"EPUB 的 OPF 清单无法解析：{exc}") from exc

        opf_dir = posixpath.dirname(rootfile_path)
        manifest: Dict[str, Dict[str, str]] = {}
        for item in opf_root.findall(".//{*}manifest/{*}item"):
            item_id = str(item.attrib.get("id") or "").strip()
            href = str(item.attrib.get("href") or "").strip()
            media_type = str(item.attrib.get("media-type") or "").strip().lower()
            if item_id and href:
                manifest[item_id] = {"href": href, "media_type": media_type}

        text_chunks: List[str] = []
        visited_paths: set[str] = set()
        for itemref in opf_root.findall(".//{*}spine/{*}itemref"):
            item_id = str(itemref.attrib.get("idref") or "").strip()
            item = manifest.get(item_id)
            if not item:
                continue
            href = item.get("href", "")
            media_type = item.get("media_type", "")
            book_path = posixpath.normpath(posixpath.join(opf_dir, href))
            if book_path in visited_paths or book_path not in names:
                continue
            if media_type and "html" not in media_type and not book_path.lower().endswith(
                (".html", ".htm", ".xhtml")
            ):
                continue
            visited_paths.add(book_path)
            text = _html_to_text(_read_text_bytes(archive.read(book_path)))
            if text:
                text_chunks.append(text)

        if not text_chunks:
            for name in sorted(names):
                lower_name = name.lower()
                if not lower_name.endswith((".html", ".htm", ".xhtml")):
                    continue
                if "/toc" in lower_name or "nav" in lower_name:
                    continue
                text = _html_to_text(_read_text_bytes(archive.read(name)))
                if text:
                    text_chunks.append(text)

        text = _normalize_text("\n\n".join(text_chunks))
        if not text:
            raise ValueError("EPUB 里没有可用的正文文本。")
        return text


def _read_book_source(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".txt":
        return _read_text_file(path)
    if suffix == ".epub":
        return _extract_epub_text(path)
    raise ValueError("只支持 .txt 或 .epub 文件。")


def _is_supported_book_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_BOOK_SUFFIXES


def _resolve_import_file(data_dir: str, relative_path: str) -> str:
    raw_value = (relative_path or "").strip()
    if not raw_value:
        raise ValueError("请提供要导入的文件名。")

    candidate = Path(raw_value)
    if candidate.is_absolute():
        raise ValueError("只支持导入 imports 目录里的相对路径。")

    imports_root = Path(_imports_dir(data_dir)).resolve()
    target = (imports_root / candidate).resolve()
    try:
        target.relative_to(imports_root)
    except ValueError as exc:
        raise ValueError("文件路径越界，只能读取 imports 目录内的文件。") from exc

    if not _is_supported_book_file(target):
        raise ValueError("只支持导入 .txt 或 .epub 文件。")
    if not target.exists() or not target.is_file():
        raise ValueError(f"没找到文件：{raw_value}")
    return str(target)


def _split_chapters(text: str) -> List[Tuple[str, str]]:
    """按常见中文/英文章节标题切分；找不到章节时按约 3000 字切。"""
    text = _normalize_text(text)
    if not text:
        return []

    matches = list(_RE_CHAPTER.finditer(text))

    chapters: List[Tuple[str, str]] = []
    if matches:
        for i, m in enumerate(matches):
            title = m.group(1).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            chapters.append((title, body or title))
        return chapters

    chunk_size = 3000
    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size].strip()
        if chunk:
            chapters.append((f"第 {len(chapters) + 1} 章", chunk))
    return chapters


def _save_book(data_dir: str, book_name: str, text: str) -> Dict[str, Any]:
    _ensure_dir(data_dir)
    book_dir = _book_dir(data_dir, book_name)
    os.makedirs(book_dir, exist_ok=True)

    chapters = _split_chapters(text)
    if not chapters:
        raise ValueError("文本为空，无法保存。")

    chapter_meta = []
    for idx, (title, content) in enumerate(chapters, start=1):
        with open(_chapter_path(data_dir, book_name, idx), "w", encoding="utf-8") as f:
            f.write(content)
        chapter_meta.append({"no": idx, "title": title, "chars": len(content)})

    index = {
        "name": book_name.strip(),
        "aliases": _build_book_aliases(book_name),
        "safe_name": _safe_name(book_name),
        "created_at": _now(),
        "updated_at": _now(),
        "current_chapter": 1,
        "total_chapters": len(chapters),
        "chapters": chapter_meta,
    }
    _save_json(_index_path(data_dir, book_name), index)
    if not os.path.exists(_notes_path(data_dir, book_name)):
        _save_json(_notes_path(data_dir, book_name), [])
    if not os.path.exists(_thoughts_path(data_dir, book_name)):
        _save_json(_thoughts_path(data_dir, book_name), [])
    return index


def _choose_available_book_name(data_dir: str, book_name: str) -> str:
    base_name = _collapse_book_spaces(str(book_name or "").strip()) or "未命名书籍"
    candidate = base_name
    serial = 2
    while os.path.exists(_index_path(data_dir, candidate)):
        candidate = f"{base_name}（{serial}）"
        serial += 1
    return candidate


def _resolve_book_index(data_dir: str, book_name: str) -> Tuple[Optional[Dict[str, Any]], str]:
    requested = str(book_name or "").strip()
    if not requested:
        return None, requested

    direct_path = _index_path(data_dir, requested)
    if os.path.exists(direct_path):
        data = _load_json(direct_path, None)
        if data:
            return data, str(data.get("name") or requested)

    requested_key = _lookup_key(requested)
    requested_simple_key = _lookup_key(_simplify_book_title(requested))
    best_match: Optional[Dict[str, Any]] = None
    best_score = -1

    for index in _list_books(data_dir):
        canonical_name = str(index.get("name") or "").strip()
        aliases = _build_book_aliases(canonical_name, index.get("aliases") or [])
        alias_keys = {_lookup_key(alias) for alias in aliases if alias}
        if not alias_keys:
            continue

        score = -1
        if requested_key and requested_key in alias_keys:
            score = 100
        elif requested_simple_key and requested_simple_key in alias_keys:
            score = 95
        else:
            for alias in aliases:
                alias_key = _lookup_key(alias)
                if requested_key and requested_key and requested_key in alias_key:
                    score = max(score, 80)
                if requested_simple_key and requested_simple_key and requested_simple_key in alias_key:
                    score = max(score, 70)
                if alias_key and requested_key and alias_key in requested_key:
                    score = max(score, 60)

        if score > best_score:
            best_match = index
            best_score = score

    if best_match is None:
        return None, requested
    return best_match, str(best_match.get("name") or requested)


def _load_index(data_dir: str, book_name: str) -> Optional[Dict[str, Any]]:
    index, _resolved_name = _resolve_book_index(data_dir, book_name)
    return index


def _list_books(data_dir: str) -> List[Dict[str, Any]]:
    _ensure_dir(data_dir)
    books = []
    for dirname in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, dirname, "index.json")
        if os.path.exists(path):
            data = _load_json(path, None)
            if data:
                books.append(data)
    return books


def _author_name(event: AstrMessageEvent) -> str:
    uid = str(event.get_sender_id())
    owner_uid = os.environ.get("BOOKSHELF_OWNER_UID", "")
    if uid == owner_uid and owner_uid:
        return os.environ.get("BOOKSHELF_OWNER_NAME", "主人")
    return event.get_sender_name() or uid


def _format_chapter_preview(data_dir: str, book_name: str, chapter_no: int, limit: int = 3500) -> str:
    index, resolved_name = _resolve_book_index(data_dir, book_name)
    if not index:
        return f"没找到《{book_name}》。"
    total = int(index.get("total_chapters", 0))
    if chapter_no < 1 or chapter_no > total:
        return f"章节不存在。《{resolved_name}》共有 {total} 章。"

    path = _chapter_path(data_dir, resolved_name, chapter_no)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    title = index["chapters"][chapter_no - 1].get("title", f"第 {chapter_no} 章")
    index["current_chapter"] = chapter_no
    index["updated_at"] = _now()
    _save_json(_index_path(data_dir, resolved_name), index)

    suffix = "" if len(content) <= limit else f"\n\n……本章较长，已截断显示前 {limit} 字。"
    return f"《{resolved_name}》\n{title}\n\n{content[:limit]}{suffix}"


def _chapter_title(index: Dict[str, Any], chapter_no: int) -> str:
    chapters = index.get("chapters", []) or []
    if 1 <= chapter_no <= len(chapters):
        return str(chapters[chapter_no - 1].get("title") or f"第 {chapter_no} 章")
    return f"第 {chapter_no} 章"


def _trim_inline(text: str, limit: int = 72) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _build_recent_discussion_lines(
    notes: List[Dict[str, Any]],
    thoughts: List[Dict[str, Any]],
    *,
    limit: int = 3,
) -> List[str]:
    entries: List[Tuple[str, Dict[str, Any]]] = [("笔记", note) for note in notes] + [
        ("读后感", thought) for thought in thoughts
    ]
    if not entries:
        return ["最近讨论：还没有留下笔记或读后感。"]

    entries.sort(key=lambda item: str(item[1].get("time") or ""), reverse=True)
    lines = ["最近讨论："]
    for kind, payload in entries[:limit]:
        chapter = int(payload.get("chapter", 0) or 0)
        author = str(payload.get("author") or "未知")
        content = _trim_inline(str(payload.get("content") or ""))
        lines.append(f"- {kind}｜第 {chapter} 章｜{author}：{content}")
    return lines


def _build_shared_panel_text(data_dir: str, book_name: str) -> str:
    index, resolved_name = _resolve_book_index(data_dir, book_name)
    if not index:
        return f"没找到《{book_name}》。"

    notes = _load_json(_notes_path(data_dir, resolved_name), [])
    thoughts = _load_json(_thoughts_path(data_dir, resolved_name), [])
    current = int(index.get("current_chapter", 1) or 1)
    total = int(index.get("total_chapters", 0) or 0)
    current = min(max(current, 1), total or 1)
    percent = 0 if total == 0 else current / total * 100
    current_title = _chapter_title(index, current)
    prev_ch = max(current - 1, 1)
    next_ch = min(current + 1, total) if total else 1

    lines = [
        f"《{resolved_name}》共读面板",
        f"进度：第 {current}/{total} 章，约 {percent:.1f}%",
        f"当前章节：{current_title}",
    ]

    if total > 0:
        if current > 1:
            lines.append(f"上一章：第 {prev_ch} 章 {_chapter_title(index, prev_ch)}")
        if current < total:
            lines.append(f"下一章：第 {next_ch} 章 {_chapter_title(index, next_ch)}")
        else:
            lines.append("下一章：已经到最后一章了。")

    lines.append(f"笔记数：{len(notes)}")
    lines.append(f"读后感数：{len(thoughts)}")
    lines.extend(_build_recent_discussion_lines(notes, thoughts))
    lines.extend(
        [
            "快捷操作：",
            f"- /继续读 {resolved_name}",
            f"- /下一章 {resolved_name}",
            f"- /上一章 {resolved_name}",
            f"- /跳到 {resolved_name} 第{current}章",
        ]
    )
    return "\n".join(lines)


def _serialize_book_summary(index: Dict[str, Any]) -> Dict[str, Any]:
    current = int(index.get("current_chapter", 1) or 1)
    total = int(index.get("total_chapters", 0) or 0)
    return {
        "name": str(index.get("name") or ""),
        "safe_name": str(index.get("safe_name") or ""),
        "current_chapter": current,
        "total_chapters": total,
        "progress_percent": 0 if total == 0 else round(current / total * 100, 1),
        "updated_at": str(index.get("updated_at") or ""),
        "created_at": str(index.get("created_at") or ""),
    }


def _load_chapter_detail(
    data_dir: str,
    book_name: str,
    chapter_no: int,
    *,
    update_progress: bool = False,
) -> Dict[str, Any]:
    index, resolved_name = _resolve_book_index(data_dir, book_name)
    if not index:
        raise ValueError(f"没找到《{book_name}》。")

    total = int(index.get("total_chapters", 0) or 0)
    if chapter_no < 1 or chapter_no > total:
        raise ValueError(f"章节不存在。《{resolved_name}》共有 {total} 章。")

    with open(_chapter_path(data_dir, resolved_name, chapter_no), "r", encoding="utf-8") as f:
        content = f.read().strip()

    title = _chapter_title(index, chapter_no)
    if update_progress:
        index["current_chapter"] = chapter_no
        index["updated_at"] = _now()
        _save_json(_index_path(data_dir, resolved_name), index)

    return {
        "book_name": resolved_name,
        "chapter_no": chapter_no,
        "title": title,
        "content": content,
        "chars": len(content),
        "total_chapters": total,
        "has_prev": chapter_no > 1,
        "has_next": chapter_no < total,
    }


def _serialize_recent_discussions(
    notes: List[Dict[str, Any]],
    thoughts: List[Dict[str, Any]],
    *,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for note in notes:
        entries.append(
            {
                "kind": "note",
                "chapter": int(note.get("chapter", 0) or 0),
                "author": str(note.get("author") or "未知"),
                "content": str(note.get("content") or ""),
                "time": str(note.get("time") or ""),
            }
        )
    for thought in thoughts:
        entries.append(
            {
                "kind": "thought",
                "chapter": int(thought.get("chapter", 0) or 0),
                "author": str(thought.get("author") or "未知"),
                "content": str(thought.get("content") or ""),
                "time": str(thought.get("time") or ""),
            }
        )
    entries.sort(key=lambda item: item["time"], reverse=True)
    return entries[:limit]


def _build_discussion_prompt(book_name: str, chapter_no: int, chapter_title: str) -> str:
    return (
        f"我们继续共读《{book_name}》第 {chapter_no} 章《{chapter_title}》。"
        "请先结合当前章节内容，和我讨论这一章的关键情节、人物变化、值得注意的细节；"
        "如果需要，请直接读取当前章节再回答。"
    )


def _build_recent_discussion_prompt_lines(
    recent_items: List[Dict[str, Any]],
    *,
    limit: int = 4,
) -> List[str]:
    if not recent_items:
        return ["最近共读记录：暂时还没有笔记或读后感。"]
    lines = ["最近共读记录："]
    for item in recent_items[:limit]:
        kind = "读后感" if str(item.get("kind") or "") == "thought" else "笔记"
        chapter = int(item.get("chapter", 0) or 0)
        author = str(item.get("author") or "未知")
        content = _trim_inline(str(item.get("content") or ""), limit=120)
        lines.append(f"- {kind}｜第 {chapter} 章｜{author}：{content}")
    return lines


def _summarize_chapter_excerpt(text: str, limit: int = 260) -> str:
    clean = " ".join(str(text or "").split()).strip()
    if not clean:
        return "这章正文还没有成功加载出来，请先结合标题与上下文发挥。"
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _normalize_call_context_mode(raw_value: Any) -> str:
    value = str(raw_value or "").strip().lower()
    if value in CALL_CONTEXT_MODES:
        return value
    return CALL_CONTEXT_MODE_AUTO


def _build_chapter_context_for_qq(
    text: str,
    *,
    mode: str = CALL_CONTEXT_MODE_AUTO,
    full_threshold: int = 3200,
    head_limit: int = 1000,
    middle_limit: int = 700,
    tail_limit: int = 1000,
) -> str:
    clean = _normalize_text(text)
    if not clean:
        return "正文上下文：这章正文还没有成功加载出来，请先结合标题与上下文发挥。"
    normalized_mode = _normalize_call_context_mode(mode)
    if normalized_mode == CALL_CONTEXT_MODE_FULL:
        return f"正文上下文（按全文模式提供）：\n{clean}"
    if normalized_mode == CALL_CONTEXT_MODE_AUTO and len(clean) <= full_threshold:
        return f"正文上下文（本章较短，直接提供全文）：\n{clean}"

    middle_start = max(head_limit, (len(clean) - middle_limit) // 2)
    middle_end = min(len(clean), middle_start + middle_limit)
    middle_chunk = clean[middle_start:middle_end].strip()
    tail_chunk = clean[-tail_limit:].strip()
    head_chunk = clean[:head_limit].strip()
    return "\n".join(
        [
            "正文上下文（按节选模式提供，使用开头 / 中段 / 结尾帮助你自然接话）：",
            f"[开头]\n{head_chunk}",
            f"[中段]\n{middle_chunk or '中段内容较短，已与其他片段重叠。'}",
            f"[结尾]\n{tail_chunk}",
        ]
    )


def _build_prior_qq_discussion_lines(
    history_items: List[Dict[str, Any]],
    *,
    limit: int = 3,
) -> List[str]:
    if not history_items:
        return ["之前的主动陪读记录：这是第一次由书架直接呼叫你去聊天继续聊这本书。"]

    lines = ["之前的主动陪读记录（请自然承接，不要当成第一次开场）："]
    for item in history_items[-limit:]:
        chapter_no = int(item.get("chapter_no", 0) or 0)
        title = str(item.get("chapter_title") or f"第 {chapter_no} 章")
        sent_at = str(item.get("time") or "")
        message = _trim_inline(str(item.get("message") or ""), limit=150)
        lines.append(f"- {sent_at}｜第 {chapter_no} 章《{title}》：{message}")
    return lines


def _build_qq_call_system_prompt() -> str:
    return (
        "你是一位正在聊天平台上陪读的朋友。"
        "请只输出一条可以直接发送到 QQ 的中文消息。"
        "语气自然、温柔、真诚，像刚刚已经在聊天里的朋友，不要写成客服腔、提纲腔或总结报告。"
        "不要使用 markdown 标题或项目符号。"
        "可以只接一个你真正在意的点，也可以顺手追问一句，但不要每次都机械地复述章节信息。"
        "如果已经有前文，就直接承接那个语气和话头。"
    )


def _build_qq_call_prompt(
    *,
    book_name: str,
    chapter_no: int,
    chapter_title: str,
    chapter_content: str,
    call_context_mode: str,
    recent_items: List[Dict[str, Any]],
    history_items: List[Dict[str, Any]],
) -> str:
    return "\n".join(
        [
            f"目标：替陪读助手发一条聊天消息，讨论《{book_name}》第 {chapter_no} 章《{chapter_title}》。",
            "要求：",
            "1. 像 QQ 私聊或群里自然接话，不要写成完整书评、读书笔记或任务回复；",
            "2. 优先聊一个你真正在意的点，不要为了显得完整而硬拆成固定结构；",
            "3. 可以顺手追问一句，也可以只接着感受往下说，但整体要像真人聊天，不要模板味太重；",
            "4. 如果之前已经聊过这本书，请自然接着上次的话头往下走，让对方感觉你记得之前在聊什么；",
            "5. 允许带一点亲近感，但不要过分夸张，不要自称 AI；",
            "6. 控制在 50 到 160 字，输出一段自然文本即可。",
            _build_chapter_context_for_qq(
                chapter_content,
                mode=call_context_mode,
            ),
            *_build_recent_discussion_prompt_lines(recent_items),
            *_build_prior_qq_discussion_lines(history_items),
        ]
    )


def _build_proactive_discussion_context_user_message(
    *,
    book_name: str,
    chapter_no: int,
    chapter_title: str,
) -> Dict[str, str]:
    return {
        "role": "user",
        "content": (
            "书架页记录：你刚刚通过共读页主动发起了一次聊天讨论。"
            f"讨论对象是《{book_name}》第 {chapter_no} 章《{chapter_title}》。"
            "后续如果用户继续接着聊，请把这次主动发言视为你刚刚已经说过的话，自然承接。"
        ),
    }


def _build_proactive_discussion_context_assistant_message(message: str) -> Dict[str, str]:
    return {"role": "assistant", "content": str(message or "").strip()}


def _build_bookshelf_page_payload(
    data_dir: str,
    book_name: str,
    *,
    chapter_no: Optional[int] = None,
    update_progress: bool = False,
) -> Dict[str, Any]:
    index, resolved_name = _resolve_book_index(data_dir, book_name)
    if not index:
        raise ValueError(f"没找到《{book_name}》。")

    active_chapter = int(chapter_no or index.get("current_chapter", 1) or 1)
    chapter = _load_chapter_detail(
        data_dir,
        resolved_name,
        active_chapter,
        update_progress=update_progress,
    )
    notes = _load_json(_notes_path(data_dir, resolved_name), [])
    thoughts = _load_json(_thoughts_path(data_dir, resolved_name), [])
    current_chapter = int(chapter["chapter_no"])
    total = int(index.get("total_chapters", 0) or 0)

    return {
        "book": {
            **_serialize_book_summary(index),
            "name": resolved_name,
            "current_chapter": current_chapter,
            "current_title": str(chapter["title"]),
            "total_chapters": total,
            "discussion_prompt": _build_discussion_prompt(
                resolved_name,
                current_chapter,
                str(chapter["title"]),
            ),
        },
        "chapters": [
            {
                "no": int(item.get("no", 0) or 0),
                "title": str(item.get("title") or ""),
                "chars": int(item.get("chars", 0) or 0),
                "is_current": int(item.get("no", 0) or 0) == current_chapter,
            }
            for item in index.get("chapters", []) or []
        ],
        "current_chapter": chapter,
        "discussion": {
            "notes_count": len(notes),
            "thoughts_count": len(thoughts),
            "recent_items": _serialize_recent_discussions(notes, thoughts),
        },
    }


class BookshelfPluginPageApi:
    PLUGIN_NAME = "astrbot_plugin_bookshelf"
    PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"

    def __init__(self, plugin: "BookshelfPlugin") -> None:
        self.plugin = plugin

    @staticmethod
    def _ok(data: Any = None) -> Dict[str, Any]:
        return {"status": "ok", "data": data}

    @staticmethod
    def _error(message: str) -> Dict[str, Any]:
        return {"status": "error", "message": str(message)}

    @staticmethod
    def _validate_qq_target_umo(raw_value: str) -> str:
        value = str(raw_value or "").strip()
        if not value:
            return ""
        try:
            session = MessageSesion.from_str(value)
        except Exception as exc:
            raise ValueError(
                "QQ 目标会话 UMO 不合法，请使用 napcat_qq:FriendMessage:123456 或 napcat_qq:GroupMessage:123456。"
            ) from exc
        if not str(session.platform_name or "").strip() or not str(session.session_id or "").strip():
            raise ValueError("QQ 目标会话 UMO 不完整，请检查平台 ID、消息类型和会话 ID。")
        return str(session)

    async def _get_default_qq_target_umo(self) -> str:
        return str(await self.plugin.get_kv_data(DEFAULT_QQ_TARGET_KV_KEY, "") or "").strip()

    async def _get_default_call_context_mode(self) -> str:
        return _normalize_call_context_mode(
            await self.plugin.get_kv_data(
                DEFAULT_CALL_CONTEXT_MODE_KV_KEY,
                CALL_CONTEXT_MODE_AUTO,
            )
        )

    async def _build_settings_payload(self) -> Dict[str, Any]:
        return {
            "qq_target_umo": await self._get_default_qq_target_umo(),
            "call_context_mode": await self._get_default_call_context_mode(),
        }

    async def _get_qq_discussion_history(
        self,
        *,
        book_name: str,
        qq_target_umo: str,
    ) -> List[Dict[str, Any]]:
        return await self.plugin._get_qq_discussion_history(
            book_name=book_name,
            qq_target_umo=qq_target_umo,
        )

    def _build_upload_response(
        self,
        *,
        result: Dict[str, Any],
        requested_name: str,
        resolved_name: str,
    ) -> Dict[str, Any]:
        return self._ok(
            {
                "upload": {
                    **result,
                    "requested_book_name": requested_name,
                    "renamed": resolved_name != requested_name,
                },
                "page": _build_bookshelf_page_payload(self.plugin.data_dir, resolved_name),
                "selected_book_name": resolved_name,
                "books": [
                    _serialize_book_summary(item)
                    for item in sorted(
                        _list_books(self.plugin.data_dir),
                        key=lambda item: str(item.get("updated_at") or ""),
                        reverse=True,
                    )
                ],
            }
        )

    def _resolve_default_book_name(self) -> str:
        books = _list_books(self.plugin.data_dir)
        if not books:
            return ""
        books.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return str(books[0].get("name") or "")

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        register(
            f"{self.PAGE_API_PREFIX}/books",
            self.list_books,
            ["GET"],
            "Bookshelf Page books",
        )
        register(
            f"{self.PAGE_API_PREFIX}/book",
            self.get_book,
            ["GET"],
            "Bookshelf Page book detail",
        )
        register(
            f"{self.PAGE_API_PREFIX}/chapter",
            self.select_chapter,
            ["POST"],
            "Bookshelf Page select chapter",
        )
        register(
            f"{self.PAGE_API_PREFIX}/upload",
            self.upload_book,
            ["POST"],
            "Bookshelf Page upload book",
        )
        register(
            f"{self.PAGE_API_PREFIX}/settings",
            self.page_settings,
            ["GET", "POST"],
            "Bookshelf Page settings",
        )
        register(
            f"{self.PAGE_API_PREFIX}/call_aran",
            self.call_aran_in_qq,
            ["POST"],
            "Bookshelf Page call aran in qq",
        )

    async def list_books(self) -> Dict[str, Any]:
        books = _list_books(self.plugin.data_dir)
        books.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        selected = self._resolve_default_book_name()
        return self._ok(
            {
                "books": [_serialize_book_summary(item) for item in books],
                "selected_book_name": selected,
                "settings": await self._build_settings_payload(),
            }
        )

    async def get_book(self) -> Dict[str, Any]:
        if _quart_request is None:
            return self._error("当前环境不支持插件页面请求。")

        requested_name = str(_quart_request.args.get("book_name") or "").strip()
        selected_name = requested_name or self._resolve_default_book_name()
        if not selected_name:
            return self._ok({"books": [], "selected_book_name": "", "page": None})

        try:
            books = _list_books(self.plugin.data_dir)
            books.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            return self._ok(
                {
                    "books": [_serialize_book_summary(item) for item in books],
                    "selected_book_name": selected_name,
                    "page": _build_bookshelf_page_payload(self.plugin.data_dir, selected_name),
                    "settings": await self._build_settings_payload(),
                }
            )
        except Exception as exc:
            logger.warning("bookshelf page get_book failed: %s", exc, exc_info=True)
            return self._error(str(exc))

    async def select_chapter(self) -> Dict[str, Any]:
        if _quart_request is None:
            return self._error("当前环境不支持插件页面请求。")

        try:
            payload = await _quart_request.get_json(silent=True) or {}
            book_name = str(payload.get("book_name") or "").strip()
            chapter_no = int(payload.get("chapter_no") or 0)
            if not book_name:
                raise ValueError("请选择一本书。")
            if chapter_no <= 0:
                raise ValueError("请选择有效章节。")
            return self._ok(
                _build_bookshelf_page_payload(
                    self.plugin.data_dir,
                    book_name,
                    chapter_no=chapter_no,
                    update_progress=True,
                )
            )
        except Exception as exc:
            logger.warning("bookshelf page select_chapter failed: %s", exc, exc_info=True)
            return self._error(str(exc))

    async def upload_book(self) -> Dict[str, Any]:
        if _quart_request is None:
            return self._error("当前环境不支持文件上传。")

        temp_path: Optional[Path] = None
        try:
            payload = await _quart_request.get_json(silent=True)
            if isinstance(payload, dict) and payload.get("file_base64"):
                filename = str(payload.get("file_name") or "").strip()
                if not filename:
                    raise ValueError("没有收到书籍文件名。")
                suffix = Path(filename).suffix.lower()
                if suffix not in SUPPORTED_BOOK_SUFFIXES:
                    raise ValueError("只支持 .txt 或 .epub 文件。")

                requested_name = str(payload.get("book_name") or "").strip()
                if not requested_name:
                    requested_name = (
                        _simplify_book_title(filename) or Path(filename).stem or "未命名书籍"
                    )
                resolved_name = _choose_available_book_name(
                    self.plugin.data_dir, requested_name
                )
                try:
                    upload_bytes = base64.b64decode(
                        str(payload.get("file_base64") or ""),
                        validate=True,
                    )
                except Exception as exc:
                    raise ValueError(f"上传内容解码失败：{exc}") from exc
                temp_path = Path(_imports_dir(self.plugin.data_dir)) / (
                    f".page-upload-{int(time.time() * 1000)}{suffix}"
                )
                temp_path.write_bytes(upload_bytes)
                result = self.plugin._import_from_source_file(str(temp_path), resolved_name)
                return self._build_upload_response(
                    result=result,
                    requested_name=requested_name,
                    resolved_name=resolved_name,
                )
            files = await _quart_request.files
            form = await _quart_request.form
            upload = files.get("file") or files.get("book")
            if upload is None:
                raise ValueError("没有收到书籍文件。")

            filename = str(upload.filename or "").strip()
            suffix = Path(filename).suffix.lower()
            if suffix not in SUPPORTED_BOOK_SUFFIXES:
                raise ValueError("只支持 .txt 或 .epub 文件。")

            requested_name = str(form.get("book_name") or "").strip()
            if not requested_name:
                requested_name = _simplify_book_title(filename) or Path(filename).stem or "未命名书籍"
            resolved_name = _choose_available_book_name(self.plugin.data_dir, requested_name)

            upload_bytes = await upload.read()
            temp_path = Path(_imports_dir(self.plugin.data_dir)) / f".page-upload-{int(time.time() * 1000)}{suffix}"
            temp_path.write_bytes(upload_bytes)

            result = self.plugin._import_from_source_file(str(temp_path), resolved_name)
            return self._build_upload_response(
                result=result,
                requested_name=requested_name,
                resolved_name=resolved_name,
            )
        except Exception as exc:
            logger.warning("bookshelf page upload failed: %s", exc, exc_info=True)
            return self._error(str(exc))
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    logger.warning("bookshelf page upload temp cleanup failed: %s", temp_path)

    async def page_settings(self) -> Dict[str, Any]:
        if _quart_request is None:
            return self._error("当前环境不支持插件页面请求。")
        try:
            if str(_quart_request.method or "GET").upper() == "GET":
                return self._ok(await self._build_settings_payload())

            payload = await _quart_request.get_json(silent=True) or {}
            normalized_umo = self._validate_qq_target_umo(payload.get("qq_target_umo"))
            call_context_mode = _normalize_call_context_mode(payload.get("call_context_mode"))
            await self.plugin.put_kv_data(DEFAULT_QQ_TARGET_KV_KEY, normalized_umo)
            await self.plugin.put_kv_data(DEFAULT_CALL_CONTEXT_MODE_KV_KEY, call_context_mode)
            return self._ok(await self._build_settings_payload())
        except Exception as exc:
            logger.warning("bookshelf page settings failed: %s", exc, exc_info=True)
            return self._error(str(exc))

    async def call_aran_in_qq(self) -> Dict[str, Any]:
        if _quart_request is None:
            return self._error("当前环境不支持插件页面请求。")
        try:
            payload = await _quart_request.get_json(silent=True) or {}
            book_name = str(payload.get("book_name") or "").strip()
            if not book_name:
                raise ValueError("请先选择一本书。")

            requested_mode = str(payload.get("call_context_mode") or "").strip()
            if requested_mode:
                call_context_mode = _normalize_call_context_mode(requested_mode)
                await self.plugin.put_kv_data(
                    DEFAULT_CALL_CONTEXT_MODE_KV_KEY,
                    call_context_mode,
                )
            else:
                call_context_mode = await self._get_default_call_context_mode()
            requested_umo = str(payload.get("qq_target_umo") or "").strip()
            if requested_umo:
                target_umo = self._validate_qq_target_umo(requested_umo)
                await self.plugin.put_kv_data(DEFAULT_QQ_TARGET_KV_KEY, target_umo)
            else:
                target_umo = await self._get_default_qq_target_umo()
            if not target_umo:
                raise ValueError("请先填写 QQ 目标会话 UMO。")

            index, resolved_name = _resolve_book_index(self.plugin.data_dir, book_name)
            if not index:
                raise ValueError(f"没找到《{book_name}》。")
            chapter_no = int(payload.get("chapter_no") or index.get("current_chapter", 1) or 1)
            page = _build_bookshelf_page_payload(
                self.plugin.data_dir,
                resolved_name,
                chapter_no=chapter_no,
                update_progress=False,
            )
            chapter = page["current_chapter"]
            discussion = page["discussion"]
            provider_id = await self.plugin._resolve_chat_provider_id_for_umo(target_umo)
            history_items = await self._get_qq_discussion_history(
                book_name=resolved_name,
                qq_target_umo=target_umo,
            )
            prompt = _build_qq_call_prompt(
                book_name=resolved_name,
                chapter_no=int(chapter.get("chapter_no", 0) or 0),
                chapter_title=str(chapter.get("title") or f"第 {chapter_no} 章"),
                chapter_content=str(chapter.get("content") or ""),
                call_context_mode=call_context_mode,
                recent_items=discussion.get("recent_items", []) or [],
                history_items=history_items,
            )
            llm_resp = await self.plugin.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=_build_qq_call_system_prompt(),
                prompt=prompt,
            )
            reply_text = str(llm_resp.completion_text or "").strip()
            if not reply_text:
                raise ValueError("这次没有生成可发送的内容，请再试一次。")
            sent = await self.plugin.context.send_message(
                target_umo,
                MessageChain().message(reply_text),
            )
            if not sent:
                raise ValueError("消息没有发送出去，请检查 QQ 平台连接状态。")
            history_size = await self.plugin._append_qq_discussion_history(
                book_name=resolved_name,
                qq_target_umo=target_umo,
                chapter_no=int(chapter.get("chapter_no", 0) or 0),
                chapter_title=str(chapter.get("title") or ""),
                message=reply_text,
            )
            await self.plugin._persist_proactive_discussion_to_conversation(
                qq_target_umo=target_umo,
                book_name=resolved_name,
                chapter_no=int(chapter.get("chapter_no", 0) or 0),
                chapter_title=str(chapter.get("title") or ""),
                message=reply_text,
            )
            return self._ok(
                {
                    "book_name": resolved_name,
                    "chapter_no": int(chapter.get("chapter_no", 0) or 0),
                    "chapter_title": str(chapter.get("title") or ""),
                    "qq_target_umo": target_umo,
                    "call_context_mode": call_context_mode,
                    "provider_id": provider_id,
                    "reply_text": reply_text,
                    "history_size": history_size,
                }
            )
        except Exception as exc:
            logger.warning("bookshelf page call_aran_in_qq failed: %s", exc, exc_info=True)
            return self._error(str(exc))


@register("astrbot_plugin_bookshelf", "Companion Starter Maintainers", "书架插件", "2.0.0", "https://github.com/yussica1016/astrbot_plugin_bookshelf")
class BookshelfPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self._pending_uploads: Dict[str, Tuple[str, float]] = {}  # uid -> (book_name, timestamp)
        self.data_dir = str(StarTools.get_data_dir(self.name))
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(_imports_dir(self.data_dir), exist_ok=True)
        self.page_api = None
        self._register_official_page_api_if_available()

    def _get_cfg(self, key: str, default: Any) -> Any:
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return default

    def _register_official_page_api_if_available(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            return
        try:
            self.page_api = BookshelfPluginPageApi(self)
            self.page_api.register_routes()
        except Exception as exc:
            self.page_api = None
            logger.warning(
                "bookshelf: official page api register failed: %s",
                exc,
                exc_info=True,
            )

    def _file_vault_root(self) -> Path:
        raw = str(self._get_cfg("file_vault_root", "") or "").strip()
        candidates: List[Path] = []
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
                return path
        return Path(raw) if raw else Path(DEFAULT_FILE_VAULT_ROOTS[0])

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

    def _item_path(self, item_id: str) -> Path:
        safe_item_id = _safe_name(item_id)
        return self._file_vault_root() / "items" / f"{safe_item_id}.json"

    def _load_file_vault_item(self, item_id: str) -> Dict[str, Any]:
        path = self._item_path(item_id)
        if not path.exists():
            raise FileNotFoundError(f"file_vault 条目不存在：{item_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_file_vault_stored_file(self, item: Dict[str, Any]) -> Path:
        stored_rel_path = str(item.get("stored_rel_path") or "").strip()
        if not stored_rel_path:
            raise ValueError("file_vault 条目缺少 stored_rel_path。")
        file_vault_root = self._file_vault_root().resolve()
        stored_file = (file_vault_root / stored_rel_path).resolve()
        try:
            stored_file.relative_to(file_vault_root)
        except ValueError as exc:
            raise ValueError("file_vault 文件路径越界。") from exc
        if not stored_file.is_file():
            raise FileNotFoundError(f"file_vault 文件不存在：{stored_file}")
        return stored_file

    def _import_from_source_file(self, source_path: str, book_name: str) -> Dict[str, Any]:
        text = _read_book_source(source_path)
        index = _save_book(self.data_dir, book_name, text)
        return {
            "book_name": book_name,
            "total_chapters": int(index.get("total_chapters", 0) or 0),
            "source_name": os.path.basename(source_path),
            "source_path": source_path,
        }

    def _import_file_vault_item(self, item_id: str, book_name: str = "") -> Dict[str, Any]:
        item = self._load_file_vault_item(item_id)
        stored_file = self._resolve_file_vault_stored_file(item)
        if not _is_supported_book_file(stored_file):
            raise ValueError("目前共读只支持从 file_vault 导入 .txt 或 .epub 文件。")
        requested_book_name = (
            str(book_name or "").strip()
            or _simplify_book_title(str(item.get("title") or ""))
            or _simplify_book_title(str(item.get("original_name") or ""))
            or str(item.get("title") or "").strip()
            or stored_file.stem
        )
        resolved_book_name = _choose_available_book_name(self.data_dir, requested_book_name)
        result = self._import_from_source_file(str(stored_file), resolved_book_name)
        result.update(
            {
                "requested_book_name": requested_book_name,
                "renamed": resolved_book_name != requested_book_name,
                "item_id": str(item.get("item_id") or item_id),
                "original_name": str(item.get("original_name") or stored_file.name),
                "stored_rel_path": str(item.get("stored_rel_path") or ""),
            }
        )
        return result

    def _cleanup_stale_uploads(self, max_age: int = 300):
        """清理超过 max_age 秒的待上传记录，防止内存泄漏"""
        now = time.time()
        stale = [uid for uid, (_, ts) in self._pending_uploads.items() if now - ts > max_age]
        for uid in stale:
            del self._pending_uploads[uid]

    async def _resolve_chat_provider_id_for_umo(self, umo: str) -> str:
        try:
            return await self.context.get_current_chat_provider_id(umo=umo)
        except Exception:
            provider = self.context.get_using_provider(umo) or self.context.get_using_provider()
            if provider is not None:
                return str(provider.meta().id)
        raise ValueError("当前没有可用的聊天模型，请先在 AstrBot 里启用一个对话模型。")

    @staticmethod
    def _qq_discussion_history_kv_key(book_name: str, qq_target_umo: str) -> str:
        return f"qq_discussion_history::{_safe_name(book_name)}::{_safe_name(qq_target_umo)}"

    async def _get_qq_discussion_history(
        self,
        *,
        book_name: str,
        qq_target_umo: str,
    ) -> List[Dict[str, Any]]:
        key = self._qq_discussion_history_kv_key(book_name, qq_target_umo)
        value = await self.get_kv_data(key, [])
        if not isinstance(value, list):
            return []
        history: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            history.append(
                {
                    "time": str(item.get("time") or ""),
                    "chapter_no": int(item.get("chapter_no", 0) or 0),
                    "chapter_title": str(item.get("chapter_title") or ""),
                    "message": str(item.get("message") or ""),
                }
            )
        return history[-QQ_DISCUSSION_HISTORY_LIMIT:]

    async def _append_qq_discussion_history(
        self,
        *,
        book_name: str,
        qq_target_umo: str,
        chapter_no: int,
        chapter_title: str,
        message: str,
    ) -> int:
        history = await self._get_qq_discussion_history(
            book_name=book_name,
            qq_target_umo=qq_target_umo,
        )
        history.append(
            {
                "time": _now(),
                "chapter_no": int(chapter_no),
                "chapter_title": str(chapter_title or ""),
                "message": str(message or "").strip(),
            }
        )
        history = history[-QQ_DISCUSSION_HISTORY_LIMIT:]
        await self.put_kv_data(
            self._qq_discussion_history_kv_key(book_name, qq_target_umo),
            history,
        )
        return len(history)

    async def _persist_proactive_discussion_to_conversation(
        self,
        *,
        qq_target_umo: str,
        book_name: str,
        chapter_no: int,
        chapter_title: str,
        message: str,
    ) -> None:
        conv_mgr = self.context.conversation_manager
        if conv_mgr is None:
            return

        cid = await conv_mgr.get_curr_conversation_id(qq_target_umo)
        if not cid:
            cid = await conv_mgr.new_conversation(qq_target_umo)
        conversation = await conv_mgr.get_conversation(
            qq_target_umo,
            cid,
            create_if_not_exists=True,
        )
        history: List[Dict[str, Any]] = []
        if conversation:
            try:
                history = json.loads(conversation.history or "[]")
            except Exception:
                history = []
        history.append(
            _build_proactive_discussion_context_user_message(
                book_name=book_name,
                chapter_no=chapter_no,
                chapter_title=chapter_title,
            )
        )
        history.append(_build_proactive_discussion_context_assistant_message(message))
        await conv_mgr.update_conversation(
            qq_target_umo,
            cid,
            history=history,
        )

    @filter.command("上传书籍")
    async def upload_book_text(self, event: AstrMessageEvent, book_name: str, content: str):
        """上传书籍全文：/上传书籍 书名 全文"""
        try:
            index = _save_book(self.data_dir, book_name, content)
            yield event.plain_result(
                f"已保存《{book_name}》。\n共 {index['total_chapters']} 章。\n可以用 /目录 {book_name} 查看目录。"
            )
        except Exception as exc:
            logger.exception("bookshelf: upload_book_text failed")
            yield event.plain_result(f"上传失败：{exc}")

    @filter.command("上传文本")
    async def wait_text_file(self, event: AstrMessageEvent, book_name: str):
        """先登记书名，然后下一条消息发送 txt 或 epub 文件。"""
        uid = str(event.get_sender_id())
        self._pending_uploads[uid] = (book_name.strip(), time.time())
        yield event.plain_result(
            f"好，把《{book_name}》的 .txt 或 .epub 文件发过来。\n"
            f"如果文件太大传不上来，也可以先放到 { _imports_dir(self.data_dir) } ，"
            f"再用 /导入书籍文件 {book_name} 文件名.txt 或 文件名.epub 导入。"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def receive_file(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        # 提前return：只在有待上传记录时才继续处理，避免每条消息都走后续逻辑
        if uid not in self._pending_uploads:
            return
        self._cleanup_stale_uploads()
        if uid not in self._pending_uploads:
            return

        file_comp = None
        for comp in getattr(event.message_obj, "message", []) or []:
            if isinstance(comp, File):
                file_comp = comp
                break
        if file_comp is None:
            return

        # 文件类型检查
        filename = getattr(file_comp, "file", "") or getattr(file_comp, "name", "") or ""
        if not _is_supported_book_file(Path(filename)):
            yield event.plain_result("只支持 .txt 或 .epub 格式的书籍文件。")
            return

        book_name, _ = self._pending_uploads.pop(uid)
        try:
            file_path = await file_comp.get_file()
            index = _save_book(self.data_dir, book_name, _read_book_source(file_path))
            yield event.plain_result(f"已导入《{book_name}》。共 {index['total_chapters']} 章。")
        except Exception as exc:
            logger.exception("bookshelf: receive_file failed")
            yield event.plain_result(f"文件导入失败：{exc}")

    @filter.command("书籍导入目录")
    async def show_import_dir(self, event: AstrMessageEvent):
        yield event.plain_result(
            "大 txt/epub 传不上来时，可以把文件直接放到这个目录后再导入：\n"
            f"{_imports_dir(self.data_dir)}\n\n"
            "命令格式：/导入书籍文件 书名 文件名.txt 或 文件名.epub"
        )

    @filter.command("导入书籍文件")
    async def import_local_book_file(self, event: AstrMessageEvent, book_name: str, relative_path: str):
        """从插件 imports 目录导入本地 txt/epub，适合绕过 IM 附件大小限制。"""
        try:
            file_path = _resolve_import_file(self.data_dir, relative_path)
            index = _save_book(self.data_dir, book_name, _read_book_source(file_path))
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            yield event.plain_result(
                f"已从本地文件导入《{book_name}》。共 {index['total_chapters']} 章。\n"
                f"来源：{os.path.basename(file_path)} ({size_mb:.2f} MB)"
            )
        except Exception as exc:
            logger.exception("bookshelf: import_local_book_file failed")
            yield event.plain_result(f"本地导入失败：{exc}")

    @filter.regex(r"^/?导入代存书籍\s+(\S+)(?:\s+(.+))?$")
    async def import_file_vault_book(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        match = _RE_IMPORT_VAULT.match(raw)
        if not match:
            return
        item_id = match.group(1).strip()
        book_name = str(match.group(2) or "").strip()
        try:
            result = self._import_file_vault_item(item_id, book_name)
            message = (
                f"已把 file_vault 条目 {result['item_id']} 导入共读《{result['book_name']}》。\n"
                f"共 {result['total_chapters']} 章，来源：{result['original_name']}"
            )
            if result.get("renamed"):
                message += (
                    f"\n检测到书名已存在，已自动改名为《{result['book_name']}》，"
                    "避免覆盖原来的共读进度。"
                )
            yield event.plain_result(message)
        except Exception as exc:
            logger.exception("bookshelf: import_file_vault_book failed")
            yield event.plain_result(f"导入 file_vault 书籍失败：{exc}")

    @filter.command("书架", alias={"/书架"})
    async def list_books(self, event: AstrMessageEvent):
        books = _list_books(self.data_dir)
        if not books:
            yield event.plain_result("书架还是空的。可以用 /上传文本 书名 上传 txt/epub，或用 /导入代存书籍 item_id。")
            return
        lines = ["我的书架："]
        for b in books:
            lines.append(
                f"- 《{b.get('name')}》：{b.get('total_chapters', 0)} 章，当前第 {b.get('current_chapter', 1)} 章"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("目录")
    async def catalog(self, event: AstrMessageEvent, book_name: str):
        index, resolved_name = _resolve_book_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        lines = [f"《{resolved_name}》目录："]
        for ch in index.get("chapters", []):
            lines.append(f"{ch['no']}. {ch.get('title', '')}（{ch.get('chars', 0)}字）")
        yield event.plain_result("\n".join(lines[:120]))

    @filter.regex(r"^/?读第\s+(.+?)\s+第?\s*(\d+)\s*章?$")
    async def read_chapter(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        m = _RE_READ_CHAPTER.match(raw)
        if not m:
            return
        book_name = m.group(1).strip()
        chapter_no = int(m.group(2))
        yield event.plain_result(_format_chapter_preview(self.data_dir, book_name, chapter_no))

    @filter.regex(r"^/?继续读\s+(.+?)\s*$")
    async def continue_read(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        m = _RE_CONTINUE_READ.match(raw)
        if not m:
            return
        book_name = m.group(1).strip()
        index = _load_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        current = int(index.get("current_chapter", 1) or 1)
        yield event.plain_result(_format_chapter_preview(self.data_dir, book_name, current))

    @filter.regex(r"^/?下一章\s+(.+?)\s*$")
    async def read_next_chapter(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        m = _RE_NEXT_CHAPTER.match(raw)
        if not m:
            return
        book_name = m.group(1).strip()
        index = _load_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        current = int(index.get("current_chapter", 1) or 1)
        total = int(index.get("total_chapters", 0) or 0)
        if total <= 0:
            yield event.plain_result(f"《{book_name}》还没有可读章节。")
            return
        if current >= total:
            yield event.plain_result(f"《{book_name}》已经读到最后一章了，目前停在第 {current} 章。")
            return
        yield event.plain_result(_format_chapter_preview(self.data_dir, book_name, current + 1))

    @filter.regex(r"^/?上一章\s+(.+?)\s*$")
    async def read_prev_chapter(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        m = _RE_PREV_CHAPTER.match(raw)
        if not m:
            return
        book_name = m.group(1).strip()
        index = _load_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        current = int(index.get("current_chapter", 1) or 1)
        if current <= 1:
            yield event.plain_result(f"《{book_name}》已经在第一章了。")
            return
        yield event.plain_result(_format_chapter_preview(self.data_dir, book_name, current - 1))

    @filter.regex(r"^/?跳到\s+(.+?)\s+第?\s*(\d+)\s*章?$")
    async def jump_to_chapter(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        m = _RE_JUMP_CHAPTER.match(raw)
        if not m:
            return
        book_name = m.group(1).strip()
        chapter_no = int(m.group(2))
        yield event.plain_result(_format_chapter_preview(self.data_dir, book_name, chapter_no))

    @filter.command("阅读进度")
    async def progress(self, event: AstrMessageEvent, book_name: str):
        index, resolved_name = _resolve_book_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        current = int(index.get("current_chapter", 1))
        total = int(index.get("total_chapters", 0))
        percent = 0 if total == 0 else current / total * 100
        yield event.plain_result(f"《{resolved_name}》阅读进度：第 {current}/{total} 章，约 {percent:.1f}%。")

    @filter.command("删除书籍")
    async def delete_book(self, event: AstrMessageEvent, book_name: str):
        import shutil

        index, resolved_name = _resolve_book_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        path = _book_dir(self.data_dir, resolved_name)
        shutil.rmtree(path)
        yield event.plain_result(f"已删除《{resolved_name}》。")

    @filter.regex(r"^/?写笔记\s+(.+?)\s+第?\s*(\d+)\s*章\s+(.+)$")
    async def write_note(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        m = _RE_WRITE_NOTE.match(raw)
        if not m:
            return
        book_name, chapter_no, content = m.group(1).strip(), int(m.group(2)), m.group(3).strip()
        index, resolved_name = _resolve_book_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        notes = _load_json(_notes_path(self.data_dir, resolved_name), [])
        notes.append({"author": _author_name(event), "chapter": chapter_no, "content": content, "time": _now()})
        _save_json(_notes_path(self.data_dir, resolved_name), notes)
        yield event.plain_result(f"已记录《{resolved_name}》第 {chapter_no} 章笔记。")

    @filter.regex(r"^/?看笔记\s+(.+?)\s+第?\s*(\d+)\s*章?$")
    async def read_notes(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        m = _RE_READ_NOTES.match(raw)
        if not m:
            return
        book_name, chapter_no = m.group(1).strip(), int(m.group(2))
        index, resolved_name = _resolve_book_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        notes = [n for n in _load_json(_notes_path(self.data_dir, resolved_name), []) if int(n.get("chapter", 0)) == chapter_no]
        if not notes:
            yield event.plain_result(f"《{resolved_name}》第 {chapter_no} 章还没有笔记。")
            return
        lines = [f"《{resolved_name}》第 {chapter_no} 章笔记："]
        for n in notes[-20:]:
            lines.append(f"- {n.get('author')}｜{n.get('time')}\n  {n.get('content')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("所有笔记")
    async def all_notes(self, event: AstrMessageEvent, book_name: str):
        index, resolved_name = _resolve_book_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        notes = _load_json(_notes_path(self.data_dir, resolved_name), [])
        if not notes:
            yield event.plain_result(f"《{resolved_name}》还没有笔记。")
            return
        lines = [f"《{resolved_name}》全部笔记："]
        for n in notes[-50:]:
            lines.append(f"- 第 {n.get('chapter')} 章｜{n.get('author')}｜{n.get('time')}\n  {n.get('content')}")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/?读后感\s+(.+?)\s+第?\s*(\d+)\s*章\s+(.+)$")
    async def write_thought(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        m = _RE_WRITE_THOUGHT.match(raw)
        if not m:
            return
        book_name, chapter_no, content = m.group(1).strip(), int(m.group(2)), m.group(3).strip()
        index, resolved_name = _resolve_book_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        thoughts = _load_json(_thoughts_path(self.data_dir, resolved_name), [])
        thoughts.append({"author": _author_name(event), "chapter": chapter_no, "content": content, "time": _now()})
        _save_json(_thoughts_path(self.data_dir, resolved_name), thoughts)
        yield event.plain_result(f"已记录《{resolved_name}》第 {chapter_no} 章读后感。")

    @filter.regex(r"^/?看读后感\s+(.+?)(?:\s+第?\s*(\d+)\s*章?)?$")
    async def read_thoughts(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        m = _RE_READ_THOUGHTS.match(raw)
        if not m:
            return
        book_name = m.group(1).strip()
        chapter_raw = m.group(2)
        index, resolved_name = _resolve_book_index(self.data_dir, book_name)
        if not index:
            yield event.plain_result(f"没找到《{book_name}》。")
            return
        thoughts = _load_json(_thoughts_path(self.data_dir, resolved_name), [])
        if chapter_raw:
            chapter_no = int(chapter_raw)
            thoughts = [t for t in thoughts if int(t.get("chapter", 0)) == chapter_no]
        if not thoughts:
            yield event.plain_result(f"《{resolved_name}》还没有对应读后感。")
            return
        lines = [f"《{resolved_name}》读后感："]
        for t in thoughts[-20:]:
            lines.append(f"- 第 {t.get('chapter')} 章｜{t.get('author')}｜{t.get('time')}\n  {t.get('content')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("共读")
    async def shared_panel(self, event: AstrMessageEvent, book_name: str):
        yield event.plain_result(_build_shared_panel_text(self.data_dir, book_name))

    @filter.llm_tool(name="get_bookshelf_shared_panel")
    async def get_bookshelf_shared_panel(self, event: AstrMessageEvent, book_name: str) -> str:
        """
        查看某本书当前的共读面板，包括进度、当前章节、最近讨论和下一步建议。

        Args:
            book_name(string): 书名。
        """
        return _build_shared_panel_text(self.data_dir, book_name)

    @filter.llm_tool(name="continue_bookshelf_reading")
    async def continue_bookshelf_reading(self, event: AstrMessageEvent, book_name: str) -> str:
        """
        从当前阅读进度继续读某本书，返回当前章节正文预览。

        Args:
            book_name(string): 书名。
        """
        index = _load_index(self.data_dir, book_name)
        if not index:
            return f"没找到《{book_name}》。"
        current = int(index.get("current_chapter", 1) or 1)
        return _format_chapter_preview(self.data_dir, book_name, current)

    @filter.llm_tool(name="read_bookshelf_chapter")
    async def read_bookshelf_chapter(
        self,
        event: AstrMessageEvent,
        book_name: str,
        chapter_no: int,
    ) -> str:
        """
        读取某本书指定章节，返回该章节正文预览，并同步更新阅读进度。

        Args:
            book_name(string): 书名。
            chapter_no(number): 章节号，从 1 开始。
        """
        return _format_chapter_preview(self.data_dir, book_name, int(chapter_no))

    @filter.llm_tool(name="import_file_vault_item_to_bookshelf")
    async def import_file_vault_item_to_bookshelf(
        self,
        event: AstrMessageEvent,
        item_id: str,
        book_name: str = "",
    ) -> Dict[str, Any]:
        """
        把 file_vault 中的 txt/epub 文件导入共读书架，自动切章并生成阅读进度。

        Args:
            item_id(string): file_vault 条目编号。
            book_name(string): 导入后的书名，可留空，留空时优先沿用条目标题。
        """
        try:
            result = self._import_file_vault_item(str(item_id or "").strip(), str(book_name or "").strip())
            result["status"] = "success"
            result["message"] = (
                f"已把 file_vault 条目 {result['item_id']} 导入共读《{result['book_name']}》，"
                f"共 {result['total_chapters']} 章。"
            )
            if result.get("renamed"):
                result["message"] += "检测到重名书籍，已自动改名以避免覆盖原书。"
            return result
        except Exception as exc:
            logger.error("bookshelf: import_file_vault_item_to_bookshelf failed: %s", exc, exc_info=True)
            return {
                "status": "error",
                "item_id": str(item_id or "").strip(),
                "book_name": str(book_name or "").strip(),
                "message": f"导入共读失败：{exc}",
            }
