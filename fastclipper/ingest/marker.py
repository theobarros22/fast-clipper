"""
Gerenciador de marcação manual com janela temporal e ciclo de vida de candidatos.

Garante que o candidato só seja emitido para revisão humana após a conclusão
da janela posterior (post_window_s), mantendo os segmentos protegidos no buffer.
"""

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from fastclipper.ingest.buffer import SegmentedBuffer
from fastclipper.ingest.clock import VirtualClock

logger = logging.getLogger(__name__)


class CandidateStatus(str, Enum):
    AGUARDANDO_POST_WINDOW = "aguardando_post_window"
    PRONTO_PARA_REVISAO = "pronto_para_revisao"
    EM_EDICAO = "em_edicao"
    APROVADO = "aprovado"
    REJEITADO = "rejeitado"
    RENDERIZANDO = "renderizando"
    CONCLUIDO = "concluido"
    FALHA = "falha"
    REQUER_REVISAO = "requer_revisao"


@dataclass
class CandidateClip:
    id: str
    stream_id: str
    timestamp_marca: float
    inicio_s: float
    fim_s: float
    pre_window_s: float
    post_window_s: float
    titulo: str
    status: CandidateStatus = CandidateStatus.AGUARDANDO_POST_WINDOW
    versao: int = 1
    locked_segment_ids: List[int] = field(default_factory=list)
    erro: Optional[str] = None
    criado_em: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


class ManualMarker:
    """Controlador de marcações e candidatos gerados por operadores."""

    def __init__(
        self,
        buffer: SegmentedBuffer,
        clock: VirtualClock,
        pre_window_s: float = 15.0,
        post_window_s: float = 20.0,
    ):
        self.buffer = buffer
        self.clock = clock
        self.pre_window_s = float(pre_window_s)
        self.post_window_s = float(post_window_s)

        self._candidates: Dict[str, CandidateClip] = {}
        self._lock = threading.Lock()

    def mark(
        self,
        timestamp: Optional[float] = None,
        titulo: str = "Momento Marcado",
        pre_s: Optional[float] = None,
        post_s: Optional[float] = None,
    ) -> CandidateClip:
        """Registra uma marcação manual no tempo indicado (ou no tempo atual do relógio)."""
        ts = self.clock.current_time() if timestamp is None else float(timestamp)
        pre = self.pre_window_s if pre_s is None else float(pre_s)
        post = self.post_window_s if post_s is None else float(post_s)

        t_inicio = max(0.0, ts - pre)
        t_fim = ts + post
        cand_id = f"cand_{uuid.uuid4().hex[:8]}"

        candidato = CandidateClip(
            id=cand_id,
            stream_id=self.buffer.stream_id,
            timestamp_marca=round(ts, 2),
            inicio_s=round(t_inicio, 2),
            fim_s=round(t_fim, 2),
            pre_window_s=pre,
            post_window_s=post,
            titulo=titulo,
            status=CandidateStatus.AGUARDANDO_POST_WINDOW,
        )

        with self._lock:
            # Trava os segmentos que já existem no buffer
            locked = self.buffer.lock_range(candidato.inicio_s, ts, lock_id=cand_id)
            candidato.locked_segment_ids = locked
            self._candidates[cand_id] = candidato

        # Verifica imediatamente se a janela futura já está disponível
        self._check_post_window(candidato)
        return candidato

    def check_pending_candidates(self, current_time: Optional[float] = None, stream_finished: bool = False) -> List[CandidateClip]:
        """Atualiza o estado de candidatos pendentes verificando se o tempo atual ultrapassou a janela posterior."""
        agora = self.clock.current_time() if current_time is None else current_time
        prontos: List[CandidateClip] = []

        with self._lock:
            for cand in self._candidates.values():
                if cand.status == CandidateStatus.AGUARDANDO_POST_WINDOW:
                    if agora >= cand.fim_s or stream_finished:
                        # Se o buffer possuir segmentos ativos, exige que cubram o intervalo do candidato
                        segs = self.buffer.get_segments_in_range(cand.inicio_s, cand.fim_s)
                        buffer_tem_segs = len(self.buffer) > 0
                        buffer_cobre = (not buffer_tem_segs) or (segs and segs[-1].timestamp_fim >= cand.fim_s) or stream_finished

                        if buffer_cobre:
                            novos_locks = self.buffer.lock_range(cand.inicio_s, cand.fim_s, lock_id=cand.id)
                            cand.locked_segment_ids = novos_locks
                            cand.status = CandidateStatus.PRONTO_PARA_REVISAO
                            prontos.append(cand)

        return prontos

    def _check_post_window(self, cand: CandidateClip) -> None:
        agora = self.clock.current_time()
        if agora >= cand.fim_s:
            segs = self.buffer.get_segments_in_range(cand.inicio_s, cand.fim_s)
            buffer_tem_segs = len(self.buffer) > 0
            buffer_cobre = (not buffer_tem_segs) or (segs and segs[-1].timestamp_fim >= cand.fim_s)
            if buffer_cobre:
                novos_locks = self.buffer.lock_range(cand.inicio_s, cand.fim_s, lock_id=cand.id)
                cand.locked_segment_ids = novos_locks
                cand.status = CandidateStatus.PRONTO_PARA_REVISAO

    def get_candidate(self, candidate_id: str) -> Optional[CandidateClip]:
        with self._lock:
            return self._candidates.get(candidate_id)

    def list_candidates(self, status: Optional[CandidateStatus] = None) -> List[CandidateClip]:
        with self._lock:
            if status is None:
                return list(self._candidates.values())
            return [c for c in self._candidates.values() if c.status == status]

    def adjust_boundaries(self, candidate_id: str, new_start_s: float, new_end_s: float) -> CandidateClip:
        """Permite ao operador ajustar início e fim do clipe antes da renderização."""
        if new_start_s < 0:
            raise ValueError("O início do clipe não pode ser negativo.")
        if new_end_s <= new_start_s:
            raise ValueError("O fim do clipe deve ser estritamente maior que o início.")

        with self._lock:
            cand = self._candidates.get(candidate_id)
            if not cand:
                raise KeyError(f"Candidato não encontrado: {candidate_id}")

            cand.inicio_s = round(float(new_start_s), 2)
            cand.fim_s = round(float(new_end_s), 2)
            cand.versao += 1

            # Atualiza locks de segmentos
            self.buffer.unlock_range(lock_id=cand.id)
            cand.locked_segment_ids = self.buffer.lock_range(cand.inicio_s, cand.fim_s, lock_id=cand.id)
            return cand

    def approve(self, candidate_id: str) -> CandidateClip:
        with self._lock:
            cand = self._candidates.get(candidate_id)
            if not cand:
                raise KeyError(f"Candidato não encontrado: {candidate_id}")
            cand.status = CandidateStatus.APROVADO
            cand.versao += 1
            return cand

    def reject(self, candidate_id: str) -> CandidateClip:
        with self._lock:
            cand = self._candidates.get(candidate_id)
            if not cand:
                raise KeyError(f"Candidato não encontrado: {candidate_id}")
            cand.status = CandidateStatus.REJEITADO
            cand.versao += 1
            # Libera todos os locks de buffer vinculados a este candidato
            self.buffer.unlock_range(lock_id=cand.id)
            cand.locked_segment_ids.clear()
            return cand
