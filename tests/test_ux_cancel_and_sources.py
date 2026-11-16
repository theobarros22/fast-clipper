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
    assert "innerHTML" not in html
    assert 'id="fc-statusbar"' in html
    assert 'id="btn-cancel-action"' in html
    assert 'id="sources-select"' in html
    assert 'id="dl_url"' in html
    assert 'id="export-platform"' in html
    assert 'id="source-player"' in html
    assert 'role="tablist"' in html


def test_source_video_stream_and_delete_clip():
    # 1. Source video streaming (S3) + bloqueio fora de pastas permitidas
    res_src = client.get(f"/api/v1/studio/source-video?path={SYNTHETIC_VIDEO}")
    assert res_src.status_code == 200
    assert "video/mp4" in res_src.headers.get("content-type", "")

    res_forb = client.get("/api/v1/studio/source-video?path=README.md")
    assert res_forb.status_code == 403

    # 2. Renderiza e exclui clipe (S15) + stacked_facecam_height (S11)
    res_rend = client.post(
        "/api/v1/studio/render-now",
        json={
            "video_path": str(SYNTHETIC_VIDEO),
            "inicio_s": 0.0,
            "fim_s": 1.2,
            "layout": "stacked",
            "facecam_rect": {"x": 50, "y": 50, "w": 300, "h": 220},
            "stacked_facecam_height": 680,
            "burn_subtitles": False,
        },
    )
    assert res_rend.status_code == 200
    fname = res_rend.json()["filename"]
    assert Path(res_rend.json()["output_path"]).is_file()

    res_del = client.delete(f"/api/v1/studio/clips/{fname}")
    assert res_del.status_code == 200
    assert res_del.json()["deleted"] is True

    res_del_404 = client.delete(f"/api/v1/studio/clips/{fname}")
    assert res_del_404.status_code == 404

    res_trav = client.delete("/api/v1/studio/clips/..%5C..%5CREADME.md")
    assert res_trav.status_code in (403, 404)
