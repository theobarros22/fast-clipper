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

    twitch = YouTubeLiveIngestor("https://www.twitch.tv/streamer", tmp_path / "twitch")
    assert twitch.detect_platform("https://www.twitch.tv/streamer") == "twitch"

    kick = YouTubeLiveIngestor("https://kick.com/streamer", tmp_path / "kick")
    assert kick.detect_platform("https://kick.com/streamer") == "kick"

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


def test_live_session_auto_transcribe_subtitles(tmp_path):
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    queue = DurableTaskQueue()
    out_dir = tmp_path / "clips_sub_out"
    runner = PipelineRunner(queue=queue, output_dir=out_dir)

    called = {"count": 0}

    def fake_transcribe(media_path, lang):
        called["count"] += 1
        assert media_path.is_file()
        assert lang == "pt"
        return {
            "full_text": "jogada insana ao vivo",
            "words": [
                {"word": "jogada", "start_s": 0.1, "end_s": 0.6},
                {"word": "insana", "start_s": 0.7, "end_s": 1.2},
                {"word": "ao", "start_s": 1.3, "end_s": 1.6},
                {"word": "vivo", "start_s": 1.7, "end_s": 2.2},
            ],
        }

    session = LiveClipSession(
        session_id="live_test_subs",
        url="https://www.youtube.com/watch?v=live123",
        session_dir=tmp_path / "live_subs",
        pipeline_runner=runner,
        segment_duration_s=2.0,
        default_render_opts={"burn_subtitles": True, "language": "pt", "layout": "solo"},
        stream_resolver=lambda _: str(FIXTURE_VIDEO.resolve()),
        transcribe_fn=fake_transcribe,
    )
    session.start()
    try:
        deadline = time.time() + 15.0
        while time.time() < deadline:
            session._scan_new_segments(include_last=not session.running)
            if len(session.buffer) >= 1 and (not session.running or len(session.buffer) >= 2):
                break
            time.sleep(0.3)

        res = session.mark_and_clip(pre_s=3.0, post_s=0.0, titulo="Live Legendada", background=False)
        assert res["status"] == "done", res
        assert called["count"] == 1
        assert res["transcript_text"] == "jogada insana ao vivo"
        assert (out_dir / res["filename"]).is_file()
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
    assert "live_keywords" in home.text
    assert "live_clip_duration" in home.text
    assert "live-monitor-player" in home.text
    assert "live_autoclip_mode" in home.text
    assert "btn-live-replay-buffer" in home.text
    assert "live-queue-wrap" in home.text
    assert "live-clips-feed-wrap" in home.text
    assert "btn-cleanup-live" in home.text
    assert "btn-copy-export-text" in home.text
    assert "innerHTML" not in home.text
    assert "alert(" not in home.text

    # Sem live ativa, stream de monitor retorna 404
    no_stream = client.get("/api/v1/studio/live/stream.mp4")
    assert no_stream.status_code == 404

    storage = client.get("/api/v1/studio/storage")
    assert storage.status_code == 200
    sjson = storage.json()
    assert "output_clips_mb" in sjson and "exports_mb" in sjson and "live_buffers_mb" in sjson

    cleanup = client.post("/api/v1/studio/live/cleanup")
    assert cleanup.status_code == 200
    assert "removed_sessions" in cleanup.json()


def test_live_auto_clip_keywords_trigger(tmp_path):
    runner = PipelineRunner(queue=DurableTaskQueue(), output_dir=tmp_path / "out")
    seg_file = tmp_path / "seg_00000.ts"
    seg_file.write_bytes(b"fake")

    session = LiveClipSession(
        session_id="live_kw_test",
        url="https://x.com/user/status/1",
        session_dir=tmp_path / "sess",
        pipeline_runner=runner,
        auto_clip=True,
        auto_clip_threshold_db=10.0,  # evita trigger por volume no teste unitário
        auto_clip_keywords=["Absurdo", "olha isso"],
        transcribe_fn=lambda _p, _l: {"full_text": "Cara, isso aqui foi ABSURDO demais!", "words": []},
    )
    triggered = session._evaluate_auto_clip_trigger(seg_file)
    assert triggered == "Auto Palavra: absurdo"
    assert "ABSURDO" in session.last_transcript_snippet


def test_live_control_flow_preview_suggestions_replay_and_discard(tmp_path):
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    queue = DurableTaskQueue()
    out_dir = tmp_path / "clips_ctrl"
    runner = PipelineRunner(queue=queue, output_dir=out_dir)

    session = LiveClipSession(
        session_id="live_ctrl_01",
        url="https://www.twitch.tv/streamer",
        session_dir=tmp_path / "live_ctrl_sess",
        pipeline_runner=runner,
        segment_duration_s=2.0,
        auto_clip=True,
        auto_clip_mode="suggest_only",
        auto_clip_threshold_db=10.0,
        auto_clip_keywords=["urgente"],
        default_render_opts={"layout": "solo", "solo_align": "center"},
        stream_resolver=lambda _: str(FIXTURE_VIDEO.resolve()),
        transcribe_fn=lambda _p, _l: {"full_text": "Plantão urgente ao vivo", "words": []},
    )
    session.start()
    try:
        # 1. Monitor ao vivo em fMP4 produz cabeçalho MP4 válido (ftyp/moov)
        gen = session.iter_preview_fmp4(chunk_size=8192)
        first_chunk = next(gen)
        gen.close()
        assert len(first_chunk) > 0
        assert b"ftyp" in first_chunk or b"moov" in first_chunk

        # Aguarda segmentos no buffer
        deadline = time.time() + 15.0
        while time.time() < deadline:
            session._scan_new_segments(include_last=not session.running)
            if len(session.buffer) >= 1 and (not session.running or len(session.buffer) >= 2):
                break
            time.sleep(0.3)
        assert len(session.buffer) >= 1

        # 2. Marcação com post_window (+15s) pode ser visualizada e cancelada
        pending = session.mark_and_clip(pre_s=2.0, post_s=60.0, titulo="Aguardando Fim")
        assert pending["mode"] == "post_window"
        st1 = session.get_status()
        assert any(p["candidate_id"] == pending["candidate_id"] for p in st1["pending_items"])
        assert session.cancel_pending_mark(pending["candidate_id"]) is True
        st2 = session.get_status()
        assert not any(p["candidate_id"] == pending["candidate_id"] for p in st2["pending_items"])

        # 3. Modo suggest_only cria sugestões que podem ser ignoradas ou aprovadas
        seg0 = session.segments_dir / "seg_00000.ts"
        session._last_auto_clip_s = -999.0
        session._async_auto_clip_check(seg0, session.clock.current_time())
        st3 = session.get_status()
        assert len(st3["suggested_cuts"]) >= 1
        sug_id = st3["suggested_cuts"][0]["suggestion_id"]

        approved = session.approve_suggestion(sug_id, background=False)
        assert approved["status"] == "done"
        fname = approved["filename"]
        assert (out_dir / fname).is_file()

        # 4. Aprovar / manter clipe e remover do feed
        assert session.approve_clip(fname) is True
        assert session.get_status()["recent_clips"][-1]["approved"] is True

        # 5. Extrair replay bruto do buffer para corte fino no player principal
        replay = session.export_buffer_replay(dest_dir=tmp_path / "replays", duration_s=4.0)
        assert Path(replay["file_path"]).is_file()
        assert replay["duration_s"] >= 1.0

        # 6. Atualização dinâmica das regras em tempo real
        st4 = session.update_settings(auto_clip=False, auto_clip_mode="auto_render", auto_clip_keywords=["gol"])
        assert st4["auto_clip"] is False
        assert st4["auto_clip_mode"] == "auto_render"
        assert st4["auto_clip_keywords"] == ["gol"]

        assert session.remove_clip_entry(fname) is True
        assert len(session.get_status()["recent_clips"]) == 0
    finally:
        session.stop()
