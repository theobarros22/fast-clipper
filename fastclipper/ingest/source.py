"""
Adaptador de fonte local para simulação de transmissões ao vivo.

Lê uma gravação autorizada local e a fragmenta em segmentos sequenciais no buffer,
avançando no ritmo do VirtualClock (1.0x, acelerado ou pausado).
"""

import json
import logging
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from fastclipper.ingest.buffer import SegmentedBuffer
from fastclipper.ingest.clock import ClockState, VirtualClock

logger = logging.getLogger(__name__)


class LocalRecordingSource:
    """Simulador de live a partir de um arquivo de vídeo gravado localmente."""

    def __init__(
        self,
        video_path: str | Path,
        buffer: SegmentedBuffer,
        clock: Optional[VirtualClock] = None,
        segment_duration_s: float = 2.0,
        ffmpeg_bin: Optional[str] = None,
    ):
        self.video_path = Path(video_path).resolve()
        if not self.video_path.is_file():
            raise FileNotFoundError(f"Vídeo de origem não encontrado: {self.video_path}")

        self.buffer = buffer
        self.clock = clock or VirtualClock(speed=1.0)
        self.segment_duration_s = float(segment_duration_s)
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_bin = shutil.which("ffprobe") or "ffprobe"

        self.total_duration_s = self._obter_duracao_total()
        self.total_segments = math.ceil(self.total_duration_s / self.segment_duration_s)

        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._current_segment_idx = 0
        self._on_segment_callback: Optional[Callable[[int, float], None]] = None

    def _obter_duracao_total(self) -> float:
        cmd = [
            self.ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(self.video_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        dados = json.loads(res.stdout)
        return float(dados["format"]["duration"])

    def set_segment_callback(self, callback: Callable[[int, float], None]) -> None:
        self._on_segment_callback = callback

    def start(self) -> None:
        with self._lock:
            if self._active:
                return
            self._active = True
            self.clock.start()
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="SimulatedStreamWorker")
            self._thread.start()

    def pause(self) -> None:
        self.clock.pause()

    def resume(self) -> None:
        self.clock.resume()

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self.clock.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def is_finished(self) -> bool:
        with self._lock:
            return self._current_segment_idx >= self.total_segments

    def get_status(self) -> dict:
        cur_t = min(self.clock.current_time(), self.total_duration_s)
        return {
            "clock_time": round(cur_t, 2),
            "total_duration": round(self.total_duration_s, 2),
            "state": self.clock.state.value,
            "speed": self.clock.speed,
            "segments_emitted": self._current_segment_idx,
            "total_segments": self.total_segments,
            "finished": self.is_finished(),
        }

    def emit_segment_now(self, segment_idx: int) -> bool:
        """Gera e injeta um segmento específico no buffer."""
        if segment_idx >= self.total_segments:
            return False

        t_inicio = segment_idx * self.segment_duration_s
        t_fim = min(t_inicio + self.segment_duration_s, self.total_duration_s)
        duracao = t_fim - t_inicio

        nome_segmento = f"seg_{segment_idx:05d}.mp4"
        caminho_segmento = self.buffer.buffer_dir / nome_segmento

        # Se o segmento ainda não foi extraído para disco, extrai via FFmpeg
        if not caminho_segmento.exists():
            cmd = [
                self.ffmpeg_bin,
                "-loglevel", "error",
                "-y",
                "-ss", str(t_inicio),
                "-t", str(duracao),
                "-i", str(self.video_path),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(caminho_segmento),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                logger.error("Erro ao gerar segmento %d via ffmpeg: %s", segment_idx, res.stderr)
                return False

        self.buffer.add_segment(
            segment_id=segment_idx,
            filename=nome_segmento,
            timestamp_inicio=t_inicio,
            timestamp_fim=t_fim,
        )

        with self._lock:
            if segment_idx >= self._current_segment_idx:
                self._current_segment_idx = segment_idx + 1

        if self._on_segment_callback:
            try:
                self._on_segment_callback(segment_idx, t_fim)
            except Exception as e:
                logger.warning("Erro no callback de segmento: %s", e)

        return True

    def _run_loop(self) -> None:
        """Loop de monitoramento do avanço do VirtualClock."""
        while self._active and self._current_segment_idx < self.total_segments:
            if self.clock.state == ClockState.PAUSED:
                time.sleep(0.05)
                continue

            current_t = self.clock.current_time()
            proximo_fim = (self._current_segment_idx + 1) * self.segment_duration_s

            if current_t >= proximo_fim:
                sucesso = self.emit_segment_now(self._current_segment_idx)
                if sucesso:
                    self._current_segment_idx += 1
                    # Executa limpeza periódica do buffer
                    self.buffer.prune_expired(current_t)
            else:
                # Dorme pequeno intervalo calibrado pela velocidade do relógio
                intervalo_espera = max(0.01, min(0.05, (proximo_fim - current_t) / max(1.0, self.clock.speed)))
                time.sleep(intervalo_espera)

        if self._current_segment_idx >= self.total_segments:
            self.clock.stop()
            with self._lock:
                self._active = False
