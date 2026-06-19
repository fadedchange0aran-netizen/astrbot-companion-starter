# Aran Adapter

`aran-adapter` is a small FastAPI service that keeps the external API stable
while allowing the internal backend to change later.

Current goals:

- Accept legacy gateway-compatible chat paths
- Preserve `Authorization` and `X-Session-Id` semantics
- Support OpenAI-compatible frontends
- Keep backend integration replaceable

## Layout

- This repository keeps the managed source under `adapter/`.
- Your live runtime directory can be anywhere, for example:
  - `/opt/aran-adapter`
  - `/srv/bot/adapter`
- Treat `.env`, `.venv`, `data/`, and `.dbg` as runtime-only paths.
- If you hotfix the runtime copy first, backfill the changes into this repo and
  then redeploy from here.

Supported routes:

- `POST /v1/chat/completions`
- `POST /chat/completions`
- `POST /api/v1/chat/completions`
- `GET /healthz`
- `GET /readyz`

## Backend Modes

### `openai_proxy`

Forwards the request body to another OpenAI-compatible upstream endpoint.
This is the safest first deployment mode because it lets the adapter become
the stable edge service immediately.

### `astrbot_http`

Transforms the external OpenAI-compatible request into AstrBot's official
developer HTTP API request and wraps the reply back into OpenAI-compatible
shape.

Current mapping:

- `latest user message` -> `message`
- `owner_id / user / default username` -> `username`
- `X-Session-Id` -> `session_id`
- `model` -> `selected_model`

AstrBot side requirements:

- AstrBot version supports `/api/v1/chat`
- WebUI has created an API key
- adapter can reach `http://<astrbot-host>:6185/api/v1/chat`

Recommended settings:

- `ARAN_ASTRBOT_TARGET_URL=http://127.0.0.1:6185/api/v1/chat`
- `ARAN_ASTRBOT_API_KEY=<astrbot_api_key>`
- `ARAN_ASTRBOT_USERNAME=assistant`

Notes:

- The adapter calls AstrBot with SSE enabled.
- If the frontend sends `stream=false`, the adapter aggregates AstrBot SSE back
  into a normal OpenAI-style JSON response.
- If the frontend sends `stream=true`, the adapter re-emits OpenAI-compatible
  SSE chunks to the frontend.
- `ARAN_ASTRBOT_TARGET_URL` may be either the full `/api/v1/chat` endpoint or
  the bare AstrBot base URL such as `http://127.0.0.1:6185`.

Compatibility notes for common frontends:

- Final text always remains available at `choices[0].message.content`
- Model thinking is mirrored to `choices[0].message.reasoning_content` when detected
- Tool call deltas are normalized into `choices[0].message.tool_calls` when detected
- Tool status / tool results / attachments are additionally exposed under
  `adapter_metadata.tool_events` and `adapter_metadata.attachments`
- If AstrBot returns Markdown image links, the adapter also extracts them into
  `adapter_metadata.attachments`
- Incoming `image_url`, `input_image`, and `input_file` blocks are uploaded to
  AstrBot `/api/v1/file` first when possible, then rewritten into
  `attachment_id` based message segments
- Common inline media payloads such as `b64_json`, `base64`, `data_url`, and
  nested `file/image_url` objects are also bridged when the frontend does not
  provide a direct URL

Current limits:

- OpenAI SSE compatibility is best-effort for common frontends; attachment and
  tool-result events are exposed as extra delta fields and some clients may
  ignore them
- Input media bridging now covers common URL, data-URL, base64, and nested
  file/image object payloads, but it still does not implement frontend-private
  local file IDs or every custom upload schema
- Tool results are exposed in adapter metadata instead of a strict OpenAI
  standard field, because OpenAI chat completions has no canonical sync field
  for arbitrary tool-result transcript details

## Transcript Archive

The adapter can write a full request/response transcript archive to local JSONL
files. This archive is meant for backup and audit, not for model context.

Recommended settings:

- `ARAN_TRANSCRIPT_ENABLED=true`
- `ARAN_TRANSCRIPT_ROOT=./data/transcripts`
- `ARAN_MANUAL_BACKUP_ROOT=./data/backups`
- `ARAN_MANUAL_BACKUP_EXTRA_PATHS_JSON=[]`
- `ARAN_ASTRBOT_DATA_DB_PATH=./data/astrbot/data_v4.db`
- `ARAN_QQ_CHAT_BACKUP_ROOT=./data/qq_chat_backups`
- `ARAN_QQ_CHAT_BACKUP_SESSIONS_JSON=[]`

The archive captures:

- owner id
- session id
- platform
- original request body
- final response body

## Manual Backup Endpoint

The adapter also exposes a protected local backup endpoint:

- `POST /admin/backups/create`

Auth:

- `Authorization: Bearer <ARAN_MANUAL_BACKUP_TOKEN>`
- falls back to `ARAN_ADAPTER_TOKEN` if `ARAN_MANUAL_BACKUP_TOKEN` is empty

Example body:

```json
{"label":"nightly"}
```

This endpoint creates a local `tar.gz` backup bundle from transcripts and any
extra paths listed in `ARAN_MANUAL_BACKUP_EXTRA_PATHS_JSON`.

## Quick Start

1. Create a virtual environment.
2. Install requirements:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and edit it.
4. Run the server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

5. Point your frontend to one of these routes:

```text
/v1/chat/completions
/chat/completions
/api/v1/chat/completions
```

## Suggested First Deployment

Set:

- `ARAN_ADAPTER_BACKEND_TYPE=openai_proxy`
- `ARAN_UPSTREAM_CHAT_URL` to the current gateway or any OpenAI-compatible target

This makes the adapter usable immediately, while keeping the external URL,
headers, and session contract fixed for the future AstrBot migration.
