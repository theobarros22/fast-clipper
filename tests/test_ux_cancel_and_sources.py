"""
Testes das melhorias de UX, cancelamento real de FFmpeg e status de lote/hardware.
"""

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from fastclipper.api.app import app, durable_queue, pipeline_runner
from fastclipper.rendering.encoder import HardwareEncoderManager
from fastclipper.rendering.render_core import render_vertical_ffmpeg

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_VIDEO = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"


def test_render_core_cancel_event_terminates_cleanly(tmp_path):
    cancel_ev = threading.Event()
    cancel_ev.set()  # Cancelamento já sinalizado
    out_file = tmp_path / "cancelled_out.mp4"
    enc = HardwareEncoderManager()

    ok = render_vertical_ffmpeg(
        output_path=out_file,
        start_s=0.0,
        duration_s=2.0,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        encoder_manager=enc,
        source_path=SYNTHETIC_VIDEO,
        cancel_event=cancel_ev,
    )
    assert ok is False
    assert not out_file.exists()


def test_api_cancel_batch_status_and_hardware_chips():
    res_status = client.get("/api/v1/status")
    assert res_status.status_code == 200
    st = res_status.json()
    assert "cuda_available" in st
    assert "nvenc_available" in st
    assert "ffmpeg" in st

    durable_queue.clear_all()
    job = durable_queue.enqueue("org_default", "render_clip", {"video_path": "x.mp4"})
    res_bs = client.get(f"/api/v1/studio/batch-status?ids={job.job_id}")
    assert res_bs.status_code == 200
    assert res_bs.json()["total"] == 1
    assert res_bs.json()["active"] == 1

    res_cancel = client.post("/api/v1/studio/cancel")
    assert res_cancel.status_code == 200
    assert res_cancel.json()["cancelled"] is True
    pipeline_runner.clear_cancel()


def test_studio_html_ux_invariants():
    html = (ROOT / "fastclipper" / "web" / "studio.html").read_text(encoding="utf-8")
    assert "alert(" not in html
    assert 'id="fc-statusbar"' in html
    assert 'id="btn-cancel-action"' in html
    assert 'id="sources-select"' in html
    assert 'id="dl_url"' in html
    assert 'id="export-platform"' in html
