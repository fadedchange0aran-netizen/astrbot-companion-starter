# MemOS / Memos 全量迁移到 LivingMemory

这份文档面向一种明确诉求：

- 不想继续保留 MemOS / Memos 当旧记忆源
- 想把旧记忆一次性迁到 `LivingMemory`
- 后续只维护 `AstrBot + LivingMemory`

先说结论：

1. 先冻结旧 MemOS 写入
2. 把旧记忆完整导出
3. 转成中间 JSONL 导入包
4. 用本仓库的导入脚本写入 `livingmemory.db`
5. 执行 `/lmem rebuild-index` 和 `/lmem rebuild-graph`

## 迁移前要接受的一件事

这次迁移的目标不是“1:1 保留 MemOS 内部结构和调度细节”，而是：

- 保留旧记忆的文本内容
- 尽量保留时间、来源、标签、会话线索
- 把它们落进 `LivingMemory` 的 `documents` 主表
- 让后续检索、图谱和群聊 / 私聊记忆都统一走 `LivingMemory`

也就是说：

- MemOS 的 memory cube / 调度层 / 内部索引不会原样搬过来
- 但用户可感知的长期记忆内容，是可以整批迁过来的

## 0. 迁移前准备

至少先确认这几件事：

- 旧侧停止继续写入 MemOS
- 旧侧已经做过备份
- 新侧 `LivingMemory` 已经安装并能正常启动
- 你已经确定新的 `owner_id`
- 你已经确定新的 `persona_id`

如果她后续只服务一个人，最重要的是：

- `owner_id` 保持稳定
- 不要今天叫 `alice`，明天又换成 `alice_phone`

## 1. 先备份旧侧和新侧

最少备份下面这些东西：

- MemOS / Memos 原始导出文件
- 如果是自托管 MOS，再备份整个 memory cube 目录
- 新实例当前的 `livingmemory.db`

如果新实例已经跑过几天，建议先把新侧也备份一份，避免导入失败后没法回滚。

## 2. 从 MemOS / Memos 导出旧记忆

### 路线 A：她用的是 MemOS Cloud / API

优先拿到：

- `MEMOS_API_KEY`
- 原来的 `MEMOS_USER_ID`
- 如果有 cube 维度，再拿 `mem_cube_id`

导出目标是“拿到当前用户的完整记忆列表”。

如果她手里只有 MCP 配置，没有后台导出权限，要先补拿：

- Cloud API 权限
- 或者旧应用本身保留的原始导出能力

原因很简单：

- MCP 常用的是 `add_message` / `search_memory`
- 这适合日常读写，不适合做完整 dump

### 路线 B：她用的是自托管 MOS

这时更直接，优先走开源 MOS 自己的导出能力：

- `memory.dump(...)`
- `get_all(...)`
- `register_mem_cube(...)` 对应的 cube 目录

如果已经能直接访问 memory cube 目录，先做一份原样备份，再继续后面的转换。

## 3. 转成中间 JSONL 导入包

本仓库提供的导入脚本不直接吃 MemOS 原始格式，而是吃一个统一的 JSONL：

```text
tools/import_jsonl_to_livingmemory.py
```

每一行一个 JSON 对象，推荐最少包含这些字段：

```json
{
  "doc_id": "memos-user-123-0001",
  "text": "用户长期偏好简短回复，不喜欢很长的解释。",
  "owner_id": "alice",
  "persona_id": "default",
  "session_id": "memos:conversation:0610",
  "source_platform": "memos_cloud",
  "source_session": "0610",
  "importance": 0.8,
  "created_at": "2026-06-10T12:00:00+00:00",
  "updated_at": "2026-06-10T12:00:00+00:00",
  "metadata": {
    "tags": ["preference", "reply_style"],
    "legacy_memory_id": "abc123"
  }
}
```

注意：

- `text` 是真正要迁过去的记忆正文
- `importance` 可以写 `0-1`，也可以写 `0-10`
- `metadata` 里的额外字段会原样保留
- `doc_id` 最好稳定，不要每次转换都变

## 4. 一个通用转换思路

不同 MemOS 部署导出的 JSON 结构不完全一样，所以推荐先做“导出 JSON -> 标准 JSONL”这一步。

下面这个模板适合大多数情况：你只需要把 `items = ...` 那一行改成自己的真实导出结构。

```python
import json
from pathlib import Path


def first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


source = json.loads(Path("memos_export.json").read_text(encoding="utf-8"))

# 按你的真实导出结构改这里：
items = source.get("data", source)
if isinstance(items, dict):
    items = items.get("items") or items.get("memories") or items.get("data") or []

rows = []
for index, item in enumerate(items, start=1):
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    text = first_non_empty(
        item.get("text"),
        item.get("memory"),
        item.get("content"),
        item.get("memory_value"),
        metadata.get("text"),
        metadata.get("memory"),
        metadata.get("content"),
    )
    if not text:
        continue

    rows.append(
        {
            "doc_id": first_non_empty(item.get("id"), item.get("memory_id")) or f"memos-{index}",
            "text": text,
            "session_id": first_non_empty(
                item.get("conversation_id"),
                item.get("session_id"),
                metadata.get("conversation_id"),
            ) or f"memos:import:{index}",
            "source_platform": first_non_empty(
                item.get("source_platform"),
                item.get("platform"),
            ) or "memos_cloud",
            "source_session": first_non_empty(
                item.get("conversation_id"),
                item.get("session_id"),
            ) or f"memos:import:{index}",
            "importance": item.get("importance", metadata.get("importance", 0.65)),
            "created_at": first_non_empty(item.get("created_at"), item.get("timestamp")),
            "updated_at": first_non_empty(item.get("updated_at"), item.get("timestamp")),
            "metadata": {
                "legacy_memory_id": first_non_empty(item.get("id"), item.get("memory_id")),
                "legacy_tags": item.get("tags") or metadata.get("tags") or [],
                "legacy_type": first_non_empty(item.get("type"), metadata.get("type")),
                "migrated_from": "memos_export",
            },
        }
    )

with Path("livingmemory-import.jsonl").open("w", encoding="utf-8") as fp:
    for row in rows:
        fp.write(json.dumps(row, ensure_ascii=False) + "\n")
```

## 5. 如果她是自托管 MOS，可以直接从 `get_all()` 转

如果她能跑 Python 并直接访问自托管 MOS，可以直接把 `get_all()` 结果写成 JSONL：

```python
import json
from pathlib import Path

from memos import MOS
from memos.configs.mem_os import MOSConfig


config = MOSConfig.from_json_file("path/to/mos_config.json")
memory = MOS(config)
items = memory.get_all(mem_cube_id="your_cube_id", user_id="your_user_id")

with Path("livingmemory-import.jsonl").open("w", encoding="utf-8") as fp:
    for index, item in enumerate(items, start=1):
        text = str(getattr(item, "memory", "") or getattr(item, "text", "")).strip()
        if not text:
            continue
        fp.write(
            json.dumps(
                {
                    "doc_id": f"mos-{index}",
                    "text": text,
                    "session_id": f"mos:import:{index}",
                    "source_platform": "mos",
                    "source_session": f"mos:import:{index}",
                    "importance": 0.65,
                    "metadata": {
                        "migrated_from": "mos_get_all",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
```

这段代码是模板，字段名可能要按她自己的 MemOS 版本稍微改一下。

## 6. 先 dry-run 导入到 LivingMemory

导入脚本位置：

```text
tools/import_jsonl_to_livingmemory.py
```

先 dry-run：

```bash
python3 tools/import_jsonl_to_livingmemory.py \
  --source /path/to/livingmemory-import.jsonl \
  --livingmemory-db /path/to/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db \
  --owner-id alice \
  --persona-id default \
  --report-path /path/to/livingmemory-import-report.json \
  --dry-run
```

先看这几个结果：

- `parsed_rows` 是否接近预期
- `error_count` 是否为 0
- `errors_preview` 里有没有大量空文本或字段缺失

## 7. 确认数量后正式导入

```bash
python3 tools/import_jsonl_to_livingmemory.py \
  --source /path/to/livingmemory-import.jsonl \
  --livingmemory-db /path/to/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db \
  --owner-id alice \
  --persona-id default \
  --report-path /path/to/livingmemory-import-report.json
```

如果你打算重复执行同一份导入包，并且希望相同 `doc_id` 直接覆盖，带上：

```bash
--upsert-doc-id
```

## 8. 导入后必须重建索引

这一步不能省。

导入脚本只是把文本和 metadata 写进 `LivingMemory` 的 `documents` 表，后续检索和图谱还要重建。

在 AstrBot 中执行：

```text
/lmem rebuild-index
/lmem rebuild-graph
```

## 9. 导入后怎么验收

至少做这几步：

1. `/lmem status`
2. `/lmem search 她确定旧库里存在的关键词`
3. 打开 `LivingMemory` WebUI 看记忆总数是否明显增长
4. 让机器人围绕旧偏好 / 旧人物关系自然聊几轮
5. 观察是否能从旧记忆中稳定召回

## 10. 迁移后建议立刻做的清理

全量迁过来以后，通常还要做一轮轻整理：

- 删除明显重复条目
- 把极长导入文本拆短
- 给超泛化的“总结型大记忆”降重要性

这是正常现象，不代表迁移失败。

因为：

- MemOS 和 `LivingMemory` 的记忆粒度本来就不完全一致
- 同一段旧记忆在新系统里，可能更适合拆成多条

## 11. 如果原来就是 LivingMemory

如果她原来的旧记忆已经是 `LivingMemory` 数据库，不要走这份文档，直接看：

- [旧记忆导入](legacy-memory-import.md)

## 外部参考

- [MemOS MCP Usage Guide](https://memos-docs.openmem.net/cn/mcp_agent/mcp/guide/)
- [MemOS Cloud Quick Start](https://memos-docs.openmem.net/dashboard/quick_start/)
- [MOS API Overview](https://memos-docs.openmem.net/open_source/modules/mos/overview/)
