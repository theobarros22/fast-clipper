from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app
from fastclipper.ingest.precheck import MediaPrechecker
from fastclipper.queue.durable_queue import DurableTaskQueue
from fastclipper.rendering.pipeline_runner import PipelineRunner

FIXTURE_VIDEO = Path("tests/fixtures/synthetic/sintetico_gameplay_facecam.mp4")


def test_pipeline_renders_16x9_and_both(tmp_path):
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    runner = PipelineRunner(queue=DurableTaskQueue(), output_dir=tmp_path)
    prechecker = MediaPrechecker(min_duration_s=0.5)

    # 1) Apenas 16:9
    res_16 = runner.render_clip({
        "video_path": str(FIXTURE_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 2.0,
        "layout": "solo",
        "aspect_ratio": "16:9",
        "burn_subtitles": False,
    })
    assert res_16.success, res_16.error
    p16 = Path(res_16.output_video_path)
    assert p16.name.endswith("_16x9.mp4")
    probe16 = prechecker.probe(p16)
    assert probe16.width == 1920 and probe16.height == 1080

    # 2) Ambos (9:16 + 16:9)
    res_both = runner.render_clip({
        "video_path": str(FIXTURE_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 2.0,
        "layout": "solo",
        "aspect_ratio": "both",
        "burn_subtitles": False,
    })
    assert res_both.success, res_both.error
    p9 = Path(res_both.output_video_path)
    probe9 = prechecker.probe(p9)
    assert probe9.width == 1080 and probe9.height == 1920

    extras = res_both.metadata.get("extra_outputs", [])
    assert len(extras) == 1
    assert extras[0]["aspect_ratio"] == "16:9"
    p_extra = Path(extras[0]["output_path"])
    assert p_extra.is_file()
    probe_ex = prechecker.probe(p_extra)
    assert probe_ex.width == 1920 and probe_ex.height == 1080


def test_studio_render_now_both_and_ui_selector():
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    client = TestClient(app)
    home = client.get("/")
    assert home.status_code == 200
    assert 'id="aspect_ratio"' in home.text
    assert 'value="16:9"' in home.text
    assert 'value="both"' in home.text

    resp = client.post("/api/v1/studio/render-now", json={
        "video_path": str(FIXTURE_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 2.0,
        "layout": "solo",
        "aspect_ratio": "both",
        "burn_subtitles": False,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["filename"].endswith("_9x16.mp4")
    assert len(data["extra_outputs"]) == 1
    assert data["extra_outputs"][0]["filename"].endswith("_16x9.mp4")

    # Limpa os dois arquivos gerados no diretório de saída padrão
    for fname in (data["filename"], data["extra_outputs"][0]["filename"]):
        client.delete(f"/api/v1/studio/clips/{fname}")


def test_render_with_normalize_audio(tmp_path):
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    runner = PipelineRunner(queue=DurableTaskQueue(), output_dir=tmp_path)
    res = runner.render_clip({
        "video_path": str(FIXTURE_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 2.0,
        "layout": "solo",
        "aspect_ratio": "9:16",
        "burn_subtitles": False,
        "normalize_audio": True,
    })
    assert res.success, res.error
    assert Path(res.output_video_path).is_file()
