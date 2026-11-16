"""
Motor de geração e pós-processamento de legendas animadas em 9:16.

Implementa proteções validadas no AutoClip (anti-blink, chunks curtos para vertical)
e geração de arquivos ASS / SRT estritamente compatíveis com o filtro subtitles do FFmpeg.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import List, Optional

from fastclipper.subtitles.models import CaptionCue, CaptionPreset, WordTiming

logger = logging.getLogger(__name__)


def to_srt_time(sec: float) -> str:
    """Converte segundos para o formato SRT: 00:01:23,456."""
    total_ms = int(round(max(0.0, float(sec)) * 1000))
    h, rest = divmod(total_ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_ass_time(sec: float) -> str:
    """Converte segundos para o formato ASS: 0:01:23.45 (centésimos de segundo)."""
    total_cs = int(round(max(0.0, float(sec)) * 100))
    h, rest = divmod(total_cs, 360_000)
    m, rest = divmod(rest, 6000)
    s, cs = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


class SubtitleEngine:
    """Processador de legendas de alto impacto para telas verticais 9:16."""

    def __init__(self, blink_threshold_s: float = 0.15):
        self.blink_threshold_s = float(blink_threshold_s)

    def chunk_words(
        self,
        words: List[WordTiming],
        max_words_per_cue: int = 4,
        max_duration_s: float = 2.2,
    ) -> List[CaptionCue]:
        """Agrupa palavras individuais em blocos curtos (3 a 5 palavras) para telas verticais."""
        if not words:
            return []

        cues: List[CaptionCue] = []
        bloco_atual: List[WordTiming] = []
        cue_idx = 1

        for w in words:
            if not bloco_atual:
                bloco_atual.append(w)
                continue

            dur_atual = w.end_s - bloco_atual[0].start_s
            gap = w.start_s - bloco_atual[-1].end_s

            # Quebra se exceder contagem de palavras, duração ou pausa vocal > 0.6s
            if len(bloco_atual) >= max_words_per_cue or dur_atual > max_duration_s or gap > 0.6:
                texto = " ".join(item.word for item in bloco_atual)
                cues.append(
                    CaptionCue(
                        cue_id=f"cue_{cue_idx:04d}",
                        start_s=bloco_atual[0].start_s,
                        end_s=bloco_atual[-1].end_s,
                        text=texto,
                        words=list(bloco_atual),
                    )
                )
                cue_idx += 1
                bloco_atual = [w]
            else:
                bloco_atual.append(w)

        if bloco_atual:
            texto = " ".join(item.word for item in bloco_atual)
            cues.append(
                CaptionCue(
                    cue_id=f"cue_{cue_idx:04d}",
                    start_s=bloco_atual[0].start_s,
                    end_s=bloco_atual[-1].end_s,
                    text=texto,
                    words=list(bloco_atual),
                )
            )

        return self.remove_blinks(cues)

    def remove_blinks(self, cues: List[CaptionCue]) -> List[CaptionCue]:
        """Elimina piscamentos (flicker): se o intervalo entre duas falas for minúsculo, estende o fim da anterior."""
        if len(cues) <= 1:
            return cues

        resultado: List[CaptionCue] = []
        for i in range(len(cues)):
            atual = cues[i]
            if i + 1 < len(cues):
                proximo = cues[i + 1]
                gap = proximo.start_s - atual.end_s
                # Se o intervalo for menor que o limiar (ex: 0.15s), une os tempos para evitar que a legenda pisque
                if 0.0 < gap <= self.blink_threshold_s:
                    atual.end_s = proximo.start_s
            resultado.append(atual)

        return resultado

    def generate_srt(self, cues: List[CaptionCue]) -> str:
        """Gera arquivo no formato SubRip (.srt)."""
        linhas = []
        for i, cue in enumerate(cues, 1):
            linhas.append(str(i))
            linhas.append(f"{to_srt_time(cue.start_s)} --> {to_srt_time(cue.end_s)}")
            linhas.append(cue.text)
            linhas.append("")
        return "\n".join(linhas)

    def generate_ass(
        self,
        cues: List[CaptionCue],
        preset: CaptionPreset = CaptionPreset.VIRAL_BOUNCE,
        play_res_x: int = 1080,
        play_res_y: int = 1920,
    ) -> str:
        """Gera arquivo Advanced SubStation Alpha (.ass) com estilo e posicionamento preciso."""
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
"""

        # Configurações de estilo
        if preset == CaptionPreset.VIRAL_BOUNCE:
            # Amarelo vibrante, borda preta grossa, fonte maiúscula e pesada
            style_line = "Style: Default,Arial,68,&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,40,40,240,1\n"
        elif preset == CaptionPreset.MINIMAL_CLEAN:
            # Estilo AutoClip Calm: branco puro, borda preta discreta de 2px
            style_line = "Style: Default,Arial,56,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,200,1\n"
        else:  # CINEMA_EDITORIAL
            style_line = "Style: Default,Georgia,54,&H00E0E0E0,&H000000FF,&H00000000,&HB0000000,0,0,0,0,100,100,2,0,1,3,2,2,50,50,220,1\n"

        events_header = """
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        def _escape_ass(txt: str) -> str:
            return txt.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")

        dialogues = []
        for cue in cues:
            start_str = to_ass_time(cue.start_s)
            end_str = to_ass_time(cue.end_s)

            # Se houver timings individuais de palavras e o preset for VIRAL_BOUNCE, gera efeito karaoke centésimo de segundo
            if preset == CaptionPreset.VIRAL_BOUNCE and cue.words:
                partes = []
                for w in cue.words:
                    dur_cs = int(round((w.end_s - w.start_s) * 100))
                    partes.append(f"{{\\k{dur_cs}}}{_escape_ass(w.word)}")
                texto_formatado = " ".join(partes)
            else:
                texto_formatado = _escape_ass(cue.text)

            dialogues.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{texto_formatado}")

        return header + style_line + events_header + "\n".join(dialogues) + "\n"
