"""
Modelos de dados para o motor de legendas e estilização dinâmica.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class CaptionPreset(str, Enum):
    VIRAL_BOUNCE = "viral_bounce"      # Destaque dinâmico na palavra atual, amarelo vibrante (#FFD700)
    MINIMAL_CLEAN = "minimal_clean"    # Estilo AutoClip Calm Premium: caixa preta translúcida, tipografia limpa
    CINEMA_EDITORIAL = "cinema_editorial"  # Estilo jornal/documentário com alto contraste


@dataclass(frozen=True)
class WordTiming:
    word: str
    start_s: float
    end_s: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "word": self.word,
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
        }


@dataclass
class CaptionCue:
    cue_id: str
    start_s: float
    end_s: float
    text: str
    words: List[WordTiming] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cue_id": self.cue_id,
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }
