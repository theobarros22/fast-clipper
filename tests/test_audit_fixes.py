import importlib
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

app_mod = importlib.import_module("fastclipper.api.app")
app = app_mod.app
durable_queue = app_mod.durable_queue
pipeline_runner = app_mod.pipeline_runner
from fastclipper.queue.durable_queue import DurableTaskQueue, JobStatus
from fastclipper.rendering.pipeline_runner import PipelineRunner
from fastclipper.storage.sqlite_store import SQLiteStore

FIXTURE_VIDEO = Path("tests/fixtures/synthetic/sintetico_gameplay_facecam.mp4")


def test_n1_upload_blocks_project_id_traversal():
    client = TestClient(app)
    res = client.post(
        "/api/v1/studio/sources/upload?filename=clip.mp4&project_id=../../../../../tmp/fc_escape",
        content=b"fake-mp4-bytes",
    )
    assert res.status_code == 400


def test_n15_upload_size_limit_and_no_leftover_part(monkeypatch):
    monkeypatch.setenv("FASTCLIPPER_MAX_UPLOAD_BYTES", "100")
    client = TestClient(app)
    res = client.post(
        "/api/v1/studio/sources/upload?filename=big.mp4&project_id=proj_default",
        content=b"x" * 250,
    )
    assert res.status_code == 413
    src_dir = app_mod.DATA_DIR / "projects" / "proj_default" / "source"
    if src_dir.exists():
        assert not list(src_dir.glob("*.part"))


def test_n2_cancel_does_not_break_live_or_independent_render(tmp_path):
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    client = TestClient(app)
    c_res = client.post("/api/v1/studio/cancel")
    assert c_res.status_code == 200

    # Render independente (como os da live) deve funcionar normalmente após /cancel
    res = pipeline_runner.render_clip({
        "video_path": str(FIXTURE_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 1.5,
        "layout": "solo",
        "burn_subtitles": False,
    })
    assert res.success, res.error
    Path(res.output_video_path).unlink(missing_ok=True)


def test_n3_batch_runs_only_its_own_jobs_and_marks_cancelled(tmp_path):
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    q = DurableTaskQueue()
    runner = PipelineRunner(queue=q, output_dir=tmp_path)

    stale = q.enqueue("org_default", "render_clip", {"video_path": str(FIXTURE_VIDEO), "inicio_s": 0.0, "fim_s": 1.0, "burn_subtitles": False}, "stale_1")
    new1 = q.enqueue("org_default", "render_clip", {"video_path": str(FIXTURE_VIDEO), "inicio_s": 0.0, "fim_s": 1.0, "burn_subtitles": False}, "new_1")
    new2 = q.enqueue("org_default", "render_clip", {"video_path": str(FIXTURE_VIDEO), "inicio_s": 1.0, "fim_s": 2.0, "burn_subtitles": False}, "new_2")

    ev = threading.Event()
    ev.set()
    runner.run_jobs([new1.job_id, new2.job_id], cancel_event=ev)

    assert q.get_job(stale.job_id).status == JobStatus.CRIADO
    assert q.get_job(new1.job_id).status == JobStatus.CANCELADO
    assert q.get_job(new2.job_id).status == JobStatus.CANCELADO

    # Agora executa somente new1
    q2 = DurableTaskQueue()
    runner2 = PipelineRunner(queue=q2, output_dir=tmp_path)
    stale2 = q2.enqueue("org_default", "render_clip", {"video_path": str(FIXTURE_VIDEO), "inicio_s": 0.0, "fim_s": 1.0, "burn_subtitles": False}, "stale_2")
    target = q2.enqueue("org_default", "render_clip", {"video_path": str(FIXTURE_VIDEO), "inicio_s": 0.0, "fim_s": 1.0, "burn_subtitles": False}, "target_1")
    runner2.run_jobs([target.job_id])
    assert q2.get_job(target.job_id).status == JobStatus.APROVADO
    assert q2.get_job(stale2.job_id).status == JobStatus.CRIADO


def test_n4_n5_clamps_end_and_allows_video_without_audio(tmp_path):
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    no_audio = tmp_path / "sem_audio.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(FIXTURE_VIDEO), "-an", "-c:v", "copy", str(no_audio)],
        check=True,
        capture_output=True,
    )

    runner = PipelineRunner(queue=DurableTaskQueue(), output_dir=tmp_path)
    res = runner.render_clip({
        "video_path": str(no_audio),
        "inicio_s": 0.0,
        "fim_s": 15.0,  # maior que os 6s do vídeo -> deve ajustar para duração real
        "layout": "solo",
        "burn_subtitles": False,
    })
    assert res.success, res.error
    assert Path(res.output_video_path).is_file()


def test_n6_transcription_cache_key_includes_language_and_model(tmp_path):
    store = SQLiteStore(tmp_path / "cache.db")
    store.save_transcription("abc123:pt:small", "pt", "small", 5.0, [{"word": "oi", "start_s": 0.0, "end_s": 0.5}], "oi")
    store.save_transcription("abc123:en:small", "en", "small", 5.0, [{"word": "hi", "start_s": 0.0, "end_s": 0.5}], "hi")

    assert store.get_transcription("abc123:pt:small")["full_text"] == "oi"
    assert store.get_transcription("abc123:en:small")["full_text"] == "hi"
    assert store.get_transcription("abc123:pt:large") is None


def test_n7_tests_do_not_write_to_repo_data_dir():
    repo_data = (Path(__file__).resolve().parent.parent / "data").resolve()
    assert app_mod.DATA_DIR != repo_data


def test_preview_layout_incomplete_rect_returns_400():
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")
    client = TestClient(app)
    res = client.post("/api/v1/studio/preview-layout", json={
        "video_path": str(FIXTURE_VIDEO),
        "layout": "stacked",
        "facecam_rect": {"x": 10, "y": 10},
    })
    assert res.status_code == 400
