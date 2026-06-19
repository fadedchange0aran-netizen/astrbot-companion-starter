# 链接解析与卡片解析

这套 starter 里的“链接体验”不是靠单个插件完成的，而是三层配合：

1. `astrbot_plugin_continuous_message`
2. `astrbot_plugin_media_parser`
3. `astrbot_plugin_link_context`

## 三层分别做什么

### `continuous_message`

主要负责：

- 连续输入整合
- QQ Json 卡片提链
- 前置链接增强

它解决的是“平台发过来的原始消息不够好读”这个问题。

### `media_parser`

主要负责：

- 识别 B 站、小红书等链接
- 解析标题、作者、摘要、热评
- 处理短链和 QQ 小程序卡片里的真实链接

它更像底层解析器。

### `link_context`

主要负责：

- 提供 `read_link_context` 工具
- 把解析结果变成更适合 LLM 继续聊的文本上下文
- 记住最近一次成功工具结果
- 在白名单群里看到链接时自动唤醒

它解决的是“机器人能不能顺着链接内容继续聊天”。

## 推荐配置

### `continuous_message`

建议开启：

- `enable_qq_card_parsing = true`
- `enable_link_parsing = true`
- `link_parser_success_prompt = "[链接解析]"`

如果你不想把 B 站 / 小红书的 QQ 壳链接直接写进正文，可用：

```text
qq_card_disabled_platforms = ["bilibili", "xhs"]
```

### `media_parser`

如果你只是想让机器人“读懂链接并继续聊”，建议：

- `trigger.auto_parse = false`
- `trigger.reply_trigger = false`
- B 站设为 `仅文本`
- 小红书设为 `仅文本`

这样能减少重复触发，也更省 token。

### `link_context`

建议开启：

- `auto_parse_links_on_request = true`
- `inject_recent_tool_context = true`

## 群聊自动接话怎么开

`link_context` 的群聊自动唤醒不是全局开的，而是看 AstrBot 的主动回复白名单。

如果你希望某个群里发 B 站 / 小红书链接就能自动接话，需要把这个群加入主动回复白名单。

## 推荐验证顺序

1. 私聊发一条 B 站链接
2. 私聊发一条小红书链接
3. 群里发一张 B 站 / 小红书 QQ 卡片
4. 观察机器人是只会复读链接，还是能直接聊内容

如果能直接顺着标题、摘要、热评接话，这条链就算跑通了。

## 常见问题

### 机器人完全不理链接

优先检查：

- `link_context` 是否加载成功
- `auto_parse_links_on_request` 是否开启
- `media_parser` 是否可用

### QQ 卡片没提取出真实链接

优先检查：

- `continuous_message.enable_qq_card_parsing`
- `qq_card_disabled_platforms`

### 群里要 @ 才回复

这不一定是解析链坏了，也可能是该群不在 AstrBot 主动回复白名单里。
