"""
Persistência local durável em SQLite (modo WAL) para projetos, fontes,
cache de transcrições, clipes exportados e fila de tarefas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastclipper.queue.durable_queue import JobStatus, QueueJob

logger = logging.getLogger(__name__)


class SQLiteStore:
    """Gerenciador de conexões e esquema SQLite em modo WAL."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    origin_type TEXT NOT NULL,
                    origin_url TEXT,
                    file_path TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    authorization_ref TEXT NOT NULL,
                    duration_s REAL,
                    width INTEGER,
                    height INTEGER,
                    has_audio INTEGER,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transcriptions (
                    file_sha256 TEXT PRIMARY KEY,
                    language TEXT,
                    model_name TEXT,
                    duration_s REAL,
                    words_json TEXT NOT NULL,
                    full_text TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clips (
                    clip_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    start_s REAL NOT NULL,
                    end_s REAL NOT NULL,
                    layout TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    subtitles_path TEXT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    params_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress_percent REAL NOT NULL,
                    step_description TEXT NOT NULL,
                    eta_seconds REAL,
                    worker_id TEXT,
                    leased_until REAL,
                    attempts INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_presets (
                    channel_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    layout_mode TEXT NOT NULL,
                    facecam_rect_json TEXT,
                    gameplay_rect_json TEXT,
                    stacked_facecam_height INTEGER NOT NULL DEFAULT 720,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clip_results (
                    result_id TEXT PRIMARY KEY,
                    clip_filename TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    post_url TEXT,
                    views_24h INTEGER NOT NULL DEFAULT 0,
                    views_7d INTEGER NOT NULL DEFAULT 0,
                    revenue_brl REAL NOT NULL DEFAULT 0.0,
                    layout_mode TEXT,
                    subtitle_preset TEXT,
                    channel_id TEXT,
                    notes TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    # --- Projetos ---

    def ensure_default_project(self, project_id: str = "proj_default", name: str = "Projeto Principal", slug: str = "principal") -> Dict[str, Any]:
        now = time.time()
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
            if row:
                return dict(row)
            conn.execute(
                "INSERT INTO projects (project_id, name, slug, created_at) VALUES (?, ?, ?, ?)",
                (project_id, name, slug, now),
            )
            return {"project_id": project_id, "name": name, "slug": slug, "created_at": now}

    # --- Presets de Layout por Canal ---

    def save_channel_preset(
        self,
        channel_id: str,
        name: str,
        layout_mode: str,
        facecam_rect: Optional[Dict[str, int]] = None,
        gameplay_rect: Optional[Dict[str, int]] = None,
        stacked_facecam_height: int = 720,
    ) -> Dict[str, Any]:
        now = time.time()
        fc_json = json.dumps(facecam_rect) if facecam_rect else None
        gp_json = json.dumps(gameplay_rect) if gameplay_rect else None
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO channel_presets
                (channel_id, name, layout_mode, facecam_rect_json, gameplay_rect_json, stacked_facecam_height, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (channel_id, name, layout_mode, fc_json, gp_json, int(stacked_facecam_height), now),
            )
        return {
            "channel_id": channel_id,
            "name": name,
            "layout_mode": layout_mode,
            "facecam_rect": facecam_rect,
            "gameplay_rect": gameplay_rect,
            "stacked_facecam_height": int(stacked_facecam_height),
            "updated_at": now,
        }

    def get_channel_preset(self, channel_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM channel_presets WHERE channel_id = ?", (channel_id,)).fetchone()
            if not row:
                return None
            return self._row_to_preset(row)

    def list_channel_presets(self) -> List[Dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute("SELECT * FROM channel_presets ORDER BY updated_at DESC").fetchall()
            return [self._row_to_preset(r) for r in rows]

    def _row_to_preset(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "channel_id": row["channel_id"],
            "name": row["name"],
            "layout_mode": row["layout_mode"],
            "facecam_rect": json.loads(row["facecam_rect_json"]) if row["facecam_rect_json"] else None,
            "gameplay_rect": json.loads(row["gameplay_rect_json"]) if row["gameplay_rect_json"] else None,
            "stacked_facecam_height": int(row["stacked_facecam_height"]),
            "updated_at": float(row["updated_at"]),
        }

    # --- Resultados e Receita ---

    def save_clip_result(
        self,
        clip_filename: str,
        platform: str,
        post_url: Optional[str] = None,
        views_24h: int = 0,
        views_7d: int = 0,
        revenue_brl: float = 0.0,
        layout_mode: Optional[str] = None,
        subtitle_preset: Optional[str] = None,
        channel_id: Optional[str] = None,
        notes: Optional[str] = None,
        result_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        rid = result_id or f"res_{uuid.uuid4().hex[:10]}"
        now = time.time()
        with self._lock, self.connect() as conn:
            existing = conn.execute("SELECT created_at FROM clip_results WHERE result_id = ?", (rid,)).fetchone()
            created_at = float(existing["created_at"]) if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO clip_results (
                    result_id, clip_filename, platform, post_url, views_24h, views_7d,
                    revenue_brl, layout_mode, subtitle_preset, channel_id, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid, clip_filename, platform.lower(), post_url, int(views_24h), int(views_7d),
                    float(revenue_brl), layout_mode, subtitle_preset, channel_id, notes,
                    created_at, now,
                ),
            )
            row = conn.execute("SELECT * FROM clip_results WHERE result_id = ?", (rid,)).fetchone()
            return dict(row)

    def list_clip_results(self) -> List[Dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute("SELECT * FROM clip_results ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]

    def get_results_summary(self) -> Dict[str, Any]:
        with self._lock, self.connect() as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*) AS total_posts,
                       COALESCE(SUM(views_24h), 0) AS total_views_24h,
                       COALESCE(SUM(views_7d), 0) AS total_views_7d,
                       COALESCE(SUM(revenue_brl), 0.0) AS total_revenue_brl
                FROM clip_results
                """
            ).fetchone()
            by_platform = conn.execute(
                """
                SELECT platform, COUNT(*) AS posts,
                       COALESCE(SUM(views_7d), 0) AS views_7d,
                       COALESCE(SUM(revenue_brl), 0.0) AS revenue_brl
                FROM clip_results
                GROUP BY platform
                ORDER BY revenue_brl DESC, views_7d DESC
                """
            ).fetchall()
            return {
                "totals": dict(totals),
                "by_platform": [dict(r) for r in by_platform],
            }

    # --- Fontes ---

    def save_source(
        self,
        project_id: str,
        title: str,
        origin_type: str,
        file_path: str,
        file_sha256: str,
        authorization_ref: str,
        origin_url: Optional[str] = None,
        duration_s: Optional[float] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        has_audio: bool = True,
        source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        sid = source_id or f"src_{uuid.uuid4().hex[:10]}"
        now = time.time()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sources (
                    source_id, project_id, title, origin_type, origin_url,
                    file_path, file_sha256, authorization_ref, duration_s,
                    width, height, has_audio, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid, project_id, title, origin_type, origin_url,
                    file_path, file_sha256, authorization_ref, duration_s,
                    width, height, 1 if has_audio else 0, now,
                ),
            )
            row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (sid,)).fetchone()
            return dict(row)

    def get_source(self, source_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
            return dict(row) if row else None

    def list_sources(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock, self.connect() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM sources WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM sources ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    # --- Cache de Transcrições ---

    def get_transcription(self, file_sha256: str) -> Optional[Dict[str, Any]]:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM transcriptions WHERE file_sha256 = ?", (file_sha256,)).fetchone()
            if not row:
                return None
            data = dict(row)
            data["words"] = json.loads(data.pop("words_json"))
            return data

    def save_transcription(
        self,
        file_sha256: str,
        language: str,
        model_name: str,
        duration_s: float,
        words: List[Dict[str, Any]],
        full_text: str,
    ) -> Dict[str, Any]:
        now = time.time()
        words_json = json.dumps(words, ensure_ascii=False)
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO transcriptions
                (file_sha256, language, model_name, duration_s, words_json, full_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (file_sha256, language, model_name, duration_s, words_json, full_text, now),
            )
        return {
            "file_sha256": file_sha256,
            "language": language,
            "model_name": model_name,
            "duration_s": duration_s,
            "words": words,
            "full_text": full_text,
            "created_at": now,
        }


class SQLiteTaskQueue:
    """Fila de tarefas durável em SQLite com leasing atômico e recuperação após reinício."""

    def __init__(self, store: SQLiteStore):
        self.store = store
        self._lock = threading.Lock()

    def _row_to_job(self, row: sqlite3.Row) -> QueueJob:
        return QueueJob(
            job_id=row["job_id"],
            org_id=row["org_id"],
            job_type=row["job_type"],
            idempotency_key=row["idempotency_key"],
            params=json.loads(row["params_json"]),
            status=JobStatus(row["status"]),
            progress_percent=float(row["progress_percent"]),
            step_description=row["step_description"],
            eta_seconds=row["eta_seconds"],
            worker_id=row["worker_id"],
            leased_until=row["leased_until"],
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def enqueue(
        self,
        org_id: str,
        job_type: str,
        params: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
    ) -> QueueJob:
        if not idempotency_key:
            dados = json.dumps({"org": org_id, "type": job_type, "params": params}, sort_keys=True)
            idempotency_key = hashlib.sha256(dados.encode("utf-8")).hexdigest()

        now = time.time()
        with self._lock, self.store.connect() as conn:
            existente = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existente:
                return self._row_to_job(existente)

            job_id = f"job_{uuid.uuid4().hex[:10]}"
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, org_id, job_type, idempotency_key, params_json,
                    status, progress_percent, step_description, eta_seconds,
                    worker_id, leased_until, attempts, max_attempts,
                    result_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, org_id, job_type, idempotency_key, json.dumps(params, ensure_ascii=False),
                    JobStatus.CRIADO.value, 0.0, "Aguardando processamento", None,
                    None, None, 0, max_attempts,
                    None, None, now, now,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(row)

    def lease_next_job(
        self,
        worker_id: str,
        job_types: Optional[List[str]] = None,
        lease_duration_s: float = 60.0,
    ) -> Optional[QueueJob]:
        now = time.time()
        with self._lock, self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                   OR (status IN (?, ?) AND leased_until IS NOT NULL AND leased_until < ? AND attempts < max_attempts)
                ORDER BY created_at ASC
                """,
                (JobStatus.CRIADO.value, JobStatus.ANALISANDO.value, JobStatus.RENDERIZANDO.value, now),
            ).fetchall()

            target = None
            for row in rows:
                if job_types and row["job_type"] not in job_types:
                    continue
                target = row
                break

            if not target:
                conn.rollback()
                return None

            new_status = JobStatus.RENDERIZANDO.value if "render" in target["job_type"] else JobStatus.ANALISANDO.value
            conn.execute(
                """
                UPDATE jobs
                SET worker_id = ?, leased_until = ?, attempts = attempts + 1,
                    status = ?, step_description = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (worker_id, now + lease_duration_s, new_status, f"Assumido pelo worker {worker_id}", now, target["job_id"]),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (target["job_id"],)).fetchone()
            return self._row_to_job(updated)

    def lease_job(
        self,
        job_id: str,
        worker_id: str,
        lease_duration_s: float = 60.0,
    ) -> Optional[QueueJob]:
        now = time.time()
        with self._lock, self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE job_id = ?
                  AND (status = ?
                       OR (status IN (?, ?) AND leased_until IS NOT NULL AND leased_until < ? AND attempts < max_attempts))
                """,
                (job_id, JobStatus.CRIADO.value, JobStatus.ANALISANDO.value, JobStatus.RENDERIZANDO.value, now),
            ).fetchone()
            if not row:
                conn.rollback()
                return None

            new_status = JobStatus.RENDERIZANDO.value if "render" in row["job_type"] else JobStatus.ANALISANDO.value
            conn.execute(
                """
                UPDATE jobs
                SET worker_id = ?, leased_until = ?, attempts = attempts + 1,
                    status = ?, step_description = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (worker_id, now + lease_duration_s, new_status, f"Assumido pelo worker {worker_id}", now, job_id),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated)

    def cancel_job(self, job_id: str, reason: str = "Cancelado pelo usuário") -> Optional[QueueJob]:
        now = time.time()
        with self._lock, self.store.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, step_description = ?, error = ?, leased_until = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (JobStatus.CANCELADO.value, reason, reason, now, job_id),
            )
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def update_progress(
        self,
        job_id: str,
        worker_id: str,
        progress_percent: float,
        step_description: str,
        eta_seconds: Optional[float] = None,
    ) -> None:
        now = time.time()
        pct = round(min(100.0, max(0.0, float(progress_percent))), 2)
        with self._lock, self.store.connect() as conn:
            row = conn.execute("SELECT worker_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row or row["worker_id"] != worker_id:
                raise PermissionError("Worker não possui a concessão ativa desta tarefa.")
            conn.execute(
                "UPDATE jobs SET progress_percent = ?, step_description = ?, eta_seconds = ?, updated_at = ? WHERE job_id = ?",
                (pct, step_description, eta_seconds, now, job_id),
            )

    def complete_job(
        self,
        job_id: str,
        worker_id: str,
        result: Dict[str, Any],
        final_status: JobStatus = JobStatus.APROVADO,
    ) -> QueueJob:
        now = time.time()
        with self._lock, self.store.connect() as conn:
            row = conn.execute("SELECT worker_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row or row["worker_id"] != worker_id:
                raise PermissionError("Worker não possui a concessão ativa desta tarefa.")
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, progress_percent = 100.0, step_description = 'Concluído com sucesso',
                    result_json = ?, leased_until = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (final_status.value, json.dumps(result, ensure_ascii=False), now, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated)

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error_message: str,
        retryable: bool = True,
    ) -> QueueJob:
        now = time.time()
        with self._lock, self.store.connect() as conn:
            row = conn.execute("SELECT worker_id, attempts, max_attempts FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row or row["worker_id"] != worker_id:
                raise PermissionError("Worker não possui a concessão ativa desta tarefa.")

            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if retryable and attempts < max_attempts:
                new_status = JobStatus.CRIADO.value
                desc = f"Tentativa {attempts} falhou: {error_message}. Aguardando retry."
            else:
                new_status = JobStatus.FALHOU.value
                if not retryable:
                    desc = f"Falha não recuperável: {error_message}"
                else:
                    desc = f"Falha definitiva após {attempts} tentativas: {error_message}"

            conn.execute(
                """
                UPDATE jobs
                SET status = ?, step_description = ?, error = ?, leased_until = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (new_status, desc, error_message, now, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated)

    def get_job(self, job_id: str) -> Optional[QueueJob]:
        with self._lock, self.connect_ro() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def connect_ro(self) -> sqlite3.Connection:
        return self.store.connect()

    def clear_all(self) -> None:
        with self._lock, self.store.connect() as conn:
            conn.execute("DELETE FROM jobs")
