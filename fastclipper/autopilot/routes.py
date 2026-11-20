"""Rotas da fila de aprovação do Piloto Automático (montadas no app do Studio)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from fastclipper.autopilot.config import AutopilotConfig, default_config_path, load_config
from fastclipper.autopilot.publisher import PublishError, publish_candidate
from fastclipper.autopilot.state import AutopilotState

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Piloto Automático"])

_run_lock = threading.Lock()
_last_run: Dict[str, Any] = {"running": False, "summary": None, "error": None}


def _cfg() -> AutopilotConfig:
    try:
        return load_config()
    except FileNotFoundError:
        return AutopilotConfig()  # sem config ainda: a fila funciona, publicação não


def _state() -> AutopilotState:
    return AutopilotState(_cfg().db_path)


class ApproveRequest(BaseModel):
    caption: Optional[str] = None
    platforms: Optional[List[str]] = None


@router.get("/aprovacao", response_class=HTMLResponse, include_in_schema=False)
def approval_page():
    html = Path(__file__).resolve().parents[1] / "web" / "aprovacao.html"
    return HTMLResponse(html.read_text(encoding="utf-8"))


@router.get("/api/v1/autopilot/status")
def autopilot_status():
    cfg = _cfg()
    st = _state()
    return {
        "config_found": default_config_path().is_file(),
        "config_path": str(default_config_path()),
        "inbox_dir": str(cfg.inbox_dir),
        "watchlist": [{"name": s.name, "url": s.url, "enabled": s.enabled} for s in cfg.watchlist],
        "buffer_ready": bool((cfg.buffer or {}).get("api_key")) and bool((cfg.buffer or {}).get("channels")),
        "media_host_ready": (cfg.media_host or {}).get("type") == "s3",
        "platforms": cfg.platforms,
        "counts": st.counts(),
        "recent_videos": st.list_videos(20),
        "last_run": _last_run,
    }


@router.get("/api/v1/autopilot/candidates")
def list_candidates(status: Optional[str] = Query("pendente")):
    return _state().list_candidates(status=status or None)


@router.get("/api/v1/autopilot/video/{candidate_id}")
def candidate_video(candidate_id: str):
    cand = _state().get_candidate(candidate_id)
    if not cand:
        raise HTTPException(404, "Candidato não encontrado.")
    p = Path(cand["clip_path"])
    if not p.is_file():
        raise HTTPException(404, "Arquivo do clipe não encontrado.")
    return FileResponse(str(p), media_type="video/mp4")


@router.post("/api/v1/autopilot/candidates/{candidate_id}/approve")
def approve(candidate_id: str, req: ApproveRequest):
    cfg = _cfg()
    st = AutopilotState(cfg.db_path)
    cand = st.get_candidate(candidate_id)
    if not cand:
        raise HTTPException(404, "Candidato não encontrado.")
    if cand["status"] not in ("pendente", "aprovado", "falhou"):
        raise HTTPException(409, f"Candidato já está '{cand['status']}'.")
    if req.caption is not None and req.caption.strip():
        cand = st.update_candidate(candidate_id, caption=req.caption.strip()) or cand

    platforms = req.platforms or cfg.platforms
    buffer_ready = bool((cfg.buffer or {}).get("api_key")) and bool((cfg.buffer or {}).get("channels"))
    if not buffer_ready:
        log = {"note": "Buffer não configurado: publique manualmente.", "package_zip": cand.get("package_zip")}
        import json
        return st.update_candidate(candidate_id, status="aprovado", publish_log=json.dumps(log, ensure_ascii=False))

    import json
    try:
        log = publish_candidate(cand, cfg.buffer, cfg.media_host, platforms)
    except PublishError as exc:
        return st.update_candidate(
            candidate_id, status="aprovado",
            publish_log=json.dumps({"error": str(exc)}, ensure_ascii=False),
        )
    ok = any(r.get("ok") for r in log["results"].values())
    return st.update_candidate(
        candidate_id, status="agendado" if ok else "falhou", publish_log=json.dumps(log, ensure_ascii=False)
    )


@router.post("/api/v1/autopilot/candidates/{candidate_id}/reject")
def reject(candidate_id: str):
    st = _state()
    if not st.get_candidate(candidate_id):
        raise HTTPException(404, "Candidato não encontrado.")
    return st.update_candidate(candidate_id, status="rejeitado")


@router.post("/api/v1/autopilot/run-now")
def run_now():
    """Dispara um ciclo em segundo plano (mesmo efeito do autopilot.bat)."""
    if not _run_lock.acquire(blocking=False):
        return {"started": False, "message": "Já existe um ciclo rodando."}

    def _job():
        from fastclipper.autopilot.runner import AutopilotRunner
        try:
            _last_run.update(running=True, error=None)
            _last_run["summary"] = AutopilotRunner(load_config()).run_once()
        except Exception as exc:
            logger.exception("Ciclo do piloto falhou")
            _last_run["error"] = str(exc)
        finally:
            _last_run["running"] = False
            _run_lock.release()

    threading.Thread(target=_job, daemon=True, name="autopilot-run-now").start()
    return {"started": True}
