"""
Ciclo do Piloto Automático.

Para cada vídeo novo (watchlist ou pasta de entrada):
  importar/baixar → transcrever → sugerir momentos → renderizar em lote → gerar texto → empacotar
  → registrar candidato 'pendente' na fila de aprovação.

Nada é publicado aqui. Publicação só acontece quando um humano aprova (publisher.py).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastclipper.autopilot.config import AutopilotConfig, WatchSource
from fastclipper.autopilot.discovery import (
    VideoCandidate,
    list_inbox,
    list_source_videos,
    resolve_duration,
)
from fastclipper.autopilot.state import AutopilotState
from fastclipper.autopilot.studio_client import StudioClient, StudioError

logger = logging.getLogger("fastclipper.autopilot")

DONE_STATUSES = {"aprovado", "publicado", "agendado", "aguardando_revisao"}
FAIL_STATUSES = {"falhou", "cancelado"}


class AutopilotRunner:
    def __init__(
        self,
        config: AutopilotConfig,
        client: Optional[StudioClient] = None,
        state: Optional[AutopilotState] = None,
        lister: Optional[Callable[..., List[VideoCandidate]]] = None,
        duration_fn: Optional[Callable[[VideoCandidate], Optional[float]]] = None,
        poll_interval_s: float = 3.0,
        render_timeout_s: float = 3600.0,
    ):
        self.cfg = config
        self.client = client or StudioClient(config.studio_url)
        self.state = state or AutopilotState(config.db_path)
        self.lister = lister or list_source_videos
        self.duration_fn = duration_fn or resolve_duration
        self.poll_interval_s = poll_interval_s
        self.render_timeout_s = render_timeout_s

    # ------------------------------------------------------------------ ciclo
    def run_once(self) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"processed": [], "skipped": [], "errors": [], "new_candidates": 0}
        if not self.client.health():
            raise StudioError(f"Studio fora do ar em {self.cfg.studio_url}. Rode o iniciar.bat.")

        queue: List[tuple] = []  # (VideoCandidate, source_name, auth_ref, layout)
        for cand in list_inbox(self.cfg.inbox_dir):
            if not self.state.is_seen(cand.key):
                queue.append((cand, "pasta de entrada", self.cfg.inbox_authorization_ref, None))

        for src in self.cfg.watchlist:
            if not src.enabled:
                continue
            try:
                queue.extend(self._discover_source(src))
            except Exception as exc:
                logger.warning("Falha ao listar %s: %s", src.name, exc)
                summary["errors"].append({"source": src.name, "error": str(exc)})

        for cand, source_name, auth_ref, layout in queue:
            try:
                n = self.process_video(cand, source_name, auth_ref, layout)
                summary["processed"].append({"title": cand.title, "clips": n})
                summary["new_candidates"] += n
            except Exception as exc:
                logger.exception("Falha processando %s", cand.title)
                self.state.mark_video(cand.key, "falhou", source_name, cand.url, cand.title, str(exc))
                summary["errors"].append({"video": cand.title, "error": str(exc)})
        return summary

    def _discover_source(self, src: WatchSource) -> List[tuple]:
        videos = self.lister(src.url, limit=6)
        first_time = not self.state.source_has_history(src.name)
        picked: List[tuple] = []
        for idx, v in enumerate(videos):
            if self.state.is_seen(v.key):
                continue
            if first_time and idx >= self.cfg.max_new_per_source:
                self.state.mark_video(v.key, "baseline", src.name, v.url, v.title, "histórico anterior à watchlist")
                continue
            if len(picked) >= self.cfg.max_new_per_source:
                break  # o restante fica para o próximo ciclo
            dur = self.duration_fn(v)
            if dur is None:
                continue  # live em andamento ou sem duração: tenta de novo no próximo ciclo
            if dur > self.cfg.max_video_minutes * 60:
                self.state.mark_video(
                    v.key, "ignorado", src.name, v.url, v.title,
                    f"{dur/60:.0f} min > limite {self.cfg.max_video_minutes:.0f} min",
                )
                continue
            v.duration_s = dur
            picked.append((v, src.name, src.authorization_ref, src.layout))
        return picked

    # ------------------------------------------------------------ um vídeo
    def process_video(
        self, cand: VideoCandidate, source_name: str, auth_ref: str, layout: Optional[str] = None
    ) -> int:
        c = self.client
        self.state.mark_video(cand.key, "processando", source_name, cand.url, cand.title)
        logger.info("▶ %s (%s)", cand.title, source_name)

        if cand.is_local:
            src = c.post("/api/v1/studio/sources/import", {
                "video_path": str(cand.local_path), "authorization_ref": auth_ref, "title": cand.title,
            })
        else:
            src = c.post("/api/v1/studio/sources/download", {
                "url": cand.url, "authorization_ref": auth_ref, "title": cand.title,
            })
        video_path = src["file_path"]

        words: Optional[List[Dict[str, Any]]] = None
        transcript_warning = ""
        try:
            tr = c.post("/api/v1/studio/transcribe", {"source_id": src["source_id"], "language": self.cfg.language})
            words = tr.get("words") or None
        except StudioError as exc:
            transcript_warning = "sem transcrição: corte só por áudio, sem legenda"
            logger.warning("%s (%s)", transcript_warning, exc)

        sug = c.post("/api/v1/studio/suggest-moments", {
            "video_path": video_path,
            "words": words,
            "target_duration_s": self.cfg.clip_seconds,
            "top_k": self.cfg.clips_per_video,
        })
        moments = [m for m in sug.get("suggestions", []) if float(m.get("score", 0)) >= self.cfg.min_score]
        if not moments:
            self.state.mark_video(cand.key, "concluido", source_name, cand.url, cand.title, "nenhum momento acima do score mínimo")
            return 0

        copies = []
        for m in moments:
            text = m.get("preview_text") or cand.title
            copies.append(c.post("/api/v1/studio/generate-copy", {
                "transcript_text": text, "platform": "tiktok", "channel_name": source_name,
            }))

        batch = c.post("/api/v1/studio/render-batch", {
            "video_path": video_path,
            "items": [
                {"inicio_s": m["inicio_s"], "fim_s": m["fim_s"], "hook_title": cp.get("hook")}
                for m, cp in zip(moments, copies)
            ],
            "layout": layout or self.cfg.layout,
            "words": words,
            "subtitles_are_absolute": True,
            "subtitle_preset": self.cfg.subtitle_preset,
            "burn_subtitles": bool(words),
            "normalize_audio": bool(self.cfg.normalize_audio),
            "watermark_text": self.cfg.page_handle or None,
        })
        jobs = self._wait_jobs(batch.get("job_ids", []))

        created = 0
        for m, cp, job in zip(moments, copies, jobs):
            if not job or job.get("status") in FAIL_STATUSES:
                logger.warning("Render falhou para %.1f–%.1fs: %s", m["inicio_s"], m["fim_s"], (job or {}).get("error"))
                continue
            out_path = ((job.get("result") or {}).get("output_video_path")) or ""
            if not out_path or not Path(out_path).is_file():
                continue
            caption = self._caption(cp, source_name)
            clip_words = [w for w in (words or []) if m["inicio_s"] <= float(w.get("start_s", -1)) <= m["fim_s"]]
            pkg = None
            try:
                pkg = c.post("/api/v1/studio/export-package", {
                    "clip_filename": Path(out_path).name,
                    "platform": "tiktok",
                    "title": cp.get("title") or cp.get("hook") or cand.title,
                    "description": caption,
                    "hashtags": cp.get("hashtags", []),
                    "words": clip_words or None,
                    "inicio_s": m["inicio_s"],
                    "fim_s": m["fim_s"],
                })
            except StudioError as exc:
                logger.warning("Pacote não gerado (%s); o MP4 segue para aprovação.", exc)
            reason = m.get("reason") or ""
            if transcript_warning:
                reason = f"{reason} · {transcript_warning}"
            self.state.add_candidate(
                video_key=cand.key,
                source_name=source_name,
                source_url=cand.url,
                clip_path=out_path,
                package_zip=(pkg or {}).get("zip_file"),
                inicio_s=m["inicio_s"],
                fim_s=m["fim_s"],
                score=m.get("score"),
                reason=reason,
                hook=cp.get("hook"),
                caption=caption,
                hashtags=cp.get("hashtags", []),
            )
            created += 1

        self.state.mark_video(cand.key, "concluido", source_name, cand.url, cand.title, f"{created} clipes na fila")
        logger.info("✔ %s → %d clipes aguardando aprovação", cand.title, created)
        return created

    def _caption(self, cp: Dict[str, Any], source_name: str) -> str:
        hook = (cp.get("hook") or "").strip()
        credit = f"🎥 {source_name}" if source_name and source_name != "pasta de entrada" else ""
        return "\n\n".join(x for x in (hook, credit) if x)

    def _wait_jobs(self, job_ids: List[str]) -> List[Optional[Dict[str, Any]]]:
        deadline = time.time() + self.render_timeout_s
        results: Dict[str, Optional[Dict[str, Any]]] = {j: None for j in job_ids}
        while time.time() < deadline:
            pending = False
            for jid in job_ids:
                if results[jid] and results[jid]["status"] in DONE_STATUSES | FAIL_STATUSES:
                    continue
                try:
                    results[jid] = self.client.get(f"/api/v1/queue/jobs/{jid}")
                except StudioError:
                    results[jid] = None
                st = (results[jid] or {}).get("status")
                if st not in DONE_STATUSES | FAIL_STATUSES:
                    pending = True
            if not pending:
                break
            time.sleep(self.poll_interval_s)
        return [results[j] for j in job_ids]
