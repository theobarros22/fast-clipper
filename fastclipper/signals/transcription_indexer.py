"""
Indexador de transcrição e aplicação de perfil editorial de gaming.

Separa o reconhecimento de eventos objetivos do jogo da análise de reação do criador,
atribuindo bônus de pontuação para momentos de coincidência direta.
"""

import re
from typing import Any, Dict, List, Optional

from fastclipper.signals.models import (
    GamingEditorialProfile,
    SignalEvent,
    SignalType,
)


class TranscriptionIndexer:
    """Indexador de texto falado e detecção de intenções e reações editoriais."""

    def __init__(self, profile: Optional[GamingEditorialProfile] = None):
        self.profile = profile or GamingEditorialProfile()

    def index_transcription(
        self,
        segments: List[Dict[str, Any]],
    ) -> List[SignalEvent]:
        """Analisa segmentos de transcrição [{'start': float, 'end': float, 'text': str}]."""
        eventos: List[SignalEvent] = []
        if not segments:
            return eventos

        for seg in segments:
            texto = seg.get("text", "").lower()
            t_inicio = float(seg.get("start", 0.0))
            t_fim = float(seg.get("end", t_inicio + 1.0))
            t_centro = round((t_inicio + t_fim) / 2.0, 2)

            eventos_jogo_encontrados = []
            for kw in self.profile.game_event_keywords:
                if re.search(r"\b" + re.escape(kw) + r"\b", texto):
                    eventos_jogo_encontrados.append(kw)

            reacoes_criador_encontradas = []
            for kw in self.profile.creator_reaction_keywords:
                if re.search(r"\b" + re.escape(kw) + r"\b", texto):
                    reacoes_criador_encontradas.append(kw)

            # Se encontrou evento de jogo ou reação do streamer
            if eventos_jogo_encontrados or reacoes_criador_encontradas:
                intensidade_base = 0.6
                coincidencia = bool(eventos_jogo_encontrados and reacoes_criador_encontradas)
                if coincidencia:
                    intensidade_base += self.profile.coincidence_boost

                rotulo = "Reação ao Jogo" if coincidencia else (
                    "Evento do Jogo" if eventos_jogo_encontrados else "Reação Vocal do Criador"
                )

                eventos.append(
                    SignalEvent(
                        signal_type=SignalType.TRANSCRIPTION_KEYWORD,
                        timestamp_s=t_centro,
                        intensity=round(intensidade_base, 3),
                        label=rotulo,
                        metadata={
                            "trecho_fala": seg.get("text", "").strip(),
                            "eventos_jogo": eventos_jogo_encontrados,
                            "reacoes_criador": reacoes_criador_encontradas,
                            "coincidencia_evento_reacao": coincidencia,
                        },
                    )
                )

        return eventos
