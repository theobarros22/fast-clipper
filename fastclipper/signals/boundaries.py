"""
Ajuste fino e alinhamento de limites em sentenças completas (Sentence Boundary Snapping).

Inspirado e adaptado da engenharia de limites do AutoClip (boundaries.py).
Garante que nenhum clipe termine em meio a uma frase incompleta ou deixe perguntas
pendentes do tópico seguinte no final do vídeo.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

TERMINAL_PUNCT = re.compile(r'[.?!。？！…」』"”]\s*$')
INNER_END = re.compile(r'[.?!。？！…]+["”」』]?(?=\s|$)|[。？！]')
QUESTION_ENDING = re.compile(r'(\?|？|né|sabe|entende|certo)\s*$', re.IGNORECASE)
PAUSE_SEC = 0.6
MAX_LEAD_SEC = 15.0
MAX_TAIL_SEC = 20.0


@dataclass(frozen=True)
class SubtitleRow:
    start_s: float
    end_s: float
    text: str


class SentenceBoundarySnapper:
    """Ajustador de fronteiras para frases e pensamentos completos."""

    def __init__(
        self,
        pause_threshold_s: float = PAUSE_SEC,
        max_lead_s: float = MAX_LEAD_SEC,
        max_tail_s: float = MAX_TAIL_SEC,
    ):
        self.pause_threshold_s = float(pause_threshold_s)
        self.max_lead_s = float(max_lead_s)
        self.max_tail_s = float(max_tail_s)

    def snap_boundaries(
        self,
        rows: List[SubtitleRow],
        raw_start_s: float,
        raw_end_s: float,
    ) -> Tuple[float, float]:
        """Ajusta início e fim para respeitar o fechamento de sentenças e pensamentos."""
        if not rows:
            return raw_start_s, raw_end_s

        ordenadas = sorted(rows, key=lambda r: r.start_s)

        # 1. Encontra índices das linhas correspondentes
        idx_inicio = self._find_row_index(ordenadas, raw_start_s)
        idx_fim = self._find_row_index(ordenadas, raw_end_s)

        # 2. Ajuste de Início: recua até o início da frase/pensamento
        novo_inicio = raw_start_s
        if idx_inicio is not None:
            curr_idx = idx_inicio
            while curr_idx > 0 and (raw_start_s - ordenadas[curr_idx].start_s) <= self.max_lead_s:
                prev_row = ordenadas[curr_idx - 1]
                # Se a linha anterior termina uma frase ou tem uma pausa longa, paramos aqui
                if self._ends_sentence(prev_row, ordenadas[curr_idx]):
                    break
                curr_idx -= 1
            novo_inicio = ordenadas[curr_idx].start_s

        # 3. Ajuste de Fim: avança até a conclusão da frase atual ou descarta pergunta solta
        novo_fim = raw_end_s
        if idx_fim is not None:
            row_final = ordenadas[idx_fim]
            # Se a linha final for uma pergunta e tivermos conteúdo anterior no clipe, descarta a pergunta
            if QUESTION_ENDING.search(row_final.text) and idx_fim > 0 and (idx_inicio is None or idx_fim > idx_inicio):
                novo_fim = ordenadas[idx_fim - 1].end_s
            else:
                curr_idx = idx_fim
                while curr_idx < len(ordenadas) and (ordenadas[curr_idx].end_s - raw_end_s) <= self.max_tail_s:
                    row = ordenadas[curr_idx]
                    next_row = ordenadas[curr_idx + 1] if curr_idx + 1 < len(ordenadas) else None

                    # Se a linha atual fecha sentença, o clipe termina aqui
                    if self._ends_sentence(row, next_row):
                        novo_fim = row.end_s
                        break
                    curr_idx += 1

                if curr_idx < len(ordenadas):
                    novo_fim = max(novo_fim, ordenadas[min(curr_idx, len(ordenadas) - 1)].end_s)

        # Garante ordem coerente
        if novo_fim <= novo_inicio:
            novo_fim = novo_inicio + (raw_end_s - raw_start_s)

        return round(novo_inicio, 2), round(novo_fim, 2)

    def _find_row_index(self, rows: List[SubtitleRow], t: float) -> Optional[int]:
        for i, r in enumerate(rows):
            if r.start_s <= t <= r.end_s or (i + 1 < len(rows) and r.end_s <= t < rows[i + 1].start_s):
                return i
        return None

    def _ends_sentence(self, current: SubtitleRow, next_row: Optional[SubtitleRow]) -> bool:
        """Determina se a linha encerra um pensamento completo."""
        texto = current.text.strip()
        if TERMINAL_PUNCT.search(texto):
            return True

        # Se não houver pontuação, detecta pausa longa de fala
        if next_row is not None:
            gap = next_row.start_s - current.end_s
            if gap >= self.pause_threshold_s:
                return True

        return next_row is None
