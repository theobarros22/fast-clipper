"""
Testes automatizados para o Fast Clipper Studio Local (API, Player e Renderização 9:16).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app
from fastclipper.cli import main

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_VIDEO = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"


def test_studio_dashboard_html():
    res = client.get("/")
    assert res.status_code == 200
    assert "Fast" in res.text
    assert "Clipper" in res.text
    assert "Studio Local" in res.text
    assert "Estúdio de Clipping 9:16" in res.text


def test_studio_list_clips_endpoint():
    res = client.get("/api/v1/studio/clips")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)


def test_studio_render_now_solo(tmp_path):
    payload = {
        "video_path": str(SYNTHETIC_VIDEO),
        "inicio_s": 0.5,
        "fim_s": 1.5,
        "layout": "solo",
        "burn_subtitles": False,
    }
    res = client.post("/api/v1/studio/render-now", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["filename"] is not None
    assert data["download_url"] is not None
    assert Path(data["output_path"]).is_file()


def test_studio_render_now_with_subtitles(tmp_path):
    payload = {
        "video_path": str(SYNTHETIC_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 1.2,
        "layout": "solo",
        "subtitle_text": "JOGADA DE MESTRE NO FINAL",
        "subtitle_preset": "viral_bounce",
        "burn_subtitles": True,
    }
    res = client.post("/api/v1/studio/render-now", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert Path(data["output_path"]).is_file()

    # Testa streaming do clipe recém-gerado
    res_stream = client.get(data["download_url"])
    assert res_stream.status_code == 200
    assert "video/mp4" in res_stream.headers.get("content-type", "")


def test_studio_render_invalid_video():
    payload = {
        "video_path": "tests/fixtures/synthetic/video_inexistente.mp4",
        "inicio_s": 0.0,
        "fim_s": 5.0,
    }
    res = client.post("/api/v1/studio/render-now", json=payload)
    assert res.status_code == 400
    assert "detail" in res.json()


def test_studio_render_nonzero_start_has_relative_subtitles():
    """Garante que legendas com inicio_s > 0 comecem em ~0s no clipe cortado (A1)."""
    payload = {
        "video_path": str(SYNTHETIC_VIDEO),
        "inicio_s": 2.0,
        "fim_s": 3.5,
        "layout": "solo",
        "subtitle_text": "CORTE NO MEIO DO VIDEO",
        "subtitle_preset": "viral_bounce",
        "burn_subtitles": True,
    }
    res = client.post("/api/v1/studio/render-now", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True

    # Verifica o arquivo .ass gerado no diretório de saída
    out_video = Path(data["output_path"])
    ass_candidates = list(out_video.parent.glob(f"{out_video.stem.replace('_9x16', '')}*_subs.ass"))
    assert len(ass_candidates) == 1
    ass_text = ass_candidates[0].read_text(encoding="utf-8")
    assert "Dialogue: 0,0:00:00.00," in ass_text


def test_studio_security_guards():
    """Garante proteção contra acesso a pastas arbitrárias, cross-origin e host spoofing (A5)."""
    # 1. Caminho fora das pastas permitidas (data/ ou tests/fixtures/)
    res_path = client.post(
        "/api/v1/studio/render-now",
        json={"video_path": "C:/Windows/System32/notepad.exe", "inicio_s": 0.0, "fim_s": 2.0},
    )
    assert res_path.status_code == 403

    # 2. Precheck fora das pastas permitidas
    res_pre = client.post("/api/v1/precheck", json={"video_path": "README.md"})
    assert res_pre.status_code == 403

    # 3. Origem cross-origin maliciosa
    res_origin = client.post(
        "/api/v1/studio/render-now",
        headers={"Origin": "https://evil-site.example"},
        json={"video_path": str(SYNTHETIC_VIDEO), "inicio_s": 0.0, "fim_s": 1.0},
    )
    assert res_origin.status_code == 403

    # 4. Host inválido (DNS rebinding)
    res_host = client.get("/health", headers={"Host": "evil.com:8000"})
    assert res_host.status_code == 403

    # 5. Path traversal no endpoint de streaming
    res_trav = client.get("/api/v1/studio/video/..%5C..%5CREADME.md")
    assert res_trav.status_code in (403, 404)


def test_studio_stream_clip_not_found():
    res = client.get("/api/v1/studio/video/arquivo_inexistente_9999.mp4")
    assert res.status_code == 404


def test_studio_realign_words_endpoint():
    payload = {
        "words": [
            {"word": "esse", "start_s": 1.0, "end_s": 1.4},
            {"word": "jogada", "start_s": 1.45, "end_s": 1.9},
            {"word": "foi", "start_s": 1.95, "end_s": 2.2},
            {"word": "incrivel", "start_s": 2.25, "end_s": 2.8},
        ],
        "edited_text": "essa jogada foi insana",
        "inicio_s": 1.0,
        "fim_s": 3.0,
    }
    res = client.post("/api/v1/studio/realign-words", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 4
    words = data["words"]
    # "jogada" e "foi" mantiveram os timestamps exatos
    assert words[1]["word"] == "jogada" and words[1]["start_s"] == 1.45
    assert words[2]["word"] == "foi" and words[2]["start_s"] == 1.95
    # "essa" e "insana" foram substituídos dentro da janela de tempo correta
    assert words[0]["word"] == "essa" and words[0]["start_s"] == 1.0
    assert words[3]["word"] == "insana" and 2.5 <= words[3]["end_s"] <= 2.8


def test_studio_render_retention_hook_and_progress_bar():
    payload = {
        "video_path": str(SYNTHETIC_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 1.2,
        "layout": "solo",
        "hook_title": "VEJA ISSO AGORA",
        "progress_bar": True,
        "enhance_voice": True,
        "burn_subtitles": True,
        "subtitle_text": "RETENÇÃO VIRAL ATIVA",
    }
    res = client.post("/api/v1/studio/render-now", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    out_video = Path(data["output_path"])
    assert out_video.is_file()

    ass_candidates = list(out_video.parent.glob(f"{out_video.stem.replace('_9x16', '')}*_subs.ass"))
    assert len(ass_candidates) == 1
    ass_text = ass_candidates[0].read_text(encoding="utf-8")
    assert "HookBanner" in ass_text
    assert "VEJA ISSO AGORA" in ass_text


def test_studio_render_batch_with_retention_options():
    payload = {
        "video_path": str(SYNTHETIC_VIDEO),
        "items": [
            {"inicio_s": 0.0, "fim_s": 1.0, "hook_title": "GANCHO 1"},
            {"inicio_s": 1.0, "fim_s": 2.0},
        ],
        "layout": "solo",
        "hook_title": "GANCHO PADRAO",
        "progress_bar": True,
        "enhance_voice": True,
        "burn_subtitles": False,
    }
    res = client.post("/api/v1/studio/render-batch", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["enqueued"] == 2
    assert len(data["job_ids"]) == 2


def test_studio_ui_api_content_type_and_url_guard():
    """Garante que o JS de studio.html envie Content-Type application/json, formate 422 e bloqueie links em video_path."""
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    assert "if (typeof opts.body === 'string')" in html
    assert "'Content-Type': 'application/json'" in html
    assert "Array.isArray(detail)" in html
    assert "isRemoteWebUrl(videoPath)" in html
    assert "getSafeFacecamHeight()" in html


def test_live_split_hls_stream_resolution_and_ffmpeg_args(monkeypatch):
    """Valida suporte a lives YouTube com faixas HLS separadas de vídeo e áudio (requested_formats)."""
    from fastclipper.live.live_manager import LiveClipSession, resolve_live_stream_url

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {
                "url": None,
                "requested_formats": [
                    {"format_id": "312", "url": "https://manifest.googlevideo.com/video_1080p.m3u8"},
                    {"format_id": "234", "url": "https://manifest.googlevideo.com/audio_aac.m3u8"},
                ],
            }

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    resolved = resolve_live_stream_url("https://www.youtube.com/live/x50ONQrBPRM")
    lines = resolved.splitlines()
    assert len(lines) == 2
    assert lines[0] == "https://manifest.googlevideo.com/video_1080p.m3u8"
    assert lines[1] == "https://manifest.googlevideo.com/audio_aac.m3u8"

    ffmpeg_args = LiveClipSession._build_ffmpeg_input_args(resolved, realtime_local=False)
    assert ffmpeg_args.count("-i") == 2
    assert "-map" in ffmpeg_args
    assert "0:v:0" in ffmpeg_args
    assert "1:a:0" in ffmpeg_args
