"""
Modelos de dados para sinais multimodais, perfis editoriais e sugestões explicáveis.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SignalType(str, Enum):
    AUDIO_PEAK = "audio_peak"
    CHAT_SPIKE = "chat_spike"
    TRANSCRIPTION_KEYWORD = "transcription_keyword"
    VISUAL_CUT = "visual_cut"
    MANUAL = "manual"


@dataclass
class SignalEvent:
    signal_type: SignalType
    timestamp_s: float
    intensity: float  # z-score ou pontuação normalizada [0.0 .. 1.0+]
    label: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signal_type"] = self.signal_type.value
        return d


@dataclass
class SignalWeights:
    audio_weight: float = 0.35
    chat_weight: float = 0.30
    transcription_weight: float = 0.25
    visual_weight: float = 0.10

    # Opção estrita de ligar/desligar cada detector independentemente
    enable_audio: bool = True
    enable_chat: bool = True
    enable_transcription: bool = True
    enable_visual: bool = True

    min_candidate_score: float = 0.55


@dataclass
class GamingEditorialProfile:
    """Perfil editorial especializado para jogos e GTA V.

    Separa o reconhecimento de eventos objetivos do jogo da análise de reação do criador.
    """
    profile_name: str = "GTA V / Ação"

    # Eventos objetivos do jogo
    game_event_keywords: List[str] = field(default_factory=lambda: [
        "polícia", "policia", "perseguição", "perseguicao", "tiro", "tiroteio",
        "explosão", "explosao", "busted", "wasted", "salto", "rampa", "helicóptero",
        "helicoptero", "míssil", "missil", "fuga", "acidente", "capotou", "assalto"
    ])

    # Reações subjetivas do criador
    creator_reaction_keywords: List[str] = field(default_factory=lambda: [
        "meu deus", "caraca", "mentira", "não acredito", "nao acredito",
        "olha isso", "socorro", "morri", "que isso", "inacreditável", "inacreditavel",
        "mano do céu", "mano do ceu", "nossa", "eita", "rindo muito", "me ferrei"
    ])

    coincidence_boost: float = 0.25  # Bônus quando evento do jogo coincide com reação do streamer


@dataclass
class CandidateSuggestion:
    """Candidato a momento gerado de forma explicável por sinais combinados."""
    id: str
    timestamp_s: float
    start_s: float
    end_s: float
    score: float
    category: str
    method_version: str = "m4_multimodal_v1.0"
    signals_breakdown: Dict[str, Any] = field(default_factory=dict)
    explanation_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
