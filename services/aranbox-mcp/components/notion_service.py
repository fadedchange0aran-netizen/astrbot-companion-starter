# 文件名: components/notion_service.py
# 版本：v3.1 - 分类路由 + 年月日结构

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlparse
import pytz
from notion_client import Client

TZ_CN = pytz.timezone('Asia/Shanghai')
_PAGE_CACHE_TTL_SECONDS = 300
_PAGE_CACHE = {}

NOTION_ROUTE_ROOTS = {
    "inbox": ("ARAN_NOTION_INBOX_PAGE", "https://www.notion.so/00-37bee01d3056802aa536ff065d84ab24"),
    "love_diary": ("ARAN_NOTION_LOVE_DIARY_PAGE", "https://www.notion.so/01-313ee01d30568045a752d56df5d22242"),
    "timeline": ("ARAN_NOTION_TIMELINE_PAGE", "https://www.notion.so/02-37bee01d30568074b175fb2460aa26cc"),
    "memory_profile": ("ARAN_NOTION_MEMORY_PROFILE_PAGE", "https://www.notion.so/03-37bee01d3056805f890acb37d9e85cb5"),
    "memo": ("ARAN_NOTION_MEMO_PAGE", "https://www.notion.so/04-37bee01d305680f49716d34306409fdd"),
    "todo": ("ARAN_NOTION_TODO_PAGE", "https://www.notion.so/05-37bee01d305680dc815ff9f1c5743280"),
    "archive": ("ARAN_NOTION_ARCHIVE_PAGE", "https://www.notion.so/06-37bee01d3056800491f1e048a9d83578"),
    "creation": ("ARAN_NOTION_CREATION_PAGE", "https://www.notion.so/07-37bee01d305680348a1dceaee1d01ef9"),
    "discard": ("ARAN_NOTION_DISCARD_PAGE", "https://www.notion.so/99-37bee01d305680cda888fdf0c1ea9d2e"),
}

NOTION_CATEGORY_ALIASES = {
    "inbox": "inbox",
    "待整理": "inbox",
    "love_diary": "love_diary",
    "恋爱日记": "love_diary",
    "timeline": "timeline",
    "时间线": "timeline",
    "memory_profile": "memory_profile",
    "记忆设定": "memory_profile",
    "memo": "memo",
    "备忘录": "memo",
    "todo": "todo",
    "待办": "todo",
    "archive": "archive",
    "资料档案": "archive",
    "creation": "creation",
    "创作": "creation",
    "discard": "discard",
    "作废与重复": "discard",
}

CREATION_TYPE_ALIASES = {
    "series": "series",
    "系列": "series",
    "short": "short",
    "short_story": "short",
    "短篇": "short",
    "extra": "extra",
    "egg": "extra",
    "彩蛋": "extra",
}

NOTION_SEARCH_CATEGORY_LABELS = {
    "inbox": "待整理",
    "love_diary": "恋爱日记",
    "timeline": "时间线",
    "memory_profile": "记忆设定",
    "memo": "备忘录",
    "todo": "待办",
    "archive": "资料档案",
    "creation": "创作",
    "discard": "作废与重复",
}

def _normalize_title(title: str) -> str:
    return str(title or "").strip().lower()


def _normalize_page_id(raw: str) -> str:
    token = re.sub(r"[^0-9a-fA-F]", "", str(raw or ""))
    if len(token) != 32:
        return ""
    return f"{token[:8]}-{token[8:12]}-{token[12:16]}-{token[16:20]}-{token[20:]}"


def _page_id_from_ref(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = _normalize_page_id(raw)
    if normalized:
        return normalized
    parsed = urlparse(raw)
    path = parsed.path or raw
    tail = path.rstrip("/").split("/")[-1]
    return _normalize_page_id(tail.split("-")[-1])


def _resolve_route_root_id(route_name: str) -> str:
    env_name, default_ref = NOTION_ROUTE_ROOTS.get(route_name, ("", ""))
    configured = os.getenv(env_name, "").strip() if env_name else ""
    return _page_id_from_ref(configured or default_ref)


def _normalize_date_parts(year: int, month: int, day: int) -> str | None:
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except Exception:
        return None


def _normalize_month_parts(year: int, month: int) -> str | None:
    try:
        return f"{int(year):04d}-{int(month):02d}"
    except Exception:
        return None


def _extract_normalized_query_date(text: str) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None

    patterns = [
        r"(20\d{2})[./年-]\s*(\d{1,2})[./月-]\s*(\d{1,2})",
        r"(20\d{2})(\d{2})(\d{2})",
        r"(?<!\d)(\d{1,2})[./月]\s*(\d{1,2})(?:日)?(?!\d)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if not match:
            continue
        if len(match.groups()) == 3 and len(match.group(1)) == 4:
            return _normalize_date_parts(match.group(1), match.group(2), match.group(3))
        if len(match.groups()) == 2:
            current_year = datetime.now(TZ_CN).year
            return _normalize_date_parts(current_year, match.group(1), match.group(2))
    return None


def _extract_normalized_query_month(text: str) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None

    patterns = [
        r"(20\d{2})[./年-]\s*(\d{1,2})(?!\d)(?!\s*[./月-]\s*\d)",
        r"(20\d{2})(\d{2})(?!\d)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if not match:
            continue
        return _normalize_month_parts(match.group(1), match.group(2))
    return None


def _extract_normalized_title_dates(title: str) -> set[str]:
    raw = str(title or "")
    if not raw:
        return set()

    found: set[str] = set()
    current_year = datetime.now(TZ_CN).year

    for year, month, day in re.findall(r"(20\d{2})[./年-]\s*(\d{1,2})[./月-]\s*(\d{1,2})", raw):
        normalized = _normalize_date_parts(year, month, day)
        if normalized:
            found.add(normalized)

    for year, month, day in re.findall(r"(20\d{2})(\d{2})(\d{2})", raw):
        normalized = _normalize_date_parts(year, month, day)
        if normalized:
            found.add(normalized)

    for month, day in re.findall(r"(?<!\d)(\d{1,2})[./月]\s*(\d{1,2})(?:日)?(?!\d)", raw):
        normalized = _normalize_date_parts(current_year, month, day)
        if normalized:
            found.add(normalized)

    return found


def _extract_normalized_title_months(title: str) -> set[str]:
    raw = str(title or "")
    if not raw:
        return set()

    found: set[str] = set()

    for year, month, day in re.findall(r"(20\d{2})[./年-]\s*(\d{1,2})[./月-]\s*(\d{1,2})", raw):
        normalized_day = _normalize_date_parts(year, month, day)
        normalized_month = _normalize_month_parts(year, month)
        if normalized_day:
            found.add(normalized_day[:7])
        if normalized_month:
            found.add(normalized_month)

    for year, month, day in re.findall(r"(20\d{2})(\d{2})(\d{2})", raw):
        normalized_day = _normalize_date_parts(year, month, day)
        normalized_month = _normalize_month_parts(year, month)
        if normalized_day:
            found.add(normalized_day[:7])
        if normalized_month:
            found.add(normalized_month)

    for year, month in re.findall(r"(20\d{2})[./年-]\s*(\d{1,2})(?!\d)(?!\s*[./月-]\s*\d)", raw):
        normalized_month = _normalize_month_parts(year, month)
        if normalized_month:
            found.add(normalized_month)

    return found


def _strip_date_tokens(text: str) -> str:
    raw = str(text or "")
    patterns = [
        r"(20\d{2})[./年-]\s*(\d{1,2})[./月-]\s*(\d{1,2})",
        r"(20\d{2})(\d{2})(\d{2})",
        r"(20\d{2})[./年-]\s*(\d{1,2})(?!\d)(?!\s*[./月-]\s*\d)",
        r"(20\d{2})(\d{2})(?!\d)",
        r"(?<!\d)(\d{1,2})[./月]\s*(\d{1,2})(?:日)?(?!\d)",
    ]
    for pattern in patterns:
        raw = re.sub(pattern, " ", raw)
    return re.sub(r"\s+", " ", raw).strip(" ,，·()（）-_").strip()

def _cache_page(page: dict, *aliases: str):
    if not page:
        return
    expires_at = time.monotonic() + _PAGE_CACHE_TTL_SECONDS
    page_id = _page_id(page)
    page_url = _page_url(page)
    names = {_page_title(page), page_id, page_url, *aliases}
    for name in names:
        key = _normalize_title(name)
        if key:
            _PAGE_CACHE[key] = (expires_at, page)

def _get_cached_page(title: str):
    key = _normalize_title(title)
    if not key:
        return None
    cached = _PAGE_CACHE.get(key)
    if not cached:
        return None
    expires_at, page = cached
    if expires_at < time.monotonic():
        _PAGE_CACHE.pop(key, None)
        return None
    return page

def _invalidate_cached_page(*titles: str):
    for title in titles:
        key = _normalize_title(title)
        if key:
            _PAGE_CACHE.pop(key, None)


def _page_id(page: dict) -> str:
    return str((page or {}).get("id") or "").strip()


def _page_url(page: dict) -> str:
    return str((page or {}).get("url") or "").strip()


def _append_paragraph_blocks(notion, page_id: str, blocks: list[dict]):
    if not blocks:
        return
    notion.blocks.children.append(block_id=page_id, children=blocks)


def _build_paragraph_blocks(content: str = None, color: str = "default", lines: list = None) -> list[dict]:
    blocks: list[dict] = []
    if lines:
        for item in lines:
            text = str((item or {}).get("text", "")).strip()
            if not text:
                continue
            item_color = str((item or {}).get("color", color or "default"))
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": text[:2000]},
                        "annotations": {"color": item_color},
                    }]
                },
            })
    elif content:
        for line in str(content).split("\n"):
            if not line.strip():
                continue
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": line[:2000]},
                        "annotations": {"color": color},
                    }]
                },
            })
    return blocks


def _find_child_page_by_title(notion, parent_id: str, title: str):
    normalized = _normalize_title(title)
    if not parent_id or not normalized:
        return None
    has_more = True
    start_cursor = None
    while has_more:
        kwargs = {"block_id": parent_id, "page_size": 100}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        resp = notion.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            if block.get("type") != "child_page":
                continue
            child_title = str(block.get("child_page", {}).get("title") or "").strip()
            if _normalize_title(child_title) != normalized:
                continue
            try:
                page = notion.pages.retrieve(page_id=block["id"])
            except Exception:
                continue
            if page and page.get("object") == "page":
                _cache_page(page, title, child_title)
                return page
        has_more = bool(resp.get("has_more"))
        start_cursor = resp.get("next_cursor")
    return None


def _ensure_child_page(notion, parent_id: str, title: str):
    existing = _find_child_page_by_title(notion, parent_id, title)
    if existing:
        return existing
    page = notion.pages.create(
        parent={"type": "page_id", "page_id": parent_id},
        properties={"title": {"title": [{"text": {"content": str(title)[:2000]}}]}},
    )
    _cache_page(page, title)
    return page


def _resolve_route_datetime(date_hint: str = "", title: str = "", content: str = "") -> datetime:
    for raw in (date_hint, title, content):
        normalized = _extract_normalized_query_date(raw)
        if not normalized:
            continue
        try:
            return TZ_CN.localize(datetime.strptime(normalized, "%Y-%m-%d"))
        except Exception:
            pass
    return datetime.now(TZ_CN)


def _normalize_notion_category(category: str) -> str:
    key = str(category or "").strip()
    return NOTION_CATEGORY_ALIASES.get(key, NOTION_CATEGORY_ALIASES.get(key.lower(), ""))


def _normalize_creation_type(creation_type: str, series_name: str = "") -> str:
    key = str(creation_type or "").strip()
    if key:
        return CREATION_TYPE_ALIASES.get(key, CREATION_TYPE_ALIASES.get(key.lower(), "short"))
    return "series" if str(series_name or "").strip() else "short"


def _search_category_label(category: str) -> str:
    normalized = _normalize_notion_category(category)
    return NOTION_SEARCH_CATEGORY_LABELS.get(normalized, "")


def _build_notion_search_seed(query: str, category: str = "") -> str:
    raw_query = str(query or "").strip()
    if not raw_query:
        return ""

    label = _search_category_label(category)
    if not label:
        return raw_query

    raw_norm = _normalize_title(raw_query)
    label_norm = _normalize_title(label)
    if label_norm and label_norm in raw_norm:
        return raw_query

    keyword_query = _strip_date_tokens(raw_query)
    base_query = keyword_query or raw_query
    if label_norm and label_norm in _normalize_title(base_query):
        return base_query
    return f"{label} {base_query}".strip()


def _timestamped_line(moment: datetime, text: str) -> str:
    return f"【{moment.strftime('%Y-%m-%d %H:%M')}】{str(text or '').strip()}"


def _route_notion_target(
    notion,
    *,
    category: str,
    title: str = "",
    content: str = "",
    date_hint: str = "",
    series_name: str = "",
    creation_type: str = "",
):
    normalized_category = _normalize_notion_category(category)
    if not normalized_category:
        raise ValueError("未识别的 Notion 分类")

    root_id = _resolve_route_root_id(normalized_category)
    if not root_id:
        raise ValueError(f"未配置 {normalized_category} 顶级页面")

    moment = _resolve_route_datetime(date_hint=date_hint, title=title, content=content)
    year = moment.strftime("%Y")
    month = moment.strftime("%Y-%m")
    day = moment.strftime("%Y-%m-%d")
    route_titles: list[str] = []

    if normalized_category == "love_diary":
        year_page = _ensure_child_page(notion, root_id, f"恋爱日记｜{year}")
        month_page = _ensure_child_page(notion, year_page["id"], f"恋爱日记｜{month}")
        day_page = _ensure_child_page(notion, month_page["id"], day)
        route_titles = [f"恋爱日记｜{year}", f"恋爱日记｜{month}", day]
        return {"page": day_page, "route_titles": route_titles, "category": normalized_category, "moment": moment}

    if normalized_category == "timeline":
        year_page = _ensure_child_page(notion, root_id, f"时间线｜{year}")
        month_page = _ensure_child_page(notion, year_page["id"], f"时间线｜{month}")
        route_titles = [f"时间线｜{year}", f"时间线｜{month}"]
        return {"page": month_page, "route_titles": route_titles, "category": normalized_category, "moment": moment}

    if normalized_category == "memo":
        year_page = _ensure_child_page(notion, root_id, f"备忘录｜{year}")
        month_page = _ensure_child_page(notion, year_page["id"], f"备忘录｜{month}")
        route_titles = [f"备忘录｜{year}", f"备忘录｜{month}"]
        return {"page": month_page, "route_titles": route_titles, "category": normalized_category, "moment": moment}

    if normalized_category == "inbox":
        target_title = "待整理｜当前"
        page = _ensure_child_page(notion, root_id, target_title)
        route_titles = [target_title]
        return {"page": page, "route_titles": route_titles, "category": normalized_category, "moment": moment}

    if normalized_category == "todo":
        page = _ensure_child_page(notion, root_id, "待办｜当前")
        return {"page": page, "route_titles": ["待办｜当前"], "category": normalized_category, "moment": moment}

    if normalized_category == "memory_profile":
        leaf_title = str(title or "").strip() or "核心记忆"
        page = _ensure_child_page(notion, root_id, leaf_title)
        return {"page": page, "route_titles": [leaf_title], "category": normalized_category, "moment": moment}

    if normalized_category == "archive":
        leaf_title = str(title or "").strip() or "参考资料"
        page = _ensure_child_page(notion, root_id, leaf_title)
        return {"page": page, "route_titles": [leaf_title], "category": normalized_category, "moment": moment}

    if normalized_category == "discard":
        leaf_title = str(title or "").strip() or "待清理"
        page = _ensure_child_page(notion, root_id, leaf_title)
        return {"page": page, "route_titles": [leaf_title], "category": normalized_category, "moment": moment}

    creation_mode = _normalize_creation_type(creation_type, series_name=series_name)
    if creation_mode == "series":
        series_root = _ensure_child_page(notion, root_id, "创作｜系列")
        series_page = _ensure_child_page(notion, series_root["id"], str(series_name or "").strip() or "未命名系列")
        if str(title or "").strip() and _normalize_title(title) != _normalize_title(series_name):
            page = _ensure_child_page(notion, series_page["id"], str(title).strip())
            route_titles = ["创作｜系列", _page_title(series_page), _page_title(page)]
            return {"page": page, "route_titles": route_titles, "category": normalized_category, "moment": moment}
        return {"page": series_page, "route_titles": ["创作｜系列", _page_title(series_page)], "category": normalized_category, "moment": moment}

    if creation_mode == "extra":
        extra_root = _ensure_child_page(notion, root_id, "创作｜彩蛋")
        page = _ensure_child_page(
            notion,
            extra_root["id"],
            str(title or "").strip() or f"彩蛋｜{day}",
        )
        return {"page": page, "route_titles": ["创作｜彩蛋", _page_title(page)], "category": normalized_category, "moment": moment}

    short_root = _ensure_child_page(notion, root_id, "创作｜短篇")
    page = _ensure_child_page(
        notion,
        short_root["id"],
        str(title or "").strip() or f"短篇｜{day}",
    )
    return {"page": page, "route_titles": ["创作｜短篇", _page_title(page)], "category": normalized_category, "moment": moment}

def get_notion_client():
    api_key = os.getenv("ARAN_NOTION_TOKEN")
    if not api_key:
        print("[get_notion_client] ❌ 未配置 ARAN_NOTION_TOKEN")
        return None
    return Client(auth=api_key)


def _build_rich_text_fragments(value) -> list[dict]:
    items = value if isinstance(value, list) else [value]
    fragments: list[dict] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        while text:
            chunk = text[:2000]
            fragments.append({
                "type": "text",
                "text": {"content": chunk},
            })
            text = text[2000:]
    return fragments


def _extract_database_title_property_name(database: dict, requested: str = "") -> str:
    props = database.get("properties", {}) or {}
    requested_name = str(requested or "").strip()
    if requested_name and props.get(requested_name, {}).get("type") == "title":
        return requested_name
    for name, prop in props.items():
        if (prop or {}).get("type") == "title":
            return str(name)
    return requested_name


def _coerce_notion_database_property(value):
    if isinstance(value, dict):
        if "title" in value:
            fragments = _build_rich_text_fragments(value.get("title"))
            return {"title": fragments} if fragments else None
        if "rich_text" in value:
            fragments = _build_rich_text_fragments(value.get("rich_text"))
            return {"rich_text": fragments} if fragments else None
        if "number" in value:
            raw = value.get("number")
            return {"number": None if raw in ("", None) else float(raw)}
        if "date" in value:
            raw_date = value.get("date")
            if isinstance(raw_date, dict):
                start = str(raw_date.get("start", "")).strip()
                if not start:
                    return None
                payload = {"start": start}
                end = str(raw_date.get("end", "")).strip()
                time_zone = str(raw_date.get("time_zone", "")).strip()
                if end:
                    payload["end"] = end
                if time_zone:
                    payload["time_zone"] = time_zone
                return {"date": payload}
            start = str(raw_date or "").strip()
            return {"date": {"start": start}} if start else None
        if "select" in value:
            option = str(value.get("select") or "").strip()
            return {"select": {"name": option}} if option else None
        if "multi_select" in value:
            options = [
                {"name": str(item).strip()}
                for item in (value.get("multi_select") or [])
                if str(item or "").strip()
            ]
            return {"multi_select": options} if options else None
        if "checkbox" in value:
            return {"checkbox": bool(value.get("checkbox"))}
        if "url" in value:
            raw = str(value.get("url") or "").strip()
            return {"url": raw or None}
        if "email" in value:
            raw = str(value.get("email") or "").strip()
            return {"email": raw or None}
        if "phone_number" in value:
            raw = str(value.get("phone_number") or "").strip()
            return {"phone_number": raw or None}
        return None

    fragments = _build_rich_text_fragments(value)
    return {"rich_text": fragments} if fragments else None


def append_notion_database_row(
    database_id: str,
    title: str = "",
    properties_json: str = "",
    title_property: str = "名称",
) -> str:
    notion = get_notion_client()
    if not notion:
        return "❌ 未配置 Notion Token"
    try:
        normalized_database_id = _page_id_from_ref(database_id)
        if not normalized_database_id:
            return "❌ database_id 无效，请传 Notion 数据库链接或 32 位 database_id"

        database = notion.databases.retrieve(database_id=normalized_database_id)
        resolved_title_property = _extract_database_title_property_name(database, title_property)
        if not resolved_title_property:
            return "❌ 没找到数据库的标题字段"

        raw_properties = {}
        if properties_json:
            raw_properties = json.loads(properties_json)
            if not isinstance(raw_properties, dict):
                return "❌ properties_json 必须是 JSON 对象"

        properties_payload = {}
        for property_name, property_value in raw_properties.items():
            coerced = _coerce_notion_database_property(property_value)
            if coerced is None:
                return f"❌ 字段 {property_name} 的值无法识别，请使用 rich_text/date/number/select/multi_select/checkbox/url/email/phone_number"
            properties_payload[str(property_name)] = coerced

        title_value = str(title or "").strip()
        if title_value:
            title_fragments = _build_rich_text_fragments(title_value)
            if not title_fragments:
                return "❌ title 不能为空白"
            properties_payload[resolved_title_property] = {"title": title_fragments}
        elif resolved_title_property not in properties_payload:
            return f"❌ 缺少标题字段，请传 title，或在 properties_json 里提供 {resolved_title_property}"

        created = notion.pages.create(
            parent={"database_id": normalized_database_id},
            properties=properties_payload,
        )
        created_url = str(created.get("url") or "").strip()
        title_desc = title_value or resolved_title_property
        suffix = f"：{created_url}" if created_url else ""
        return f"✅ 已新增数据库记录《{title_desc}》{suffix}"
    except json.JSONDecodeError as e:
        return f"❌ properties_json 不是合法 JSON：{e}"
    except Exception as e:
        print(f"[append_notion_database_row] ❌ 写入失败: {e}")
        return f"❌ 写入失败: {e}"


def summarize_notion_database_numbers(
    database_id: str,
    number_property: str = "金额",
    status_property: str = "状态",
    confirmed_status: str = "已确认",
    pending_status: str = "待 owner 确认",
) -> dict:
    notion = get_notion_client()
    if not notion:
        return {"ok": False, "error": "❌ 未配置 Notion Token"}
    try:
        normalized_database_id = _page_id_from_ref(database_id)
        if not normalized_database_id:
            return {"ok": False, "error": "❌ database_id 无效，请传 Notion 数据库链接或 32 位 database_id"}

        total_amount = 0.0
        confirmed_amount = 0.0
        pending_amount = 0.0
        positive_amount = 0.0
        negative_amount = 0.0
        record_count = 0
        start_cursor = None

        while True:
            kwargs = {
                "database_id": normalized_database_id,
                "page_size": 100,
            }
            if start_cursor:
                kwargs["start_cursor"] = start_cursor
            resp = notion.databases.query(**kwargs)
            for row in resp.get("results", []):
                props = row.get("properties", {}) or {}
                number_info = props.get(number_property, {}) or {}
                raw_number = None
                if number_info.get("type") == "number":
                    raw_number = number_info.get("number")
                if raw_number is None:
                    continue

                amount = float(raw_number)
                record_count += 1
                total_amount += amount
                if amount >= 0:
                    positive_amount += amount
                else:
                    negative_amount += amount

                status_info = props.get(status_property, {}) or {}
                status_name = ""
                if status_info.get("type") == "select":
                    status_name = str((status_info.get("select") or {}).get("name") or "").strip()
                elif status_info.get("type") == "status":
                    status_name = str((status_info.get("status") or {}).get("name") or "").strip()

                if status_name == confirmed_status:
                    confirmed_amount += amount
                elif status_name == pending_status:
                    pending_amount += amount

            if not resp.get("has_more"):
                break
            start_cursor = resp.get("next_cursor")

        return {
            "ok": True,
            "database_id": normalized_database_id,
            "number_property": number_property,
            "status_property": status_property,
            "record_count": record_count,
            "total_amount": round(total_amount, 2),
            "confirmed_amount": round(confirmed_amount, 2),
            "pending_amount": round(pending_amount, 2),
            "positive_amount": round(positive_amount, 2),
            "negative_amount": round(negative_amount, 2),
            "currency": "CNY",
        }
    except Exception as e:
        print(f"[summarize_notion_database_numbers] ❌ 读取失败: {e}")
        return {"ok": False, "error": f"❌ 读取失败: {e}"}

def get_current_week_info():
    """获取当前周的信息"""
    now = datetime.now(TZ_CN)
    year, week_num, _ = now.isocalendar()
    week_start = now - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=6)
    week_title = f"{year}-W{week_num:02d}"
    week_display = f"{week_start.strftime('%m.%d')}-{week_end.strftime('%m.%d')}"
    return week_title, week_display, year

def _iter_search_pages(notion, query: str, max_results: int = 30):
    max_results = max(1, min(int(max_results or 30), 100))
    has_more = True
    start_cursor = None
    yielded = 0
    while has_more:
        kwargs = {
            "query": query,
            "filter": {"property": "object", "value": "page"},
            "page_size": min(100, max_results),
        }
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        resp = notion.search(**kwargs)
        for item in resp.get("results", []):
            yield item
            yielded += 1
            if yielded >= max_results:
                return
        has_more = bool(resp.get("has_more"))
        start_cursor = resp.get("next_cursor")


def _search_pages_with_filters(
    notion,
    query: str,
    *,
    category: str = "",
    max_results: int = 30,
) -> tuple[list[dict], bool]:
    query = str(query or "").strip()
    if not query:
        return [], False

    query_date = _extract_normalized_query_date(query)
    query_month = _extract_normalized_query_month(query)
    keyword_query = _strip_date_tokens(query)
    search_seed = _build_notion_search_seed(keyword_query or query, category=category)
    max_results = max(1, min(int(max_results or 30), 100))

    pages = [p for p in _iter_search_pages(notion, search_seed, max_results=max_results) if _page_title(p)]
    truncated = len(pages) >= max_results
    if keyword_query:
        keyword_norm = _normalize_title(keyword_query)
        pages = [p for p in pages if keyword_norm in _normalize_title(_page_title(p))]
    if query_date:
        pages = [
            p for p in pages
            if query_date in _extract_normalized_title_dates(_page_title(p))
        ]
    elif query_month:
        pages = [
            p for p in pages
            if query_month in _extract_normalized_title_months(_page_title(p))
        ]
    return pages, truncated


def search_pages(
    query: str,
    category: str = "",
    sample_limit: int = 8,
    max_results: int = 30,
) -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        query = str(query or "").strip()
        if not query:
            return "❌ query 不能为空"

        sample_limit = max(1, min(int(sample_limit or 8), 12))
        max_results = max(1, min(int(max_results or 30), 100))
        query_date = _extract_normalized_query_date(query)
        query_month = _extract_normalized_query_month(query)
        category_label = _search_category_label(category)
        pages, truncated = _search_pages_with_filters(
            notion,
            query,
            category=category,
            max_results=max_results,
        )
        if not pages:
            if category_label:
                return f"🤷 没找到（已按 {category_label} 缩小搜索）"
            return "🤷 没找到"

        for page in pages:
            _cache_page(page)

        titles = [_page_title(p) for p in pages]
        normalized_query = _normalize_title(query)
        exact_titles = [title for title in titles if _normalize_title(title) == normalized_query]
        title_counter = Counter(titles)
        first_page_by_title = {}
        for page in pages:
            first_page_by_title.setdefault(_page_title(page), page)
        sample_titles = sorted(
            title_counter.items(),
            key=lambda item: (
                0 if _normalize_title(item[0]) == normalized_query else 1,
                -item[1],
                item[0].lower(),
            ),
        )

        scope_suffix = f"（分类：{category_label}）" if category_label else ""
        if query_date:
            lines = [f"🔎 共找到 {len(pages)} 个页面结果{scope_suffix}（日期归一化匹配到 {query_date}）"]
        elif query_month:
            lines = [f"🔎 共找到 {len(pages)} 个页面结果{scope_suffix}（月份归一化匹配到 {query_month}）"]
        else:
            lines = [f"🔎 共找到 {len(pages)} 个页面结果{scope_suffix}"]
        if truncated:
            lines.append(f"⚠️ 本次只检查前 {max_results} 个搜索结果；若还太多，请把 query 说得更具体")
        if exact_titles:
            lines.append(f"🎯 标题完全等于“{query}”的页面有 {len(exact_titles)} 个")

        shown = sample_titles[:sample_limit]
        lines.append("🧾 标题样本:")
        for idx, (title, count) in enumerate(shown, start=1):
            suffix = f" x{count}" if count > 1 else ""
            page = first_page_by_title.get(title) or {}
            page_id = _page_id(page)
            page_hint = f" | page_id={page_id}" if page_id else ""
            lines.append(f"{idx}. {title}{suffix}{page_hint}")

        remaining = len(sample_titles) - len(shown)
        if remaining > 0:
            lines.append(f"…… 其余还有 {remaining} 个不同标题未展开")

        if len(exact_titles) == 1:
            exact_page = next((p for p in pages if _normalize_title(_page_title(p)) == normalized_query), None)
            exact_page_id = _page_id(exact_page or {})
            if exact_page_id:
                lines.append(
                    f"💡 可以直接用 read_notion_page_content(page_id=\"{exact_page_id}\") 继续读取"
                )
            else:
                lines.append(f"💡 可以直接用 read_notion_page_content(title=\"{query}\") 继续读取")
        elif len(exact_titles) > 1:
            lines.append(f"💡 “{query}”存在多个完全重名页面，建议先改得更具体一些再读取，避免读错页")
        else:
            lines.append("💡 结果太多时，优先补日期、年月或分类后再搜")

        return "\n".join(lines)
    except Exception as e:
        print(f"[search_pages] ❌ 搜索失败: {e}")
        return "❌ 搜索失败"

def _page_title(page: dict) -> str:
    title_items = (
        page.get("properties", {})
        .get("title", {})
        .get("title", [])
    )
    return "".join(t.get("plain_text") or t.get("text", {}).get("content", "") for t in title_items).strip()


def _find_page_by_id(notion, page_id: str):
    page_id = str(page_id or "").strip()
    if not page_id:
        return None, "❌ page_id 不能为空"
    cached = _get_cached_page(page_id)
    if cached and _page_id(cached) == page_id:
        return cached, None
    try:
        page = notion.pages.retrieve(page_id=page_id)
    except Exception as exc:
        return None, f"❌ 读取页面失败: {exc}"
    if not page or page.get("object") != "page":
        return None, "🤷 没找到页面"
    _cache_page(page, page_id)
    return page, None

def _find_page_by_title(notion, title: str):
    cached = _get_cached_page(title)
    if cached:
        cached_title = _page_title(cached)
        if cached_title and cached_title.lower() == str(title).strip().lower():
            return cached, None

    pages, _ = _search_pages_with_filters(notion, title, max_results=80)
    if not pages:
        return None, "🤷 没找到页面"

    exact = [p for p in pages if _page_title(p) == title]
    if len(exact) == 1:
        _cache_page(exact[0], title)
        return exact[0], None
    if len(exact) > 1:
        return None, f"⚠️ 找到 {len(exact)} 个完全同名页面，请使用更具体的标题再读取"

    exact_ci = [p for p in pages if _page_title(p).lower() == title.lower()]
    if len(exact_ci) == 1:
        _cache_page(exact_ci[0], title)
        return exact_ci[0], None
    if len(exact_ci) > 1:
        return None, f"⚠️ 找到 {len(exact_ci)} 个大小写无关的同名页面，请使用更具体的标题再读取"

    partial = [p for p in pages if title.lower() in _page_title(p).lower()]
    if len(partial) == 1:
        _cache_page(partial[0], title)
        return partial[0], None
    if len(partial) > 1:
        titles = ", ".join(_page_title(p) for p in partial[:5])
        return None, f"⚠️ 找到多个相似页面，请用更精确标题：{titles}"

    return None, "🤷 没找到页面"


def _resolve_page(notion, title: str = "", page_id: str = ""):
    if str(page_id or "").strip():
        return _find_page_by_id(notion, page_id)
    return _find_page_by_title(notion, title)

def _get_paragraph_blocks(notion, page_id: str):
    blocks = []
    has_more = True
    start_cursor = None
    while has_more:
        kwargs = {"block_id": page_id, "page_size": 100}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        resp = notion.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            if block.get("type") != "paragraph":
                continue
            rich_text = block.get("paragraph", {}).get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rich_text).strip()
            color = block.get("paragraph", {}).get("color", "default")
            blocks.append({
                "id": block["id"],
                "text": text,
                "color": color,
            })
        has_more = resp.get("has_more", False)
        start_cursor = resp.get("next_cursor")
    return blocks

def _get_todo_blocks(notion, page_id: str):
    blocks = []
    has_more = True
    start_cursor = None
    while has_more:
        kwargs = {"block_id": page_id, "page_size": 100}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        resp = notion.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            if block.get("type") != "to_do":
                continue
            rich_text = block.get("to_do", {}).get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rich_text).strip()
            color = block.get("to_do", {}).get("color", "default")
            checked = bool(block.get("to_do", {}).get("checked", False))
            blocks.append({
                "id": block["id"],
                "text": text,
                "color": color,
                "checked": checked,
            })
        has_more = resp.get("has_more", False)
        start_cursor = resp.get("next_cursor")
    return blocks


def _list_child_page_entries(notion, page_id: str) -> list[dict]:
    entries: list[dict] = []
    has_more = True
    start_cursor = None
    while has_more:
        kwargs = {"block_id": page_id, "page_size": 100}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        resp = notion.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            if block.get("type") != "child_page":
                continue
            title = str(block.get("child_page", {}).get("title") or "").strip()
            entries.append({
                "id": block.get("id", ""),
                "title": title,
            })
        has_more = bool(resp.get("has_more"))
        start_cursor = resp.get("next_cursor")
    return entries


def _strip_timestamp_prefix(text: str) -> str:
    raw = str(text or "").strip()
    return re.sub(r"^【\d{4}-\d{2}-\d{2} \d{2}:\d{2}】", "", raw).strip()

def _create_simple_page(notion, title: str):
    parent_id = os.getenv("ARAN_NOTION_PARENT_ID")
    if not parent_id:
        raise ValueError("未配置 ARAN_NOTION_PARENT_ID")
    page = notion.pages.create(
        parent={"type": "page_id", "page_id": parent_id},
        properties={"title": {"title": [{"text": {"content": title[:2000]}}]}},
    )
    _cache_page(page, title)
    return page

def read_page_content(
    title: str = "",
    start_index: int = 0,
    max_length: int = 3000,
    include_block_index: bool = False,
    page_id: str = "",
) -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        page, err = _resolve_page(notion, title=title, page_id=page_id)
        if not page:
            return err or "🤷 没找到页面"
        blocks = [b for b in _get_paragraph_blocks(notion, page["id"]) if b["text"]]
        page_title = _page_title(page) or title or page_id
        lines = [
            f"[{idx}] {block['text']}" if include_block_index else block["text"]
            for idx, block in enumerate(blocks, start=1)
        ]
        full_content = "\n".join(lines).replace('"', "'")
        total = len(full_content)
        if start_index >= total: return "📖 已读完"
        end = min(start_index + max_length, total)
        res = f"📖 {start_index}-{end}/{total}:\n\n{full_content[start_index:end]}"
        if end < total:
            extra = ", include_block_index=True" if include_block_index else ""
            next_ref = f'page_id="{page["id"]}"' if page_id or _page_id(page) else f'title="{page_title}"'
            res += f"\n\n📄 继续读取：read_page_content({next_ref}, start_index={end}{extra})"
        return res
    except Exception as e:
        print(f"[read_page_content] ❌ 读取失败: {e}")
        return "❌ 读取失败"

def list_notion_blocks(title: str = "", start_index: int = 1, limit: int = 20, page_id: str = "") -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        page, err = _resolve_page(notion, title=title, page_id=page_id)
        if not page:
            return err or "🤷 没找到页面"
        blocks = _get_paragraph_blocks(notion, page["id"])
        if not blocks:
            return "📄 页面里还没有可编辑的段落"
        start = max(1, start_index)
        end = min(len(blocks), start + max(1, limit) - 1)
        rows = [f"[{idx}] {blocks[idx - 1]['text']}" for idx in range(start, end + 1)]
        page_title = _page_title(page) or title or page_id
        res = f"📄 {page_title} 的段落编号（{start}-{end}/{len(blocks)}）:\n\n" + "\n".join(rows)
        if end < len(blocks):
            next_ref = f'page_id="{page["id"]}"' if page_id or _page_id(page) else f'title="{page_title}"'
            res += f"\n\n📄 继续查看可用：list_notion_blocks({next_ref}, start_index={end + 1})"
        return res
    except Exception as e:
        print(f"[list_notion_blocks] ❌ 读取失败: {e}")
        return f"❌ 读取失败: {e}"

def update_notion_block(title: str = "", block_index: int = 1, content: str = "", color: str = "default", page_id: str = "") -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        page, err = _resolve_page(notion, title=title, page_id=page_id)
        if not page:
            return err or "🤷 没找到页面"
        blocks = _get_paragraph_blocks(notion, page["id"])
        if not blocks:
            return "📄 页面里还没有可编辑的段落"
        if block_index < 1 or block_index > len(blocks):
            return f"❌ 段落编号超出范围，当前共有 {len(blocks)} 段"
        block = blocks[block_index - 1]
        notion.blocks.update(
            block_id=block["id"],
            paragraph={
                "rich_text": [{
                    "type": "text",
                    "text": {"content": str(content)[:2000]},
                    "annotations": {"color": color},
                }]
            },
        )
        page_title = _page_title(page) or title or page_id
        return f"✅ 已更新《{page_title}》的第 {block_index} 段"
    except Exception as e:
        print(f"[update_notion_block] ❌ 更新失败: {e}")
        return f"❌ 更新失败: {e}"

def delete_notion_block(title: str = "", block_index: int = 1, page_id: str = "") -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        page, err = _resolve_page(notion, title=title, page_id=page_id)
        if not page:
            return err or "🤷 没找到页面"
        blocks = _get_paragraph_blocks(notion, page["id"])
        if not blocks:
            return "📄 页面里还没有可删除的段落"
        if block_index < 1 or block_index > len(blocks):
            return f"❌ 段落编号超出范围，当前共有 {len(blocks)} 段"
        block = blocks[block_index - 1]
        preview = block["text"][:40] + ("..." if len(block["text"]) > 40 else "")
        notion.blocks.delete(block_id=block["id"])
        page_title = _page_title(page) or title or page_id
        return f"✅ 已删除《{page_title}》的第 {block_index} 段：{preview}"
    except Exception as e:
        print(f"[delete_notion_block] ❌ 删除失败: {e}")
        return f"❌ 删除失败: {e}"

def rename_notion_page(old_title: str, new_title: str, page_id: str = "") -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        page, err = _resolve_page(notion, title=old_title, page_id=page_id)
        if not page:
            return err or "🤷 没找到页面"
        if not str(new_title).strip():
            return "❌ 新标题不能为空"

        new_title = str(new_title).strip()
        current_title = _page_title(page)
        if current_title == new_title:
            return f"ℹ️ 页面标题已经是《{new_title}》"

        conflict, _ = _find_page_by_title(notion, new_title)
        if conflict and conflict.get("id") != page.get("id"):
            return f"⚠️ 已存在同名页面《{new_title}》，请换一个标题"

        notion.pages.update(
            page_id=page["id"],
            properties={
                "title": {
                    "title": [
                        {"type": "text", "text": {"content": new_title[:2000]}}
                    ]
                }
            },
        )
        _invalidate_cached_page(old_title, current_title)
        _cache_page(page, new_title)
        return f"✅ 已将页面《{current_title}》重命名为《{new_title}》"
    except Exception as e:
        print(f"[rename_notion_page] ❌ 重命名失败: {e}")
        return f"❌ 重命名失败: {e}"

def add_notion_todo(text: str, page_title: str = "待办｜当前") -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        text = str(text).strip()
        if not text:
            return "❌ 待办内容不能为空"
        page, err = _find_page_by_title(notion, page_title)
        if not page:
            if page_title == "待办｜当前":
                page = _route_notion_target(notion, category="todo", content=text)["page"]
            else:
                page = _create_simple_page(notion, page_title)
        notion.blocks.children.append(
            block_id=page["id"],
            children=[{
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": text[:2000]},
                    }],
                    "checked": False,
                    "color": "default",
                },
            }],
        )
        return f"✅ 已添加待办到《{page_title}》：{text[:60]}"
    except Exception as e:
        print(f"[add_notion_todo] ❌ 添加失败: {e}")
        return f"❌ 添加失败: {e}"

def list_notion_todos(page_title: str = "待办｜当前", show_completed: bool = False) -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        page, err = _find_page_by_title(notion, page_title)
        if not page:
            return err or "🤷 没找到待办页"
        todos = _get_todo_blocks(notion, page["id"])
        if not show_completed:
            todos = [t for t in todos if not t["checked"]]
        if not todos:
            return f"🗒️ 《{page_title}》里暂无{'未完成' if not show_completed else ''}待办"
        rows = []
        for idx, todo in enumerate(todos, start=1):
            mark = "x" if todo["checked"] else " "
            rows.append(f"[{idx}] [{mark}] {todo['text']}")
        return f"🗒️ 《{page_title}》的待办列表：\n\n" + "\n".join(rows)
    except Exception as e:
        print(f"[list_notion_todos] ❌ 读取失败: {e}")
        return f"❌ 读取失败: {e}"

def _resolve_todo_by_index(notion, page_title: str, todo_index: int, include_completed: bool = True):
    page, err = _find_page_by_title(notion, page_title)
    if not page:
        return None, err or "🤷 没找到待办页"
    todos = _get_todo_blocks(notion, page["id"])
    if not include_completed:
        todos = [t for t in todos if not t["checked"]]
    if not todos:
        return None, "🗒️ 当前没有可操作的待办"
    if todo_index < 1 or todo_index > len(todos):
        return None, f"❌ 待办编号超出范围，当前共有 {len(todos)} 条"
    return todos[todo_index - 1], None

def complete_notion_todo(page_title: str, todo_index: int) -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        todo, err = _resolve_todo_by_index(notion, page_title, todo_index, include_completed=False)
        if err:
            return err
        notion.blocks.update(
            block_id=todo["id"],
            to_do={
                "rich_text": [{"type": "text", "text": {"content": todo["text"][:2000]}}],
                "checked": True,
                "color": todo.get("color", "default"),
            },
        )
        return f"✅ 已完成《{page_title}》的第 {todo_index} 条待办"
    except Exception as e:
        print(f"[complete_notion_todo] ❌ 更新失败: {e}")
        return f"❌ 更新失败: {e}"

def update_notion_todo(page_title: str, todo_index: int, text: str) -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        text = str(text).strip()
        if not text:
            return "❌ 待办内容不能为空"
        todo, err = _resolve_todo_by_index(notion, page_title, todo_index, include_completed=True)
        if err:
            return err
        notion.blocks.update(
            block_id=todo["id"],
            to_do={
                "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
                "checked": todo.get("checked", False),
                "color": todo.get("color", "default"),
            },
        )
        return f"✅ 已更新《{page_title}》的第 {todo_index} 条待办"
    except Exception as e:
        print(f"[update_notion_todo] ❌ 更新失败: {e}")
        return f"❌ 更新失败: {e}"

def uncheck_notion_todo(page_title: str, todo_index: int) -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        todo, err = _resolve_todo_by_index(notion, page_title, todo_index, include_completed=True)
        if err:
            return err
        notion.blocks.update(
            block_id=todo["id"],
            to_do={
                "rich_text": [{"type": "text", "text": {"content": todo["text"][:2000]}}],
                "checked": False,
                "color": todo.get("color", "default"),
            },
        )
        return f"✅ 已取消完成《{page_title}》的第 {todo_index} 条待办"
    except Exception as e:
        print(f"[uncheck_notion_todo] ❌ 更新失败: {e}")
        return f"❌ 更新失败: {e}"

def delete_notion_todo(page_title: str, todo_index: int) -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        todo, err = _resolve_todo_by_index(notion, page_title, todo_index, include_completed=True)
        if err:
            return err
        preview = todo["text"][:40] + ("..." if len(todo["text"]) > 40 else "")
        notion.blocks.delete(block_id=todo["id"])
        return f"✅ 已删除《{page_title}》的第 {todo_index} 条待办：{preview}"
    except Exception as e:
        print(f"[delete_notion_todo] ❌ 删除失败: {e}")
        return f"❌ 删除失败: {e}"

def clear_completed_notion_todos(page_title: str = "待办｜当前") -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        page, err = _find_page_by_title(notion, page_title)
        if not page:
            return err or "🤷 没找到待办页"
        todos = _get_todo_blocks(notion, page["id"])
        completed = [t for t in todos if t["checked"]]
        if not completed:
            return f"🗒️ 《{page_title}》里没有已完成待办"
        for todo in completed:
            notion.blocks.delete(block_id=todo["id"])
        return f"✅ 已清理《{page_title}》中 {len(completed)} 条已完成待办"
    except Exception as e:
        print(f"[clear_completed_notion_todos] ❌ 清理失败: {e}")
        return f"❌ 清理失败: {e}"

def manage_notion_todo(
    action: str,
    text: str = None,
    page_title: str = "待办｜当前",
    todo_index: int = None,
    show_completed: bool = False,
) -> str:
    """统一待办管理入口。action: add / list / complete / edit / uncheck / delete / clear_completed"""
    action = (action or "").strip().lower()
    if action == "add":
        return add_notion_todo(text=text, page_title=page_title)
    elif action == "list":
        return list_notion_todos(page_title=page_title, show_completed=show_completed)
    elif action == "complete":
        if todo_index is None:
            return "❌ complete 操作需要 todo_index"
        return complete_notion_todo(page_title=page_title, todo_index=todo_index)
    elif action == "edit":
        if todo_index is None:
            return "❌ edit 操作需要 todo_index"
        return update_notion_todo(page_title=page_title, todo_index=todo_index, text=text)
    elif action == "uncheck":
        if todo_index is None:
            return "❌ uncheck 操作需要 todo_index"
        return uncheck_notion_todo(page_title=page_title, todo_index=todo_index)
    elif action == "delete":
        if todo_index is None:
            return "❌ delete 操作需要 todo_index"
        return delete_notion_todo(page_title=page_title, todo_index=todo_index)
    elif action == "clear_completed":
        return clear_completed_notion_todos(page_title=page_title)
    else:
        return f"❌ 不支持的操作: {action}，可选 add / list / complete / edit / uncheck / delete / clear_completed"


def write_to_notion(title: str, content: str = None, color: str = "default", lines: list = None, parent_title: str = None) -> str:
    notion = get_notion_client()
    if not notion: return "❌ 未配置 Notion Token"
    try:
        blocks = []
        if lines:
            for i in lines: blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": str(i.get("text", ""))[:2000]}, "annotations": {"color": str(i.get("color", "default"))}}]}})
        elif content:
            for l in content.split('\n'):
                if l.strip(): blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": l[:2000]}, "annotations": {"color": color}}]}})
        if not blocks:
            return "❌ 没有可写入的内容"
        page, _ = _find_page_by_title(notion, title)
        if page:
            notion.blocks.children.append(block_id=page["id"], children=blocks)
            return f"✅ 已追加至 {title}"
        else:
            parent_id = os.getenv("ARAN_NOTION_PARENT_ID")
            if not parent_id:
                return "❌ 未配置 ARAN_NOTION_PARENT_ID"
            page = notion.pages.create(parent={"type": "page_id", "page_id": parent_id}, properties={"title": {"title": [{"text": {"content": title}}]}}, children=blocks)
            _cache_page(page, title)
            return f"✅ 已创建页面 {title}"
    except Exception as e:
        print(f"[write_to_notion] ❌ 写入失败: {e}")
        return f"❌ 写入失败: {e}"


def write_to_existing_notion_page(title: str, content: str, color: str = "default") -> str:
    notion = get_notion_client()
    if not notion:
        return "❌ 未配置 Notion Token"
    try:
        title = str(title or "").strip()
        content = str(content or "").strip()
        if not title:
            return "❌ 旧模式直写必须提供 title"
        if not content:
            return "❌ content 不能为空"
        page, err = _find_page_by_title(notion, title)
        if not page:
            return err or f"❌ 旧模式直写仅允许写入已存在页面，未找到《{title}》"
        blocks = _build_paragraph_blocks(content=content, color=color)
        if not blocks:
            return "❌ 没有可写入的内容"
        _append_paragraph_blocks(notion, page["id"], blocks)
        return f"✅ 已按旧模式追加至现有页面《{_page_title(page) or title}》"
    except Exception as e:
        print(f"[write_to_existing_notion_page] ❌ 写入失败: {e}")
        return f"❌ 写入失败: {e}"


def write_structured_notion(
    category: str,
    content: str,
    title: str = "",
    color: str = "default",
    date_hint: str = "",
    series_name: str = "",
    creation_type: str = "",
) -> str:
    notion = get_notion_client()
    if not notion:
        return "❌ 未配置 Notion Token"
    try:
        content = str(content or "").strip()
        title = str(title or "").strip()
        if not content:
            return "❌ content 不能为空"

        target = _route_notion_target(
            notion,
            category=category,
            title=title,
            content=content,
            date_hint=date_hint,
            series_name=series_name,
            creation_type=creation_type,
        )
        page = target["page"]
        moment = target["moment"]
        normalized_category = target["category"]

        if normalized_category == "todo":
            notion.blocks.children.append(
                block_id=page["id"],
                children=[{
                    "object": "block",
                    "type": "to_do",
                    "to_do": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": content[:2000]},
                        }],
                        "checked": False,
                        "color": "default",
                    },
                }],
            )
        else:
            entry_text = content
            if normalized_category in {"timeline", "memo", "inbox"}:
                entry_text = _timestamped_line(moment, content)
                if title:
                    entry_text = f"{entry_text} ｜ {title}"
            blocks = _build_paragraph_blocks(content=entry_text, color=color)
            if not blocks:
                return "❌ 没有可写入的内容"
            _append_paragraph_blocks(notion, page["id"], blocks)

        route_desc = " -> ".join(target["route_titles"])
        page_title = _page_title(page) or title or category
        return f"✅ 已写入 Notion：{route_desc}（目标页：{page_title}）"
    except Exception as e:
        print(f"[write_structured_notion] ❌ 写入失败: {e}")
        return f"❌ 写入失败: {e}"


def list_notion_inbox_entries(page_title: str = "待整理｜当前", start_index: int = 1, limit: int = 20) -> str:
    notion = get_notion_client()
    if not notion:
        return "❌ 未配置 Notion Token"
    try:
        page, err = _find_page_by_title(notion, page_title)
        if not page:
            return err or "🤷 没找到待整理页"
        blocks = [b for b in _get_paragraph_blocks(notion, page["id"]) if b["text"]]
        if not blocks:
            return f"📭 《{page_title}》里当前没有待整理条目"
        start = max(1, int(start_index))
        end = min(len(blocks), start + max(1, int(limit)) - 1)
        rows = [f"[{idx}] {blocks[idx - 1]['text']}" for idx in range(start, end + 1)]
        res = f"📥 《{page_title}》待整理条目（{start}-{end}/{len(blocks)}）:\n\n" + "\n".join(rows)
        if end < len(blocks):
            res += f"\n\n📄 继续查看：list_notion_inbox_entries(page_title=\"{page_title}\", start_index={end + 1})"
        return res
    except Exception as e:
        print(f"[list_notion_inbox_entries] ❌ 读取失败: {e}")
        return f"❌ 读取失败: {e}"


def triage_notion_inbox_entry(
    item_index: int,
    target_category: str,
    page_title: str = "待整理｜当前",
    target_title: str = "",
    date_hint: str = "",
    series_name: str = "",
    creation_type: str = "",
    delete_source: bool = True,
    color: str = "default",
) -> str:
    notion = get_notion_client()
    if not notion:
        return "❌ 未配置 Notion Token"
    try:
        normalized_category = _normalize_notion_category(target_category)
        if not normalized_category:
            return "❌ target_category 无效"
        if normalized_category == "inbox":
            return "❌ 待整理分流目标不能仍然是 inbox / 待整理"

        page, err = _find_page_by_title(notion, page_title)
        if not page:
            return err or "🤷 没找到待整理页"
        blocks = [b for b in _get_paragraph_blocks(notion, page["id"]) if b["text"]]
        if not blocks:
            return f"📭 《{page_title}》里当前没有待整理条目"
        if item_index < 1 or item_index > len(blocks):
            return f"❌ item_index 超出范围，当前共有 {len(blocks)} 条"

        source_block = blocks[item_index - 1]
        source_text = source_block["text"]
        routed = write_structured_notion(
            category=normalized_category,
            title=target_title,
            content=_strip_timestamp_prefix(source_text),
            color=color,
            date_hint=date_hint,
            series_name=series_name,
            creation_type=creation_type,
        )
        if not routed.startswith("✅"):
            return routed

        if delete_source:
            notion.blocks.delete(block_id=source_block["id"])
            return f"{routed}\n🧹 已从《{page_title}》删除原待整理条目"

        notion.blocks.update(
            block_id=source_block["id"],
            paragraph={
                "rich_text": [{
                    "type": "text",
                    "text": {"content": f"[已分流] {source_text}"[:2000]},
                    "annotations": {"color": "gray"},
                }]
            },
        )
        return f"{routed}\n📝 原待整理条目已标记为已分流"
    except Exception as e:
        print(f"[triage_notion_inbox_entry] ❌ 分流失败: {e}")
        return f"❌ 分流失败: {e}"


def get_notion_structure(category: str = "", max_children: int = 20) -> str:
    notion = get_notion_client()
    if not notion:
        return "❌ 未配置 Notion Token"
    try:
        max_children = max(1, int(max_children))
        if not str(category or "").strip():
            lines = ["🗂️ Notion 顶层分类入口："]
            for key in ("inbox", "love_diary", "timeline", "memory_profile", "memo", "todo", "archive", "creation", "discard"):
                root_id = _resolve_route_root_id(key)
                page, err = _find_page_by_id(notion, root_id)
                title = _page_title(page or {}) if page else key
                suffix = f" | page_id={root_id}" if root_id else ""
                if page:
                    lines.append(f"- {title}{suffix}")
                else:
                    lines.append(f"- {key}（读取失败：{err or '未知错误'}）")
            return "\n".join(lines)

        normalized_category = _normalize_notion_category(category)
        if not normalized_category:
            return "❌ category 无效"

        root_id = _resolve_route_root_id(normalized_category)
        page, err = _find_page_by_id(notion, root_id)
        if not page:
            return err or "🤷 没找到分类入口页"
        root_title = _page_title(page) or normalized_category
        children = _list_child_page_entries(notion, page["id"])
        lines = [f"🗂️ 《{root_title}》当前结构："]
        if not children:
            lines.append("- 暂无子页面")
            return "\n".join(lines)

        shown_children = children[:max_children]
        for child in shown_children:
            lines.append(f"- {child['title']} | page_id={child['id']}")

        if normalized_category in {"love_diary", "timeline", "memo"} and shown_children:
            focus = shown_children[0]
            sub_page, sub_err = _find_page_by_id(notion, focus["id"])
            if sub_page:
                grand_children = _list_child_page_entries(notion, sub_page["id"])[:max_children]
                if grand_children:
                    lines.append(f"\n📂 《{focus['title']}》下的子页面：")
                    for child in grand_children:
                        lines.append(f"- {child['title']} | page_id={child['id']}")
            elif sub_err:
                lines.append(f"\n⚠️ 无法继续读取《{focus['title']}》：{sub_err}")

        remaining = len(children) - len(shown_children)
        if remaining > 0:
            lines.append(f"\n…… 其余还有 {remaining} 个子页面未展开")
        return "\n".join(lines)
    except Exception as e:
        print(f"[get_notion_structure] ❌ 读取失败: {e}")
        return f"❌ 读取失败: {e}"

def find_or_create_parent_page(notion):
    """查找或创建母页面"""
    parent_page, _ = _find_page_by_title(notion, "机器人的时间线")
    if parent_page:
        print(f"[find_parent] ✅ 找到母页面: {_page_title(parent_page)}")
        return parent_page["id"]
    
    # 没找到，创建母页面
    parent_id = os.getenv("ARAN_NOTION_PARENT_ID")
    print("[find_parent] 📝 创建新的母页面...")
    new_page = notion.pages.create(
        parent={"type": "page_id", "page_id": parent_id},
        properties={"title": {"title": [{"text": {"content": "机器人的时间线"}}]}}
    )
    _cache_page(new_page, "机器人的时间线")
    print(f"[find_parent] ✅ 母页面已创建: {new_page['id'][:20]}...")
    return new_page["id"]

def find_or_create_week_page(notion, parent_id, week_title, week_display):
    """查找或创建当周页面"""
    page, err = _find_page_by_title(notion, week_title)
    if page:
        print(f"[find_week] ✅ 找到当周页面: {_page_title(page)}")
        return page["id"]
    
    # 没找到，创建当周页面
    full_title = f"{week_title}（{week_display}）"
    print(f"[find_week] 📝 创建新周页面: {full_title}")
    new_page = notion.pages.create(
        parent={"type": "page_id", "page_id": parent_id},
        properties={"title": {"title": [{"text": {"content": full_title}}]}}
    )
    _cache_page(new_page, week_title, full_title)
    print(f"[find_week] ✅ 周页面已创建: {new_page['id'][:20]}...")
    return new_page["id"]

def write_timeline(summary: str) -> str:
    """写入时间线 - 自动按年月分页"""
    color = "red" if any(k in summary for k in ["重要", "生日", "纪念日", "开心", "难过", "实验成功"]) else "blue"
    return write_structured_notion(
        category="timeline",
        content=summary,
        color=color,
    )

def read_timeline(days: int = 7, limit: int = 30) -> str:
    """读取时间线 - 按月读取，兼容新的年月分页结构"""
    notion = get_notion_client()
    if not notion:
        return "❌ 未配置 Notion Token"

    all_records: list[str] = []
    now = datetime.now(TZ_CN)
    months_needed = max(1, (max(int(days), 1) + 30) // 31)
    cursor = now.replace(day=1)

    for _ in range(months_needed):
        month_title = f"时间线｜{cursor.strftime('%Y-%m')}"
        page, _ = _find_page_by_title(notion, month_title)
        if not page:
            cursor = (cursor - timedelta(days=1)).replace(day=1)
            continue

        month_records = [block["text"] for block in _get_paragraph_blocks(notion, page["id"]) if block["text"]]
        all_records.extend(month_records)
        cursor = (cursor - timedelta(days=1)).replace(day=1)

    if not all_records:
        return f"📖 最近{days}天没记录"

    def get_time(line: str):
        match = re.search(r'【(\d{4}-\d{2}-\d{2} \d{2}:\d{2})】', line)
        return match.group(1) if match else "0000-00-00 00:00"

    all_records.sort(key=get_time, reverse=True)

    seen, final = set(), []
    for record in all_records:
        if record in seen:
            continue
        seen.add(record)
        final.append(record)

    res = f"📖 机器人的时间线（最近{days}天）：\n" + "\n".join(final[:limit])
    if len(final) > limit:
        res += f"\n\n📄 还有 {len(final) - limit} 条..."
    return res
