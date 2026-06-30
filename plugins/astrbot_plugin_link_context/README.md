# astrbot_plugin_link_context

给机器人补两件事：

- 把 B 站 / 小红书 / 微博 / 抖音 / Twitter(X) / 闲鱼链接解析成适合 LLM 直接讨论的文本摘要
- 记住最近一次成功工具结果，让下一轮别像没发生过一样

## 功能

- 提供 `read_link_context` 工具
- 复用 `astrbot_plugin_media_parser` 的 B 站 / 小红书 / 微博 / 抖音 / Twitter(X) / 闲鱼解析层
- 在下一轮请求前注入“最近完成事项提示”
- 记录点歌、生图等成功结果，减少“机器人刚做完就忘了”的体验问题
- 链接理解摘要默认只保留平台、标题、作者、摘要、热评等内容，不把原始网页 URL 注入给机器人，减少 token 占用

## 依赖

- `astrbot_plugin_media_parser` 已安装
- 解析配置默认读取 `/AstrBot/data/config/astrbot_plugin_media_parser_config.json`

## 安装

把本插件目录放到 `/AstrBot/data/plugins/astrbot_plugin_link_context`，然后重启 AstrBot。
