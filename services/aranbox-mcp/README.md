# aranbox-mcp

`aranbox-mcp` 是一个可选 MCP 工具服务模板，适合和本仓库里的 `adapter`、AstrBot 运行目录或其他兼容前端一起使用。

这一版保留了通用能力，同时把默认配置改成更适合公开分享的中性模板。

## 适合谁

- 你想给机器人增加 MCP 工具，但不想一开始就接入任意 shell
- 你想保留文件仓库、网页生成、天气、地图、邮件等常见工具
- 你需要一个“默认隐藏高权限操作”的工具分层方案

## 默认能力分层

- `daily`
  - 高频能力，例如 adapter 状态、file vault、playground、基础读写
- `extended`
  - 常用外部集成，例如天气、地图、邮件、网页生成、Notion
- `admin`
  - 高权限或运维能力，例如 safe deploy、服务重启、受限工作区读写

默认启用：

```json
["daily", "extended"]
```

也就是说，这份模板默认不会主动暴露管理层工具。

## 运行方式

入口文件：

- `server.py`
- `server_bia.py`
  - 兼容旧入口，公开使用时优先运行 `server.py`

启动：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python server.py
```

## 环境变量

参考：

- `.env.example`

重点变量：

- `ARANBOX_ADAPTER_URL`
- `ARANBOX_ADAPTER_TOKEN`
- `ARANBOX_ENABLED_TOOL_LAYERS_JSON`
- `ARANBOX_DEFAULT_OWNER_ID`
- `ARANBOX_FILE_VAULT_ROOT`
- `ARANBOX_REPO_WORKSPACE_ROOT`
- `ARANBOX_SAFE_DEPLOY_ENABLED`
- `ARANBOX_BIND_HOST`
- `ARANBOX_BIND_PORT`
- `ARANBOX_WEB_PUBLIC_BASE_URL`

这份公开模板默认把运行路径指向当前目录下的 `./runtime/...`，避免直接绑定某个特定 VPS 目录结构。

## 安全说明

- `admin` 层默认隐藏
- `safe deploy` 默认关闭
- 即使启用了 `safe deploy`，仍然只允许固定白名单动作，不提供任意 shell
- `repo_workspace` 也只允许访问受限根目录和指定扩展名

## 当前保留的通用能力

- `get_adapter_status()`
- `get_mcp_tool_layer_status()`
- `get_file_vault_status()` / `store_file_in_vault()`
- `read_file_vault_preview()` / `read_file_vault_text_slice()`
- `get_weather()`
- `search_nearby_places_tool()`
- `render_html()` / `list_generated_pages()`
- `send_email()` / `read_emails()` / `read_specific_email()`

## 可选但需要你自己评估的能力

- `Notion` 读写
- `safe deploy`
- 管理页和一次性管理授权
- 备份、服务重启、受限工作区读写

如果你只是想给机器人一个轻量 MCP 工具包，建议先只用 `daily + extended`。

## 说明

- 这里保留的是通用模板，不包含你的生产同步脚本、systemd 单元或域名反代配置
- 部分工具和文案仍然更适合“陪伴型机器人”场景，你可以按自己的角色设定继续调整
