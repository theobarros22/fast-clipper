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
        "video_path": "caminho/nao/existente/video.mp4",
        "inicio_s": 0.0,
        "fim_s": 5.0,
    }
    res = client.post("/api/v1/studio/render-now", json=payload)
    assert res.status_code == 400
    assert "detail" in res.json()


def test_studio_stream_clip_not_found():
    res = client.get("/api/v1/studio/video/arquivo_inexistente_9999.mp4")
    assert res.status_code == 404
