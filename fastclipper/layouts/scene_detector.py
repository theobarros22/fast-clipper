"""
Detecção de troca de cena para validação de integridade de layout 9:16.

Monitora mudanças de tomada/cena via FFmpeg ou análise de frames. Se ocorrer
uma transição abrupta em um clipe que utiliza layout fixo (ex: o streamer entra
em menu ou Just Chatting), emite alerta para revisão humana.
"""

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SceneCut:
    timestamp_s: float
    score: float


class SceneDetector:
    """Detector de cortes de cena baseado no filtro nativo do FFmpeg."""

    def __init__(self, ffmpeg_bin: Optional[str] = None, default_threshold: float = 0.35):
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.default_threshold = float(default_threshold)

    def detect_cuts(
        self,
        video_path: str | Path,
        start_s: float = 0.0,
        duration_s: Optional[float] = None,
        threshold: Optional[float] = None,
    ) -> List[SceneCut]:
        """Detecta timestamps de trocas de cena no intervalo especificado."""
        video = Path(video_path)
        if not video.is_file():
            raise FileNotFoundError(f"Arquivo de vídeo não encontrado: {video}")

        th = self.default_threshold if threshold is None else float(threshold)

        # Utiliza o filtro select='gt(scene,threshold)' com showinfo
        filtro = f"select='gt(scene,{th})',showinfo"

        cmd = [
            self.ffmpeg_bin,
            "-v", "info",
            "-ss", str(start_s),
        ]
        if duration_s is not None:
            cmd.extend(["-t", str(duration_s)])

        cmd.extend([
            "-i", str(video),
            "-vf", filtro,
            "-f", "null",
            "-",
        ])

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        # O showinfo imprime no stderr
        saida = res.stderr

        cortes: List[SceneCut] = []
        # Exemplo de linha do showinfo:
        # [Parsed_showinfo_1 @ ...] n:   1 pts: 180180 pts_time:6.006 ...
        padrao_pts = re.compile(r"pts_time:([0-9\.]+)")
        for linha in saida.splitlines():
            if "Parsed_showinfo" in linha and "pts_time:" in linha:
                m = padrao_pts.search(linha)
                if m:
                    offset_relativo = float(m.group(1))
                    ts_absoluto = round(start_s + offset_relativo, 3)
                    cortes.append(SceneCut(timestamp_s=ts_absoluto, score=th))

        return cortes

    def validate_layout_stability(
        self,
        video_path: str | Path,
        start_s: float,
        duration_s: float,
        has_facecam: bool = True,
    ) -> dict:
        """Verifica se há trocas de cena durante o clipe que invalidem o layout fixo."""
        cortes = self.detect_cuts(video_path, start_s=start_s, duration_s=duration_s)

        if not cortes:
            return {
                "stable": True,
                "requires_review": False,
                "cuts": [],
                "reason": None,
            }

        # Se houver troca de cena e o clipe usar facecam ou recorte específico, requer revisão
        motivo = (
            f"Detectada(s) {len(cortes)} troca(s) de cena no intervalo "
            f"[{start_s:.1f}s - {start_s + duration_s:.1f}s] em: "
            f"{', '.join(f'{c.timestamp_s:.2f}s' for c in cortes[:3])}"
        )
        if len(cortes) > 3:
            motivo += f" (+{len(cortes)-3} outros)"

        return {
            "stable": False,
            "requires_review": True if has_facecam else False,
            "cuts": [{"timestamp_s": c.timestamp_s, "score": c.score} for c in cortes],
            "reason": motivo,
        }
