"""
Executor contínuo de pipeline de renderização e processamento em background.

Conecta a Fila Durável (DurableTaskQueue) aos motores de mídia:
1. Validação estrita de integridade via MediaPrechecker (inspirado no AutoClip)
2. Enquadramento e composição vertical 9:16 (Solo / Stacked com proteção de Facecam e Gameplay)
3. Geração e queima de legendas animadas (ASS / SRT) com proteção anti-blink
4. Validação final via FFprobe e decodificação total do arquivo gerado
5. Atualização atômica de progresso, leasing e resultado do job na fila
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastclipper.ingest.buffer import SegmentedBuffer
from fastclipper.ingest.marker import CandidateClip, CandidateStatus
from fastclipper.ingest.precheck import MediaPrechecker
from fastclipper.layouts.config import ChannelLayoutConfig, LayoutMode, Rectangle
from fastclipper.layouts.engine import LayoutEngine
from fastclipper.multicreator.models import ComparisonMode, CreatorReactionTrack, CrossCreatorEvent
from fastclipper.multicreator.renderer import ComparisonRenderer
from fastclipper.queue.durable_queue import DurableTaskQueue, JobStatus, QueueJob
from fastclipper.rendering.adapter import IsolatedRenderAdapter, RenderResult
from fastclipper.rendering.encoder import HardwareEncoderManager, SOFTWARE_ENCODER
from fastclipper.rendering.render_core import render_vertical_ffmpeg
from fastclipper.subtitles.engine import SubtitleEngine
from fastclipper.subtitles.models import CaptionCue, CaptionPreset, WordTiming

logger = logging.getLogger(__name__)


@dataclass
class JobExecutionResult:
    job_id: str
    success: bool
    output_video_path: Optional[str] = None
    subtitles_path: Optional[str] = None
    duration: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PipelineRunner:
    """Worker de mídia autônomo que processa tarefas da fila durável."""

    def __init__(
        self,
        queue: Optional[DurableTaskQueue] = None,
        output_dir: Optional[str | Path] = None,
        worker_id: str = "worker-01",
        ffmpeg_bin: Optional[str] = None,
        ffprobe_bin: Optional[str] = None,
    ):
        self.queue = queue or DurableTaskQueue()
        self.output_dir = Path(output_dir or Path.cwd() / "data" / "output_clips")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.worker_id = worker_id

        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_bin = ffprobe_bin or shutil.which("ffprobe") or "ffprobe"

        self.prechecker = MediaPrechecker(ffprobe_bin=self.ffprobe_bin, min_duration_s=1.0)
        self.render_adapter = IsolatedRenderAdapter(
            output_dir=self.output_dir,
            ffmpeg_bin=self.ffmpeg_bin,
            ffprobe_bin=self.ffprobe_bin,
        )
        self.subtitle_engine = SubtitleEngine()
        self.comparison_renderer = ComparisonRenderer(
            output_dir=self.output_dir,
            ffmpeg_bin=self.ffmpeg_bin,
            ffprobe_bin=self.ffprobe_bin,
        )
        self.encoder_manager = HardwareEncoderManager(ffmpeg_bin=self.ffmpeg_bin)
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        """Sinaliza cancelamento imediato da renderização e do lote atual."""
        self._cancel_event.set()

    def clear_cancel(self) -> None:
        """Limpa o sinal de cancelamento antes de iniciar novo processamento."""
        self._cancel_event.clear()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def render_clip(self, params: Dict[str, Any], job_id: Optional[str] = None) -> JobExecutionResult:
        """Executa a cadeia de renderização de um clipe (9:16, 16:9 ou ambos) a partir de um vídeo fonte."""
        job_id = job_id or f"clip_{uuid.uuid4().hex[:8]}"
        t0 = time.time()

        aspect_ratio = str(params.get("aspect_ratio", "9:16")).strip().lower()
        if aspect_ratio in ("both", "ambos"):
            res_v = self.render_clip({**params, "aspect_ratio": "9:16"}, job_id=job_id)
            if not res_v.success:
                return res_v
            res_h = self.render_clip(
                {**params, "aspect_ratio": "16:9", "output_filename": f"{job_id}_16x9.mp4"},
                job_id=f"{job_id}_16x9",
            )
            if not res_h.success:
                return res_h
            extra = list(res_v.metadata.get("extra_outputs", []))
            if res_h.output_video_path:
                extra.append({
                    "aspect_ratio": "16:9",
                    "filename": Path(res_h.output_video_path).name,
                    "output_path": res_h.output_video_path,
                })
            res_v.metadata["extra_outputs"] = extra
            res_v.metadata["aspect_ratio"] = "both"
            return res_v

        video_path_str = params.get("video_path")
        if not video_path_str:
            return JobExecutionResult(
                job_id=job_id,
                success=False,
                error="Parâmetro obrigatório 'video_path' ausente.",
            )

        video_path = Path(video_path_str).resolve()
        inicio_s = float(params.get("inicio_s", 0.0))
        fim_s = float(params.get("fim_s", 0.0))
        duracao_planejada = fim_s - inicio_s

        if duracao_planejada <= 0:
            return JobExecutionResult(
                job_id=job_id,
                success=False,
                error=f"Duração inválida do corte: início={inicio_s}s, fim={fim_s}s.",
            )

        # 1. Media Pre-Check (validação antecipada)
        precheck_res = self.prechecker.probe(video_path, require_audio=params.get("require_audio", True))
        if not precheck_res.is_valid:
            return JobExecutionResult(
                job_id=job_id,
                success=False,
                error=f"Mídia de origem reprovada no pré-check: {precheck_res.message}",
                metadata={"precheck": precheck_res.to_dict()},
            )

        # 2. Configuração de Layout (9:16 Solo/Empilhado ou 16:9 Horizontal)
        is_16x9 = aspect_ratio == "16:9"
        target_w, target_h = (1920, 1080) if is_16x9 else (1080, 1920)
        layout_mode_str = params.get("layout_mode", params.get("layout", "solo")).lower()
        mode = LayoutMode.SOLO if is_16x9 else (LayoutMode.STACKED if layout_mode_str == "stacked" else LayoutMode.SOLO)

        fc_rect = None
        if not is_16x9 and "facecam_rect" in params and params["facecam_rect"]:
            r = params["facecam_rect"]
            fc_rect = Rectangle(int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"]))

        gp_rect = None
        if not is_16x9 and "gameplay_rect" in params and params["gameplay_rect"]:
            r = params["gameplay_rect"]
            gp_rect = Rectangle(int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"]))

        layout_config = ChannelLayoutConfig(
            channel_id=params.get("channel_id", "default_channel"),
            mode=mode,
            facecam_rect=fc_rect,
            gameplay_rect=gp_rect,
            stacked_facecam_height=int(params.get("stacked_facecam_height", 720)),
            target_width=target_w,
            target_height=target_h,
        )

        # 3. Geração de Legendas (.ass) se houver palavras/cues
        subtitles_data = params.get("subtitles")
        subtitles_are_absolute = bool(params.get("subtitles_are_absolute", False))
        ass_path: Optional[Path] = None

        if subtitles_data:
            cues = self._parse_subtitles_data(
                subtitles_data,
                inicio_s=inicio_s,
                duracao_s=duracao_planejada,
                subtitles_are_absolute=subtitles_are_absolute,
            )
            if cues:
                preset_name = params.get("subtitle_preset", "viral_bounce")
                try:
                    preset = CaptionPreset(preset_name)
                except ValueError:
                    preset = CaptionPreset.VIRAL_BOUNCE

                ass_content = self.subtitle_engine.generate_ass(
                    cues, preset=preset, play_res_x=target_w, play_res_y=target_h
                )
                ass_path = self.output_dir / f"{job_id}_subs.ass"
                ass_path.write_text(ass_content, encoding="utf-8")

        # 4. Renderização e Queima de Legenda Opcional
        burn_subtitles = params.get("burn_subtitles", True) if ass_path else False
        suffix = "_16x9.mp4" if is_16x9 else "_9x16.mp4"
        output_filename = params.get("output_filename") or f"{job_id}{suffix}"
        caminho_saida = self.output_dir / output_filename

        render_ok = self._render_ffmpeg_direct(
            source_path=video_path,
            start_s=inicio_s,
            duration_s=duracao_planejada,
            output_path=caminho_saida,
            layout_config=layout_config,
            ass_path=ass_path if burn_subtitles else None,
            normalize_audio=bool(params.get("normalize_audio", False)),
            watermark_text=params.get("watermark_text"),
        )

        if not render_ok or not caminho_saida.exists():
            return JobExecutionResult(
                job_id=job_id,
                success=False,
                error="Falha na execução do pipeline FFmpeg de renderização.",
            )

        # 5. Validação Estrita do Clipe Renderizado com FFprobe
        validacao = self.render_adapter.validate_media_ffprobe(
            caminho_arquivo=caminho_saida,
            duracao_esperada=duracao_planejada,
            largura_esperada=target_w,
            altura_esperada=target_h,
            require_audio=params.get("require_audio", True),
        )

        if not validacao["valid"]:
            return JobExecutionResult(
                job_id=job_id,
                success=False,
                output_video_path=str(caminho_saida),
                error=f"Clipe gerado falhou na validação estrita do FFprobe: {validacao['errors']}",
                metadata={"ffprobe_errors": validacao["errors"]},
            )

        tempo_total = round(time.time() - t0, 3)

        return JobExecutionResult(
            job_id=job_id,
            success=True,
            output_video_path=str(caminho_saida),
            subtitles_path=str(ass_path) if ass_path else None,
            duration=validacao["duration"],
            metadata={
                "render_time_s": tempo_total,
                "layout": mode.value,
                "subtitles_burned": burn_subtitles,
                "file_size_bytes": caminho_saida.stat().st_size,
            },
        )

    def process_job(self, job: QueueJob) -> JobExecutionResult:
        """Executa um trabalho assumido da fila atualizando progresso e estados."""
        logger.info("Worker %s iniciando job %s (%s)", self.worker_id, job.job_id, job.job_type)

        try:
            if job.job_type == "render_clip":
                self.queue.update_progress(
                    job_id=job.job_id,
                    worker_id=self.worker_id,
                    progress_percent=15.0,
                    step_description="Validando mídia e parâmetros",
                )

                result = self.render_clip(job.params, job_id=job.job_id)

                if result.success:
                    self.queue.complete_job(
                        job_id=job.job_id,
                        worker_id=self.worker_id,
                        result=result.to_dict(),
                        final_status=JobStatus.APROVADO,
                    )
                    logger.info("Job %s concluído com sucesso em %s", job.job_id, result.output_video_path)
                else:
                    self.queue.fail_job(
                        job_id=job.job_id,
                        worker_id=self.worker_id,
                        error_message=result.error or "Erro desconhecido na renderização",
                    )
                    logger.warning("Job %s falhou: %s", job.job_id, result.error)

                return result

            elif job.job_type == "render_comparison":
                self.queue.update_progress(
                    job_id=job.job_id,
                    worker_id=self.worker_id,
                    progress_percent=20.0,
                    step_description="Sincronizando faixas de criadores",
                )
                result = self._process_comparison_job(job)
                return result

            else:
                msg = f"Tipo de trabalho não suportado: {job.job_type}"
                self.queue.fail_job(job.job_id, self.worker_id, msg)
                return JobExecutionResult(job_id=job.job_id, success=False, error=msg)

        except Exception as e:
            logger.exception("Exceção não tratada durante o processamento do job %s", job.job_id)
            self.queue.fail_job(job.job_id, self.worker_id, str(e))
            return JobExecutionResult(job_id=job.job_id, success=False, error=str(e))

    def run_single_step(self, job_types: Optional[List[str]] = None) -> Optional[JobExecutionResult]:
        """Tenta assumir e processar um único trabalho da fila."""
        job = self.queue.lease_next_job(worker_id=self.worker_id, job_types=job_types)
        if not job:
            return None
        return self.process_job(job)

    def run_worker_loop(
        self,
        max_jobs: Optional[int] = None,
        poll_interval_s: float = 0.5,
        stop_event: Optional[threading.Event] = None,
    ) -> List[JobExecutionResult]:
        """Loop contínuo de consumo da fila com controle de término."""
        results: List[JobExecutionResult] = []
        jobs_processed = 0

        while True:
            if stop_event and stop_event.is_set():
                break

            if max_jobs is not None and jobs_processed >= max_jobs:
                break

            res = self.run_single_step()
            if res:
                results.append(res)
                jobs_processed += 1
            else:
                time.sleep(poll_interval_s)

        return results

    # -------------------------------------------------------------------------
    # Métodos Auxiliares Internos
    # -------------------------------------------------------------------------

    def _render_ffmpeg_direct(
        self,
        source_path: Path,
        start_s: float,
        duration_s: float,
        output_path: Path,
        layout_config: ChannelLayoutConfig,
        ass_path: Optional[Path] = None,
        normalize_audio: bool = False,
        watermark_text: Optional[str] = None,
    ) -> bool:
        """Renderiza via núcleo unificado render_core com suporte a layout 9:16 e legenda ASS."""
        return render_vertical_ffmpeg(
            output_path=output_path,
            start_s=start_s,
            duration_s=duration_s,
            ffmpeg_bin=self.ffmpeg_bin,
            ffprobe_bin=self.ffprobe_bin,
            encoder_manager=self.encoder_manager,
            source_path=source_path,
            layout_config=layout_config,
            ass_path=ass_path,
            cancel_event=self._cancel_event,
            normalize_audio=normalize_audio,
            watermark_text=watermark_text,
        )

    def _parse_subtitles_data(
        self,
        data: Any,
        inicio_s: float = 0.0,
        duracao_s: float = 0.0,
        subtitles_are_absolute: bool = False,
    ) -> List[CaptionCue]:
        """Converte dados brutos de legendas em CaptionCue válidos e relativos ao início do clipe (0s)."""
        if not data:
            return []

        cues: List[CaptionCue] = []

        # Se já for lista de CaptionCue
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], CaptionCue):
            cues = list(data)
        elif isinstance(data, list):
            # Caso 1: lista de palavras individuais {"word": "...", "start_s": ..., "end_s": ...}
            if all("word" in item for item in data if isinstance(item, dict)):
                words = [
                    WordTiming(
                        word=str(item.get("word", "")),
                        start_s=float(item.get("start_s", 0.0)),
                        end_s=float(item.get("end_s", 0.0)),
                    )
                    for item in data
                ]
                cues = self.subtitle_engine.chunk_words(words)
            else:
                # Caso 2: lista de cues estruturadas {"start_s": ..., "end_s": ..., "text": ...}
                for idx, item in enumerate(data):
                    if isinstance(item, dict):
                        cues.append(
                            CaptionCue(
                                cue_id=item.get("cue_id", f"cue_{idx:04d}"),
                                start_s=float(item.get("start_s", 0.0)),
                                end_s=float(item.get("end_s", 0.0)),
                                text=str(item.get("text", "")),
                            )
                        )

        return self._normalize_cues_to_clip(
            cues=cues,
            inicio_s=inicio_s,
            duracao_s=duracao_s,
            subtitles_are_absolute=subtitles_are_absolute,
        )

    def _normalize_cues_to_clip(
        self,
        cues: List[CaptionCue],
        inicio_s: float,
        duracao_s: float,
        subtitles_are_absolute: bool = False,
    ) -> List[CaptionCue]:
        """Garante que os timestamps das legendas sejam relativos ao início do clipe (0.0s)."""
        if not cues or inicio_s <= 0.0:
            return cues

        min_start = min(c.start_s for c in cues)
        should_shift = subtitles_are_absolute or (min_start >= inicio_s)
        if not should_shift:
            return cues

        normalized: List[CaptionCue] = []
        for cue in cues:
            rel_start = cue.start_s - inicio_s
            rel_end = cue.end_s - inicio_s
            if rel_end <= 0.0 or (duracao_s > 0.0 and rel_start >= duracao_s):
                continue

            clamped_start = max(0.0, rel_start)
            clamped_end = min(duracao_s, rel_end) if duracao_s > 0.0 else rel_end
            if clamped_end <= clamped_start:
                continue

            rel_words: List[WordTiming] = []
            for w in cue.words:
                w_start = max(0.0, w.start_s - inicio_s)
                w_end = min(duracao_s, w.end_s - inicio_s) if duracao_s > 0.0 else (w.end_s - inicio_s)
                if w_end > w_start:
                    rel_words.append(WordTiming(word=w.word, start_s=round(w_start, 3), end_s=round(w_end, 3)))

            normalized.append(
                CaptionCue(
                    cue_id=cue.cue_id,
                    start_s=round(clamped_start, 3),
                    end_s=round(clamped_end, 3),
                    text=cue.text,
                    words=rel_words,
                )
            )
        return normalized

    def _process_comparison_job(self, job: QueueJob) -> JobExecutionResult:
        """Processa trabalho de comparação Cross-POV multi-criador."""
        p = job.params
        event_id = p.get("event_id", f"evt_{uuid.uuid4().hex[:6]}")
        mode_str = p.get("mode", "split").lower()
        mode = ComparisonMode.SPLIT_VERTICAL if mode_str == "split" else ComparisonMode.SEQUENTIAL

        tracks: List[CreatorReactionTrack] = []
        for idx, t in enumerate(p.get("tracks", []), 1):
            tracks.append(
                CreatorReactionTrack(
                    track_id=t.get("track_id", f"track_{idx}"),
                    event_id=event_id,
                    creator_name=t["creator_name"],
                    video_path=t["video_path"],
                    stream_offset_s=float(t.get("offset", 0.0)),
                    start_s=float(t.get("start_s", p.get("start_s", 0.0))),
                    end_s=float(t.get("end_s", p.get("end_s", 2.0))),
                    has_authorization=bool(t.get("authorized", True)),
                    authorization_ref=t.get("auth_ref", "AUTH-DEFAULT-2026"),
                    has_spoiler=bool(t.get("spoiler", False)),
                    order=idx,
                )
            )

        event = CrossCreatorEvent(
            event_id=event_id,
            title=p.get("title", f"Evento {event_id}"),
            description=p.get("description", "Comparação de reação multi-criador"),
            tracks=tracks,
        )

        res = self.comparison_renderer.render_event_comparison(
            event=event,
            mode=mode,
            output_filename=p.get("output_filename"),
        )

        is_ok = bool(res.get("ok") or res.get("success"))
        if is_ok:
            self.queue.complete_job(
                job_id=job.job_id,
                worker_id=self.worker_id,
                result=res,
                final_status=JobStatus.APROVADO,
            )
            return JobExecutionResult(
                job_id=job.job_id,
                success=True,
                output_video_path=res.get("output_path"),
                duration=res.get("duration", 0.0),
                metadata=res,
            )
        else:
            err_msg = str(res.get("errors") or res.get("error") or "Falha na comparação")
            self.queue.fail_job(job.job_id, self.worker_id, err_msg)
            return JobExecutionResult(
                job_id=job.job_id,
                success=False,
                error=err_msg,
                metadata=res,
            )
