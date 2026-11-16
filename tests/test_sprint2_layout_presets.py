"""
Testes automatizados do Sprint 2:
1. Extração de frame de origem e geração de prévia 9:16 (Solo e Empilhado)
2. Detector de Facecam (presets de canto, IoU e fallback sem [vision])
3. Presets por canal persistidos em SQLite (sobrevivendo a reinício)
4. Base do Golden Set (@pytest.mark.golden)
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app
from fastclipper.layouts.config import Rectangle
from fastclipper.layouts.facecam_detector import FacecamDetector, compute_iou
from fastclipper.storage.sqlite_store import SQLiteStore

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_VIDEO = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"


def test_sqlite_channel_presets_persist_across_restart(tmp_path):
    db_file = tmp_path / "presets.db"
    store1 = SQLiteStore(db_file)
    saved = store1.save_channel_preset(
        channel_id="canal_gta_01",
        name="Canal GTA Principal",
        layout_mode="stacked",
        facecam_rect={"x": 40, "y": 40, "w": 360, "h": 270},
        gameplay_rect={"x": 0, "y": 0, "w": 1920, "h": 1080},
        stacked_facecam_height=700,
    )
    assert saved["channel_id"] == "canal_gta_01"

    store2 = SQLiteStore(db_file)
    loaded = store2.get_channel_preset("canal_gta_01")
    assert loaded is not None
    assert loaded["layout_mode"] == "stacked"
    assert loaded["facecam_rect"] == {"x": 40, "y": 40, "w": 360, "h": 270}
    assert loaded["stacked_facecam_height"] == 700


def test_facecam_detector_corner_fallback_and_iou():
    detector = FacecamDetector()
    r1 = detector.suggest_corner_preset(1920, 1080, corner="top-left")
    r2 = detector.suggest_corner_preset(1920, 1080, corner="bottom-right")

    assert r1.validate_bounds(1920, 1080)
    assert r2.validate_bounds(1920, 1080)
    assert compute_iou(r1, r1) == 1.0
    assert compute_iou(r1, r2) == 0.0

    r_shifted = Rectangle(r1.x + 20, r1.y + 10, r1.w, r1.h)
    assert compute_iou(r1, r_shifted) >= 0.8


def test_api_preview_frame_and_layout():
    res_frame = client.post(
        "/api/v1/studio/preview-frame",
        json={"video_path": str(SYNTHETIC_VIDEO), "timestamp_s": 1.0, "detect_facecam": True},
    )
    assert res_frame.status_code == 200
    data = res_frame.json()
    assert data["source_width"] == 1920
    assert data["source_height"] == 1080
    assert data["facecam_suggestion"] is not None
    assert "facecam_rect" in data["facecam_suggestion"]

    res_img = client.get(data["image_url"])
    assert res_img.status_code == 200
    assert "image/jpeg" in res_img.headers["content-type"]

    # Gera prévia 9:16 empilhada usando o retângulo sugerido
    res_layout = client.post(
        "/api/v1/studio/preview-layout",
        json={
            "video_path": str(SYNTHETIC_VIDEO),
            "timestamp_s": 1.0,
            "layout": "stacked",
            "facecam_rect": data["facecam_suggestion"]["facecam_rect"],
        },
    )
    assert res_layout.status_code == 200
    res_layout_img = client.get(res_layout.json()["image_url"])
    assert res_layout_img.status_code == 200
    assert "image/jpeg" in res_layout_img.headers["content-type"]


def test_api_channel_presets_crud():
    res_save = client.post(
        "/api/v1/studio/presets",
        json={
            "channel_id": "canal_teste_api",
            "name": "Streamer X",
            "layout_mode": "stacked",
            "facecam_rect": {"x": 1400, "y": 40, "w": 450, "h": 340},
            "stacked_facecam_height": 720,
        },
    )
    assert res_save.status_code == 200

    res_get = client.get("/api/v1/studio/presets/canal_teste_api")
    assert res_get.status_code == 200
    assert res_get.json()["name"] == "Streamer X"

    res_list = client.get("/api/v1/studio/presets")
    assert res_list.status_code == 200
    assert any(p["channel_id"] == "canal_teste_api" for p in res_list.json())

    res_404 = client.get("/api/v1/studio/presets/canal_inexistente_xyz")
    assert res_404.status_code == 404


@pytest.mark.golden
def test_golden_set_manifest_runner():
    manifest_path = ROOT / "tests" / "fixtures" / "manifesto_local.json"
    if not manifest_path.is_file():
        pytest.skip("manifesto_local.json não encontrado (golden set local com vídeos reais ainda não populado).")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest if isinstance(manifest, list) else manifest.get("items", [])
    assert len(entries) > 0
