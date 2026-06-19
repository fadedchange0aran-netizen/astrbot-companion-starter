# 快速开始

这份 starter 默认面向下面的组合：

- AstrBot 本体
- `LivingMemory`
- `astrbot_plugin_link_context`
- `astrbot_plugin_media_parser`
- `astrbot_plugin_continuous_message`
- `adapter`

## 1. 安装 LivingMemory

请单独拉取：

- `https://github.com/fadedchange0aran-netizen/astrbot_plugin_livingmemory`

把插件目录放到 AstrBot 的 `data/plugins/` 下，然后在插件配置页完成基础配置：

- `provider_settings.embedding_provider_id`
- `provider_settings.llm_provider_id`

更多配置见：

- [记忆配置](memory-setup.md)
- [旧记忆导入](legacy-memory-import.md)

## 2. 安装链接体验插件

把下面三个目录放到 AstrBot 的 `data/plugins/` 下：

- `plugins/astrbot_plugin_link_context`
- `plugins/third_party_patches/astrbot_plugin_media_parser`
- `plugins/third_party_patches/astrbot_plugin_continuous_message`

重启或重载 AstrBot 后，在插件页面确认这三个插件都已加载。

## 3. 推荐先开哪些配置

### `astrbot_plugin_link_context`

建议保持：

- `auto_parse_links_on_request = true`
- `inject_recent_tool_context = true`

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

## 5. 验证体验链路

建议依次验证：

1. 私聊里普通问答是否正常
2. RikkiHub / OpenAI 前端是否能正常调用 `adapter`
3. 发送一条 B 站链接，机器人是否能接着聊内容
4. 发送一条小红书链接，机器人是否能接着聊内容
5. 发送 QQ 卡片，看是否能成功提链

如果上面五步都通过，这套最小体验包就已经基本成型了。*** End Patch
