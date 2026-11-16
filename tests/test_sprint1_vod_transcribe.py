"""
Testes automatizados do Sprint 1:
1. Persistência real em SQLite (WAL) com recuperação após reinício
2. Ingestão local e validação de autorização / SSRF
3. Serviço de transcrição faster-whisper com cache por SHA-256
4. Renderização usando timestamps reais de palavras deslocados para o clipe
"""

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app, sqlite_store, whisper_service
from fastclipper.ingest.vod_ingest import VodIngestor
from fastclipper.live.youtube_live import SecuritySSRFError
from fastclipper.queue.durable_queue import JobStatus
from fastclipper.storage.sqlite_store import SQLiteStore, SQLiteTaskQueue
from fastclipper.transcribe.whisper_service import WhisperTranscriptionService

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_VIDEO = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"


def test_sqlite_queue_persists_across_restart(tmp_path):
    db_file = tmp_path / "test_fc.db"

    # Instância 1: enfileira tarefa
    store1 = SQLiteStore(db_file)
    q1 = SQLiteTaskQueue(store1)
    job = q1.enqueue(
        org_id="org_local",
        job_type="render_clip",
        params={"video_path": "x.mp4"},
        idempotency_key="persist_key_1",
    )
    assert job.status == JobStatus.CRIADO

    # Simula reinício do servidor criando nova instância apontando para o mesmo SQLite
    store2 = SQLiteStore(db_file)
    q2 = SQLiteTaskQueue(store2)
    recovered = q2.get_job(job.job_id)
    assert recovered is not None
    assert recovered.job_id == job.job_id
    assert recovered.status == JobStatus.CRIADO

    leased = q2.lease_next_job("worker_after_restart", lease_duration_s=0.05)
    assert leased is not None
    assert leased.attempts == 1

    # Simula worker caindo e lease expirando -> outro worker recupera
    time.sleep(0.08)
    re_leased = q2.lease_next_job("worker_recovery")
    assert re_leased is not None
    assert re_leased.job_id == job.job_id
    assert re_leased.attempts == 2


def test_vod_ingestor_requires_auth_and_blocks_ssrf(tmp_path):
    store = SQLiteStore(tmp_path / "ingest.db")
    ingestor = VodIngestor(store=store, projects_root=tmp_path / "projects")

    with pytest.raises(ValueError, match="autorização"):
        ingestor.import_local_file(SYNTHETIC_VIDEO, authorization_ref="")

    with pytest.raises(SecuritySSRFError):
        ingestor.download_from_url("http://127.0.0.1:8080/live.mp4", authorization_ref="AUTH-OK")

    src = ingestor.import_local_file(
        SYNTHETIC_VIDEO,
        authorization_ref="TERMO-DEMO-01",
        title="Gameplay Sintético",
    )
    assert src["source_id"].startswith("src_")
    assert src["authorization_ref"] == "TERMO-DEMO-01"
    assert len(src["file_sha256"]) == 64
    assert src["duration_s"] > 0


def test_whisper_service_caches_by_sha256(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "whisper.db")
    svc = WhisperTranscriptionService(store=store, model_name="small")

    fake_segments = [
        SimpleNamespace(
            text="Olha essa jogada incrível",
            end=2.5,
            words=[
                SimpleNamespace(word=" Olha", start=0.2, end=0.6),
                SimpleNamespace(word=" essa", start=0.7, end=1.0),
                SimpleNamespace(word=" jogada", start=1.1, end=1.7),
                SimpleNamespace(word=" incrível", start=1.8, end=2.4),
            ],
        )
    ]
    fake_info = SimpleNamespace(duration=5.0, language="pt")
    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter(fake_segments), fake_info)

    monkeypatch.setattr(svc, "_get_model", lambda *args, **kwargs: mock_model)

    # 1ª chamada: executa modelo
    res1 = svc.transcribe(SYNTHETIC_VIDEO, language="pt")
    assert res1["from_cache"] is False
    assert len(res1["words"]) == 4
    assert mock_model.transcribe.call_count == 1

    # 2ª chamada: vem direto do cache SQLite sem chamar o modelo
    res2 = svc.transcribe(SYNTHETIC_VIDEO, language="pt")
    assert res2["from_cache"] is True
    assert res2["words"] == res1["words"]
    assert mock_model.transcribe.call_count == 1


def test_api_studio_import_transcribe_and_render_with_real_words(monkeypatch):
    # 1. Importa fonte
    res_imp = client.post(
        "/api/v1/studio/sources/import",
        json={
            "video_path": str(SYNTHETIC_VIDEO),
            "authorization_ref": "AUTH-TESTE-SPRINT1",
            "title": "Teste Sprint 1",
        },
    )
    assert res_imp.status_code == 200
    src = res_imp.json()
    source_id = src["source_id"]

    # 2. Transcreve (com modelo mockado para rapidez no CI)
    fake_segments = [
        SimpleNamespace(
            text="Agora vai acontecer o corte",
            end=3.4,
            words=[
                SimpleNamespace(word="Agora", start=2.1, end=2.4),
                SimpleNamespace(word="vai", start=2.5, end=2.7),
                SimpleNamespace(word="acontecer", start=2.8, end=3.2),
            ],
        )
    ]
    fake_info = SimpleNamespace(duration=5.0, language="pt")
    mock_model = MagicMock()
    mock_model.transcribe.return_value = (iter(fake_segments), fake_info)
    monkeypatch.setattr(whisper_service, "_get_model", lambda *args, **kwargs: mock_model)

    res_tr = client.post(
        "/api/v1/studio/transcribe",
        json={"source_id": source_id, "language": "pt", "force_retranscribe": True},
    )
    assert res_tr.status_code == 200
    tr = res_tr.json()
    assert len(tr["words"]) == 3

    # 3. Renderiza usando timestamps absolutos das palavras para corte em inicio_s=2.0
    res_rend = client.post(
        "/api/v1/studio/render-now",
        json={
            "video_path": str(SYNTHETIC_VIDEO),
            "inicio_s": 2.0,
            "fim_s": 3.5,
            "layout": "solo",
            "words": tr["words"],
            "subtitles_are_absolute": True,
            "burn_subtitles": True,
        },
    )
    assert res_rend.status_code == 200
    data = res_rend.json()
    assert data["success"] is True
    assert Path(data["output_path"]).is_file()
