"""Estado persistente do Piloto Automático (SQLite próprio em data/autopilot/autopilot.db)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUSES = ("pendente", "aprovado", "agendado", "rejeitado", "falhou")


class AutopilotState:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_videos (
                    video_key TEXT PRIMARY KEY,
                    source_name TEXT,
                    url TEXT,
                    title TEXT,
                    status TEXT NOT NULL,      -- baseline | processando | concluido | ignorado | falhou
                    detail TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    video_key TEXT NOT NULL,
                    source_name TEXT,
                    source_url TEXT,
                    clip_path TEXT NOT NULL,
                    package_zip TEXT,
                    inicio_s REAL,
                    fim_s REAL,
                    score REAL,
                    reason TEXT,
                    hook TEXT,
                    caption TEXT,
                    hashtags TEXT,
                    status TEXT NOT NULL DEFAULT 'pendente',
                    publish_log TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ---------------- vídeos vistos ----------------
    def is_seen(self, video_key: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM seen_videos WHERE video_key=?", (video_key,)).fetchone()
            return row is not None

    def source_has_history(self, source_name: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM seen_videos WHERE source_name=? LIMIT 1", (source_name,)).fetchone()
            return row is not None

    def mark_video(self, video_key: str, status: str, source_name: str = "", url: str = "",
                   title: str = "", detail: str = "") -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO seen_videos (video_key, source_name, url, title, status, detail, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(video_key) DO UPDATE SET status=excluded.status, detail=excluded.detail,
                    title=COALESCE(NULLIF(excluded.title,''), seen_videos.title), updated_at=excluded.updated_at
                """,
                (video_key, source_name, url, title, status, detail[:2000], now, now),
            )

    def list_videos(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM seen_videos WHERE status != 'baseline' ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------------- candidatos ----------------
    def add_candidate(self, **kw: Any) -> Dict[str, Any]:
        cid = kw.pop("candidate_id", None) or f"cand_{uuid.uuid4().hex[:10]}"
        now = time.time()
        hashtags = kw.get("hashtags") or []
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO candidates (candidate_id, video_key, source_name, source_url, clip_path, package_zip,
                    inicio_s, fim_s, score, reason, hook, caption, hashtags, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'pendente', ?, ?)
                """,
                (
                    cid, kw["video_key"], kw.get("source_name"), kw.get("source_url"), kw["clip_path"],
                    kw.get("package_zip"), kw.get("inicio_s"), kw.get("fim_s"), kw.get("score"),
                    kw.get("reason"), kw.get("hook"), kw.get("caption"), json.dumps(hashtags, ensure_ascii=False),
                    now, now,
                ),
            )
        return self.get_candidate(cid)  # type: ignore[return-value]

    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            return self._row(row) if row else None

    def list_candidates(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM candidates WHERE status=? ORDER BY score DESC, created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM candidates ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self._row(r) for r in rows]

    def update_candidate(self, candidate_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {"status", "caption", "hook", "publish_log"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if "status" in sets and sets["status"] not in STATUSES:
            raise ValueError(f"Status inválido: {sets['status']}")
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE candidates SET {cols}, updated_at=? WHERE candidate_id=?",
                    (*sets.values(), time.time(), candidate_id),
                )
        return self.get_candidate(candidate_id)

    def counts(self) -> Dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) n FROM candidates GROUP BY status").fetchall()
            return {r["status"]: r["n"] for r in rows}

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["hashtags"] = json.loads(d.get("hashtags") or "[]")
        except json.JSONDecodeError:
            d["hashtags"] = []
        try:
            d["publish_log"] = json.loads(d["publish_log"]) if d.get("publish_log") else None
        except json.JSONDecodeError:
            pass
        return d
