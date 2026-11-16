"""
Analisador de energia e picos de áudio normalizados por canal (z-score de RMS).

Calcula a linha de base móvel de volume para evitar confundir jogos naturalmente
barulhentos com momentos de interesse real do streamer.
"""

import array
import logging
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from fastclipper.signals.models import SignalEvent, SignalType

logger = logging.getLogger(__name__)


class AudioPeakDetector:
    """Detector de picos de energia de áudio baseado em z-score de RMS móvel."""

    def __init__(
        self,
        ffmpeg_bin: Optional[str] = None,
        sample_rate: int = 16000,
        window_duration_s: float = 0.5,
        zscore_threshold: float = 2.5,
    ):
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.sample_rate = sample_rate
        self.window_duration_s = window_duration_s
        self.zscore_threshold = zscore_threshold

    def analyze_audio(
        self,
        video_path: str | Path,
        start_s: float = 0.0,
        duration_s: Optional[float] = None,
    ) -> List[SignalEvent]:
        """Extrai fluxo de áudio PCM e detecta picos estatísticos de energia sonora."""
        video = Path(video_path)
        if not video.is_file():
            raise FileNotFoundError(f"Arquivo não encontrado: {video}")

        cmd = [
            self.ffmpeg_bin,
            "-v", "error",
            "-ss", str(start_s),
        ]
        if duration_s is not None:
            cmd.extend(["-t", str(duration_s)])

        cmd.extend([
            "-i", str(video),
            "-vn",
            "-ac", "1",
            "-ar", str(self.sample_rate),
            "-f", "s16le",
            "-",
        ])

        amostras_por_janela = int(self.sample_rate * self.window_duration_s)
        if amostras_por_janela <= 0:
            return []

        window_bytes = amostras_por_janela * 2
        janelas_rms = []
        sample_offset = 0

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            logger.warning("Falha ao iniciar FFmpeg para análise de áudio de %s: %s", video, e)
            return []

        try:
            assert proc.stdout is not None
            remainder = b""
            while True:
                chunk = proc.stdout.read(window_bytes - len(remainder))
                if not chunk:
                    buf = remainder
                    remainder = b""
                else:
                    buf = remainder + chunk
                    if len(buf) < window_bytes:
                        remainder = buf
                        continue
                    remainder = b""

                usable = (len(buf) // 2) * 2
                if usable >= 2:
                    arr = array.array("h")
                    arr.frombytes(buf[:usable])
                    if sys.byteorder != "little":
                        arr.byteswap()
                    n = len(arr)
                    soma_quadrados = sum(x * x for x in arr)
                    rms = math.sqrt(soma_quadrados / n)
                    tempo_centro = start_s + (sample_offset + n / 2) / self.sample_rate
                    janelas_rms.append((tempo_centro, rms))
                    sample_offset += n

                if not chunk:
                    break

            rc = proc.wait(timeout=180)
            if rc != 0:
                logger.warning("FFmpeg retornou código %s ao extrair áudio de %s", rc, video)
                if not janelas_rms:
                    return []
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
            logger.warning("Tempo limite excedido na análise de áudio de %s", video)
            return []

        if not janelas_rms:
            return []

        # Linha de base do canal: média e desvio padrão de RMS
        valores_rms = [r for _, r in janelas_rms]
        media_rms = sum(valores_rms) / len(valores_rms)
        variancia = sum((x - media_rms) ** 2 for x in valores_rms) / max(1, len(valores_rms) - 1)
        desvio_rms = math.sqrt(variancia)

        picos: List[SignalEvent] = []
        if desvio_rms < 1e-4:
            return picos  # Áudio essencialmente plano ou silêncio absoluto

        for ts, rms in janelas_rms:
            z = (rms - media_rms) / desvio_rms
            if z >= self.zscore_threshold:
                # Normaliza intensidade para escala 0.0 .. 1.0+
                intensidade = min(2.0, max(0.0, z / 4.0))
                picos.append(
                    SignalEvent(
                        signal_type=SignalType.AUDIO_PEAK,
                        timestamp_s=round(ts, 2),
                        intensity=round(intensidade, 3),
                        label="Pico de Energia de Áudio",
                        metadata={
                            "zscore": round(z, 2),
                            "rms": round(rms, 1),
                            "media_canal": round(media_rms, 1),
                        },
                    )
                )

        return picos
