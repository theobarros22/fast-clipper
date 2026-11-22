"""
Testes de falha rápida por falta de memória (S1).

Valida:
1. `transcribe` com memória baixa levanta InsufficientMemoryError e não chama `_get_model`.
2. Job de render que lança `Cannot allocate memory` termina em FALHOU com attempts == 1.
3. Falha comum (`Invalid argument`) continua sendo recolocada na fila (retryable=True).
4. `POST /api/v1/studio/transcribe` com memória baixa devolve HTTP 503 com mensagem em português.
5. `POST /api/v1/studio/render-now` com memória baixa devolve HTTP 503 com mensagem em português.
6. Cenário do piloto com memória baixa encerra espera em menos de 10 s (time.monotonic).
7. `AutopilotRunner._wait_jobs` loga apenas quando status ou step_description muda.
8. Cobertura H2: valor inválido em FASTCLIPPER_MIN_FREE_GB recorre ao padrão com warning.
9. Cobertura H2: limite exato onde livre_gb == minimo_gb não levanta exceção.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app
from fastclipper.autopilot.config import AutopilotConfig
from fastclipper.autopilot.runner import AutopilotRunner
from fastclipper.autopilot.state import AutopilotState
from fastclipper.process.memory_guard import (
    MINIMOS_GB,
    InsufficientMemoryError,
    ensure_free_memory,
    is_out_of_memory_error,
)
from fastclipper.queue.durable_queue import DurableTaskQueue, JobStatus
from fastclipper.rendering.pipeline_runner import JobExecutionResult, PipelineRunner
from fastclipper.storage.sqlite_store import SQLiteStore, SQLiteTaskQueue
from fastclipper.transcribe.whisper_service import WhisperTranscriptionService

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_VIDEO = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
client = TestClient(app)


# ---------------------------------------------------------------------------
# Testes herdados da H2 (cobertura adicional solicitada)
# ---------------------------------------------------------------------------


def test_h2_invalid_env_var_fallback(monkeypatch, caplog):
    """Valor inválido em FASTCLIPPER_MIN_FREE_GB emite warning e usa o mínimo padrão sem quebrar."""
    monkeypatch.setenv("FASTCLIPPER_MIN_FREE_GB", "texto_invalido_xyz")
    monkeypatch.setattr("fastclipper.process.memory_guard.free_memory_gb", lambda: 3.5)

    with caplog.at_level(logging.WARNING):
        res = ensure_free_memory(2.5, "transcricao")
        assert res == 3.5
        assert any("FASTCLIPPER_MIN_FREE_GB" in r.message for r in caplog.records)

    # Com memória insuficiente para o padrão (2.5), deve falhar
    monkeypatch.setattr("fastclipper.process.memory_guard.free_memory_gb", lambda: 1.5)
    with pytest.raises(InsufficientMemoryError) as exc_info:
        ensure_free_memory(2.5, "transcricao")
    assert exc_info.value.minimo_gb == 2.5
    assert exc_info.value.livre_gb == 1.5


def test_h2_exact_boundary_memory(monkeypatch):
    """Quando memória livre é exatamente igual ao mínimo exigido, a operação é permitida."""
    monkeypatch.delenv("FASTCLIPPER_MIN_FREE_GB", raising=False)
    monkeypatch.setattr("fastclipper.process.memory_guard.free_memory_gb", lambda: 2.5)

    # livre == minimo não deve levantar exceção
    res = ensure_free_memory(2.5, "transcricao")
    assert res == 2.5

    # livre == minimo para render
    monkeypatch.setattr("fastclipper.process.memory_guard.free_memory_gb", lambda: 2.0)
    res_render = ensure_free_memory(2.0, "render")
    assert res_render == 2.0


# ---------------------------------------------------------------------------
# Whisper fail-fast
# ---------------------------------------------------------------------------


def test_whisper_transcribe_low_memory_fails_fast_without_loading_model(monkeypatch, tmp_path):
    """Com 0.5 GB livres, transcribe levanta InsufficientMemoryError e NÃO chama _get_model."""
    monkeypatch.delenv("FASTCLIPPER_MIN_FREE_GB", raising=False)
    monkeypatch.setattr("fastclipper.process.memory_guard.free_memory_gb", lambda: 0.5)

    # Cria arquivo fictício de mídia
    dummy_media = tmp_path / "audio.mp4"
    dummy_media.write_bytes(b"dummy media content")

    service = WhisperTranscriptionService(store=None)
    mock_get_model = MagicMock()
    monkeypatch.setattr(service, "_get_model", mock_get_model)

    with pytest.raises(InsufficientMemoryError) as exc_info:
        service.transcribe(dummy_media, save_to_cache=False)

    assert exc_info.value.etapa == "transcricao"
    assert exc_info.value.livre_gb == 0.5
    assert exc_info.value.minimo_gb == MINIMOS_GB["transcricao"]
    assert "Memória livre 0,5 GB" in str(exc_info.value)
    assert "2,5 GB" in str(exc_info.value)
    mock_get_model.assert_not_called()


# ---------------------------------------------------------------------------
# DurableTaskQueue fail_job retryable parameter
# ---------------------------------------------------------------------------


def test_durable_queue_fail_job_retryable_behavior():
    """Valida o novo parâmetro retryable em DurableTaskQueue.fail_job."""
    queue = DurableTaskQueue()
    job = queue.enqueue("org_teste", "render_clip", {"video_path": "teste.mp4"}, max_attempts=3)

    # Lease para tentativa 1
    leased = queue.lease_job(job.job_id, "worker_1")
    assert leased is not None
    assert leased.attempts == 1

    # Falha retryable (padrão)
    queue.fail_job(job.job_id, "worker_1", "falha temporária de rede", retryable=True)
    assert job.status == JobStatus.CRIADO
    assert "Aguardando retry" in job.step_description

    # Lease para tentativa 2
    leased2 = queue.lease_job(job.job_id, "worker_1")
    assert leased2 is not None
    assert leased2.attempts == 2

    # Falha NÃO retryable: vai direto para FALHOU mesmo com attempts < max_attempts
    queue.fail_job(job.job_id, "worker_1", "Sem memória: Cannot allocate memory", retryable=False)
    assert job.status == JobStatus.FALHOU
    assert job.attempts == 2
    assert "Falha não recuperável" in job.step_description


def test_sqlite_task_queue_fail_job_retryable_behavior(tmp_path):
    """Valida o parâmetro retryable na SQLiteTaskQueue real com persistência SQLite."""
    store = SQLiteStore(tmp_path / "queue_test.db")
    queue = SQLiteTaskQueue(store)

    job = queue.enqueue("org_test", "render_clip", {"video_path": "x.mp4"}, max_attempts=3)
    leased = queue.lease_job(job.job_id, "worker_sql")
    assert leased is not None
    assert leased.attempts == 1

    # Falha comum com retryable=True deve voltar para CRIADO
    failed_retry = queue.fail_job(job.job_id, "worker_sql", "Invalid argument", retryable=True)
    assert failed_retry.status == JobStatus.CRIADO
    assert failed_retry.attempts == 1

    # Lease da tentativa 2
    leased2 = queue.lease_job(job.job_id, "worker_sql")
    assert leased2 is not None
    assert leased2.attempts == 2

    # Falha de memória com retryable=False deve ir direto para FALHOU
    failed_oom = queue.fail_job(
        job.job_id, "worker_sql", "Sem memória: Cannot allocate memory", retryable=False
    )
    assert failed_oom.status == JobStatus.FALHOU
    assert failed_oom.attempts == 2
    assert "Falha não recuperável" in failed_oom.step_description


# ---------------------------------------------------------------------------
# PipelineRunner fail-fast & OOM handling
# ---------------------------------------------------------------------------


def test_render_job_oom_terminates_in_failed_with_attempts_1(monkeypatch, tmp_path):
    """Job de render com Cannot allocate memory termina em FALHOU com attempts == 1."""
    queue = DurableTaskQueue()
    runner = PipelineRunner(queue=queue, output_dir=tmp_path / "out", worker_id="worker_oom")

    job = queue.enqueue("org_teste", "render_clip", {"video_path": "dummy.mp4"}, max_attempts=3)
    leased = queue.lease_job(job.job_id, runner.worker_id)
    assert leased is not None
    assert leased.attempts == 1

    # Simula render_clip retornando erro de OOM do FFmpeg
    def fake_render_clip(params, job_id=None, cancel_event=None):
        return JobExecutionResult(
            job_id=job.job_id,
            success=False,
            error="ffmpeg exited with code 1: Cannot allocate memory",
        )

    monkeypatch.setattr(runner, "render_clip", fake_render_clip)

    res = runner.process_job(leased)
    assert res.success is False
    assert job.status == JobStatus.FALHOU
    assert job.attempts == 1
    assert job.error.startswith("Sem memória:")


def test_render_job_insufficient_memory_exception_terminates_in_failed(monkeypatch, tmp_path):
    """Quando ensure_free_memory levanta InsufficientMemoryError, termina em FALHOU com attempts == 1."""
    queue = DurableTaskQueue()
    runner = PipelineRunner(queue=queue, output_dir=tmp_path / "out", worker_id="worker_oom_exc")

    job = queue.enqueue("org_teste", "render_clip", {"video_path": "dummy.mp4"}, max_attempts=3)
    leased = queue.lease_job(job.job_id, runner.worker_id)
    assert leased is not None

    def fake_render_clip(params, job_id=None, cancel_event=None):
        raise InsufficientMemoryError(etapa="render", livre_gb=0.8, minimo_gb=2.0)

    monkeypatch.setattr(runner, "render_clip", fake_render_clip)

    res = runner.process_job(leased)
    assert res.success is False
    assert job.status == JobStatus.FALHOU
    assert job.attempts == 1
    assert job.error.startswith("Sem memória:")
    assert "0,8 GB" in job.error


def test_render_job_common_error_retries_normally(monkeypatch, tmp_path):
    """Falha comum (Invalid argument) continua sendo recolocada na fila (comportamento antigo)."""
    queue = DurableTaskQueue()
    runner = PipelineRunner(queue=queue, output_dir=tmp_path / "out", worker_id="worker_normal")

    job = queue.enqueue("org_teste", "render_clip", {"video_path": "dummy.mp4"}, max_attempts=3)
    leased = queue.lease_job(job.job_id, runner.worker_id)
    assert leased is not None
    assert leased.attempts == 1

    def fake_render_clip(params, job_id=None, cancel_event=None):
        return JobExecutionResult(
            job_id=job.job_id,
            success=False,
            error="Invalid argument in layout calculation",
        )

    monkeypatch.setattr(runner, "render_clip", fake_render_clip)

    res = runner.process_job(leased)
    assert res.success is False
    # Falha comum deve recolocar para nova tentativa
    assert job.status == JobStatus.CRIADO
    assert job.attempts == 1
    assert "Aguardando retry" in job.step_description


# ---------------------------------------------------------------------------
# API endpoints HTTP 503 on low memory
# ---------------------------------------------------------------------------


def test_api_transcribe_low_memory_returns_503(monkeypatch):
    """POST /api/v1/studio/transcribe com memória baixa devolve HTTP 503 com detalhe em português."""
    monkeypatch.delenv("FASTCLIPPER_MIN_FREE_GB", raising=False)
    monkeypatch.setattr("fastclipper.process.memory_guard.free_memory_gb", lambda: 0.5)

    resp = client.post(
        "/api/v1/studio/transcribe",
        json={"video_path": str(SYNTHETIC_VIDEO), "force_retranscribe": True},
    )
    assert resp.status_code == 503
    data = resp.json()
    assert "detail" in data
    assert "Memória livre 0,5 GB" in data["detail"]
    assert "transcricao" in data["detail"]
    assert "2,5 GB" in data["detail"]


def test_api_render_now_low_memory_returns_503(monkeypatch):
    """POST /api/v1/studio/render-now com memória baixa devolve HTTP 503 com detalhe em português."""
    monkeypatch.delenv("FASTCLIPPER_MIN_FREE_GB", raising=False)
    monkeypatch.setattr("fastclipper.process.memory_guard.free_memory_gb", lambda: 0.5)

    resp = client.post(
        "/api/v1/studio/render-now",
        json={
            "video_path": str(SYNTHETIC_VIDEO),
            "inicio_s": 0.0,
            "fim_s": 1.0,
            "aspect_ratio": "9:16",
        },
    )
    assert resp.status_code == 503
    data = resp.json()
    assert "detail" in data
    assert "Memória livre 0,5 GB" in data["detail"]
    assert "render" in data["detail"]
    assert "2,0 GB" in data["detail"]


# ---------------------------------------------------------------------------
# Autopilot fail-fast & logging
# ---------------------------------------------------------------------------


class MockStudioClientFailFast:
    """Mock de StudioClient que simula job falhando imediatamente com Sem memória."""

    def __init__(self, job_error="Sem memória: Memória livre 0,5 GB"):
        self.job_error = job_error
        self.calls = 0

    def get(self, path):
        self.calls += 1
        return {
            "status": "falhou",
            "step_description": f"Falha não recuperável: {self.job_error}",
            "error": self.job_error,
        }


def test_autopilot_runner_low_memory_finishes_under_10_seconds(tmp_path):
    """Cenário do piloto com memória baixa termina em menos de 10 s (medido com time.monotonic)."""
    cfg = AutopilotConfig()
    mock_client = MockStudioClientFailFast("Sem memória: Memória livre 0,5 GB; a etapa precisa de 2,0 GB.")
    runner = AutopilotRunner(
        config=cfg,
        client=mock_client,
        state=AutopilotState(tmp_path / "state.db"),
        poll_interval_s=0.5,
        render_timeout_s=300.0,
    )

    t0 = time.monotonic()
    jobs = runner._wait_jobs(["job_mem_fail_1", "job_mem_fail_2"])
    elapsed = time.monotonic() - t0

    assert elapsed < 10.0, f"Demorou {elapsed:.2f}s, esperado menos de 10s"
    assert len(jobs) == 2
    assert all(j["status"] == "falhou" for j in jobs)


def test_autopilot_wait_jobs_logs_only_on_status_or_step_change(tmp_path, caplog):
    """AutopilotRunner._wait_jobs emite logger.info apenas quando status ou step_description mudar."""
    cfg = AutopilotConfig()

    class MockPollingClient:
        def __init__(self):
            self.count = 0

        def get(self, path):
            self.count += 1
            if self.count <= 3:
                return {"status": "renderizando", "step_description": "Codificando 9:16"}
            elif self.count <= 5:
                return {"status": "renderizando", "step_description": "Aplicando legendas"}
            else:
                return {"status": "aprovado", "step_description": "Concluído com sucesso"}

    runner = AutopilotRunner(
        config=cfg,
        client=MockPollingClient(),
        state=AutopilotState(tmp_path / "state.db"),
        poll_interval_s=0.02,
        render_timeout_s=5.0,
    )

    with caplog.at_level(logging.INFO, logger="fastclipper.autopilot"):
        runner._wait_jobs(["job_001"])

    job_logs = [r.message for r in caplog.records if "Job job_001:" in r.message]
    # Mesmo com ~6 consultas, deve haver exatamente 3 logs de mudança de estado:
    # 1. status=renderizando (Codificando 9:16)
    # 2. status=renderizando (Aplicando legendas)
    # 3. status=aprovado (Concluído com sucesso)
    assert len(job_logs) == 3, f"Esperado 3 logs de mudança de estado, obteve {len(job_logs)}: {job_logs}"
