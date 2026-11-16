"""
Testes automatizados para o PipelineRunner e execução em background de jobs de renderização.

Valida:
1. Renderização vertical 9:16 (Solo)
2. Renderização 9:16 com Layout Empilhado (Stacked: Facecam + Gameplay)
3. Queima de legendas animadas (.ass) com presets e word timing
4. Falha antecipada e proteção contra mídia inválida via MediaPrechecker
5. Ciclo de vida da fila durável: lease -> progresso -> conclusão idempotente
6. Execução de comparações Cross-POV via fila
7. Endpoint da API /api/v1/queue/worker/process-next
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app, durable_queue
from fastclipper.cli import main
from fastclipper.queue.durable_queue import DurableTaskQueue, JobStatus
from fastclipper.rendering.pipeline_runner import JobExecutionResult, PipelineRunner

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_VIDEO = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"


@pytest.fixture
def runner_with_tmp(tmp_path):
    queue = DurableTaskQueue()
    out_dir = tmp_path / "clips_out"
    runner = PipelineRunner(queue=queue, output_dir=out_dir, worker_id="test-worker-01")
    return runner, queue, out_dir


def test_pipeline_runner_initialization(runner_with_tmp):
    runner, queue, out_dir = runner_with_tmp
    assert runner.worker_id == "test-worker-01"
    assert out_dir.exists()
    assert runner.prechecker is not None
    assert runner.render_adapter is not None


def test_pipeline_runner_render_clip_solo(runner_with_tmp):
    runner, queue, out_dir = runner_with_tmp

    params = {
        "video_path": str(SYNTHETIC_VIDEO),
        "inicio_s": 0.5,
        "fim_s": 2.0,
        "layout": "solo",
        "require_audio": True,
    }

    result = runner.render_clip(params, job_id="test_solo_01")
    assert result.success is True
    assert result.output_video_path is not None
    assert Path(result.output_video_path).is_file()
    assert result.duration >= 1.0
    assert result.metadata["layout"] == "solo"


def test_pipeline_runner_render_clip_stacked_with_subtitles(runner_with_tmp):
    runner, queue, out_dir = runner_with_tmp

    params = {
        "video_path": str(SYNTHETIC_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 1.5,
        "layout": "stacked",
        "facecam_rect": {"x": 50, "y": 50, "w": 400, "h": 300},
        "gameplay_rect": {"x": 500, "y": 100, "w": 800, "h": 600},
        "subtitles": [
            {"word": "GTA", "start_s": 0.1, "end_s": 0.5},
            {"word": "Clipper", "start_s": 0.6, "end_s": 1.2},
        ],
        "subtitle_preset": "viral_bounce",
        "burn_subtitles": True,
        "require_audio": True,
    }

    result = runner.render_clip(params, job_id="test_stacked_sub_01")
    assert result.success is True
    assert result.output_video_path is not None
    assert Path(result.output_video_path).is_file()
    assert result.subtitles_path is not None
    assert Path(result.subtitles_path).is_file()
    assert result.metadata["layout"] == "stacked"
    assert result.metadata["subtitles_burned"] is True


def test_pipeline_runner_precheck_rejection(runner_with_tmp, tmp_path):
    runner, queue, out_dir = runner_with_tmp

    # Vídeo que não existe
    params_inexistente = {
        "video_path": str(tmp_path / "video_fantasma.mp4"),
        "inicio_s": 1.0,
        "fim_s": 3.0,
    }
    res_inex = runner.render_clip(params_inexistente, job_id="job_inex")
    assert res_inex.success is False
    assert "pré-check" in res_inex.error.lower() or "não encontrado" in res_inex.error.lower()

    # Duração menor ou igual a zero
    params_zero = {
        "video_path": str(SYNTHETIC_VIDEO),
        "inicio_s": 3.0,
        "fim_s": 2.0,
    }
    res_zero = runner.render_clip(params_zero, job_id="job_zero")
    assert res_zero.success is False
    assert "duração inválida" in res_zero.error.lower()


def test_pipeline_runner_queue_integration(runner_with_tmp):
    runner, queue, out_dir = runner_with_tmp

    # 1. Enfileira o job
    job = queue.enqueue(
        org_id="org_teste",
        job_type="render_clip",
        params={
            "video_path": str(SYNTHETIC_VIDEO),
            "inicio_s": 0.2,
            "fim_s": 1.2,
            "layout": "solo",
        },
        idempotency_key="key_teste_01",
    )
    assert job.status == JobStatus.CRIADO

    # 2. Worker executa o passo
    exec_res = runner.run_single_step()
    assert exec_res is not None
    assert exec_res.success is True

    # 3. Confere que a tarefa na fila foi atualizada para APROVADO
    job_atualizado = queue.get_job(job.job_id)
    assert job_atualizado.status == JobStatus.APROVADO
    assert job_atualizado.progress_percent == 100.0
    assert job_atualizado.result is not None


def test_pipeline_runner_comparison_job(runner_with_tmp):
    runner, queue, out_dir = runner_with_tmp

    track1_video = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    track2_video = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_duas_pessoas.mp4"

    job = queue.enqueue(
        org_id="org_teste",
        job_type="render_comparison",
        params={
            "event_id": "evt_cross_01",
            "mode": "sequential",
            "start_s": 0.5,
            "end_s": 2.0,
            "tracks": [
                {
                    "creator_id": "c1",
                    "creator_name": "Criador Alpha",
                    "video_path": str(track1_video),
                    "offset": 0.0,
                    "authorized": True,
                },
                {
                    "creator_id": "c2",
                    "creator_name": "Criador Beta",
                    "video_path": str(track2_video),
                    "offset": 0.0,
                    "authorized": True,
                },
            ],
        },
        idempotency_key="key_comp_01",
    )

    exec_res = runner.run_single_step()
    assert exec_res is not None
    assert exec_res.success is True
    assert exec_res.output_video_path is not None
    assert Path(exec_res.output_video_path).is_file()

    job_atualizado = queue.get_job(job.job_id)
    assert job_atualizado.status == JobStatus.APROVADO


def test_api_queue_worker_process_next():
    # 0. Garante isolamento da fila global
    durable_queue._jobs.clear()
    durable_queue._idempotency_index.clear()

    # 1. Fila vazia
    res_empty = client.post("/api/v1/queue/worker/process-next")
    assert res_empty.status_code == 200
    assert res_empty.json()["processed"] is False

    # 2. Enfileira um job via API
    res_enqueue = client.post(
        "/api/v1/queue/jobs",
        json={
            "job_type": "render_clip",
            "params": {
                "video_path": str(SYNTHETIC_VIDEO),
                "inicio_s": 0.5,
                "fim_s": 1.5,
                "layout": "solo",
            },
            "idempotency_key": "api_test_clip_01",
        },
    )
    assert res_enqueue.status_code == 200
    job_id = res_enqueue.json()["job_id"]

    # 3. Processa via endpoint
    res_proc = client.post("/api/v1/queue/worker/process-next")
    assert res_proc.status_code == 200
    data = res_proc.json()
    assert data["processed"] is True
    assert data["result"]["success"] is True

    # 4. Confere status do job
    res_status = client.get(f"/api/v1/queue/jobs/{job_id}")
    assert res_status.status_code == 200
    assert res_status.json()["status"] == "aprovado"


def test_cli_queue_worker_run_pipeline(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["fastclipper", "queue", "worker", "--run-pipeline"])
    ret = main()
    assert ret == 0
    captured = capsys.readouterr()
    assert "FAST CLIPPER — FILA DURÁVEL" in captured.out
