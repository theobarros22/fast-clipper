from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app
from fastclipper.export.package_exporter import PackageExporter
from fastclipper.signals.moment_suggester import MomentSuggester
from fastclipper.subtitles.engine import CaptionPreset, SubtitleEngine
from fastclipper.subtitles.models import CaptionCue

FIXTURE_VIDEO = Path("tests/fixtures/synthetic/sintetico_gameplay_facecam.mp4")


def test_r1_studio_html_has_empty_subtitle_and_unchecked_burn():
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert 'placeholder="Ex: ESSA JOGADA FOI INSANA!' in res.text
    assert '>ESSA JOGADA FOI INSANA!' not in res.text
    assert 'id="burn_subtitles" style=' in res.text  # sem atributo checked por padrão
    assert "<h1" in res.text
    assert 'step="2"' in res.text


def test_r2_export_package_srt_is_relative_to_clip(tmp_path):
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    exporter = PackageExporter(exports_root=tmp_path)
    words = [
        {"word": "antes", "start_s": 0.5, "end_s": 1.0},
        {"word": "mundo", "start_s": 2.5, "end_s": 3.2},
        {"word": "depois", "start_s": 6.0, "end_s": 6.8},
    ]
    pkg = exporter.export_package(
        clip_path=FIXTURE_VIDEO,
        platform="x",
        title="Teste SRT Relativo",
        description="Desc",
        hashtags=["#teste"],
        words=words,
        inicio_s=2.0,
        fim_s=5.0,
    )
    srt_text = Path(pkg["srt_file"]).read_text(encoding="utf-8")
    assert "mundo" in srt_text
    assert "antes" not in srt_text
    assert "depois" not in srt_text
    assert "00:00:00,500" in srt_text


def test_r3_r4_r5_r6_r7_r8_api_validations():
    if not FIXTURE_VIDEO.exists():
        pytest.skip("Fixture sintética não gerada.")

    client = TestClient(app)

    # R3: altura ímpar ou fora de [200, 1720] -> 422
    r_odd = client.post("/api/v1/studio/render-now", json={
        "video_path": str(FIXTURE_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 2.0,
        "stacked_facecam_height": 721,
    })
    assert r_odd.status_code == 422

    # R6: inicio negativo ou fim <= inicio -> 422
    r_neg = client.post("/api/v1/studio/render-now", json={
        "video_path": str(FIXTURE_VIDEO),
        "inicio_s": -2.0,
        "fim_s": 2.0,
    })
    assert r_neg.status_code == 422

    # R8: preset inválido -> 422
    r_preset = client.post("/api/v1/studio/live/start", json={
        "url": "https://www.youtube.com/watch?v=test123",
        "subtitle_preset": "viral_clean",
    })
    assert r_preset.status_code == 422

    # R4 + R5: retângulo fora do vídeo é bloqueado por validate_layout_safety antes do FFmpeg com mensagem clara
    r_bounds = client.post("/api/v1/studio/render-now", json={
        "video_path": str(FIXTURE_VIDEO),
        "inicio_s": 0.0,
        "fim_s": 2.0,
        "layout": "stacked",
        "facecam_rect": {"x": 5000, "y": 5000, "w": 200, "h": 200},
        "burn_subtitles": False,
    })
    assert r_bounds.status_code == 400
    assert "fora dos limites" in r_bounds.json()["detail"].lower()

    # R7: resultados negativos ou plataforma inválida -> 422
    r_res = client.post("/api/v1/studio/results", json={
        "clip_filename": "clip.mp4",
        "platform": "orkut",
        "views_7d": -500,
        "revenue_brl": -10.0,
    })
    assert r_res.status_code == 422

    # favicon.ico não dá 404
    assert client.get("/favicon.ico").status_code == 204


def test_r10_r14_ass_escape_and_suggester_bounds(tmp_path):
    # Escape de chaves no ASS para não virar tag de estilo
    eng = SubtitleEngine()
    ass = eng.generate_ass([CaptionCue(cue_id=1, start_s=0.0, end_s=1.0, text=r"{\an5}Teste")], preset=CaptionPreset.MINIMAL_CLEAN)
    assert r"\{\\an5\}Teste" in ass or r"\\an5\}" in ass

    # Palavra-chave por palavra inteira (gol != golpe) e erro em suggest-moments
    suggester = MomentSuggester()
    with pytest.raises(ValueError):
        suggester.suggest(FIXTURE_VIDEO, duration_s=10.0, top_k=0)

    sug = suggester.suggest(
        FIXTURE_VIDEO if FIXTURE_VIDEO.exists() else tmp_path / "missing.mp4",
        duration_s=10.0,
        words=[{"word": "golpe", "start_s": 1.0, "end_s": 1.5}],
        target_duration_s=5.0,
        top_k=2,
    )
    assert len(sug) >= 1
    assert not any("gol" in str(s.get("keywords", [])) for s in sug)
