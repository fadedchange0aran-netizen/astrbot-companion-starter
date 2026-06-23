"""
环境感知注入插件
面向通用陪伴型机器人的环境感知注入插件。
"""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register, StarTools

try:
    import chinese_calendar as chinese_cal
except Exception:
    chinese_cal = None

WEEKDAY_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
DEFAULT_OWNER_ID = 'owner'
DEFAULT_SUMMARY_LIMIT = 2
ACTIVITY_LOG_FILE = 'activity_log.jsonl'
PLATFORM_SESSION_PREFIX = 'pf::'
DEFAULT_NOTICE_OFFSETS = [0, 1, 7]
DEFAULT_ANNIVERSARY_EVENTS = []
DEFAULT_MILESTONE_RULES = []
DATE_ONLY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MONTH_DAY_RE = re.compile(r'^\d{2}-\d{2}$')
DEFAULT_CYCLE_LENGTH_DAYS = 35
DEFAULT_CYCLE_CHECKIN_COOLDOWN_DAYS = 3
CYCLE_STATE_FILE = 'cycle_tracking_state.json'
DEFAULT_STACKCHAN_LIGHT_MODE_ENABLED = False
DEFAULT_STACKCHAN_DISABLE_TOOLS = True


def get_period_label(hour: int) -> str:
    """根据小时返回时段标签。"""
    if 5 <= hour < 11:
        return '上午'
    if 11 <= hour < 14:
        return '中午'
    if 14 <= hour < 18:
        return '下午'
    if 18 <= hour < 23:
        return '晚上'
    return '深夜'


def _normalize_string_list(value) -> list[str]:
    if isinstance(value, str):
        candidates = value.replace('\n', ',').split(',')
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    normalized: list[str] = []
    for item in candidates:
        text = str(item or '').strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_int_list(value, default: list[int]) -> list[int]:
    items = value if isinstance(value, list) else default
    normalized: list[int] = []
    for item in items:
        try:
            normalized.append(int(item))
        except (TypeError, ValueError):
            continue
    return sorted({item for item in normalized if item >= 0}) or list(default)


def _parse_event_date(value: str) -> tuple[int | None, int, int] | None:
    raw = str(value or '').strip()
    if DATE_ONLY_RE.fullmatch(raw):
        parsed = date.fromisoformat(raw)
        return parsed.year, parsed.month, parsed.day
    if MONTH_DAY_RE.fullmatch(raw):
        month = int(raw[:2])
        day = int(raw[3:])
        date(2000, month, day)
        return None, month, day
    return None


def _notice_label(days_before: int) -> str:
    if days_before <= 0:
        return '今天'
    if days_before == 1:
        return '明天'
    return f'{days_before} 天后'


def _parse_iso_date(value: str) -> date | None:
    raw = str(value or '').strip()
    if not DATE_ONLY_RE.fullmatch(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _render_label_template(template: str, value: int, fallback: str) -> str:
    text = str(template or '').strip()
    if not text:
        return fallback
    try:
        rendered = text.format(value=value)
    except Exception:
        return fallback
    return rendered.strip() or fallback


@register(
    'astrbot_plugin_llmperception',
    'Companion Starter Maintainers',
    '日期/生理期/StackChan 配置',
    '1.0.0',
    '',
)
class LLMPerceptionPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        if isinstance(self.config, dict):
            self.timezone = self.config.get('timezone', 'Asia/Shanghai')
        else:
            self.timezone = 'Asia/Shanghai'
        self.enable_prompt_injection = self._get_config('enable_prompt_injection', True)
        self.owner_id = self._get_config('owner_id', DEFAULT_OWNER_ID)
        self.use_sender_id_as_owner = bool(self._get_config('use_sender_id_as_owner', False))
        self.include_cross_platform_summary = bool(
            self._get_config('include_cross_platform_summary', True)
        )
        self.enable_anniversary_perception = bool(
            self._get_config('enable_anniversary_perception', True)
        )
        self.enable_cycle_perception = bool(self._get_config('enable_cycle_perception', True))
        self.cross_platform_limit = int(
            self._get_config('cross_platform_limit', DEFAULT_SUMMARY_LIMIT)
        )
        configured_trusted_ids = _normalize_string_list(self._get_config('trusted_sender_ids', []))
        self.trusted_sender_ids = sorted({self.owner_id, *configured_trusted_ids})
        self.anniversary_notice_offsets = _normalize_int_list(
            self._get_config('anniversary_notice_offsets', DEFAULT_NOTICE_OFFSETS),
            DEFAULT_NOTICE_OFFSETS,
        )
        self.anniversary_events = self._normalize_anniversary_events(
            self._get_config('anniversary_events', DEFAULT_ANNIVERSARY_EVENTS)
        )
        self.milestone_rules = self._normalize_milestone_rules(
            self._get_config('milestone_rules', DEFAULT_MILESTONE_RULES)
        )
        self.last_period_start_date = str(self._get_config('last_period_start_date', '') or '').strip()
        self.cycle_is_private = bool(self._get_config('cycle_is_private', True))
        try:
            self.cycle_length_days = max(
                1, int(self._get_config('cycle_length_days', DEFAULT_CYCLE_LENGTH_DAYS))
            )
        except (TypeError, ValueError):
            self.cycle_length_days = DEFAULT_CYCLE_LENGTH_DAYS
        try:
            self.cycle_checkin_cooldown_days = max(
                1,
                int(
                    self._get_config(
                        'cycle_checkin_cooldown_days',
                        DEFAULT_CYCLE_CHECKIN_COOLDOWN_DAYS,
                    )
                ),
            )
        except (TypeError, ValueError):
            self.cycle_checkin_cooldown_days = DEFAULT_CYCLE_CHECKIN_COOLDOWN_DAYS
        self.enable_stackchan_light_mode = bool(
            self._get_config('enable_stackchan_light_mode', DEFAULT_STACKCHAN_LIGHT_MODE_ENABLED)
        )
        self.stackchan_selected_provider = str(
            self._get_config('stackchan_selected_provider', '') or ''
        ).strip()
        self.stackchan_selected_model = str(
            self._get_config('stackchan_selected_model', '') or ''
        ).strip()
        self.stackchan_disable_tools = bool(
            self._get_config('stackchan_disable_tools', DEFAULT_STACKCHAN_DISABLE_TOOLS)
        )
        self.shared_root = self._get_shared_root()
        self.plugin_data_dir = StarTools.get_data_dir('astrbot_plugin_llmperception')
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.cycle_state_path = self.plugin_data_dir / CYCLE_STATE_FILE

    def _get_config(self, key, default):
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return default

    def _get_shared_root(self) -> Path:
        configured = self._get_config('shared_memory_root', '')
        if configured:
            root = Path(str(configured))
            root.mkdir(parents=True, exist_ok=True)
            return root
        return StarTools.get_data_dir('astrbot_plugin_memory')

    def resolve_owner_id(self, event: AstrMessageEvent) -> str:
        if self.use_sender_id_as_owner:
            return str(event.get_sender_id() or DEFAULT_OWNER_ID)
        return str(self.owner_id)

    def _normalize_anniversary_events(self, value) -> list[dict]:
        source = value if isinstance(value, list) else DEFAULT_ANNIVERSARY_EVENTS
        normalized: list[dict] = []
        for item in source:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            event_date = str(item.get('event_date') or '').strip()
            if not name or not event_date:
                continue
            if _parse_event_date(event_date) is None:
                logger.warning(f"[llmperception] 跳过无效纪念日日期配置: {name}={event_date}")
                continue
            kind = str(item.get('kind') or 'anniversary').strip() or 'anniversary'
            recurrence = str(item.get('recurrence') or '').strip().lower()
            if recurrence not in {'yearly', 'absolute'}:
                recurrence = 'yearly' if kind in {'birthday', 'anniversary', 'awakening_day'} else 'absolute'
            normalized.append(
                {
                    'name': name,
                    'kind': kind,
                    'event_date': event_date,
                    'recurrence': recurrence,
                    'is_private': bool(item.get('is_private', True)),
                }
            )
        return normalized

    def _normalize_milestone_rules(self, value) -> list[dict]:
        source = value if isinstance(value, list) and value else DEFAULT_MILESTONE_RULES
        normalized: list[dict] = []
        for item in source:
            if not isinstance(item, dict):
                continue
            anchor_date = str(item.get('anchor_date') or '').strip()
            if not anchor_date:
                continue
            parsed = _parse_event_date(anchor_date)
            if parsed is None or parsed[0] is None:
                logger.warning(f"[llmperception] 跳过无效里程碑 anchor_date: {anchor_date}")
                continue
            rule_type = str(
                item.get('rule_type')
                or ('fixed_day_offset' if item.get('days_offset') is not None else '')
            ).strip().lower()
            is_private = bool(item.get('is_private', True))

            if rule_type in {'', 'fixed', 'fixed_day_offset'}:
                try:
                    days_offset = int(item.get('days_offset'))
                except (TypeError, ValueError):
                    continue
                if days_offset < 0:
                    continue
                normalized.append(
                    {
                        'rule_type': 'fixed_day_offset',
                        'label': str(item.get('label') or '').strip() or f'{days_offset} 天',
                        'anchor_date': anchor_date,
                        'days_offset': days_offset,
                        'is_private': is_private,
                    }
                )
                continue

            if rule_type == 'every_n_days':
                try:
                    interval_days = int(item.get('interval_days') or item.get('interval'))
                except (TypeError, ValueError):
                    continue
                try:
                    start_days = int(item.get('start_days', interval_days))
                except (TypeError, ValueError):
                    start_days = interval_days
                if interval_days <= 0 or start_days <= 0:
                    continue
                normalized.append(
                    {
                        'rule_type': 'every_n_days',
                        'anchor_date': anchor_date,
                        'interval_days': interval_days,
                        'start_days': start_days,
                        'label_template': str(item.get('label_template') or '{value} 天').strip(),
                        'is_private': is_private,
                    }
                )
                continue

            if rule_type == 'every_n_years':
                try:
                    interval_years = int(item.get('interval_years') or item.get('interval'))
                except (TypeError, ValueError):
                    continue
                try:
                    start_years = int(item.get('start_years', interval_years))
                except (TypeError, ValueError):
                    start_years = interval_years
                if interval_years <= 0 or start_years <= 0:
                    continue
                normalized.append(
                    {
                        'rule_type': 'every_n_years',
                        'anchor_date': anchor_date,
                        'interval_years': interval_years,
                        'start_years': start_years,
                        'label_template': str(item.get('label_template') or '{value} 周年').strip(),
                        'is_private': is_private,
                    }
                )
                continue

            logger.warning(f"[llmperception] 跳过未知里程碑规则类型: {rule_type}")
        return normalized

    def _resolve_milestone_label(self, rule: dict, target_date: date) -> str:
        anchor_date = date.fromisoformat(str(rule['anchor_date']))
        if target_date < anchor_date:
            return ''

        rule_type = str(rule.get('rule_type') or 'fixed_day_offset')
        if rule_type == 'fixed_day_offset':
            days_offset = int(rule.get('days_offset', 0))
            milestone_date = anchor_date + timedelta(days=days_offset)
            if milestone_date != target_date:
                return ''
            return str(rule.get('label') or f'{days_offset} 天').strip()

        if rule_type == 'every_n_days':
            interval_days = int(rule.get('interval_days', 0))
            start_days = int(rule.get('start_days', interval_days))
            days_since = (target_date - anchor_date).days
            if interval_days <= 0 or days_since < start_days or days_since % interval_days != 0:
                return ''
            return _render_label_template(
                str(rule.get('label_template') or '{value} 天'),
                days_since,
                f'{days_since} 天',
            )

        if rule_type == 'every_n_years':
            interval_years = int(rule.get('interval_years', 0))
            start_years = int(rule.get('start_years', interval_years))
            years_since = target_date.year - anchor_date.year
            if (
                interval_years <= 0
                or years_since < start_years
                or years_since % interval_years != 0
                or (target_date.month, target_date.day) != (anchor_date.month, anchor_date.day)
            ):
                return ''
            return _render_label_template(
                str(rule.get('label_template') or '{value} 周年'),
                years_since,
                f'{years_since} 周年',
            )

        return ''

    def _load_cycle_state(self) -> dict:
        if not self.cycle_state_path.exists():
            return {}
        try:
            with open(self.cycle_state_path, 'r', encoding='utf-8') as file_obj:
                payload = json.load(file_obj)
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            logger.warning(f"[llmperception] 读取生理期状态失败: {exc}")
            return {}

    def _save_cycle_state(self, payload: dict) -> None:
        try:
            with open(self.cycle_state_path, 'w', encoding='utf-8') as file_obj:
                json.dump(payload, file_obj, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"[llmperception] 保存生理期状态失败: {exc}")

    def _should_emit_cycle_checkin(self, period_start: date, today: date) -> bool:
        state = self._load_cycle_state()
        state_anchor = str(state.get('last_period_start_date') or '').strip()
        if state_anchor != period_start.isoformat():
            return True
        last_prompt_date = _parse_iso_date(str(state.get('last_prompt_date') or ''))
        if last_prompt_date is None:
            return True
        if today < last_prompt_date:
            return False
        return (today - last_prompt_date).days >= self.cycle_checkin_cooldown_days

    def _mark_cycle_checkin_emitted(self, period_start: date, today: date) -> None:
        self._save_cycle_state(
            {
                'last_period_start_date': period_start.isoformat(),
                'last_prompt_date': today.isoformat(),
            }
        )

    def _is_trusted_sender(self, event: AstrMessageEvent) -> bool:
        sender_id = str(event.get_sender_id() or '').strip()
        if not sender_id:
            return False
        return sender_id in self.trusted_sender_ids

    def _build_holiday_context(self, today: date) -> str:
        if chinese_cal:
            try:
                holiday_name = ''
                if hasattr(chinese_cal, 'get_holiday_detail'):
                    detail = chinese_cal.get_holiday_detail(today)
                    if isinstance(detail, tuple) and len(detail) >= 2:
                        holiday_name = str(detail[1] or '').strip()
                if chinese_cal.is_holiday(today):
                    return (
                        f'今天是法定节假日/休息日（{holiday_name}）。'
                        if holiday_name
                        else '今天是法定节假日/休息日。'
                    )
                if chinese_cal.is_workday(today):
                    return '今天是工作日。'
                return '今天是休息日。'
            except Exception as exc:
                logger.warning(f"[llmperception] 节假日判断失败: {exc}")
        return '今天是周末休息日。' if today.weekday() >= 5 else '今天是工作日。'

    def _event_occurs_on(self, event_item: dict, target_date: date) -> bool:
        parsed = _parse_event_date(str(event_item.get('event_date') or ''))
        if parsed is None:
            return False
        year, month, day = parsed
        recurrence = str(event_item.get('recurrence') or 'yearly').strip().lower()
        try:
            if recurrence == 'absolute':
                if year is None:
                    return False
                return date(year, month, day) == target_date
            return date(target_date.year, month, day) == target_date
        except ValueError:
            return False

    def _build_anniversary_lines(self, event: AstrMessageEvent, today: date) -> list[str]:
        if not self.enable_anniversary_perception:
            return []

        trusted_sender = self._is_trusted_sender(event)
        lines: list[str] = []
        for event_item in self.anniversary_events:
            if event_item.get('is_private', True) and not trusted_sender:
                continue
            for days_before in self.anniversary_notice_offsets:
                target_date = today + timedelta(days=days_before)
                if not self._event_occurs_on(event_item, target_date):
                    continue
                lines.append(f"{_notice_label(days_before)}是 {event_item['name']}。")
                break

        if trusted_sender:
            for rule in self.milestone_rules:
                if rule.get('is_private', True) and not trusted_sender:
                    continue
                for days_before in self.anniversary_notice_offsets:
                    label = self._resolve_milestone_label(rule, today + timedelta(days=days_before))
                    if not label:
                        continue
                    lines.append(f"{_notice_label(days_before)}会到 {label} 里程碑。")
                    break
        return lines

    def _build_cycle_lines(self, event: AstrMessageEvent, today: date) -> list[str]:
        if not self.enable_cycle_perception:
            return []

        trusted_sender = self._is_trusted_sender(event)
        if self.cycle_is_private and not trusted_sender:
            return []

        period_start = _parse_iso_date(self.last_period_start_date)
        if period_start is None or today < period_start:
            return []

        days_since_start = (today - period_start).days
        if days_since_start < self.cycle_length_days:
            return []
        if not self._should_emit_cycle_checkin(period_start, today):
            return []

        overdue_days = days_since_start - self.cycle_length_days
        if overdue_days <= 0:
            line = (
                f'距离上次生理期开始已经 {days_since_start} 天，已到你设定的 '
                f'{self.cycle_length_days} 天节点；如果聊天气氛合适，可以轻一点关心她这次是不是快来了。'
            )
        else:
            line = (
                f'距离上次生理期开始已经 {days_since_start} 天，比设定的 '
                f'{self.cycle_length_days} 天周期多了 {overdue_days} 天；'
                '如果她还没在前端更新开始日，可以轻一点问问这次是不是还没来。'
            )
        self._mark_cycle_checkin_emitted(period_start, today)
        return [f'{line} 不要像系统提醒，也不要连续追问。']

    def _extract_event_dict_value(self, event: AstrMessageEvent, attr_names, key_names) -> str:
        for attr_name in attr_names:
            container = getattr(event, attr_name, None)
            if isinstance(container, dict):
                for key_name in key_names:
                    value = container.get(key_name)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return ''

    def _parse_platform_session_id(self, session_id: str) -> tuple[str, str]:
        raw_value = str(session_id or '').strip()
        if not raw_value.startswith(PLATFORM_SESSION_PREFIX):
            return '', raw_value
        remainder = raw_value[len(PLATFORM_SESSION_PREFIX) :]
        platform, separator, original_session_id = remainder.partition('::')
        if not separator or not platform.strip() or not original_session_id.strip():
            return '', raw_value
        return platform.strip(), original_session_id.strip()

    def _extract_raw_session_value(self, event: AstrMessageEvent) -> str:
        for attr in ('session_id', 'conversation_id', 'session', 'session_name'):
            value = getattr(event, attr, None)
            if value:
                return str(value)
        return self._extract_event_dict_value(
            event,
            ('metadata', 'extra', 'headers', 'message_obj', 'raw_message'),
            ('original_session_id', 'session_id', 'conversation_id'),
        )

    def get_platform_info(self, event: AstrMessageEvent) -> str:
        for attr in (
            'unified_msg_origin',
            'platform_meta',
            'platform_name',
            'client_platform',
            'platform',
            'source',
        ):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

        nested_value = self._extract_event_dict_value(
            event,
            ('metadata', 'extra', 'headers', 'message_obj', 'raw_message'),
            ('platform', 'client_platform', 'source', 'x-platform', 'x-client-platform'),
        )
        if nested_value:
            return nested_value

        platform, _ = self._parse_platform_session_id(self._extract_raw_session_value(event))
        if platform:
            return platform
        return '未知平台'

    def get_session_info(self, event: AstrMessageEvent) -> str:
        nested_value = self._extract_raw_session_value(event)
        if nested_value:
            _, original_session_id = self._parse_platform_session_id(nested_value)
            return original_session_id
        return str(event.get_sender_id() or DEFAULT_OWNER_ID)

    def _is_stackchan_platform(self, event: AstrMessageEvent) -> bool:
        platform_value = self.get_platform_info(event).strip().lower()
        if not platform_value:
            return False
        if platform_value == 'stackchan':
            return True
        if 'stackchan' in platform_value:
            return True
        if platform_value.startswith(f'{PLATFORM_SESSION_PREFIX}stackchan::'):
            return True
        return False

    def _apply_stackchan_request_policy(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.enable_stackchan_light_mode:
            return
        if not self._is_stackchan_platform(event):
            return
        if self.stackchan_selected_model:
            req.model = self.stackchan_selected_model
        if self.stackchan_disable_tools:
            req.func_tool = None

    def get_recent_cross_platform_items(self, owner_id: str, current_session: str, current_platform: str):
        owner_dir = self.shared_root / owner_id
        log_path = owner_dir / ACTIVITY_LOG_FILE
        if not log_path.exists():
            return []

        try:
            with open(log_path, 'r', encoding='utf-8') as file_obj:
                lines = file_obj.readlines()
        except Exception as exc:
            logger.warning(f"[llmperception] 读取跨平台活动日志失败: {exc}")
            return []

        items = []
        for raw_line in reversed(lines):
            try:
                row = json.loads(raw_line)
            except Exception:
                continue
            session_id = str(row.get('session_id') or '')
            platform = str(row.get('platform') or '')
            text = str(row.get('text') or '').strip()
            if not text:
                continue
            if session_id == current_session:
                continue
            if platform == current_platform and session_id:
                continue
            items.append(
                {
                    'timestamp': row.get('timestamp', ''),
                    'platform': platform or '未知平台',
                    'text': text,
                }
            )
            if len(items) >= self.cross_platform_limit:
                break
        items.reverse()
        return items

    @filter.on_llm_request()
    async def inject_time_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 请求前注入时间、日期、平台等上下文信息。"""
        self._apply_stackchan_request_policy(event, req)
        if not bool(self.enable_prompt_injection):
            return
        try:
            timezone_obj = ZoneInfo(self.timezone)
        except Exception:
            timezone_obj = ZoneInfo('Asia/Shanghai')

        now = datetime.now(timezone_obj)
        context_parts = [
            f'发送时间: {now.strftime("%Y-%m-%d %H:%M:%S")}',
            WEEKDAY_NAMES[now.weekday()],
            get_period_label(now.hour),
        ]

        # 是否感知节假日
        holiday_enabled = isinstance(self.config, dict) and self.config.get(
            'enable_holiday_perception', True
        )

        # 是否感知平台
        platform_enabled = not isinstance(self.config, dict) or self.config.get(
            'enable_platform_perception', True
        )
        if platform_enabled:
            platform_info = self.get_platform_info(event)
            context_parts.append(f"平台: {platform_info}")

        prefix = f"[{' | '.join(context_parts)}]\n"
        owner_id = self.resolve_owner_id(event)
        current_platform = self.get_platform_info(event)
        current_session = self.get_session_info(event)
        anniversary_lines = self._build_anniversary_lines(event, now.date())
        cycle_lines = self._build_cycle_lines(event, now.date())
        calendar_block = ''
        if holiday_enabled or anniversary_lines or cycle_lines:
            lines = [
                '以下是系统先计算好的日期语境，只用于你自然接住话题，不要把它说成系统通知。',
            ]
            if holiday_enabled:
                lines.append(f"- {self._build_holiday_context(now.date())}")
            for item in anniversary_lines:
                lines.append(f"- {item}")
            for item in cycle_lines:
                lines.append(f"- {item}")
            calendar_block = '[日期语境提示]\n' + '\n'.join(lines) + '\n'
        continuity_block = ''
        if self.include_cross_platform_summary:
            recent_items = self.get_recent_cross_platform_items(
                owner_id,
                current_session,
                current_platform,
            )
            if recent_items:
                lines = [
                    '你与用户始终处于同一段连续关系中，平台切换只代表前台切换，不代表人格切换。',
                    f'当前前台: {current_platform}',
                    '最近跨平台摘要:',
                ]
                for item in recent_items:
                    lines.append(f"- [{item['platform']}] {item['text']}")
                continuity_block = '[跨平台连续性提示]\n' + '\n'.join(lines) + '\n'
        req.prompt = f'{prefix}{calendar_block}{continuity_block}{req.prompt or ""}'
