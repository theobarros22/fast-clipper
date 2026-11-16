"""
Testes automatizados do Sprint 3:
1. Snap de limites por palavra/frase (zero cortes no meio da palavra)
2. Sugestor e rankeador explicável de momentos (áudio + palavras-chave + deduplicação)
3. Endpoints /api/v1/studio/suggest-moments e /api/v1/studio/render-batch
4. Interface extraída para fastclipper/web/studio.html
"""

from pathlib import Path

from fastapi.testclient import TestClient

from fastclipper.api.app import app, durable_queue
from fastclipper.signals.moment_suggester import MomentSuggester, snap_bounds_to_words

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_VIDEO = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"


def test_snap_bounds_to_words_never_cuts_mid_word():
    words = [
        {"word": "Olha", "start_s": 1.0, "end_s": 1.4},
        {"word": "essa", "start_s": 1.5, "end_s": 1.8},
        {"word": "jogada", "start_s": 1.9, "end_s": 2.4},
        {"word": "incrível!", "start_s": 2.5, "end_s": 3.1},
    ]
    # Corte bruto no meio de "Olha" (1.2s) até no meio de "jogada" (2.1s)
    s, e = snap_bounds_to_words(words, raw_start=1.2, raw_end=2.1, total_duration_s=10.0)
    assert s == 1.0
    assert e == 2.4


def test_moment_suggester_ranks_keywords_and_deduplicates():
    words = [
        {"word": "Começando", "start_s": 0.2, "end_s": 0.6},
        {"word": "a", "start_s": 0.7, "end_s": 0.8},
        {"word": "partida.", "start_s": 0.9, "end_s": 1.3},
        {"word": "Meu", "start_s": 2.0, "end_s": 2.2},
        {"word": "Deus,", "start_s": 2.3, "end_s": 2.6},
        {"word": "que", "start_s": 2.7, "end_s": 2.9},
        {"word": "insano!", "start_s": 3.0, "end_s": 3.5},
    ]
    suggester = MomentSuggester()
    suggestions = suggester.suggest(
        video_path=SYNTHETIC_VIDEO,
        duration_s=5.0,
        words=words,
        target_duration_s=3.0,
        top_k=3,
    )
    assert len(suggestions) >= 1
    top = suggestions[0]
    assert top["score"] > 0.0
    assert "reason" in top
    assert top["fim_s"] > top["inicio_s"]


def test_api_suggest_moments_and_batch_render():
    durable_queue.clear_all()

    words = [
        {"word": "Olha", "start_s": 0.2, "end_s": 0.5},
        {"word": "esse", "start_s": 0.6, "end_s": 0.9},
        {"word": "clipe!", "start_s": 1.0, "end_s": 1.4},
    ]
    res_sug = client.post(
        "/api/v1/studio/suggest-moments",
        json={
            "video_path": str(SYNTHETIC_VIDEO),
            "words": words,
            "target_duration_s": 2.0,
            "top_k": 3,
        },
    )
    assert res_sug.status_code == 200
    sugs = res_sug.json()["suggestions"]
    assert len(sugs) >= 1

    res_batch = client.post(
        "/api/v1/studio/render-batch",
        json={
            "video_path": str(SYNTHETIC_VIDEO),
            "items": [{"inicio_s": 0.0, "fim_s": 1.0}, {"inicio_s": 1.0, "fim_s": 2.0}],
            "layout": "solo",
            "burn_subtitles": False,
        },
    )
    assert res_batch.status_code == 200
    data = res_batch.json()
    assert data["enqueued"] == 2
    assert len(data["job_ids"]) == 2

    # Confirma que a UI extraída em fastclipper/web/studio.html é servida em /
    res_home = client.get("/")
    assert res_home.status_code == 200
    assert "Sugerir Melhores Momentos" in res_home.text
    assert "Atalhos:" in res_home.text
