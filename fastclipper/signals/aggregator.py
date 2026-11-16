"""
Agregador multimodal de sinais para geração de candidatos a clipes explicáveis.

Combina picos de áudio, aceleração de chat, eventos de transcrição e cortes visuais,
respeitando a possibilidade estrita de desativação de cada detector de forma independente.
"""

import math
import uuid
from typing import Any, Dict, List, Optional

from fastclipper.signals.models import (
    CandidateSuggestion,
    SignalEvent,
    SignalType,
    SignalWeights,
)


class MultimodalSignalAggregator:
    """Motor de fusão sensorial e geração de candidatos a momentos."""

    def __init__(
        self,
        weights: Optional[SignalWeights] = None,
        cluster_window_s: float = 8.0,
        pre_window_s: float = 12.0,
        post_window_s: float = 18.0,
    ):
        self.weights = weights or SignalWeights()
        self.cluster_window_s = cluster_window_s
        self.pre_window_s = pre_window_s
        self.post_window_s = post_window_s

    def aggregate_signals(
        self,
        events: List[SignalEvent],
    ) -> List[CandidateSuggestion]:
        """Agrega lista heterogênea de eventos e emite sugestões ordenadas por score."""
        if not events:
            return []

        # 1. Filtra eventos por detectores ativos
        ativos: List[SignalEvent] = []
        for e in events:
            if e.signal_type == SignalType.AUDIO_PEAK and not self.weights.enable_audio:
                continue
            if e.signal_type == SignalType.CHAT_SPIKE and not self.weights.enable_chat:
                continue
            if e.signal_type == SignalType.TRANSCRIPTION_KEYWORD and not self.weights.enable_transcription:
                continue
            if e.signal_type == SignalType.VISUAL_CUT and not self.weights.enable_visual:
                continue
            ativos.append(e)

        if not ativos:
            return []

        # Ordena cronologicamente
        ordenados = sorted(ativos, key=lambda x: x.timestamp_s)

        # 2. Agrupamento em clusters temporais
        clusters: List[List[SignalEvent]] = []
        cluster_atual: List[SignalEvent] = [ordenados[0]]

        for e in ordenados[1:]:
            if e.timestamp_s - cluster_atual[-1].timestamp_s <= self.cluster_window_s:
                cluster_atual.append(e)
            else:
                clusters.append(cluster_atual)
                cluster_atual = [e]
        if cluster_atual:
            clusters.append(cluster_atual)

        # 3. Pontuação e explicabilidade de cada cluster
        candidatos: List[CandidateSuggestion] = []

        for grupo in clusters:
            ts_pico = sum(e.timestamp_s * e.intensity for e in grupo) / sum(e.intensity for e in grupo)
            ts_pico = round(ts_pico, 2)

            audio_int = max((e.intensity for e in grupo if e.signal_type == SignalType.AUDIO_PEAK), default=0.0)
            chat_int = max((e.intensity for e in grupo if e.signal_type == SignalType.CHAT_SPIKE), default=0.0)
            trans_int = max((e.intensity for e in grupo if e.signal_type == SignalType.TRANSCRIPTION_KEYWORD), default=0.0)
            vis_int = max((e.intensity for e in grupo if e.signal_type == SignalType.VISUAL_CUT), default=0.0)

            score_audio = self.weights.audio_weight * audio_int if self.weights.enable_audio else 0.0
            score_chat = self.weights.chat_weight * chat_int if self.weights.enable_chat else 0.0
            score_trans = self.weights.transcription_weight * trans_int if self.weights.enable_transcription else 0.0
            score_vis = self.weights.visual_weight * vis_int if self.weights.enable_visual else 0.0

            score_total = round(min(1.0, score_audio + score_chat + score_trans + score_vis), 3)

            if score_total >= self.weights.min_candidate_score:
                detalhes_explicativos = []
                if audio_int > 0:
                    detalhes_explicativos.append(f"Áudio (int={audio_int:.2f})")
                if chat_int > 0:
                    detalhes_explicativos.append(f"Chat (int={chat_int:.2f})")
                if trans_int > 0:
                    detalhes_explicativos.append(f"Fala Relevante (int={trans_int:.2f})")
                if vis_int > 0:
                    detalhes_explicativos.append("Corte Visual")

                explicacao = (
                    f"Score {score_total:.2f} gerado por: {', '.join(detalhes_explicativos)} "
                    f"no intervalo de pico em t={ts_pico:.1f}s."
                )

                t_inicio = max(0.0, round(ts_pico - self.pre_window_s, 2))
                t_fim = round(ts_pico + self.post_window_s, 2)

                candidato = CandidateSuggestion(
                    id=f"sug_{uuid.uuid4().hex[:8]}",
                    timestamp_s=ts_pico,
                    start_s=t_inicio,
                    end_s=t_fim,
                    score=score_total,
                    category="multimodal_highlight",
                    method_version="m4_multimodal_v1.0",
                    signals_breakdown={
                        "audio": {"intensity": audio_int, "weighted_score": round(score_audio, 3)},
                        "chat": {"intensity": chat_int, "weighted_score": round(score_chat, 3)},
                        "transcription": {"intensity": trans_int, "weighted_score": round(score_trans, 3)},
                        "visual": {"intensity": vis_int, "weighted_score": round(score_vis, 3)},
                        "total_events_in_cluster": len(grupo),
                    },
                    explanation_text=explicacao,
                )
                candidatos.append(candidato)

        return sorted(candidatos, key=lambda c: c.score, reverse=True)
