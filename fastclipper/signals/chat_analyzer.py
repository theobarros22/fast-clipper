"""
Analisador de velocidade e aceleração de chat de live com timestamp.

Identifica picos de mensagens por segundo em relação à linha de base do canal,
sinalizando momentos de alta reação da comunidade.
"""

import math
from typing import Any, Dict, List

from fastclipper.signals.models import SignalEvent, SignalType


class ChatSpikeDetector:
    """Detector de picos e aceleração de chat com cálculo estatístico de z-score."""

    def __init__(self, bucket_duration_s: float = 3.0, zscore_threshold: float = 2.0):
        self.bucket_duration_s = float(bucket_duration_s)
        self.zscore_threshold = float(zscore_threshold)

    def analyze_chat(self, messages: List[Dict[str, Any]]) -> List[SignalEvent]:
        """Processa lista de mensagens [{'timestamp_s': float, 'user': str, 'message': str}]."""
        if not messages:
            return []

        # Ordena mensagens por timestamp
        mensagens_ord = sorted(messages, key=lambda m: float(m.get("timestamp_s", 0.0)))
        t_min = float(mensagens_ord[0]["timestamp_s"])
        t_max = float(mensagens_ord[-1]["timestamp_s"])

        if t_max <= t_min:
            return []

        # Agrupa mensagens em baldes temporais
        num_buckets = math.ceil((t_max - t_min) / self.bucket_duration_s) + 1
        buckets = [0] * num_buckets

        for msg in mensagens_ord:
            ts = float(msg.get("timestamp_s", 0.0))
            idx = int((ts - t_min) / self.bucket_duration_s)
            if 0 <= idx < num_buckets:
                buckets[idx] += 1

        # Taxa de mensagens por segundo
        taxas_msg_s = [b / self.bucket_duration_s for b in buckets]
        media_taxa = sum(taxas_msg_s) / len(taxas_msg_s)
        variancia = sum((x - media_taxa) ** 2 for x in taxas_msg_s) / max(1, len(taxas_msg_s) - 1)
        desvio_taxa = math.sqrt(variancia)

        picos: List[SignalEvent] = []
        if desvio_taxa < 1e-4:
            return picos

        for idx, taxa in enumerate(taxas_msg_s):
            z = (taxa - media_taxa) / desvio_taxa
            if z >= self.zscore_threshold:
                tempo_centro = round(t_min + (idx + 0.5) * self.bucket_duration_s, 2)
                intensidade = min(2.0, max(0.0, z / 3.5))
                picos.append(
                    SignalEvent(
                        signal_type=SignalType.CHAT_SPIKE,
                        timestamp_s=tempo_centro,
                        intensity=round(intensidade, 3),
                        label="Pico de Aceleração do Chat",
                        metadata={
                            "zscore": round(z, 2),
                            "msg_por_segundo": round(taxa, 1),
                            "media_canal": round(media_taxa, 1),
                            "total_no_balde": buckets[idx],
                        },
                    )
                )

        return picos
