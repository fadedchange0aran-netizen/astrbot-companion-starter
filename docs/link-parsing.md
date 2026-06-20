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

先区分两种目标：

1. 想要机器人把链接当聊天上下文，看完后自然接话
2. 想要机器人像解析机器人一样，直接把标题、摘要、热评回出来

这两种都能工作，但配置方向不一样。

如果你更想要阿然现在这种“看懂链接再继续聊”的体验，下面这套推荐值优先级最高。

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

## 两种模式怎么选

### 模式 A：自然接话

这是本 starter 默认更推荐的模式。

特征是：

- 机器人不会先机械贴一大段解析结果
- 它更像先读过链接，再顺着内容继续说话
- 更适合陪伴、聊天、追问、延续上下文

推荐组合：

- `continuous_message.enable_qq_card_parsing = true`
- `continuous_message.enable_link_parsing = true`
- `media_parser.trigger.auto_parse = false`
- `media_parser.trigger.reply_trigger = false`
- `media_parser` 里的 B 站 / 小红书输出模式设为 `仅文本`
- `link_context.auto_parse_links_on_request = true`
- `link_context.inject_recent_tool_context = true`

### 模式 B：直接解析回传

如果你本来就想把链接解析结果直接回给用户，那可以保留 `media_parser` 的自动回复式触发。

特征是：

- 机器人更像媒体解析器
- 容易直接输出标题、摘要、作者、热评
- 不一定会自然承接到后续聊天

这种模式不是错误，只是和“聊天式理解链接”是两条不同路线。

## 为什么会出现“把小红书解析直接发出来”

如果你看到的现象是：

- 发一个小红书链接后，机器人直接贴解析内容
- 而不是像看完帖子后自然聊一句

大概率优先检查 `media_parser`，因为最常见的原因不是 `link_context` 失效，而是 `media_parser` 自己就在回帖。

最常见的触发组合：

- `media_parser.trigger.auto_parse = true`
- `media_parser.trigger.reply_trigger = true`
- B 站 / 小红书输出模式不是 `仅文本`

在这组配置下，链路通常会变成：

1. `continuous_message` 负责把 QQ 卡片或壳链接提出来
2. `media_parser` 直接解析并产出回复
3. 用户看到的是“解析结果回帖”
4. `link_context` 即使存在，也更像辅助，而不是主导最终表现

如果你想要的是“理解后自然接话”，优先把上面三个点改成推荐值，再重试。

## 群聊自动接话怎么开

`link_context` 的群聊自动唤醒不是全局开的，而是看 AstrBot 的主动回复白名单。

如果你希望某个群里发 B 站 / 小红书链接就能自动接话，需要把这个群加入主动回复白名单。

## 推荐验证顺序

1. 私聊发一条 B 站链接
2. 私聊发一条小红书链接
3. 群里发一张 B 站 / 小红书 QQ 卡片
4. 观察机器人是只会复读链接，还是能直接聊内容

如果能直接顺着标题、摘要、热评接话，这条链就算跑通了。

建议额外观察这一步：

5. 关闭 `media_parser` 的自动回复式触发后，再发一条相同平台链接，看表现有没有从“贴解析”变成“自然接话”

## 常见问题

### 机器人完全不理链接

优先检查：

- `link_context` 是否加载成功
- `auto_parse_links_on_request` 是否开启
- `media_parser` 是否可用

### 机器人直接把解析结果贴出来

优先检查：

- `media_parser.trigger.auto_parse` 是否误开
- `media_parser.trigger.reply_trigger` 是否误开
- B 站 / 小红书输出模式是否不是 `仅文本`
- 你当前是不是本来就在走“直接解析回传”模式

如果你的目标是聊天式理解链接，把上面三项改回推荐值后再测一次。

### QQ 卡片没提取出真实链接

优先检查：

- `continuous_message.enable_qq_card_parsing`
- `qq_card_disabled_platforms`

### 群里要 @ 才回复

这不一定是解析链坏了，也可能是该群不在 AstrBot 主动回复白名单里。

### 私聊能聊链接，群里却只沉默

优先检查：

- 该群是否在 AstrBot 主动回复白名单里
- `link_context` 是否加载成功
- `continuous_message` 是否已经把 QQ 卡片里的真实链接提出来

### 小红书能解析，但回复还是很像工具输出

优先检查：

- `inject_recent_tool_context` 是否开启
- `link_parser_success_prompt` 是否被改成了很强的工具提示
- 模型本身的系统提示词里，是否要求“先展示解析结果再回答”

如果插件配置已经是推荐值，但回复仍明显偏工具味，问题就不一定在插件链，而可能在主模型提示词。
