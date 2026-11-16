"""
Verificação preliminar de mídia (Media Pre-check Guard).

Inspirado e adaptado da engenharia de validação do AutoClip (RC156 / media_precheck.py).
Executa uma sonda estrita via FFprobe antes de qualquer processamento pesado
(transcrição Whisper, inferência ou renderização), evitando desperdício de GPU e
falhas silenciosas downstream.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PrecheckStatus(str, Enum):
    VALID = "valid"
    TOO_SHORT = "source_too_short"
    NO_AUDIO = "source_no_audio"
    NO_VIDEO = "source_no_video"
    CORRUPTED = "source_corrupted"
    FILE_NOT_FOUND = "file_not_found"


@dataclass(frozen=True)
class PrecheckResult:
    status: PrecheckStatus
    is_valid: bool
    duration_s: Optional[float]
    has_audio: bool
    has_video: bool
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float]
    message: str
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "is_valid": self.is_valid,
            "duration_s": self.duration_s,
            "has_audio": self.has_audio,
            "has_video": self.has_video,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "message": self.message,
            "metadata": self.metadata,
        }


class MediaPrechecker:
    """Verificador preliminar de sanidade de arquivos de mídia."""

    def __init__(
        self,
        ffprobe_bin: Optional[str] = None,
        min_duration_s: float = 10.0,
    ):
        self.ffprobe_bin = ffprobe_bin or shutil.which("ffprobe") or "ffprobe"
        self.min_duration_s = float(min_duration_s)

    def probe(self, video_path: str | Path, require_audio: bool = True) -> PrecheckResult:
        """Executa FFprobe e valida se a mídia possui integridade e requisitos mínimos."""
        caminho = Path(video_path)
        if not caminho.is_file():
            return PrecheckResult(
                status=PrecheckStatus.FILE_NOT_FOUND,
                is_valid=False,
                duration_s=None,
                has_audio=False,
                has_video=False,
                width=None,
                height=None,
                fps=None,
                message=f"Arquivo não encontrado: {caminho}",
                metadata={},
            )

        cmd = [
            self.ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
            "-of", "json",
            str(caminho),
        ]

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=30,
            )
            if res.returncode != 0:
                return PrecheckResult(
                    status=PrecheckStatus.CORRUPTED,
                    is_valid=False,
                    duration_s=None,
                    has_audio=False,
                    has_video=False,
                    width=None,
                    height=None,
                    fps=None,
                    message=f"FFprobe falhou ao ler arquivo: {res.stderr.strip()}",
                    metadata={"stderr": res.stderr},
                )
            dados = json.loads(res.stdout or "{}")
        except Exception as err:
            logger.warning("Falha na sonda de mídia: %s", err)
            return PrecheckResult(
                status=PrecheckStatus.CORRUPTED,
                is_valid=False,
                duration_s=None,
                has_audio=False,
                has_video=False,
                width=None,
                height=None,
                fps=None,
                message=f"Erro de I/O ao sondar arquivo: {err}",
                metadata={"error": str(err)},
            )

        streams = dados.get("streams", [])
        formato = dados.get("format", {})

        # Duração
        try:
            duracao = float(formato.get("duration", 0.0))
        except (ValueError, TypeError):
            duracao = 0.0

        v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        has_video = v_stream is not None
        has_audio = a_stream is not None

        width = int(v_stream.get("width")) if v_stream and v_stream.get("width") else None
        height = int(v_stream.get("height")) if v_stream and v_stream.get("height") else None

        fps = None
        if v_stream and v_stream.get("r_frame_rate"):
            try:
                num, den = v_stream["r_frame_rate"].split("/")
                fps = round(float(num) / float(den), 2)
            except Exception:
                fps = None

        # Validação 1: Sem vídeo
        if not has_video:
            return PrecheckResult(
                status=PrecheckStatus.NO_VIDEO,
                is_valid=False,
                duration_s=duracao,
                has_audio=has_audio,
                has_video=False,
                width=None,
                height=None,
                fps=None,
                message="Arquivo não contém stream de vídeo válido.",
                metadata=dados,
            )

        # Validação 2: Sem áudio quando requerido
        if require_audio and not has_audio:
            return PrecheckResult(
                status=PrecheckStatus.NO_AUDIO,
                is_valid=False,
                duration_s=duracao,
                has_audio=False,
                has_video=True,
                width=width,
                height=height,
                fps=fps,
                message="Arquivo não possui faixa de áudio para transcrição/análise sonora.",
                metadata=dados,
            )

        # Validação 3: Vídeo muito curto
        if duracao < self.min_duration_s:
            return PrecheckResult(
                status=PrecheckStatus.TOO_SHORT,
                is_valid=False,
                duration_s=duracao,
                has_audio=has_audio,
                has_video=True,
                width=width,
                height=height,
                fps=fps,
                message=f"Vídeo muito curto ({duracao:.1f}s). Duração mínima exigida: {self.min_duration_s:.1f}s.",
                metadata=dados,
            )

        return PrecheckResult(
            status=PrecheckStatus.VALID,
            is_valid=True,
            duration_s=duracao,
            has_audio=has_audio,
            has_video=True,
            width=width,
            height=height,
            fps=fps,
            message="Mídia verificada e aprovada no pre-check.",
            metadata=dados,
        )
