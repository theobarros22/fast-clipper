"""
Motor de geração e pós-processamento de legendas animadas em 9:16.

Implementa proteções validadas no AutoClip (anti-blink, chunks curtos para vertical)
e geração de arquivos ASS / SRT estritamente compatíveis com o filtro subtitles do FFmpeg.
"""

from __future__ import annotations

import difflib
import logging
import math
import re
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
            chars_atual = sum(len(item.word) for item in bloco_atual) + len(bloco_atual) - 1

            # Quebra se exceder contagem de palavras, caracteres por linha, duração ou pausa vocal > 0.6s
            if (
                len(bloco_atual) >= max_words_per_cue
                or (len(bloco_atual) >= 2 and chars_atual + 1 + len(w.word) > 24)
                or dur_atual > max_duration_s
                or gap > 0.6
            ):
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

    def realign_edited_words(
        self,
        words: List[WordTiming],
        edited_text: str,
        clip_start_s: float = 0.0,
        clip_end_s: Optional[float] = None,
    ) -> List[WordTiming]:
        """Atualiza palavras de um trecho editado pelo operador preservando timestamps exatos das demais."""
        if not words:
            return []
        tokens = [t for t in (edited_text or "").strip().split() if t]

        def _in_window(w: WordTiming) -> bool:
            if clip_end_s is not None:
                return w.end_s > clip_start_s and w.start_s < clip_end_s
            return w.end_s > clip_start_s

        prefix = [w for w in words if w.end_s <= clip_start_s]
        suffix = [w for w in words if clip_end_s is not None and w.start_s >= clip_end_s]
        target = [w for w in words if _in_window(w)]

        if not tokens:
            return prefix + suffix
        if not target:
            span_ini = float(clip_start_s)
            span_fim = float(clip_end_s) if (clip_end_s is not None and clip_end_s > span_ini) else span_ini + 2.0
            step = max(0.05, (span_fim - span_ini) / len(tokens))
            gen = [
                WordTiming(
                    word=tok,
                    start_s=round(span_ini + idx * step, 3),
                    end_s=round(span_ini + (idx + 0.95) * step, 3),
                )
                for idx, tok in enumerate(tokens)
            ]
            return prefix + gen + suffix

        def _norm(s: str) -> str:
            return re.sub(r"[^\w]+", "", s.lower()) or s.lower()

        orig_norm = [_norm(w.word) for w in target]
        edit_norm = [_norm(t) for t in tokens]
        matcher = difflib.SequenceMatcher(a=orig_norm, b=edit_norm, autojunk=False)

        realigned: List[WordTiming] = []
        win_start = target[0].start_s
        win_end = target[-1].end_s

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for orig_idx, edit_idx in zip(range(i1, i2), range(j1, j2)):
                    ow = target[orig_idx]
                    realigned.append(WordTiming(word=tokens[edit_idx], start_s=ow.start_s, end_s=ow.end_s))
            elif tag in ("replace", "insert"):
                sub_tokens = tokens[j1:j2]
                if not sub_tokens:
                    continue
                if i1 < i2:
                    t0 = target[i1].start_s
                    t1 = target[i2 - 1].end_s
                elif i1 > 0 and i1 < len(target):
                    t0 = target[i1 - 1].end_s
                    t1 = max(t0 + 0.12 * len(sub_tokens), target[i1].start_s)
                elif i1 == 0:
                    t1 = target[0].start_s
                    t0 = max(clip_start_s, t1 - 0.25 * len(sub_tokens))
                    if t1 <= t0:
                        t1 = t0 + 0.2 * len(sub_tokens)
                else:
                    t0 = win_end
                    bound = clip_end_s if (clip_end_s is not None and clip_end_s > t0) else (t0 + 0.3 * len(sub_tokens))
                    t1 = max(t0 + 0.15 * len(sub_tokens), bound)

                dur_span = max(0.08 * len(sub_tokens), t1 - t0)
                step = dur_span / len(sub_tokens)
                for k, tok in enumerate(sub_tokens):
                    ws = round(t0 + k * step, 3)
                    we = round(t0 + (k + 0.96) * step, 3)
                    if we <= ws:
                        we = round(ws + 0.05, 3)
                    realigned.append(WordTiming(word=tok, start_s=ws, end_s=we))
            # tag == "delete": palavras removidas pelo usuário saem sem deslocar as vizinhas

        return prefix + realigned + suffix

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
        hook_title: Optional[str] = None,
        clip_duration_s: float = 0.0,
    ) -> str:
        """Gera arquivo Advanced SubStation Alpha (.ass) com estilo, tarja de título opcional e posicionamento seguro."""
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
"""

        # Configurações de estilo com margens seguras para não cortar nas bordas em 9:16 e 16:9
        if preset == CaptionPreset.VIRAL_BOUNCE:
            # Amarelo vibrante, borda preta grossa, fonte maiúscula e pesada
            style_line = "Style: Default,Arial,56,&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,260,1\n"
        elif preset == CaptionPreset.MINIMAL_CLEAN:
            # Estilo AutoClip Calm: branco puro, borda preta discreta de 2px
            style_line = "Style: Default,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,80,80,220,1\n"
        else:  # CINEMA_EDITORIAL
            style_line = "Style: Default,Georgia,48,&H00E0E0E0,&H000000FF,&H00000000,&HB0000000,0,0,0,0,100,100,2,0,1,3,2,2,80,80,230,1\n"

        hook_clean = (hook_title or "").strip()[:140]
        if hook_clean:
            is_portrait = play_res_y >= play_res_x
            h_font = 46 if is_portrait else 38
            h_pad = 15 if is_portrait else 12
            h_margin_lr = 90 if is_portrait else 140
            h_margin_v = 165 if is_portrait else 48
            style_line += (
                f"Style: HookBanner,Arial,{h_font},&H00FFFFFF,&H0000FFFF,&H28101218,&H28101218,"
                f"-1,0,0,0,100,100,0,0,3,{h_pad},0,8,{h_margin_lr},{h_margin_lr},{h_margin_v},1\n"
            )

        events_header = """
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        def _escape_ass(txt: str) -> str:
            return txt.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")

        dialogues = []
        if hook_clean:
            dur_end = clip_duration_s if clip_duration_s > 0.0 else (cues[-1].end_s if cues else 30.0)
            dialogues.append(
                f"Dialogue: 1,0:00:00.00,{to_ass_time(max(0.5, dur_end))},HookBanner,,0,0,0,,{_escape_ass(hook_clean)}"
            )

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
