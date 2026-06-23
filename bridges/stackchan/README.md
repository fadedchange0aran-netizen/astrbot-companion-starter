# StackChan Bridge

这里放的是一个 `StackChan -> adapter` 桥接模板，适合把硬件侧语音/控制链路接到 AstrBot 或兼容的聊天上游。

## 包含内容

- `vps_bridge.py`
  - 主桥接程序
- `.env.example`
  - 运行配置模板
- `requirements.txt`
  - Python 依赖

## 依赖前提

- 你已经有可用的 `adapter` 或 OpenAI 兼容聊天上游
- 你已经准备好 StackChan 控制端和语音回传所需的 token / websocket 地址
- 如果你想让 StackChan 读取 `llmperception` 里的轻量模型切换配置：
  - 默认读取 `STACKCHAN_ASTRBOT_CONFIG_DIR=/AstrBot/data/config`
  - 也可以直接指定 `STACKCHAN_LLMPERCEPTION_CONFIG=/path/to/astrbot_plugin_llmperception_config.json`

## 快速启动

1. 进入本目录并创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

2. 按需填写 `.env` 里的 `STACKCHAN_*` 变量

3. 启动桥接服务

```bash
python vps_bridge.py
```

## 常用配置

- `STACKCHAN_ADAPTER_URL`
  - 聊天上游地址，例如 `http://127.0.0.1:8001/v1/chat/completions`
- `STACKCHAN_ADAPTER_KEY`
  - 聊天上游 API key
- `STACKCHAN_GROQ_API_KEY`
  - 语音识别 / 视觉接口使用的 API key
- `STACKCHAN_VOICE_WS_URL`
  - 语音 websocket 地址
- `STACKCHAN_VOICE_WS_TOKEN`
  - 语音 websocket token
- `STACKCHAN_CONTROL_WS_TOKEN`
  - 控制 websocket token
- `STACKCHAN_DEFAULT_ROBOT_ID`
  - 默认机器人 ID；也可以在请求里显式传入

## 配置文件说明

- 默认优先读取 `STACKCHAN_ENV_FILE`
- 如果没有显式指定，会回退到当前目录下的 `.env`
- 若 `.env` 不存在，仍会继续尝试兼容旧文件名 `aran.env`

## 说明

- 这里保留的是一个通用桥接模板，不包含你的生产部署脚本或 systemd 单元文件
- 如果你要部署到自己的服务器，建议把桥接目录当作单独服务维护
- 命名上统一使用 `chat upstream` / `adapter upstream`，不再强调旧 `gateway` 语义
