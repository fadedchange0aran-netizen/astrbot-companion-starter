# astrbot_plugin_link_context

给阿然补两件事：

- 把 B站 / 小红书链接解析成适合 LLM 直接讨论的文本摘要
- 记住最近一次成功工具结果，让下一轮别像没发生过一样

## 功能

- 提供 `read_link_context` 工具
- 复用现网 `astrbot_plugin_media_parser` 的 B站 / 小红书解析层
- 在下一轮请求前注入“最近完成事项提示”
- 记录点歌、生图等成功结果，减少“阿然刚做完就忘了”的体验问题
- 链接理解摘要默认只保留平台、标题、作者、摘要、热评等内容，不把原始网页 URL 注入给阿然，减少 token 占用

## 依赖

- `astrbot_plugin_media_parser` 已安装
- 解析配置默认读取 `/AstrBot/data/config/astrbot_plugin_media_parser_config.json`

## 当前配套用法

- 这不是一个独立全平台解析器
- 当前主路径是：
  - `astrbot_plugin_link_context`
  - `->` 复用 `astrbot_plugin_media_parser`
  - `->` 把结果整理成适合 LLM 继续聊的文本
- 当前不依赖 `astrbot_plugin_parser`
- 如果后续同时启用别的自动解析插件，要先评估是否会重复触发或抢答

## 安装

把本插件目录放到 `/AstrBot/data/plugins/astrbot_plugin_link_context`，然后重启 AstrBot。
