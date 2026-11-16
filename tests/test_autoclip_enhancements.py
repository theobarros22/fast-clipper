"""
Testes automatizados para as melhorias e casos de teste inspirados no AutoClip:
  1. Media Pre-check Guard (media_precheck.py)
  2. Hardware Video Encoder Prober & Fallback (video_encoder.py)
  3. Deduplicação Temporal e Perfis Adaptativos (quality.py / clustering.py)
  4. Sentence Boundary Snapping & Trailing Question Filter (boundaries.py)
  5. Child Process Guard e contenção Windows Job Object (child_guard.py)
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from fastclipper.ingest.precheck import MediaPrechecker, PrecheckStatus
from fastclipper.rendering.encoder import HardwareEncoderManager, SOFTWARE_ENCODER
from fastclipper.signals.clustering import (
    DurationProfile,
    TemporalDeduplicator,
    get_duration_profile,
)
from fastclipper.signals.boundaries import SentenceBoundarySnapper, SubtitleRow
from fastclipper.signals.models import CandidateSuggestion
from fastclipper.process.child_guard import register_process, unregister_process, kill_all_guarded

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "synthetic"


# -----------------------------------------------------------------------------
# 1. Testes de Media Pre-check Guard
# -----------------------------------------------------------------------------

def test_precheck_valid_media():
    prechecker = MediaPrechecker(min_duration_s=2.0)
    video_path = FIXTURES_DIR / "sintetico_gameplay_facecam.mp4"
    if not video_path.is_file():
        pytest.skip("Fixture sintética não encontrada.")

    res = prechecker.probe(video_path, require_audio=True)
    assert res.is_valid is True
    assert res.status == PrecheckStatus.VALID
    assert res.has_audio is True
    assert res.has_video is True
    assert res.duration_s is not None and res.duration_s >= 2.0


def test_precheck_file_not_found():
    prechecker = MediaPrechecker()
    res = prechecker.probe("arquivo_inexistente_12345.mp4")
    assert res.is_valid is False
    assert res.status == PrecheckStatus.FILE_NOT_FOUND


def test_precheck_no_audio_detected():
    prechecker = MediaPrechecker(min_duration_s=1.0)
    video_no_audio = FIXTURES_DIR / "sintetico_sem_audio.mp4"
    if not video_no_audio.is_file():
        pytest.skip("Fixture sem áudio não encontrada.")

    res = prechecker.probe(video_no_audio, require_audio=True)
    assert res.is_valid is False
    assert res.status == PrecheckStatus.NO_AUDIO
    assert res.has_audio is False


def test_precheck_source_too_short():
    prechecker = MediaPrechecker(min_duration_s=60.0)  # Exige 60s
    video_path = FIXTURES_DIR / "sintetico_gameplay_facecam.mp4"  # ~10s
    if not video_path.is_file():
        pytest.skip("Fixture não encontrada.")

    res = prechecker.probe(video_path)
    assert res.is_valid is False
    assert res.status == PrecheckStatus.TOO_SHORT


# -----------------------------------------------------------------------------
# 2. Testes do Hardware Video Encoder Manager
# -----------------------------------------------------------------------------

def test_encoder_selection_and_bitrate_scaling():
    manager = HardwareEncoderManager()
    enc = manager.get_encoder()
    assert enc in ("h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox", "libx264")

    # Testa cálculo de bitrate
    rate = manager.calculate_bitrate(1080, 1920, fps=30)
    assert rate == int(1080 * 1920 * 30 * 0.13)

    args = manager.get_encoder_args(SOFTWARE_ENCODER, 1080, 1920, fps=30)
    assert "-c:v" in args
    assert SOFTWARE_ENCODER in args
    assert "-crf" in args


def test_forcing_software_encoder_via_env(monkeypatch):
    monkeypatch.setenv("FASTCLIPPER_VIDEO_ENCODER", "libx264")
    manager = HardwareEncoderManager()
    assert manager.get_encoder() == SOFTWARE_ENCODER


def test_encoder_fallback_on_broken(monkeypatch):
    manager = HardwareEncoderManager()
    monkeypatch.setattr(manager, "_candidates", lambda: ["h264_nvenc"])
    manager._chosen = "h264_nvenc"

    # Simula falha em tempo de execução no NVENC
    manager.mark_broken("h264_nvenc")
    assert "h264_nvenc" in manager._broken
    assert manager.get_encoder() == SOFTWARE_ENCODER


# -----------------------------------------------------------------------------
# 3. Testes de Deduplicação Temporal e Perfis Adaptativos
# -----------------------------------------------------------------------------

def test_duration_profiles_tiers():
    prof_short = get_duration_profile(300.0)   # 5 min
    prof_med = get_duration_profile(1800.0)    # 30 min
    prof_long = get_duration_profile(7200.0)   # 2 horas

    assert prof_short.tier == "short"
    assert prof_short.max_clip_sec == 60.0
    assert prof_short.min_keep == 1

    assert prof_med.tier == "medium"
    assert prof_med.target_clip_sec == (35.0, 60.0)
    assert prof_med.min_keep == 3

    assert prof_long.tier == "long"
    assert prof_long.max_clips == 25
    assert prof_long.min_keep == 5


def test_temporal_overlap_and_deduplication():
    dedup = TemporalDeduplicator(profile=get_duration_profile(3600.0))

    # Dois momentos com 75% de sobreposição
    cand1 = CandidateSuggestion(
        id="cand_01",
        timestamp_s=20.0,
        start_s=10.0,
        end_s=30.0,
        score=0.92,
        category="highlight",
    )
    cand2 = CandidateSuggestion(
        id="cand_02",
        timestamp_s=22.0,
        start_s=12.0,
        end_s=32.0,
        score=0.75,
        category="highlight",
    )
    cand_distinto = CandidateSuggestion(
        id="cand_03",
        timestamp_s=100.0,
        start_s=90.0,
        end_s=115.0,
        score=0.88,
        category="highlight",
    )

    resultado = dedup.deduplicate([cand1, cand2, cand_distinto])

    # cand2 deve ser absorvido por cand1 pois tem overlap > 50%
    assert len(resultado) == 2
    ids_retidos = [c.id for c in resultado]
    assert "cand_01" in ids_retidos
    assert "cand_03" in ids_retidos
    assert "cand_02" not in ids_retidos
    assert "merged_cand_02" in cand1.signals_breakdown


def test_select_clips_deterministic_supplement():
    prof = DurationProfile(
        tier="test",
        total_sec=1000.0,
        min_clip_sec=10.0,
        target_clip_sec=(15.0, 30.0),
        max_clip_sec=45.0,
        min_keep=3,
        max_clips=5,
    )
    dedup = TemporalDeduplicator(profile=prof)

    # Apenas 1 candidato acima do limiar 0.50
    c1 = CandidateSuggestion(id="c1", timestamp_s=10.0, start_s=5.0, end_s=15.0, score=0.85, category="hl")
    c2 = CandidateSuggestion(id="c2", timestamp_s=40.0, start_s=35.0, end_s=45.0, score=0.40, category="hl")
    c3 = CandidateSuggestion(id="c3", timestamp_s=70.0, start_s=65.0, end_s=75.0, score=0.35, category="hl")
    c4 = CandidateSuggestion(id="c4", timestamp_s=100.0, start_s=95.0, end_s=105.0, score=0.20, category="hl")

    selecionados = dedup.select_clips([c1, c2, c3, c4], min_score=0.50)

    # Deve garantir min_keep = 3 completando com c2 e c3
    assert len(selecionados) == 3
    ids = [c.id for c in selecionados]
    assert ids == ["c1", "c2", "c3"]


# -----------------------------------------------------------------------------
# 4. Testes de Sentence Boundary Snapping & Trailing Questions
# -----------------------------------------------------------------------------

def test_sentence_boundary_snap_to_complete_thought():
    snapper = SentenceBoundarySnapper()

    rows = [
        SubtitleRow(start_s=0.0, end_s=4.0, text="Nós pensamos com muito cuidado sobre isso."),
        SubtitleRow(start_s=4.0, end_s=8.0, text="É por isso que eu entrei na indústria de games."),
        SubtitleRow(start_s=8.0, end_s=12.0, text="Qual diretor influenciou você mais?"),
    ]

    # Corta bruto de 2.0s a 11.0s (pega meio da primeira frase e avança na pergunta final)
    novo_inicio, novo_fim = snapper.snap_boundaries(rows, raw_start_s=2.0, raw_end_s=11.0)

    # O início deve recuar para 0.0s (início da frase)
    assert novo_inicio == 0.0
    # O final deve cortar antes da pergunta solta, ficando em 8.0s
    assert novo_fim <= 8.0


def test_sentence_boundary_pause_delimited():
    snapper = SentenceBoundarySnapper(pause_threshold_s=0.6)

    # Sem pontuação terminal, mas com pausa de 1.0s entre t=6.0 e t=7.0
    rows = [
        SubtitleRow(start_s=1.0, end_s=6.0, text="acelera agora porque a policia esta chegando"),
        SubtitleRow(start_s=7.0, end_s=11.0, text="conseguimos despistar eles no tunel"),
    ]

    # Corta bruto terminando em 6.2s
    novo_inicio, novo_fim = snapper.snap_boundaries(rows, raw_start_s=2.0, raw_end_s=6.2)
    assert novo_inicio == 1.0
    assert novo_fim == 6.0


# -----------------------------------------------------------------------------
# 5. Testes de Child Process Guard & Windows Job Object
# -----------------------------------------------------------------------------

def test_child_process_guard_registration():
    # Cria processo rápido 'ping' ou 'python -c'
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.1)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    register_process(proc)
    assert proc.pid is not None

    # Aguarda término
    proc.wait(timeout=2.0)
    unregister_process(proc)


def test_child_process_guard_kill_all():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5.0)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    register_process(proc)

    # Força encerramento
    kill_all_guarded()
    assert proc.poll() is not None
