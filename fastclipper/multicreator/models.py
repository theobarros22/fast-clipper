"""
Modelos de dados para sincronização e comparação multi-criador (M5).
"""

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ComparisonMode(str, Enum):
    SEQUENTIAL = "sequential"       # Corte sequencial: perspectiva Criador A, depois Criador B
    SPLIT_VERTICAL = "split_vertical" # Tela dividida 9:16: Criador A no topo (9:8) e Criador B na base (9:8)


@dataclass
class CreatorReactionTrack:
    """Faixa de reação de um criador associada a um evento compartilhado."""
    track_id: str
    event_id: str
    creator_name: str
    video_path: str
    stream_offset_s: float  # Diferença em segundos entre a live deste criador e o t=0 do evento
    start_s: float          # Início do trecho na linha do tempo do criador
    end_s: float            # Fim do trecho na linha do tempo do criador
    has_authorization: bool
    authorization_ref: str
    has_spoiler: bool = False
    order: int = 1

    @property
    def duration_s(self) -> float:
        return round(self.end_s - self.start_s, 2)

    @property
    def global_start_s(self) -> float:
        """Timestamp de início na linha de tempo global sincronizada do evento."""
        return round(self.start_s - self.stream_offset_s, 2)

    @property
    def global_end_s(self) -> float:
        """Timestamp de fim na linha de tempo global sincronizada do evento."""
        return round(self.end_s - self.stream_offset_s, 2)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CrossCreatorEvent:
    """Evento compartilhado associando múltiplos criadores à mesma ocorrência."""
    event_id: str
    title: str
    description: str
    tracks: List[CreatorReactionTrack] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tracks"] = [t.to_dict() for t in self.tracks]
        return d
