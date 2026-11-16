"""
PROTÓTIPO EM MEMÓRIA — Fila de processamento assíncrono com semântica de leasing e idempotência.

Aviso: os dados residem apenas em memória (perdidos ao reiniciar). Persistência em SQLite será
introduzida no Sprint 1 do plano-mestre-v3.
"""

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    CRIADO = "criado"
    CAPTURANDO = "capturando"
    ANALISANDO = "analisando"
    CANDIDATO = "candidato"
    RENDERIZANDO = "renderizando"
    AGUARDANDO_REVISAO = "aguardando_revisao"
    APROVADO = "aprovado"
    AGENDADO = "agendado"
    PUBLICADO = "publicado"
    FALHOU = "falhou"
    CANCELADO = "cancelado"


@dataclass
class QueueJob:
    job_id: str
    org_id: str
    job_type: str
    idempotency_key: str
    params: Dict[str, Any]
    status: JobStatus = JobStatus.CRIADO
    progress_percent: float = 0.0
    step_description: str = "Aguardando processamento"
    eta_seconds: Optional[float] = None
    worker_id: Optional[str] = None
    leased_until: Optional[float] = None
    attempts: int = 0
    max_attempts: int = 3
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


class DurableTaskQueue:
    """Fila durável thread-safe com proteção de concorrência e chave idempotente."""

    def __init__(self):
        self._jobs: Dict[str, QueueJob] = {}
        self._idempotency_index: Dict[str, str] = {}  # idempotency_key -> job_id
        self._lock = threading.Lock()

    def enqueue(
        self,
        org_id: str,
        job_type: str,
        params: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
    ) -> QueueJob:
        """Enfileira uma tarefa garantindo não duplicidade via chave idempotente."""
        with self._lock:
            # Se chave não for fornecida, calcula hash determinístico dos parâmetros
            if not idempotency_key:
                dados_serializados = json.dumps({"org": org_id, "type": job_type, "params": params}, sort_keys=True)
                idempotency_key = hashlib.sha256(dados_serializados.encode("utf-8")).hexdigest()

            # Se já existir uma tarefa com esta chave
            if idempotency_key in self._idempotency_index:
                existente_id = self._idempotency_index[idempotency_key]
                existente = self._jobs[existente_id]
                logger.info("Chave idempotente repetida detectada (%s). Retornando job existente %s.", idempotency_key, existente.job_id)
                return existente

            job_id = f"job_{uuid.uuid4().hex[:10]}"
            job = QueueJob(
                job_id=job_id,
                org_id=org_id,
                job_type=job_type,
                idempotency_key=idempotency_key,
                params=params,
                status=JobStatus.CRIADO,
                max_attempts=max_attempts,
            )
            self._jobs[job_id] = job
            self._idempotency_index[idempotency_key] = job_id
            return job

    def lease_next_job(
        self,
        worker_id: str,
        job_types: Optional[List[str]] = None,
        lease_duration_s: float = 60.0,
    ) -> Optional[QueueJob]:
        """Reserva atomicamente o próximo trabalho elegível para execução (SKIP LOCKED)."""
        now = time.time()
        with self._lock:
            for job in self._jobs.values():
                if job_types and job.job_type not in job_types:
                    continue

                # Elegível se estiver CRIADO ou se for uma tarefa cujo lease expirou
                elegivel_novo = job.status == JobStatus.CRIADO
                elegivel_recuperacao = (
                    job.status in (JobStatus.ANALISANDO, JobStatus.RENDERIZANDO)
                    and job.leased_until is not None
                    and job.leased_until < now
                    and job.attempts < job.max_attempts
                )

                if elegivel_novo or elegivel_recuperacao:
                    job.worker_id = worker_id
                    job.leased_until = now + lease_duration_s
                    job.attempts += 1
                    job.status = JobStatus.RENDERIZANDO if "render" in job.job_type else JobStatus.ANALISANDO
                    job.updated_at = now
                    job.step_description = f"Assumido pelo worker {worker_id}"
                    return job
            return None

    def update_progress(
        self,
        job_id: str,
        worker_id: str,
        progress_percent: float,
        step_description: str,
        eta_seconds: Optional[float] = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.worker_id != worker_id:
                raise PermissionError("Worker não possui a concessão ativa desta tarefa.")

            job.progress_percent = round(min(100.0, max(0.0, float(progress_percent))), 2)
            job.step_description = step_description
            job.eta_seconds = eta_seconds
            job.updated_at = time.time()

    def complete_job(
        self,
        job_id: str,
        worker_id: str,
        result: Dict[str, Any],
        final_status: JobStatus = JobStatus.PUBLICADO,
    ) -> QueueJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.worker_id != worker_id:
                raise PermissionError("Worker não possui a concessão ativa desta tarefa.")

            job.status = final_status
            job.progress_percent = 100.0
            job.step_description = "Concluído com sucesso"
            job.result = result
            job.leased_until = None
            job.updated_at = time.time()
            return job

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error_message: str,
    ) -> QueueJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.worker_id != worker_id:
                raise PermissionError("Worker não possui a concessão ativa desta tarefa.")

            job.error = error_message
            job.leased_until = None
            job.updated_at = time.time()

            if job.attempts < job.max_attempts:
                job.status = JobStatus.CRIADO  # Recoloca para nova tentativa
                job.step_description = f"Tentativa {job.attempts} falhou: {error_message}. Aguardando retry."
            else:
                job.status = JobStatus.FALHOU
                job.step_description = f"Falha definitiva após {job.attempts} tentativas: {error_message}"
            return job

    def get_job(self, job_id: str) -> Optional[QueueJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def lease_job(self, job_id: str, worker_id: str, lease_duration_s: float = 60.0) -> Optional[QueueJob]:
        now = time.time()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job.status != JobStatus.CRIADO and not (
                job.status in (JobStatus.ANALISANDO, JobStatus.RENDERIZANDO)
                and job.leased_until is not None
                and job.leased_until < now
                and job.attempts < job.max_attempts
            ):
                return None
            job.worker_id = worker_id
            job.leased_until = now + lease_duration_s
            job.attempts += 1
            job.status = JobStatus.RENDERIZANDO if "render" in job.job_type else JobStatus.ANALISANDO
            job.step_description = f"Assumido pelo worker {worker_id}"
            job.updated_at = now
            return job

    def cancel_job(self, job_id: str, reason: str = "Cancelado pelo usuário") -> Optional[QueueJob]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.status = JobStatus.CANCELADO
            job.step_description = reason
            job.error = reason
            job.leased_until = None
            job.updated_at = time.time()
            return job
