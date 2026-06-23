# 快速开始

这份 starter 默认面向下面的组合：

- AstrBot 本体
- `LivingMemory`
- `astrbot_plugin_link_context`
- `astrbot_plugin_media_parser`
- `astrbot_plugin_continuous_message`
- `astrbot_plugin_file_delivery`（可选）
- `astrbot_plugin_llmperception`（可选）
- `astrbot_plugin_bookshelf`（可选）
- `astrbot_plugin_ebooks`（可选）
- `adapter`
- `bridges/stackchan`（可选）
- `services/aranbox-mcp`（可选）

## 1. 安装 LivingMemory

请单独拉取：

- `https://github.com/fadedchange0aran-netizen/astrbot_plugin_livingmemory`

把插件目录放到 AstrBot 的 `data/plugins/` 下，然后在插件配置页完成基础配置：

- `provider_settings.embedding_provider_id`
- `provider_settings.llm_provider_id`

更多配置见：

- [记忆配置](memory-setup.md)
- [旧记忆导入](legacy-memory-import.md)
- [MemOS / Memos 全量迁移](memos-migration.md)

## 2. 安装链接体验插件

把下面三个目录放到 AstrBot 的 `data/plugins/` 下：

- `plugins/astrbot_plugin_link_context`
- `plugins/third_party_patches/astrbot_plugin_media_parser`
- `plugins/third_party_patches/astrbot_plugin_continuous_message`

重启或重载 AstrBot 后，在插件页面确认这三个插件都已加载。

如果你的 AstrBot 真源和运行目录分离，建议始终遵守这个口径：

- Git 真源里保留插件源码
- 运行时只放同步后的副本
- 不要直接在运行目录里长期手改插件

## 3. 推荐先开哪些配置

### `astrbot_plugin_link_context`

建议保持：

- `auto_parse_links_on_request = true`
- `inject_recent_tool_context = true`

如果你希望白名单群里发 B 站 / 小红书链接时，机器人能像私聊一样自动接话：

- 需要把对应群加入 AstrBot 主动回复白名单
- 同时保证 `link_context` 和链接解析底层插件都已加载

### `astrbot_plugin_continuous_message`

建议保持：

- `enable_qq_card_parsing = true`
- `enable_link_parsing = true`
- `link_parser_success_prompt = "[链接解析]"`

如果你不希望 QQ 壳链接正文直接出现在对话里，可以按需屏蔽：

- `qq_card_disabled_platforms = ["bilibili", "xhs"]`

### `astrbot_plugin_media_parser`

如果你的目标是“让机器人看懂链接继续聊”，而不是直接回媒体，建议：

- `trigger.auto_parse = false`
- `trigger.reply_trigger = false`
- B 站输出模式设为 `仅文本`
- 小红书输出模式设为 `仅文本`

这样更适合作为 `link_context` 的底层解析器。

如果你遇到的是“机器人直接把小红书 / B 站解析结果贴出来，而不是自然接话”，优先不要怀疑仓库缺代码，先看详细说明：

- [链接解析与卡片解析](link-parsing.md)

最常见原因是 `media_parser` 仍在走自动回复式触发，而不是把解析结果交给 `link_context` 当聊天上下文来用。

## 4. 启用 adapter

进入 `adapter/` 目录：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

如果你要接 AstrBot 官方 HTTP API，重点填写：

- `ARAN_ADAPTER_BACKEND_TYPE=astrbot_http`
- `ARAN_ASTRBOT_TARGET_URL=http://127.0.0.1:6185/api/v1/chat`
- `ARAN_ASTRBOT_API_KEY=<你的 AstrBot API Key>`

启动：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

## 5. 可选启用 StackChan / MCP

如果你还准备启用实用插件：

- `plugins/astrbot_plugin_file_delivery`
  - 适合把 `file_vault` 里的受管文件直接回传给当前会话或目标会话
  - 最推荐给 AstrBot 和 NapCat 挂同一个共享目录，再把 `flash_transfer_dir` 指到这个共享目录
- `plugins/astrbot_plugin_llmperception`
  - 适合给机器人补日期语境、生理期手填提醒和 StackChan 轻量模式配置
- `plugins/third_party_patches/astrbot_plugin_bookshelf`
  - 适合做共读、章节阅读和目标会话续聊
- `plugins/third_party_patches/astrbot_plugin_ebooks`
  - 适合多源电子书搜索下载，并把下载结果转存到 `file_vault`

如果你准备启用 `file_delivery`，优先看：

- `plugins/astrbot_plugin_file_delivery/README.md`

其中最关键的是：

1. `file_vault_root`
   - 通常写 AstrBot 容器内的 `/AstrBot/data/file_vault`
2. `flash_transfer_dir`
   - 最推荐写成 AstrBot 和 NapCat 都能看到的共享目录，例如都挂到 `/tmp/astrbot_flash`
3. `allowed_sender_ids`
   - 只填你自己的 `sender_id`

如果你还准备接 `StackChan`：

- 查看 `bridges/stackchan/README.md`
- 按 `.env.example` 填好 `STACKCHAN_*`
- 再单独启动桥接服务

如果你还准备启用 MCP 工具服务：

- 查看 `services/aranbox-mcp/README.md`
- 默认建议只启用 `daily + extended`
- 暂时不要急着打开 `admin` 层和 `safe deploy`

## 6. 验证体验链路

建议依次验证：

1. 私聊里普通问答是否正常
2. RikkiHub / OpenAI 前端是否能正常调用 `adapter`
3. 发送一条 B 站链接，机器人是否能接着聊内容
4. 发送一条小红书链接，机器人是否能接着聊内容
5. 发送 QQ 卡片，看是否能成功提链

如果上面五步都通过，这套最小体验包就已经基本成型了。

## 7. 思考链显示建议

如果你同时在用 QQ 和 `adapter` / RikkiHub，建议：

- AstrBot 侧保持 `display_reasoning_text = false`
- 只在 `adapter` 侧决定是否向前端暴露 reasoning

原因是 AstrBot 这个开关会把思考链直接拼进普通消息链里，容易导致：

- QQ 里把思考链夹进正文
- `adapter` / RikkiHub 同时看到正文内 reasoning 和独立 reasoning 字段
