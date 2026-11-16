"""
Sugestor e rankeador explicável de momentos para clipping (Sprint 3).

Combina:
1. Energia de áudio real extraída via FFmpeg (astats RMS por janela)
2. Palavras-chave e emoção a partir dos timestamps da transcrição Whisper
3. Alinhamento estrito de fronteiras (SentenceBoundarySnapper) para nunca cortar no meio de uma palavra
4. Deduplicação temporal para evitar sugestões redundantes
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastclipper.signals.audio_analyzer import AudioPeakDetector
from fastclipper.signals.boundaries import SentenceBoundarySnapper, SubtitleRow
from fastclipper.signals.models import GamingEditorialProfile

logger = logging.getLogger(__name__)

_RMS_RE = re.compile(r"RMS_level=([-\d\.]+|-inf)")
_PTS_RE = re.compile(r"pts_time:([\d\.]+)")


def extract_audio_rms_series(video_path: str | Path, ffmpeg_bin: Optional[str] = None) -> List[Tuple[float, float]]:
    """Extrai série temporal (timestamp_s, rms_db) do áudio via FFmpeg astats."""
    ffmpeg = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg,
        "-v", "info",
        "-i", str(video_path),
        "-af", "aselect='1',asetnsamples=n=44100:p=0,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f", "null",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
    samples: List[Tuple[float, float]] = []
    current_t = 0.0
    for line in res.stderr.splitlines():
        m_pts = _PTS_RE.search(line)
        if m_pts:
            current_t = float(m_pts.group(1))
        m_rms = _RMS_RE.search(line)
        if m_rms:
            raw = m_rms.group(1)
            db = -90.0 if raw == "-inf" else float(raw)
            # Converte dB [-90..0] para escala linear positiva [0..90] para cálculo de Z-score
            samples.append((round(current_t, 2), max(0.0, 90.0 + db)))
    return samples


def words_to_subtitle_rows(words: List[Dict[str, Any]], pause_threshold_s: float = 0.6) -> List[SubtitleRow]:
    """Agrupa palavras do Whisper em linhas/frases contínuas para snap de fronteira."""
    if not words:
        return []

    rows: List[SubtitleRow] = []
    buf: List[Dict[str, Any]] = []

    for w in words:
        if not buf:
            buf.append(w)
            continue
        gap = float(w["start_s"]) - float(buf[-1]["end_s"])
        prev_txt = str(buf[-1]["word"])
        ends_sentence = bool(re.search(r"[.?!…]\s*$", prev_txt))
        if gap > pause_threshold_s or ends_sentence or len(buf) >= 12:
            rows.append(
                SubtitleRow(
                    start_s=float(buf[0]["start_s"]),
                    end_s=float(buf[-1]["end_s"]),
                    text=" ".join(str(x["word"]).strip() for x in buf),
                )
            )
            buf = [w]
        else:
            buf.append(w)

    if buf:
        rows.append(
            SubtitleRow(
                start_s=float(buf[0]["start_s"]),
                end_s=float(buf[-1]["end_s"]),
                text=" ".join(str(x["word"]).strip() for x in buf),
            )
        )
    return rows


def snap_bounds_to_words(
    words: List[Dict[str, Any]],
    raw_start: float,
    raw_end: float,
    total_duration_s: float,
) -> Tuple[float, float]:
    """Garante que o corte comece no início de uma palavra e termine no fim de outra."""
    if not words:
        return max(0.0, round(raw_start, 2)), min(total_duration_s, round(raw_end, 2))

    # Recua ou avança até a palavra mais próxima para não mutilar fala
    start_candidates = [w for w in words if float(w["end_s"]) >= raw_start]
    end_candidates = [w for w in words if float(w["start_s"]) <= raw_end]

    snapped_start = float(start_candidates[0]["start_s"]) if start_candidates else raw_start
    snapped_end = float(end_candidates[-1]["end_s"]) if end_candidates else raw_end

    snapped_start = max(0.0, min(snapped_start, raw_start))
    snapped_end = min(total_duration_s, max(snapped_end, raw_end))

    if snapped_end <= snapped_start:
        return max(0.0, round(raw_start, 2)), min(total_duration_s, round(raw_end, 2))
    return round(snapped_start, 2), round(snapped_end, 2)


class MomentSuggester:
    """Gera sugestões ranqueadas e explicáveis de clipes a partir de vídeo e transcrição."""

    def __init__(
        self,
        profile: Optional[GamingEditorialProfile] = None,
        ffmpeg_bin: Optional[str] = None,
    ):
        self.profile = profile or GamingEditorialProfile()
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.audio_detector = AudioPeakDetector(zscore_threshold=1.5)
        self.snapper = SentenceBoundarySnapper()

    def suggest(
        self,
        video_path: str | Path,
        duration_s: float,
        words: Optional[List[Dict[str, Any]]] = None,
        target_duration_s: float = 25.0,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retorna top_k sugestões com pontuação, justificativa e limites alinhados a frases."""
        if top_k < 1 or target_duration_s < 1.0:
            raise ValueError("top_k deve ser >= 1 e target_duration_s deve ser >= 1.0.")

        self.last_warning: Optional[str] = None
        words = words or []
        total_dur = max(1.0, float(duration_s))
        half_win = max(2.0, target_duration_s / 2.0)

        # 1. Sinais de áudio (quando vídeo tiver faixa de áudio)
        try:
            audio_peaks = self.audio_detector.analyze_audio(video_path)
        except Exception as exc:
            audio_peaks = []
            self.last_warning = f"Análise de áudio indisponível ({exc}); usando janelas editoriais."

        # 2. Sinais da transcrição (palavras-chave de emoção/gancho)
        rows = words_to_subtitle_rows(words)
        kw_set = {
            k.lower()
            for k in (self.profile.game_event_keywords + self.profile.creator_reaction_keywords)
        } | {"insano", "clipe", "incrível"}
        raw_events: List[Dict[str, Any]] = []

        for pk in audio_peaks:
            raw_events.append({
                "center_s": pk.timestamp_s,
                "audio_z": float(pk.intensity),
                "kw_hits": [],
            })

        for row in rows:
            lower = row.text.lower()
            hits = [kw for kw in kw_set if re.search(rf"\b{re.escape(kw)}\b", lower)]
            if hits or "!" in row.text or "?" in row.text:
                raw_events.append({
                    "center_s": (row.start_s + row.end_s) / 2.0,
                    "audio_z": 0.0,
                    "kw_hits": hits,
                })

        # Fallback determinístico para vídeos curtos/sem picos óbvios: amostra janelas uniformes
        if not raw_events:
            step = max(target_duration_s, total_dur / max(1, top_k))
            t = half_win if total_dur > target_duration_s else total_dur / 2.0
            while t <= total_dur:
                raw_events.append({"center_s": t, "audio_z": 0.5, "kw_hits": []})
                t += step

        # 3. Pontua, aplica snap de frase/palavra e deduplica sobreposições
        scored: List[Dict[str, Any]] = []
        for ev in raw_events:
            c = float(ev["center_s"])
            raw_start = max(0.0, c - half_win)
            raw_end = min(total_dur, c + half_win)
            if raw_end - raw_start < min(3.0, total_dur):
                raw_start = 0.0
                raw_end = min(total_dur, target_duration_s)

            if rows:
                snapped_s, snapped_e = self.snapper.snap_boundaries(rows, raw_start, raw_end)
                inicio_s, fim_s = snap_bounds_to_words(words, snapped_s, snapped_e, total_dur)
            else:
                inicio_s, fim_s = round(raw_start, 2), round(raw_end, 2)

            # Reúne palavras dentro da janela para preview
            snippet_words = [
                str(w["word"]).strip()
                for w in words
                if float(w["end_s"]) >= inicio_s and float(w["start_s"]) <= fim_s
            ]
            # Soma energia de áudio próxima
            near_audio = max(
                [float(p.intensity) for p in audio_peaks if inicio_s <= p.timestamp_s <= fim_s] or [ev["audio_z"]]
            )
            kw_score = min(1.0, len(ev["kw_hits"]) * 0.35)
            audio_score = min(1.0, near_audio / 4.0)
            final_score = round(min(1.0, 0.55 * audio_score + 0.45 * kw_score + 0.15), 3)

            reasons: List[str] = []
            if near_audio >= 1.5:
                reasons.append(f"pico de áudio (z={near_audio:.1f})")
            if ev["kw_hits"]:
                reasons.append(f"palavras-chave: {', '.join(ev['kw_hits'][:3])}")
            if not reasons:
                reasons.append("janela editorial padrão")

            scored.append({
                "inicio_s": inicio_s,
                "fim_s": fim_s,
                "duration_s": round(fim_s - inicio_s, 2),
                "score": final_score,
                "keywords": ev["kw_hits"],
                "reason": " + ".join(reasons),
                "preview_text": " ".join(snippet_words[:24]),
            })

        # Ordena por maior score e remove sobreposições > 50%
        scored.sort(key=lambda x: x["score"], reverse=True)
        selected: List[Dict[str, Any]] = []
        for cand in scored:
            overlaps = False
            for kept in selected:
                inter = max(0.0, min(cand["fim_s"], kept["fim_s"]) - max(cand["inicio_s"], kept["inicio_s"]))
                min_len = max(0.1, min(cand["duration_s"], kept["duration_s"]))
                if inter / min_len >= 0.5:
                    overlaps = True
                    break
            if not overlaps:
                selected.append(cand)
            if len(selected) >= top_k:
                break

        return selected
