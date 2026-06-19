#!/usr/bin/env python3
"""Import normalized JSONL records into LivingMemory's documents table.

Each input line must be a JSON object. Recommended fields:
  - text: memory content
  - doc_id: optional stable document ID
  - owner_id: optional, can also be provided by CLI
  - persona_id: optional, can also be provided by CLI
  - session_id: optional
  - source_platform: optional
  - source_session: optional
  - importance: optional, 0-1 or 0-10
  - created_at / updated_at: optional ISO timestamp or epoch seconds
  - metadata: optional dict, merged into LivingMemory metadata

The importer writes rows into SQLite `documents`, then you should run:
  /lmem rebuild-index
  /lmem rebuild-graph
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PERSONA_ID = "default"
DEFAULT_SOURCE_PLATFORM = "memos_import"
DEFAULT_SOURCE_SESSION = "memos:import"


@dataclass
class ImportRow:
    doc_id: str
    text: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import normalized JSONL records into LivingMemory documents."
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to normalized JSONL file.",
    )
    parser.add_argument(
        "--livingmemory-db",
        required=True,
        help="Target LivingMemory SQLite database path.",
    )
    parser.add_argument(
        "--owner-id",
        required=True,
        help="Target LivingMemory owner_id.",
    )
    parser.add_argument(
        "--persona-id",
        default=DEFAULT_PERSONA_ID,
        help="Target LivingMemory persona_id.",
    )
    parser.add_argument(
        "--default-source-platform",
        default=DEFAULT_SOURCE_PLATFORM,
        help="Fallback source_platform when input line omits it.",
    )
    parser.add_argument(
        "--default-source-session",
        default=DEFAULT_SOURCE_SESSION,
        help="Fallback source_session when input line omits it.",
    )
    parser.add_argument(
        "--upsert-doc-id",
        action="store_true",
        help="Update existing rows when doc_id already exists.",
    )
    parser.add_argument(
        "--report-path",
        default="",
        help="Optional JSON report output path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse source and print summary without writing target DB.",
    )
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def clamp_importance(value: Any, default: float = 0.65) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number > 1.0:
        number = number / 10.0
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return round(number, 4)


def normalize_timestamp(value: Any) -> tuple[str, float]:
    if value is None:
        current = datetime.now(timezone.utc)
        return current.replace(microsecond=0).isoformat(), current.timestamp()
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return dt.replace(microsecond=0).isoformat(), float(value)

    text = ensure_text(value)
    if not text:
        return normalize_timestamp(None)

    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        return normalize_timestamp(numeric)

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        current = datetime.now(timezone.utc)
        return current.replace(microsecond=0).isoformat(), current.timestamp()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0).isoformat(), dt.timestamp()


def ensure_target_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_doc_id ON documents(doc_id)"
        )
        conn.commit()


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = ensure_text(value)
        if text:
            return text
    return ""


def build_doc_id(raw: dict[str, Any], text: str, line_number: int) -> str:
    explicit = first_non_empty(
        raw.get("doc_id"),
        raw.get("memory_id"),
        raw.get("id"),
        raw.get("uuid"),
    )
    if explicit:
        return explicit

    seed = "||".join(
        [
            first_non_empty(raw.get("source_platform"), DEFAULT_SOURCE_PLATFORM),
            first_non_empty(
                raw.get("source_session"),
                raw.get("conversation_id"),
                raw.get("session_id"),
                DEFAULT_SOURCE_SESSION,
            ),
            text[:200],
            str(line_number),
        ]
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"memos-import-{digest}"


def normalize_record(
    raw: dict[str, Any],
    *,
    owner_id: str,
    persona_id: str,
    default_source_platform: str,
    default_source_session: str,
    line_number: int,
) -> ImportRow:
    input_metadata = raw.get("metadata")
    metadata = input_metadata if isinstance(input_metadata, dict) else {}

    text = first_non_empty(
        raw.get("text"),
        raw.get("memory"),
        raw.get("content"),
        raw.get("memory_value"),
        raw.get("summary"),
        metadata.get("text"),
        metadata.get("memory"),
        metadata.get("content"),
        metadata.get("memory_value"),
    )
    if not text:
        raise ValueError(f"line {line_number}: missing text/memory/content")

    source_platform = first_non_empty(
        raw.get("source_platform"),
        raw.get("platform"),
        metadata.get("source_platform"),
        metadata.get("platform"),
        default_source_platform,
    )
    source_session = first_non_empty(
        raw.get("source_session"),
        raw.get("conversation_id"),
        raw.get("session_id"),
        metadata.get("source_session"),
        metadata.get("conversation_id"),
        metadata.get("session_id"),
        default_source_session,
    )
    session_id = first_non_empty(
        raw.get("session_id"),
        metadata.get("session_id"),
        source_session,
    )
    created_at, created_ts = normalize_timestamp(
        raw.get("created_at") or raw.get("timestamp") or metadata.get("created_at")
    )
    updated_at, updated_ts = normalize_timestamp(
        raw.get("updated_at")
        or metadata.get("updated_at")
        or raw.get("timestamp")
        or created_at
    )
    importance = clamp_importance(
        raw.get("importance") or metadata.get("importance"),
        default=0.65,
    )
    doc_id = build_doc_id(raw, text, line_number)

    full_metadata = dict(metadata)
    full_metadata.update(
        {
            "owner_id": owner_id,
            "persona_id": persona_id,
            "session_id": session_id,
            "source_platform": source_platform,
            "source_session": source_session,
            "importance": importance,
            "create_time": created_ts,
            "last_access_time": updated_ts,
            "migrated_from": first_non_empty(
                raw.get("migrated_from"),
                metadata.get("migrated_from"),
                "memos_jsonl_import",
            ),
            "migrated_at": now_iso(),
        }
    )

    if raw.get("owner_id"):
        full_metadata["source_owner_id"] = ensure_text(raw.get("owner_id"))

    return ImportRow(
        doc_id=doc_id,
        text=text,
        metadata=full_metadata,
        created_at=created_at,
        updated_at=updated_at,
    )


def load_rows(
    source_path: Path,
    *,
    owner_id: str,
    persona_id: str,
    default_source_platform: str,
    default_source_session: str,
) -> tuple[list[ImportRow], list[str]]:
    rows: list[ImportRow] = []
    errors: list[str] = []
    for line_number, line in enumerate(
        source_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: invalid json: {exc}")
            continue
        if not isinstance(raw, dict):
            errors.append(f"line {line_number}: JSON value must be an object")
            continue
        try:
            rows.append(
                normalize_record(
                    raw,
                    owner_id=owner_id,
                    persona_id=persona_id,
                    default_source_platform=default_source_platform,
                    default_source_session=default_source_session,
                    line_number=line_number,
                )
            )
        except ValueError as exc:
            errors.append(str(exc))
    return rows, errors


def fetch_existing_doc_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT doc_id FROM documents WHERE doc_id IS NOT NULL AND TRIM(doc_id) != ''"
        ).fetchall()
    }


def insert_rows(
    db_path: Path,
    rows: list[ImportRow],
    *,
    upsert_doc_id: bool,
) -> dict[str, int]:
    inserted = 0
    updated = 0
    skipped_existing = 0
    with sqlite3.connect(db_path) as conn:
        existing_doc_ids = fetch_existing_doc_ids(conn)
        for row in rows:
            if row.doc_id in existing_doc_ids:
                if upsert_doc_id:
                    conn.execute(
                        """
                        UPDATE documents
                        SET text = ?, metadata = ?, updated_at = ?
                        WHERE doc_id = ?
                        """,
                        (
                            row.text,
                            json.dumps(row.metadata, ensure_ascii=False),
                            row.updated_at,
                            row.doc_id,
                        ),
                    )
                    updated += 1
                else:
                    skipped_existing += 1
                continue
            conn.execute(
                """
                INSERT INTO documents (doc_id, text, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row.doc_id,
                    row.text,
                    json.dumps(row.metadata, ensure_ascii=False),
                    row.created_at,
                    row.updated_at,
                ),
            )
            inserted += 1
        conn.commit()
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_existing": skipped_existing,
    }


def build_summary(
    args: argparse.Namespace,
    rows: list[ImportRow],
    errors: list[str],
    insert_result: dict[str, int] | None,
) -> dict[str, Any]:
    summary = {
        "source": str(Path(args.source).resolve()),
        "livingmemory_db": str(Path(args.livingmemory_db).resolve()),
        "owner_id": args.owner_id,
        "persona_id": args.persona_id,
        "default_source_platform": args.default_source_platform,
        "default_source_session": args.default_source_session,
        "upsert_doc_id": bool(args.upsert_doc_id),
        "dry_run": bool(args.dry_run),
        "parsed_rows": len(rows),
        "error_count": len(errors),
        "errors_preview": errors[:20],
    }
    if insert_result is not None:
        summary.update(insert_result)
    return summary


def write_report(
    report_path: Path,
    summary: dict[str, Any],
    rows: list[ImportRow],
    errors: list[str],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": summary,
        "rows": [asdict(row) for row in rows],
        "errors": errors,
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    source_path = Path(args.source)
    livingmemory_db = Path(args.livingmemory_db)

    rows, errors = load_rows(
        source_path,
        owner_id=args.owner_id,
        persona_id=args.persona_id,
        default_source_platform=args.default_source_platform,
        default_source_session=args.default_source_session,
    )

    insert_result: dict[str, int] | None = None
    if not args.dry_run:
        ensure_target_schema(livingmemory_db)
        insert_result = insert_rows(
            livingmemory_db,
            rows,
            upsert_doc_id=bool(args.upsert_doc_id),
        )

    summary = build_summary(args, rows, errors, insert_result)
    if args.report_path:
        write_report(Path(args.report_path), summary, rows, errors)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
