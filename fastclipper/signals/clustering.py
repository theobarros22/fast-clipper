"""
Deduplicação temporal de momentos e perfis adaptativos de duração.

Inspirado e adaptado da engenharia de qualidade do AutoClip (quality.py / DurationProfile).
Garante que múltiplos sinais simultâneos não gerem candidatos redundantes sobre o
mesmo trecho de live e ajusta janelas ideais de clipe com base na duração total da transmissão.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from fastclipper.signals.models import CandidateSuggestion

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DurationProfile:
    """Perfil de duração adaptativo baseado na extensão da transmissão."""
    tier: str                      # 'short' (<15 min), 'medium' (15-45 min), 'long' (>45 min)
    total_sec: float
    min_clip_sec: float            # Duração mínima aceitável
    target_clip_sec: Tuple[float, float]  # Intervalo de duração desejado
    max_clip_sec: float            # Duração máxima antes de forçar corte
    min_keep: int                  # Quantidade mínima de clipes a reter
    max_clips: int                 # Limite superior de clipes por sessão
    overlap_merge_ratio: float = 0.5   # Proporção de sobreposição que dispara unificação
    merge_gap_sec: float = 4.0     # Intervalo máximo entre trechos para fusão

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["target_clip_sec"] = list(self.target_clip_sec)
        return d


def get_duration_profile(total_sec: float) -> DurationProfile:
    """Calcula perfil adaptativo de corte conforme a duração da gravação/live."""
    total_sec = max(0.0, float(total_sec))
    if total_sec < 900.0:  # < 15 min
        return DurationProfile(
            tier="short",
            total_sec=total_sec,
            min_clip_sec=15.0,
            target_clip_sec=(25.0, 45.0),
            max_clip_sec=60.0,
            min_keep=1,
            max_clips=6,
            overlap_merge_ratio=0.5,
        )
    elif total_sec < 2700.0:  # 15 - 45 min
        return DurationProfile(
            tier="medium",
            total_sec=total_sec,
            min_clip_sec=20.0,
            target_clip_sec=(35.0, 60.0),
            max_clip_sec=90.0,
            min_keep=3,
            max_clips=12,
            overlap_merge_ratio=0.5,
        )
    else:  # > 45 min (lives completas)
        return DurationProfile(
            tier="long",
            total_sec=total_sec,
            min_clip_sec=25.0,
            target_clip_sec=(45.0, 90.0),
            max_clip_sec=120.0,
            min_keep=5,
            max_clips=25,
            overlap_merge_ratio=0.5,
        )


class TemporalDeduplicator:
    """Agrupador e deduplicador de candidatos sobrepostos na linha do tempo."""

    def __init__(self, profile: Optional[DurationProfile] = None):
        self.profile = profile or get_duration_profile(3600.0)

    @staticmethod
    def calculate_overlap_ratio(start1: float, end1: float, start2: float, end2: float) -> float:
        """Calcula proporção de sobreposição em relação ao menor segmento."""
        dur1 = max(0.001, end1 - start1)
        dur2 = max(0.001, end2 - start2)
        inter = max(0.0, min(end1, end2) - max(start1, start2))
        return inter / min(dur1, dur2)

    def deduplicate(
        self,
        candidates: List[CandidateSuggestion],
    ) -> List[CandidateSuggestion]:
        """Elimina redundâncias e une momentos correlacionados na mesma janela."""
        if not candidates:
            return []

        # 1. Ordena decrescente por score para priorizar os melhores momentos
        ordenados = sorted(candidates, key=lambda c: c.score, reverse=True)
        retidos: List[CandidateSuggestion] = []

        for cand in ordenados:
            sobreposto = False
            for existente in retidos:
                ratio = self.calculate_overlap_ratio(
                    cand.start_s, cand.end_s, existente.start_s, existente.end_s
                )
                if ratio >= self.profile.overlap_merge_ratio:
                    sobreposto = True
                    # Absorve explicação e sinais do candidato secundário
                    existente.signals_breakdown[f"merged_{cand.id}"] = {
                        "score": cand.score,
                        "overlap_ratio": round(ratio, 2),
                    }
                    break

            if not sobreposto:
                retidos.append(cand)

        # 2. Reordena cronologicamente para consumo do operador/timeline
        retidos.sort(key=lambda c: c.start_s)
        return retidos

    def select_clips(
        self,
        candidates: List[CandidateSuggestion],
        min_score: float = 0.45,
    ) -> List[CandidateSuggestion]:
        """Seleção com garantia determinística de preenchimento mínimo (anti-vazio)."""
        dedup = self.deduplicate(candidates)

        acima_do_limiar = [c for c in dedup if c.score >= min_score]

        # Se atingir o mínimo, retorna ordenado por score ou cortado em max_clips
        if len(acima_do_limiar) >= self.profile.min_keep:
            return acima_do_limiar[: self.profile.max_clips]

        # Se houver menos que min_keep, completa com os melhores disponíveis abaixo do limiar
        abaixo_do_limiar = [c for c in dedup if c.score < min_score]
        abaixo_ordenado = sorted(abaixo_do_limiar, key=lambda c: c.score, reverse=True)

        faltantes = self.profile.min_keep - len(acima_do_limiar)
        suplemento = abaixo_ordenado[:faltantes]

        selecionados = acima_do_limiar + suplemento
        return selecionados[: self.profile.max_clips]
