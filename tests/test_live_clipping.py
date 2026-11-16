import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app
from fastclipper.live.live_manager import LiveClipSession
from fastclipper.live.youtube_live import SecuritySSRFError, YouTubeLiveIngestor
from fastclipper.queue.durable_queue import DurableTaskQueue
from fastclipper.rendering.pipeline_runner import PipelineRunner

FIXTURE_VIDEO = Path("tests/fixtures/synthetic/sintetico_gameplay_facecam.mp4")


def test_live_ssrf_allows_youtube_and_x_blocks_private(tmp_path):
    yt = YouTubeLiveIngestor("https://www.youtube.com/watch?v=abc123", tmp_path / "yt")
    assert yt.detect_platform("https://www.youtube.com/watch?v=abc123") == "youtube"

    x_live = YouTubeLiveIngestor("https://x.com/usuario/status/123456789", tmp_path / "x")
    assert x_live.detect_platform("https://x.com/usuario/status/123456789") == "x"

    tw = YouTubeLiveIngestor("https://twitter.com/i/broadcasts/1OyKAV", tmp_path / "tw")
    assert tw.detect_platform("https://twitter.com/i/broadcasts/1OyKAV") == "x"

    for bad in ("http://127.0.0.1:8000/secret", "http://localhost/live", "https://evil.internal/stream.m3u8"):
        with pytest.raises(SecuritySSRFError):
            YouTubeLiveIngestor(bad, tmp_path / "bad")


def test_live_session_segments_and_instant_clip(tmp_path):
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    queue = DurableTaskQueue()
    out_dir = tmp_path / "clips_out"
    runner = PipelineRunner(queue=queue, output_dir=out_dir)

    session = LiveClipSession(
        session_id="live_test_01",
        url="https://x.com/streamer/status/999",
        session_dir=tmp_path / "live_session",
        pipeline_runner=runner,
        segment_duration_s=2.0,
        stream_resolver=lambda _: str(FIXTURE_VIDEO.resolve()),
    )
    session.start()
    try:
        # Aguarda o FFmpeg produzir e registrar segmentos no buffer
        deadline = time.time() + 15.0
        while time.time() < deadline:
            session._scan_new_segments(include_last=not session.running)
            if len(session.buffer) >= 1 and (not session.running or len(session.buffer) >= 2):
                break
            time.sleep(0.3)

        assert len(session.buffer) >= 1
        res = session.mark_and_clip(pre_s=3.0, post_s=0.0, titulo="Teste Live X", background=False)
        assert res["status"] == "done", res
        assert res["filename"]
        clip_path = out_dir / res["filename"]
        assert clip_path.is_file()
        assert clip_path.stat().st_size > 1000
    finally:
        session.stop()


def test_studio_live_api_endpoints():
    client = TestClient(app)

    st = client.get("/api/v1/studio/live/status")
    assert st.status_code == 200
    assert "active" in st.json()

    bad = client.post("/api/v1/studio/live/start", json={"url": "http://127.0.0.1:9000/internal"})
    assert bad.status_code == 400

    stop = client.post("/api/v1/studio/live/stop")
    assert stop.status_code == 200

    home = client.get("/")
    assert home.status_code == 200
    assert "btn-live-start" in home.text
    assert "innerHTML" not in home.text
