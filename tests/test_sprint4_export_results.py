"""
Testes automatizados do Sprint 4:
1. Gerador offline de título, descrição, gancho e hashtags por plataforma
2. Exportador de pacote completo (MP4 + SRT + capa.jpg + metadados + TXT)
3. Registro de views e receita em SQLite com resumo agregado
4. Fluxo completo na API (/generate-copy, /export-package, /results)
"""

from pathlib import Path

from fastapi.testclient import TestClient

from fastclipper.api.app import app
from fastclipper.export.copy_generator import generate_post_copy
from fastclipper.export.package_exporter import PackageExporter
from fastclipper.storage.sqlite_store import SQLiteStore

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_VIDEO = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"


def test_copy_generator_offline_platforms():
    for plat in ("tiktok", "shorts", "reels", "youtube"):
        copy = generate_post_copy(
            transcript_text="Essa fuga de helicóptero foi a mais absurda da noite!",
            platform=plat,
            channel_name="Canal Alfa",
            use_llm=False,
        )
        assert copy["platform"] == plat
        assert copy["source"] == "template"
        assert len(copy["title"]) <= 65
        assert "#canalalfa" in copy["hashtags"]


def test_package_exporter_creates_full_bundle(tmp_path):
    exporter = PackageExporter(exports_root=tmp_path / "exports")
    pkg = exporter.export_package(
        clip_path=SYNTHETIC_VIDEO,
        platform="shorts",
        title="Fuga Insana de Helicóptero",
        description="Assista até o final!",
        hashtags=["#shorts", "#clips"],
        words=[
            {"word": "Olha", "start_s": 0.1, "end_s": 0.4},
            {"word": "essa", "start_s": 0.5, "end_s": 0.8},
            {"word": "fuga!", "start_s": 0.9, "end_s": 1.3},
        ],
    )
    assert Path(pkg["video_file"]).is_file()
    assert pkg["srt_file"] is not None and Path(pkg["srt_file"]).is_file()
    assert pkg["cover_file"] is not None and Path(pkg["cover_file"]).is_file()
    pkg_dir = Path(pkg["package_dir"])
    assert (pkg_dir / "metadados.json").is_file()
    assert (pkg_dir / "LEIA_PARA_POSTAR.txt").is_file()


def test_sqlite_results_and_summary(tmp_path):
    db_file = tmp_path / "results.db"
    s1 = SQLiteStore(db_file)
    s1.save_clip_result(
        clip_filename="clip_a.mp4",
        platform="tiktok",
        post_url="https://tiktok.com/@a/video/1",
        views_24h=5000,
        views_7d=15000,
        revenue_brl=120.50,
        layout_mode="stacked",
        subtitle_preset="viral_bounce",
    )
    s1.save_clip_result(
        clip_filename="clip_b.mp4",
        platform="shorts",
        views_24h=3000,
        views_7d=8000,
        revenue_brl=45.00,
        layout_mode="solo",
    )

    s2 = SQLiteStore(db_file)
    summary = s2.get_results_summary()
    assert summary["totals"]["total_posts"] == 2
    assert summary["totals"]["total_views_7d"] == 23000
    assert summary["totals"]["total_revenue_brl"] == 165.5


def test_api_export_and_results_flow():
    # 1. Renderiza um clipe rápido
    res_rend = client.post(
        "/api/v1/studio/render-now",
        json={
            "video_path": str(SYNTHETIC_VIDEO),
            "inicio_s": 0.0,
            "fim_s": 1.2,
            "layout": "solo",
            "burn_subtitles": False,
        },
    )
    assert res_rend.status_code == 200
    filename = res_rend.json()["filename"]

    # 2. Gera copy
    res_copy = client.post(
        "/api/v1/studio/generate-copy",
        json={"transcript_text": "Corte épico no GTA!", "platform": "tiktok"},
    )
    assert res_copy.status_code == 200
    copy = res_copy.json()

    # 3. Exporta pacote
    res_exp = client.post(
        "/api/v1/studio/export-package",
        json={
            "clip_filename": filename,
            "platform": "tiktok",
            "title": copy["title"],
            "description": copy["description"],
            "hashtags": copy["hashtags"],
        },
    )
    assert res_exp.status_code == 200
    assert Path(res_exp.json()["video_file"]).is_file()

    # 4. Registra resultado e consulta resumo
    res_post = client.post(
        "/api/v1/studio/results",
        json={
            "clip_filename": filename,
            "platform": "tiktok",
            "views_24h": 1200,
            "views_7d": 4500,
            "revenue_brl": 35.0,
        },
    )
    assert res_post.status_code == 200

    res_get = client.get("/api/v1/studio/results")
    assert res_get.status_code == 200
    assert res_get.json()["summary"]["totals"]["total_posts"] >= 1
