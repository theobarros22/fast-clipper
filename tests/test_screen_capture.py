import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

import fastclipper.live.live_manager as live_manager_module
from fastclipper.api.app import app
from fastclipper.live import screen_capture as sc
from fastclipper.live.live_manager import LiveClipSession
from fastclipper.queue.durable_queue import DurableTaskQueue
from fastclipper.rendering.pipeline_runner import PipelineRunner


def test_screen_url_roundtrip_and_validation():
    spec = sc.ScreenSpec(target="Minha Janela & Cia", fps=60, audio_device="Mixagem estéreo (Realtek)", backend="gdigrab")
    url = sc.build_screen_url(spec)
    assert url.startswith("screen://")
    back = sc.parse_screen_url(url)
    assert back.target == "Minha Janela & Cia"
    assert back.fps == 60
    assert back.audio_device == "Mixagem estéreo (Realtek)"
    assert back.backend == "gdigrab"

    assert sc.is_screen_url("SCREEN://?target=desktop")
    assert not sc.is_screen_url("https://x.com/a/status/1")

    with pytest.raises(sc.ScreenCaptureError):
        sc.normalize_spec(sc.ScreenSpec(fps=5))
    with pytest.raises(sc.ScreenCaptureError):
        sc.normalize_spec(sc.ScreenSpec(fps=120))
    with pytest.raises(sc.ScreenCaptureError):
        sc.normalize_spec(sc.ScreenSpec(target="janela\nruim"))
    with pytest.raises(sc.ScreenCaptureError):
        sc.normalize_spec(sc.ScreenSpec(backend="rm -rf"))
    with pytest.raises(sc.ScreenCaptureError):
        sc.normalize_spec(sc.ScreenSpec(target="x" * 201))


def test_resolve_backend_per_platform(monkeypatch):
    monkeypatch.setattr(sc, "_ffmpeg_has_filter", lambda *_: True)
    desktop = sc.ScreenSpec()
    window = sc.ScreenSpec(target="Bloco de Notas")

    assert sc.resolve_backend(desktop, "ffmpeg", "win32") == "ddagrab"
    assert sc.resolve_backend(window, "ffmpeg", "win32") == "gdigrab"
    with pytest.raises(sc.ScreenCaptureError):
        sc.resolve_backend(sc.ScreenSpec(backend="ddagrab", target="Janela"), "ffmpeg", "win32")
    assert sc.resolve_backend(desktop, "ffmpeg", "linux") == "x11grab"
    with pytest.raises(sc.ScreenCaptureError):
        sc.resolve_backend(desktop, "ffmpeg", "darwin")

    monkeypatch.setattr(sc, "_ffmpeg_has_filter", lambda *_: False)
    assert sc.resolve_backend(desktop, "ffmpeg", "win32") == "gdigrab"


def test_build_capture_command_windows_variants():
    out = "C:/buf/seg_%05d.ts"
    cmd = sc.build_capture_command(
        sc.ScreenSpec(target="Meu Jogo", fps=30, audio_device="Mixagem estéreo", backend="gdigrab"),
        "ffmpeg", out, 6.0, platform_name="win32",
    )
    assert cmd[0] == "ffmpeg"
    assert "gdigrab" in cmd and "title=Meu Jogo" in cmd
    assert "dshow" in cmd and "audio=Mixagem estéreo" in cmd
    assert cmd[-1] == out and "segment" in cmd and "6.0" in cmd
    assert "libx264" in cmd and "aac" in cmd

    dd = sc.build_capture_command(sc.ScreenSpec(fps=60, backend="ddagrab"), "ffmpeg", out, 6.0, platform_name="win32")
    graph = dd[dd.index("-i") + 1]
    assert graph == "ddagrab=framerate=60:draw_mouse=1,hwdownload,format=bgra"
    # Sem dispositivo de áudio: trilha silenciosa para todo segmento ter áudio
    assert any("anullsrc" in part for part in dd)

    # O título da janela vai como argumento único (sem shell): não há interpretação de metacaracteres
    evil = sc.build_capture_command(
        sc.ScreenSpec(target='x" & calc.exe', backend="gdigrab"), "ffmpeg", out, 6.0, platform_name="win32"
    )
    assert 'title=x" & calc.exe' in evil


def test_parse_dshow_audio_devices_new_and_old_format():
    new_fmt = (
        '[dshow @ 0000020] DirectShow video devices (some may be both video and audio devices)\n'
        '[dshow @ 0000020]  "Integrated Camera" (video)\n'
        '[dshow @ 0000020]     Alternative name "@device_pnp_\\\\?\\usb#vid_1"\n'
        '[dshow @ 0000020] DirectShow audio devices\n'
        '[dshow @ 0000020]  "Microfone (Realtek Audio)" (audio)\n'
        '[dshow @ 0000020]     Alternative name "@device_cm_{33D9A762}\\wave_{1}"\n'
        '[dshow @ 0000020]  "Mixagem estéreo (Realtek Audio)" (audio)\n'
    )
    assert sc.parse_dshow_audio_devices(new_fmt) == ["Microfone (Realtek Audio)", "Mixagem estéreo (Realtek Audio)"]

    old_fmt = (
        '[dshow @ 00000] DirectShow video devices\n'
        '[dshow @ 00000]  "Webcam"\n'
        '[dshow @ 00000] DirectShow audio devices\n'
        '[dshow @ 00000]  "Microphone Array"\n'
        '[dshow @ 00000]  "virtual-audio-capturer"\n'
    )
    assert sc.parse_dshow_audio_devices(old_fmt) == ["Microphone Array", "virtual-audio-capturer"]
    assert sc.parse_dshow_audio_devices("") == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg não instalado.")
def test_screen_session_records_segments_and_clips(tmp_path):
    """Fluxo completo com FFmpeg real, usando a fonte sintética (testsrc) no lugar da tela."""
    url = sc.build_screen_url(sc.ScreenSpec(fps=15, backend="testsrc"))
    out_dir = tmp_path / "clips_out"
    runner = PipelineRunner(queue=DurableTaskQueue(), output_dir=out_dir)
    session = LiveClipSession(
        session_id="screen_test_01",
        url=url,
        session_dir=tmp_path / "screen_session",
        pipeline_runner=runner,
        segment_duration_s=2.0,
    )
    assert session.source_type == "screen" and session.platform == "screen"
    session.start()
    try:
        deadline = time.time() + 30.0
        while time.time() < deadline:
            session._scan_new_segments(include_last=not session.running)
            if len(session.buffer) >= 2:
                break
            time.sleep(0.3)
        assert len(session.buffer) >= 2, session.last_error

        status = session.get_status()
        assert status["source_type"] == "screen"
        assert status["screen"]["backend"] == "testsrc"

        # Todo segmento precisa ter vídeo e áudio (pipeline de legendas/normalização espera os dois)
        seg = sorted(session.segments_dir.glob("seg_*.ts"))[0]
        probe = subprocess.run(
            [session.ffprobe_bin, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(seg)],
            capture_output=True, text=True, timeout=20,
        )
        kinds = set(probe.stdout.split())
        assert {"video", "audio"} <= kinds, probe.stdout

        res = session.mark_and_clip(pre_s=3.0, post_s=0.0, titulo="Corte de Tela", background=False)
        assert res["status"] == "done", res
        clip = out_dir / res["filename"]
        assert clip.is_file() and clip.stat().st_size > 1000
    finally:
        session.stop()

    # Monitor embutido é desativado para captura de tela
    assert list(session.iter_preview_fmp4()) == []


def test_screen_session_fast_fail_with_clear_message(tmp_path, monkeypatch):
    # Backend inexistente para o FFmpeg: deve falhar rápido e com mensagem, sem deixar a sessão "rodando".
    monkeypatch.setattr(live_manager_module, "resolve_backend", lambda spec, ffmpeg_bin: "gdigrab")
    runner = PipelineRunner(queue=DurableTaskQueue(), output_dir=tmp_path / "o")
    session = LiveClipSession(
        session_id="screen_fail_01",
        url=sc.build_screen_url(sc.ScreenSpec(target="Janela Que Nao Existe")),
        session_dir=tmp_path / "s",
        pipeline_runner=runner,
        segment_duration_s=2.0,
    )
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg não instalado.")
    with pytest.raises(RuntimeError) as exc:
        session.start()
    assert "captura de tela" in str(exc.value)
    assert session.running is False


def test_screen_api_validation_sources_and_flow(monkeypatch):
    client = TestClient(app)

    # URL continua obrigatória para fonte "url"
    r = client.post("/api/v1/studio/live/start", json={"source_type": "url"})
    assert r.status_code == 422
    # Limites de FPS e backend
    assert client.post("/api/v1/studio/live/start", json={"source_type": "screen", "screen_fps": 5}).status_code == 422
    assert client.post("/api/v1/studio/live/start", json={"source_type": "screen", "screen_backend": "testsrc"}).status_code == 422

    src = client.get("/api/v1/studio/screen/sources")
    assert src.status_code == 200
    body = src.json()
    assert {"platform", "supported", "backends", "windows", "audio_devices"} <= set(body)

    home = client.get("/")
    for needle in ("live_source_screen", "screen_target", "screen_audio", "screen_fps", "btn-screen-refresh"):
        assert needle in home.text
    assert "innerHTML" not in home.text

    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg não instalado.")
    monkeypatch.setattr(live_manager_module, "resolve_backend", lambda spec, ffmpeg_bin: "testsrc")
    started = client.post(
        "/api/v1/studio/live/start",
        json={"source_type": "screen", "screen_fps": 15, "segment_duration_s": 2.0},
    )
    try:
        assert started.status_code == 200, started.text
        st = started.json()
        assert st["source_type"] == "screen" and st["platform"] == "screen" and st["running"] is True
        mon = client.get("/api/v1/studio/live/stream.mp4")
        assert mon.status_code == 409
    finally:
        client.post("/api/v1/studio/live/stop")
