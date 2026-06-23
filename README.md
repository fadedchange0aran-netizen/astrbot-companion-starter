# AstrBot Companion Starter

一个给陪伴型 AstrBot 机器人准备的最小公开整合仓。

这个仓库不追求“大而全”，但会收录一组适合陪伴型机器人的实用组件模板：

- `adapter/`
  - 给 RikkiHub、OpenAI 兼容前端、旧网关提供稳定接入口
- `plugins/astrbot_plugin_link_context/`
  - 让机器人把 B 站 / 小红书链接读成可继续聊天的上下文
  - 记住最近一次成功工具结果，减少“刚做完就忘了”的体验问题
- `plugins/third_party_patches/astrbot_plugin_media_parser/`
  - 作为 B 站 / 小红书解析底层
- `plugins/third_party_patches/astrbot_plugin_continuous_message/`
  - 负责 QQ 卡片提链、链接增强和连续输入整合
- `plugins/astrbot_plugin_file_delivery/`
  - 把 `file_vault` 里的受管文件直接回传到聊天目标
- `plugins/astrbot_plugin_llmperception/`
  - 注入日期、生理期、纪念日、里程碑和 StackChan 轻量模式配置
- `plugins/third_party_patches/astrbot_plugin_bookshelf/`
  - 提供共读书架、章节阅读和目标会话续聊面板
- `plugins/third_party_patches/astrbot_plugin_ebooks/`
  - 提供多源电子书搜索下载，并支持转存到 `file_vault`

仓库里同时附带两类可选组件模板：

- `bridges/stackchan/`
  - 给 `StackChan -> adapter` 的桥接层模板
- `services/aranbox-mcp/`
  - 给 MCP 工具服务的可选模板，默认保留 `daily + extended`，隐藏高权限管理层

长期记忆插件不放在这个仓库里，请单独使用：

- [astrbot_plugin_livingmemory](https://github.com/fadedchange0aran-netizen/astrbot_plugin_livingmemory)

## 适合谁

如果你最在意的是下面这几件事，这个仓库就是为你准备的：

- 机器人能接入 RikkiHub 或 OpenAI 兼容前端
- 机器人在 QQ / 群聊里能自然“看懂” B 站、小红书、QQ 卡片链接
- 机器人有长期记忆，但你不想一上来就装太多额外工具

## 不包含什么

这个 starter 默认不把下面这些能力当成“必装”组件：

- 音乐工具
- Qzone
- 全量 MCP 管理工具
- shell / python 这类高风险泛工具

原因很简单：先把“记忆 + 接入 + 链接体验”打稳，体感提升最大，也最不容易把工具池搞乱。像 `stackchan` bridge、`aranbox-mcp`、`ebooks` 这类组件保留在仓库里，但建议按需单独启用。

## 建议安装顺序

1. 先装 AstrBot 本体
2. 安装 `LivingMemory`
3. 安装本仓库里的 `link_context`、`media_parser`、`continuous_message`
4. 配好 `adapter`
5. 如果需要，再启用 `file_delivery`、`llmperception`
6. 如果需要共读和电子书，再启用 `bookshelf`、`ebooks`
7. 如果需要，再启用 `bridges/stackchan` 或 `services/aranbox-mcp`
8. 最后再按需要加其他工具

## 文档

- [快速开始](docs/quick-start.md)
- [部署检查清单](docs/deployment-checklist.md)
- [记忆配置](docs/memory-setup.md)
- [旧记忆导入](docs/legacy-memory-import.md)
- [MemOS / Memos 迁移](docs/memos-migration.md)
- [链接解析与卡片解析](docs/link-parsing.md)

## 目录

```text
adapter/
bridges/
  stackchan/
plugins/
  astrbot_plugin_file_delivery/
  astrbot_plugin_llmperception/
  astrbot_plugin_link_context/
  third_party_patches/
    astrbot_plugin_bookshelf/
    astrbot_plugin_media_parser/
    astrbot_plugin_continuous_message/
    astrbot_plugin_ebooks/
services/
  aranbox-mcp/
docs/
tools/
```

## 许可说明

- 本仓库是一个整理后的 starter 仓，不代表所有子目录都使用同一许可证。
- 第三方插件请以各自目录中的许可证与上游说明为准。
