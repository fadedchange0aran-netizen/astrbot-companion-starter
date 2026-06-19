# 部署检查清单

这份清单面向第一次把 `LivingMemory + adapter + 链接解析` 搭起来的人。

## 1. 目录与真源

建议把目录分成三层：

- Git 真源目录
- 运行时目录
- 数据目录

推荐口径：

- 不直接在运行时目录里长期改代码
- 每个组件只认一份 Git 真源
- 新建公开仓时放到单独目录，不和现网运行目录混用

## 2. adapter 的 `.env`

至少确认这些值已经填写：

- `ARAN_ADAPTER_BACKEND_TYPE=astrbot_http`
- `ARAN_ASTRBOT_TARGET_URL=http://127.0.0.1:6185/api/v1/chat`
- `ARAN_ASTRBOT_API_KEY=<你的 AstrBot API Key>`

如果你要把 reasoning 只给 RikkiHub / OpenAI 前端看，而不想让 QQ 混进去：

- 保持 `ARAN_ASTRBOT_EXPOSE_REASONING=true`
- 同时把 AstrBot 侧 `display_reasoning_text` 关掉

## 3. AstrBot 插件目录怎么放

运行时 AstrBot 常见插件目录是：

```text
data/plugins/
```

把下面这些目录放进去：

- `astrbot_plugin_link_context`
- `astrbot_plugin_media_parser`
- `astrbot_plugin_continuous_message`
- `astrbot_plugin_livingmemory`

如果你的部署是“真源 + 同步到运行副本”，那就：

- Git 仓库存源码
- `data/plugins/` 放运行副本

## 4. 白名单群自动解析怎么开

要让白名单群里的 B 站 / 小红书链接自动接话，需要同时满足：

1. 该群在 AstrBot 主动回复白名单里
2. `astrbot_plugin_link_context` 已加载
3. `astrbot_plugin_media_parser` 已加载
4. `astrbot_plugin_continuous_message` 已开启 QQ 卡片提链

建议重点确认：

- `auto_parse_links_on_request = true`
- `enable_qq_card_parsing = true`
- `enable_link_parsing = true`

## 5. 旧记忆数据库怎么迁

如果旧记忆已经在 `LivingMemory` 里：

1. 备份旧的 `livingmemory.db`
2. 复制到新实例：

```text
data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db
```

3. 重启 AstrBot
4. 必要时执行：

```text
/lmem rebuild-index
/lmem rebuild-graph
```

更详细说明见：

- [旧记忆导入](legacy-memory-import.md)
- [MemOS / Memos 全量迁移](memos-migration.md)

## 6. 思考链显示检查

如果你发现：

- QQ 把思考链夹在正文里
- RikkiHub 同时在正文和 reasoning 区各显示一次

先检查：

- AstrBot `display_reasoning_text` 是否被打开

这个开关是全链路生效的，不只是 AstrBot 前端页面自己的显示选项。

## 7. 上线前最小验证

至少做这几步：

1. 私聊普通问答
2. RikkiHub 走 `adapter` 问答
3. 私聊 B 站链接解析
4. 私聊小红书链接解析
5. 群里白名单链接自动接话
6. `LivingMemory` 搜索旧记忆
