"""
Analisador de energia e picos de áudio normalizados por canal (z-score de RMS).

Calcula a linha de base móvel de volume para evitar confundir jogos naturalmente
barulhentos com momentos de interesse real do streamer.
"""

import logging
import math
import shutil
import struct
import subprocess
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

        try:
            res = subprocess.run(cmd, capture_output=True, check=True)
            raw_bytes = res.stdout
        except subprocess.CalledProcessError as e:
            logger.warning("Falha ao extrair áudio de %s: %s", video, e)
            return []

        if not raw_bytes:
            return []

        # Converte bytes PCM 16-bit para inteiros
        num_amostras = len(raw_bytes) // 2
        amostras = struct.unpack(f"<{num_amostras}h", raw_bytes[:num_amostras * 2])

        amostras_por_janela = int(self.sample_rate * self.window_duration_s)
        if amostras_por_janela <= 0:
            return []

        # Calcula RMS de cada janela temporal
        janelas_rms = []
        for i in range(0, len(amostras), amostras_por_janela):
            bloco = amostras[i : i + amostras_por_janela]
            if not bloco:
                continue
            soma_quadrados = sum(float(x) * float(x) for x in bloco)
            rms = math.sqrt(soma_quadrados / len(bloco))
            tempo_centro = start_s + (i + len(bloco) / 2) / self.sample_rate
            janelas_rms.append((tempo_centro, rms))

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
