# Companion Adapter

`companion-adapter` is a small FastAPI service that keeps an OpenAI-compatible edge API stable while letting the internal backend change later.

## What It Does

- Accepts legacy gateway-compatible chat paths
- Preserves `Authorization` and `X-Session-Id` semantics
- Supports OpenAI-compatible frontends
- Keeps backend integration replaceable

## Supported Routes

- `POST /v1/chat/completions`
- `POST /chat/completions`
- `POST /api/v1/chat/completions`
- `GET /healthz`
- `GET /readyz`

## Backend Modes

### `openai_proxy`

Forwards the request body to another OpenAI-compatible upstream endpoint.

### `astrbot_http`

Transforms the external OpenAI-compatible request into AstrBot's developer HTTP API request and wraps the reply back into OpenAI-compatible shape.

Recommended settings:

- `ARAN_ASTRBOT_TARGET_URL=http://127.0.0.1:6185/api/v1/chat`
- `ARAN_ASTRBOT_API_KEY=<astrbot_api_key>`
- `ARAN_ASTRBOT_USERNAME=owner`

## Transcript Archive

The adapter can write a full request/response transcript archive to local JSONL files for backup and audit.

Suggested paths:

- `ARAN_TRANSCRIPT_ROOT=./data/transcripts`
- `ARAN_MANUAL_BACKUP_ROOT=./data/backups`
- `ARAN_ASTRBOT_DATA_DB_PATH=./data/astrbot/data_v4.db`
- `ARAN_QQ_CHAT_BACKUP_ROOT=./data/astrbot/qq_chat_backups`

## Quick Start

1. Create a virtual environment
2. Install requirements

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and edit it
4. Run the server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

## Notes

- This public template intentionally removes production sync scripts and live-server path assumptions
- If you need deployment automation, add it in your own infrastructure repo
