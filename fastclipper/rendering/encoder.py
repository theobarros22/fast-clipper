"""
Gerenciador de encoders H.264 e supervisor de aceleração por hardware.

Inspirado e adaptado da estratégia de encoders do AutoClip (video_encoder.py).
Testa encoders de hardware em tempo de execução via teste sintético de 0.3s.
Suporta NVENC (NVIDIA), QSV (Intel), AMF (AMD) e VideoToolbox (macOS), com fallback
automático e resiliente para libx264 (CPU) em caso de falha de VRAM ou limite de sessões.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

SOFTWARE_ENCODER = "libx264"


class HardwareEncoderManager:
    """Gerenciador de encoders de vídeo com detecção dinâmica e fallback seguro."""

    def __init__(self, ffmpeg_bin: Optional[str] = None):
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self._lock = threading.Lock()
        self._chosen: Optional[str] = None
        self._broken: Set[str] = set()

    def _candidates(self) -> List[str]:
        if sys.platform == "darwin":
            return ["h264_videotoolbox"]
        return ["h264_nvenc", "h264_qsv", "h264_amf"]

    def _test_encoder(self, encoder_name: str) -> bool:
        """Executa um teste ultrarrápido (0.3s) com lavfi color para verificar se o encoder funciona."""
        cmd = [
            self.ffmpeg_bin,
            "-v", "error",
            "-f", "lavfi",
            "-i", "color=c=black:s=320x568:r=30:d=0.3",
            *self.get_encoder_args(encoder_name, width=320, height=568, fps=30),
            "-f", "null",
            "-",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=10)
            return res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def get_encoder(self) -> str:
        """Retorna o encoder ideal para esta máquina."""
        forced = os.getenv("FASTCLIPPER_VIDEO_ENCODER", "").strip().lower()
        if forced in ("x264", "libx264", "software", "cpu"):
            return SOFTWARE_ENCODER

        with self._lock:
            if self._chosen is None or self._chosen in self._broken:
                self._chosen = SOFTWARE_ENCODER
                for candidate in self._candidates():
                    if candidate not in self._broken and self._test_encoder(candidate):
                        self._chosen = candidate
                        logger.info("Encoder de vídeo selecionado: %s", self._chosen)
                        break
            return self._chosen

    def mark_broken(self, encoder_name: str) -> None:
        """Marca um encoder como quebrado e aciona fallback para software."""
        if encoder_name != SOFTWARE_ENCODER:
            with self._lock:
                self._broken.add(encoder_name)
                self._chosen = None
            logger.warning(
                "Encoder de hardware '%s' falhou em tempo de execução. Utilizando '%s' daqui em diante.",
                encoder_name,
                SOFTWARE_ENCODER,
            )

    def calculate_bitrate(self, width: int, height: int, fps: int = 30) -> int:
        """Calcula bitrate ideal (~0.13 bits por pixel por frame para 9:16 vertical)."""
        return max(1_500_000, int(width * height * fps * 0.13))

    def get_encoder_args(
        self,
        encoder_name: str,
        width: int = 1080,
        height: int = 1920,
        fps: int = 30,
        rate: Optional[int] = None,
    ) -> List[str]:
        """Gera lista de parâmetros de linha de comando para o encoder especificado."""
        target_rate = rate or self.calculate_bitrate(width, height, fps)
        vbr_args = [
            "-b:v", str(target_rate),
            "-maxrate", str(int(target_rate * 1.5)),
            "-bufsize", str(target_rate * 2),
        ]

        if encoder_name == "h264_nvenc":
            return [
                "-c:v", "h264_nvenc",
                "-preset", "p4",
                "-rc", "vbr",
                "-cq", "21",
                *vbr_args,
                "-pix_fmt", "yuv420p",
            ]
        elif encoder_name == "h264_qsv":
            return [
                "-c:v", "h264_qsv",
                "-preset", "medium",
                "-global_quality", "21",
                *vbr_args,
                "-pix_fmt", "nv12",
            ]
        elif encoder_name == "h264_amf":
            return [
                "-c:v", "h264_amf",
                "-quality", "balanced",
                "-rc", "vbr_peak",
                *vbr_args,
                "-pix_fmt", "yuv420p",
            ]
        elif encoder_name == "h264_videotoolbox":
            return [
                "-c:v", "h264_videotoolbox",
                *vbr_args,
                "-profile:v", "high",
                "-pix_fmt", "yuv420p",
            ]

        # Padrão: Software libx264
        return [
            "-c:v", SOFTWARE_ENCODER,
            "-preset", "veryfast",
            "-crf", "20",
            *vbr_args,
            "-pix_fmt", "yuv420p",
        ]
