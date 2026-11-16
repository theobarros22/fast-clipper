from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app
from fastclipper.export import generate_post_copy

FIXTURE_VIDEO = Path("tests/fixtures/synthetic/sintetico_gameplay_facecam.mp4")


def test_generate_copy_for_x():
    res = generate_post_copy("Gol inacreditável no último segundo!", platform="x")
    assert res["platform"] == "x"
    assert "Gol inacreditável" in res["hook"]
    assert len(res["hashtags"]) >= 3


def test_export_package_creates_zip_and_serves_download():
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    client = TestClient(app)
    r = client.post("/api/v1/studio/render-now", json={
        "video_path": str(FIXTURE_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 2.0,
        "layout": "solo",
        "aspect_ratio": "16:9",
        "burn_subtitles": False,
    })
    assert r.status_code == 200
    fname = r.json()["filename"]

    try:
        exp = client.post("/api/v1/studio/export-package", json={
            "clip_filename": fname,
            "platform": "x",
            "title": "Corte Live X",
            "description": "Melhor momento da transmissão",
            "hashtags": ["#cortes", "#live"],
        })
        assert exp.status_code == 200, exp.text
        data = exp.json()
        assert data["zip_url"].startswith("/api/v1/studio/export-zip/")
        assert Path(data["zip_file"]).is_file()

        dl = client.get(data["zip_url"])
        assert dl.status_code == 200
        assert dl.headers["content-type"] == "application/zip"
        assert len(dl.content) > 100

        bad = client.get("/api/v1/studio/export-zip/2026-01-01/..secret.zip")
        assert bad.status_code == 403
    finally:
        client.delete(f"/api/v1/studio/clips/{fname}")
