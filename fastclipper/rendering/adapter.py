"""
Adaptador de renderização vertical e validação estrita com FFprobe e decodificação total.

Renderiza o clipe aprovado a partir dos segmentos do buffer, mantendo áudio sincronizado,
resolução 1080x1920 (9:16) e registro de versão. Qualquer falha técnica ou de enquadramento
emite 'requer_revisao', nunca sucesso silencioso.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastclipper.ingest.buffer import SegmentedBuffer
from fastclipper.ingest.marker import CandidateClip, CandidateStatus
from fastclipper.layouts.config import ChannelLayoutConfig, LayoutMode
from fastclipper.layouts.engine import LayoutEngine
from fastclipper.layouts.scene_detector import SceneDetector
from fastclipper.rendering.encoder import HardwareEncoderManager, SOFTWARE_ENCODER
from fastclipper.rendering.render_core import render_vertical_ffmpeg

logger = logging.getLogger(__name__)


class RenderResult:
    def __init__(
        self,
        ok: bool,
        status: CandidateStatus,
        output_path: Optional[str] = None,
        duration: float = 0.0,
        errors: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.ok = ok
        self.status = status
        self.output_path = output_path
        self.duration = duration
        self.errors = errors or []
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status.value,
            "output_path": self.output_path,
            "duration": self.duration,
            "errors": self.errors,
            "metadata": self.metadata,
        }


class IsolatedRenderAdapter:
    """Adaptador de renderização desacoplado para exportação 9:16 e verificação estrita."""

    def __init__(
        self,
        output_dir: str | Path,
        ffmpeg_bin: Optional[str] = None,
        ffprobe_bin: Optional[str] = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_bin = ffprobe_bin or shutil.which("ffprobe") or "ffprobe"
        self.encoder_manager = HardwareEncoderManager(ffmpeg_bin=self.ffmpeg_bin)

    def render_candidate(
        self,
        candidate: CandidateClip,
        buffer: SegmentedBuffer,
        source_fallback_path: Optional[str | Path] = None,
        require_audio: bool = True,
        layout_config: Optional[ChannelLayoutConfig] = None,
        check_scene_changes: bool = True,
    ) -> RenderResult:
        """Renderiza um candidato aprovado em 1080x1920 (9:16) com validação completa."""
        nome_arquivo = f"{candidate.id}_v{candidate.versao}_9x16.mp4"
        caminho_saida = self.output_dir / nome_arquivo

        duracao_esperada = round(candidate.fim_s - candidate.inicio_s, 2)
        if duracao_esperada <= 0:
            return RenderResult(
                ok=False,
                status=CandidateStatus.REQUER_REVISAO,
                errors=["Duração calculada inválida (menor ou igual a zero)."],
            )

        segmentos = buffer.get_segments_in_range(candidate.inicio_s, candidate.fim_s)
        
        # Cria arquivo de lista de concatenação se houver múltiplos segmentos no buffer
        t0 = time.time()
        sucesso_render = False

        if segmentos and len(segmentos) > 0 and (buffer.buffer_dir / segmentos[0].filename).exists():
            sucesso_render = self._render_via_buffer_segments(
                segmentos=segmentos,
                buffer_dir=buffer.buffer_dir,
                candidate=candidate,
                output_path=caminho_saida,
                layout_config=layout_config,
            )

        # Fallback para o arquivo fonte original se os segmentos ainda não foram persistidos individualmente
        if not sucesso_render and source_fallback_path and Path(source_fallback_path).is_file():
            sucesso_render = self._render_via_source(
                source_path=Path(source_fallback_path),
                start_s=candidate.inicio_s,
                duration_s=duracao_esperada,
                output_path=caminho_saida,
                layout_config=layout_config,
            )

        if not sucesso_render:
            return RenderResult(
                ok=False,
                status=CandidateStatus.FALHA,
                errors=["Falha ao executar o comando de renderização no FFmpeg."],
            )

        # Validação estrita via FFprobe e decodificação total
        validacao = self.validate_media_ffprobe(
            caminho_saida,
            duracao_esperada=duracao_esperada,
            largura_esperada=1080,
            altura_esperada=1920,
            require_audio=require_audio,
        )

        tempo_total = round(time.time() - t0, 3)

        if not validacao["valid"]:
            logger.warning("Validação estrita falhou para o clipe %s: %s", candidate.id, validacao["errors"])
            return RenderResult(
                ok=False,
                status=CandidateStatus.REQUER_REVISAO,
                output_path=str(caminho_saida),
                duration=validacao.get("duration", 0.0),
                errors=validacao["errors"],
                metadata={"render_time_s": tempo_total, "versao": candidate.versao},
            )

        # Verificação de estabilidade de cena para layout fixo
        if check_scene_changes and source_fallback_path and Path(source_fallback_path).is_file():
            detector = SceneDetector(ffmpeg_bin=self.ffmpeg_bin)
            tem_facecam = layout_config is not None and layout_config.facecam_rect is not None
            estabilidade = detector.validate_layout_stability(
                video_path=source_fallback_path,
                start_s=candidate.inicio_s,
                duration_s=duracao_esperada,
                has_facecam=tem_facecam,
            )
            if estabilidade["requires_review"]:
                logger.info("Troca de cena detectada em clipe com facecam: marcando como requer_revisao")
                return RenderResult(
                    ok=True,
                    status=CandidateStatus.REQUER_REVISAO,
                    output_path=str(caminho_saida),
                    duration=validacao["duration"],
                    errors=[estabilidade["reason"]],
                    metadata={
                        "render_time_s": tempo_total,
                        "versao": candidate.versao,
                        "scene_cuts": estabilidade["cuts"],
                    },
                )

        return RenderResult(
            ok=True,
            status=CandidateStatus.CONCLUIDO,
            output_path=str(caminho_saida),
            duration=validacao["duration"],
            metadata={
                "render_time_s": tempo_total,
                "versao": candidate.versao,
                "streams": validacao.get("streams", {}),
            },
        )

    def _render_via_source(
        self,
        source_path: Path,
        start_s: float,
        duration_s: float,
        output_path: Path,
        layout_config: Optional[ChannelLayoutConfig] = None,
    ) -> bool:
        """Renderiza corte 9:16 via núcleo unificado render_core."""
        return render_vertical_ffmpeg(
            output_path=output_path,
            start_s=start_s,
            duration_s=duration_s,
            ffmpeg_bin=self.ffmpeg_bin,
            ffprobe_bin=self.ffprobe_bin,
            encoder_manager=self.encoder_manager,
            source_path=source_path,
            layout_config=layout_config,
        )

    def _render_via_buffer_segments(
        self,
        segmentos: list,
        buffer_dir: Path,
        candidate: CandidateClip,
        output_path: Path,
        layout_config: Optional[ChannelLayoutConfig] = None,
    ) -> bool:
        concat_file = output_path.with_suffix(".concat.txt")
        try:
            linhas = []
            for seg in segmentos:
                caminho_seg = (buffer_dir / seg.filename).resolve()
                if caminho_seg.is_file():
                    linhas.append(f"file '{caminho_seg.as_posix()}'")

            if not linhas:
                return False

            concat_file.write_text("\n".join(linhas), encoding="utf-8")

            # Offset inicial relativo ao primeiro segmento da lista
            offset_inicio = max(0.0, candidate.inicio_s - segmentos[0].timestamp_inicio)
            duracao_total = candidate.fim_s - candidate.inicio_s

            layout_engine = LayoutEngine(ffmpeg_bin=self.ffmpeg_bin, ffprobe_bin=self.ffprobe_bin)
            primeiro_seg = buffer_dir / segmentos[0].filename
            source_w, source_h = layout_engine.get_source_dimensions(primeiro_seg)

            cmd = [
                self.ffmpeg_bin,
                "-loglevel", "error",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                "-ss", str(offset_inicio),
                "-t", str(duracao_total),
            ]

            if layout_config and layout_config.effective_mode() == LayoutMode.STACKED and layout_config.facecam_rect is not None:
                filtro_complexo = layout_engine.build_filter_graph(layout_config, source_w, source_h)
                cmd.extend([
                    "-filter_complex", filtro_complexo,
                    "-map", "[outv]",
                    "-map", "0:a?",
                ])
            else:
                filtro_v = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
                if layout_config:
                    filtro_v = layout_engine.build_filter_graph(layout_config, source_w, source_h)
                cmd.extend(["-vf", filtro_v])

            enc = self.encoder_manager.get_encoder()
            enc_args = self.encoder_manager.get_encoder_args(enc, width=1080, height=1920)

            full_cmd = list(cmd) + enc_args + ["-c:a", "aac", "-b:a", "128k", str(output_path)]
            res = subprocess.run(full_cmd, capture_output=True, text=True)

            if res.returncode != 0 and enc != SOFTWARE_ENCODER:
                logger.warning("Hardware encoder '%s' falhou no buffer. Recorrendo ao %s.", enc, SOFTWARE_ENCODER)
                self.encoder_manager.mark_broken(enc)
                sw_args = self.encoder_manager.get_encoder_args(SOFTWARE_ENCODER, width=1080, height=1920)
                full_cmd = list(cmd) + sw_args + ["-c:a", "aac", "-b:a", "128k", str(output_path)]
                res = subprocess.run(full_cmd, capture_output=True, text=True)

            return res.returncode == 0
        finally:
            if concat_file.exists():
                try:
                    concat_file.unlink()
                except OSError:
                    pass

    def validate_media_ffprobe(
        self,
        caminho_arquivo: str | Path,
        duracao_esperada: Optional[float] = None,
        largura_esperada: int = 1080,
        altura_esperada: int = 1920,
        require_audio: bool = True,
        tolerancia_duracao_s: float = 0.5,
    ) -> Dict[str, Any]:
        """Validação estrita com FFprobe e decodificação total do arquivo gerado."""
        arquivo = Path(caminho_arquivo)
        if not arquivo.is_file() or arquivo.stat().st_size == 0:
            return {"valid": False, "errors": ["Arquivo de saída não existe ou está vazio."]}

        cmd = [
            self.ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json",
            str(arquivo),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            return {"valid": False, "errors": [f"FFprobe falhou ao inspecionar o arquivo: {res.stderr}"]}

        try:
            info = json.loads(res.stdout)
        except json.JSONDecodeError:
            return {"valid": False, "errors": ["Saída do FFprobe não é um JSON válido."]}

        erros: List[str] = []
        duracao_real = float(info.get("format", {}).get("duration", 0.0))
        streams = info.get("streams", [])

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        if not video_stream:
            erros.append("Arquivo não contém fluxo de vídeo.")
        else:
            w = int(video_stream.get("width", 0))
            h = int(video_stream.get("height", 0))
            if w != largura_esperada or h != altura_esperada:
                erros.append(f"Resolução incorreta: esperada {largura_esperada}x{altura_esperada}, obtida {w}x{h}.")

        if require_audio and not audio_stream:
            erros.append("Fluxo de áudio ausente no clipe renderizado.")

        if duracao_esperada is not None:
            delta = abs(duracao_real - duracao_esperada)
            if delta > tolerancia_duracao_s:
                erros.append(
                    f"Duração fora da tolerância: esperada {duracao_esperada}s, obtida {duracao_real}s (delta={delta:.2f}s)."
                )

        # Teste de decodificação completa para garantir que o MP4 não possui frames corrompidos
        decode_cmd = [
            self.ffmpeg_bin,
            "-v", "error",
            "-i", str(arquivo),
            "-f", "null",
            "-",
        ]
        decode_res = subprocess.run(decode_cmd, capture_output=True, text=True, timeout=180)
        if decode_res.returncode != 0:
            erros.append(f"Decodificação completa falhou com erros de stream: {decode_res.stderr.strip()}")

        return {
            "valid": len(erros) == 0,
            "duration": duracao_real,
            "errors": erros,
            "streams": {"video": video_stream is not None, "audio": audio_stream is not None},
        }
