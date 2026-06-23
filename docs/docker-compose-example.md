# Docker Compose 示例

这份示例给的是一套尽量小、尽量好改的组合：

- `AstrBot`
- `NapCat`
- `adapter`

目标是先把下面三件事跑通：

1. 前端或外部客户端可以通过 `adapter` 调 AstrBot
2. QQ / NapCat 可以正常和 AstrBot 交互
3. `astrbot_plugin_file_delivery` 可以通过共享目录把文件发回 QQ

这不是生产环境唯一写法，而是一份适合 starter 仓的最小模板。

## 目录规划

先在宿主机准备这些目录：

```bash
mkdir -p /data/astrbot/data
mkdir -p /data/astrbot/shared/astrbot_flash
mkdir -p /data/adapter/data
```

建议这样理解：

- `/data/astrbot/data`
  - AstrBot 主数据目录
- `/data/astrbot/shared/astrbot_flash`
  - AstrBot 和 NapCat 共享的文件中转目录
- `/data/adapter/data`
  - adapter 的本地数据目录

## Compose 示例

```yaml
services:
  astrbot:
    image: soulter/astrbot:latest
    container_name: astrbot
    restart: unless-stopped
    ports:
      - "6185:6185"
    volumes:
      - /data/astrbot/data:/AstrBot/data
      - /data/astrbot/shared/astrbot_flash:/tmp/astrbot_flash
    environment:
      - TZ=Asia/Shanghai

  napcat:
    image: mlikiowa/napcat-docker:latest
    container_name: napcat
    restart: unless-stopped
    volumes:
      - /data/astrbot/shared/astrbot_flash:/tmp/astrbot_flash
    environment:
      - TZ=Asia/Shanghai
    # ports:
    #   - "3001:3001"
    #   - "6099:6099"

  adapter:
    build: ./adapter
    container_name: companion-adapter
    restart: unless-stopped
    ports:
      - "8001:8001"
    volumes:
      - /data/adapter/data:/app/data
    env_file:
      - ./adapter/.env
    depends_on:
      - astrbot
```

## 关键挂载解释

### AstrBot

```yaml
- /data/astrbot/data:/AstrBot/data
- /data/astrbot/shared/astrbot_flash:/tmp/astrbot_flash
```

作用：

- `/AstrBot/data`
  - 给 AstrBot 挂自己的主数据目录
- `/tmp/astrbot_flash`
  - 给 `file_delivery` 提供共享中转目录

### NapCat

```yaml
- /data/astrbot/shared/astrbot_flash:/tmp/astrbot_flash
```

作用：

- 让 NapCat 能读取 AstrBot 复制进去的待发送文件

### adapter

```yaml
- /data/adapter/data:/app/data
```

作用：

- 给 adapter 保留本地转录、备份和运行数据目录

## adapter `.env` 最小示例

`adapter/.env` 可以先从最小配置开始：

```bash
ARAN_ADAPTER_BACKEND_TYPE=astrbot_http
ARAN_ASTRBOT_TARGET_URL=http://astrbot:6185/api/v1/chat
ARAN_ASTRBOT_API_KEY=your_astrbot_api_key
ARAN_ASTRBOT_USERNAME=owner
ARAN_ADAPTER_HOST=0.0.0.0
ARAN_ADAPTER_PORT=8001
```

如果你的前端直接连 `adapter`，它通常访问：

```text
http://<your-host>:8001/v1/chat/completions
```

## `file_delivery` 插件配置示例

AstrBot 里的 `astrbot_plugin_file_delivery` 建议先这样配：

```json
{
  "file_vault_root": "/AstrBot/data/file_vault",
  "flash_transfer_dir": "/tmp/astrbot_flash",
  "napcat_container_name": "napcat",
  "allowed_sender_ids": "123456789",
  "allow_anyone": false,
  "allow_cross_user_delivery": false
}
```

这里最关键的是：

- `file_vault_root`
  - 指向 AstrBot 容器内的真实路径
- `flash_transfer_dir`
  - 指向 AstrBot 和 NapCat 都能看到的共享目录
- `napcat_container_name`
  - 作为兜底配置保留；共享目录方案下通常不会优先走它

## 启动顺序

建议按下面顺序准备：

1. 启动 `astrbot`
2. 在 AstrBot WebUI 完成基础初始化
3. 配好 QQ 侧接入
4. 启动 `adapter`
5. 安装并配置 starter 里的插件
6. 最后验证 `file_delivery`

## 最小验证步骤

建议至少验证下面几项：

1. 打开 AstrBot WebUI
2. `adapter` 的 `GET /healthz` 返回正常
3. QQ 能和 AstrBot 正常收发消息
4. `link_context` 能继续聊 B 站 / 小红书链接
5. `file_delivery` 能把 `file_vault` 里的文件发回 QQ

## 为什么这份 Compose 不直接包含 StackChan / MCP

因为 starter 第一目标是先把主链路跑通：

- AstrBot
- QQ
- adapter
- 文件回传

`StackChan` 和 `aranbox-mcp` 更适合按需单独启用，不建议第一次部署就全塞进去。

## 常见坑

### `file_delivery` 发不出去

优先检查：

1. `/tmp/astrbot_flash` 是否真的同时挂进了 AstrBot 和 NapCat
2. AstrBot 插件配置里的 `flash_transfer_dir` 是否就是 `/tmp/astrbot_flash`
3. `file_vault_root` 是否填成了 AstrBot 容器内路径

### adapter 调不到 AstrBot

优先检查：

1. `ARAN_ASTRBOT_TARGET_URL` 是否用了容器内可互通地址
2. 是否写成了 `http://astrbot:6185/api/v1/chat`
3. AstrBot API Key 是否正确

### `allowed_sender_ids` 不知道填什么

最简单的做法是：

- 先只填你自己的 `sender_id`
- 如果不确定自己的 `sender_id`，先临时调试，再根据运行日志或事件信息确认

## 你最容易照抄的一版

如果你只记三件事：

1. AstrBot 数据挂到 `/AstrBot/data`
2. AstrBot 和 NapCat 共享挂载 `/tmp/astrbot_flash`
3. `file_delivery.flash_transfer_dir=/tmp/astrbot_flash`
