# AstrBot Companion Starter

一个给陪伴型 AstrBot 机器人准备的最小公开整合仓。

这个仓库不追求“大而全”，只保留最影响聊天体验的几块：

- `adapter/`
  - 给 RikkiHub、OpenAI 兼容前端、旧网关提供稳定接入口
- `plugins/astrbot_plugin_link_context/`
  - 让机器人把 B 站 / 小红书链接读成可继续聊天的上下文
  - 记住最近一次成功工具结果，减少“刚做完就忘了”的体验问题
- `plugins/third_party_patches/astrbot_plugin_media_parser/`
  - 作为 B 站 / 小红书解析底层
- `plugins/third_party_patches/astrbot_plugin_continuous_message/`
  - 负责 QQ 卡片提链、链接增强和连续输入整合

长期记忆插件不放在这个仓库里，请单独使用：

- [astrbot_plugin_livingmemory](https://github.com/fadedchange0aran-netizen/astrbot_plugin_livingmemory)

## 适合谁

如果你最在意的是下面这几件事，这个仓库就是为你准备的：

- 机器人能接入 RikkiHub 或 OpenAI 兼容前端
- 机器人在 QQ / 群聊里能自然“看懂” B 站、小红书、QQ 卡片链接
- 机器人有长期记忆，但你不想一上来就装太多额外工具

## 不包含什么

这个 starter 故意不包含下面这些高复杂度能力：

- 电子书下载
- 音乐工具
- Qzone
- 一大堆 MCP 工具
- shell / python 这类高风险泛工具

原因很简单：先把“记忆 + 接入 + 链接体验”打稳，体感提升最大，也最不容易把工具池搞乱。

## 建议安装顺序

1. 先装 AstrBot 本体
2. 安装 `LivingMemory`
3. 安装本仓库里的 `link_context`、`media_parser`、`continuous_message`
4. 配好 `adapter`
5. 最后再按需要加其他工具

## 文档

- [快速开始](docs/quick-start.md)
- [记忆配置](docs/memory-setup.md)
- [旧记忆导入](docs/legacy-memory-import.md)
- [链接解析与卡片解析](docs/link-parsing.md)

## 目录

```text
adapter/
plugins/
  astrbot_plugin_link_context/
  third_party_patches/
    astrbot_plugin_media_parser/
    astrbot_plugin_continuous_message/
docs/
```

## 许可说明

- 本仓库是一个整理后的 starter 仓，不代表所有子目录都使用同一许可证。
- 第三方插件请以各自目录中的许可证与上游说明为准。
