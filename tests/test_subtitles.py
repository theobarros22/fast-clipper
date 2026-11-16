"""
Testes automatizados para o motor de legendas animadas em 9:16 (SubtitleEngine).
"""

import pytest
from fastclipper.subtitles.models import CaptionCue, CaptionPreset, WordTiming
from fastclipper.subtitles.engine import SubtitleEngine, to_srt_time, to_ass_time


def test_time_conversions():
    assert to_srt_time(0.0) == "00:00:00,000"
    assert to_srt_time(65.432) == "00:01:05,432"
    assert to_ass_time(0.0) == "0:00:00.00"
    assert to_ass_time(65.43) == "0:01:05.43"


def test_chunk_words_and_duration_limits():
    engine = SubtitleEngine()

    words = [
        WordTiming(word="Olha", start_s=1.0, end_s=1.3),
        WordTiming(word="a", start_s=1.35, end_s=1.5),
        WordTiming(word="polícia", start_s=1.55, end_s=2.0),
        WordTiming(word="chegando", start_s=2.05, end_s=2.5),
        WordTiming(word="no", start_s=2.55, end_s=2.7),
        WordTiming(word="túnel", start_s=2.75, end_s=3.2),
    ]

    cues = engine.chunk_words(words, max_words_per_cue=3, max_duration_s=2.0)
    assert len(cues) == 2
    assert cues[0].text == "Olha a polícia"
    assert cues[1].text == "chegando no túnel"
    assert cues[0].start_s == 1.0
    assert cues[1].end_s == 3.2


def test_remove_blinks_bridges_micro_gaps():
    engine = SubtitleEngine(blink_threshold_s=0.15)

    cues = [
        CaptionCue(cue_id="c1", start_s=1.0, end_s=2.0, text="Primeira fala"),
        CaptionCue(cue_id="c2", start_s=2.08, end_s=3.0, text="Segunda fala"),  # Gap de 0.08s (< 0.15s)
        CaptionCue(cue_id="c3", start_s=4.5, end_s=6.0, text="Terceira fala"),   # Gap de 1.5s (> 0.15s)
    ]

    sem_blinks = engine.remove_blinks(cues)
    # c1 deve ser estendido até 2.08s para eliminar o blink
    assert sem_blinks[0].end_s == 2.08
    # c2 não deve ser estendido até 4.5s porque a pausa é longa
    assert sem_blinks[1].end_s == 3.0


def test_generate_srt_format():
    engine = SubtitleEngine()
    cues = [
        CaptionCue(cue_id="c1", start_s=1.0, end_s=2.5, text="Texto do clipe 1"),
        CaptionCue(cue_id="c2", start_s=2.5, end_s=4.0, text="Texto do clipe 2"),
    ]

    srt_content = engine.generate_srt(cues)
    assert "1\n00:00:01,000 --> 00:00:02,500\nTexto do clipe 1" in srt_content
    assert "2\n00:00:02,500 --> 00:00:04,000\nTexto do clipe 2" in srt_content


def test_generate_ass_with_karaoke_tags():
    engine = SubtitleEngine()
    cues = [
        CaptionCue(
            cue_id="c1",
            start_s=1.0,
            end_s=2.0,
            text="Carro capotou",
            words=[
                WordTiming(word="Carro", start_s=1.0, end_s=1.4),
                WordTiming(word="capotou", start_s=1.4, end_s=2.0),
            ],
        ),
    ]

    ass_viral = engine.generate_ass(cues, preset=CaptionPreset.VIRAL_BOUNCE)
    assert "[Script Info]" in ass_viral
    assert "PlayResX: 1080" in ass_viral
    assert "PlayResY: 1920" in ass_viral
    # Tag de karaoke {\k<centésimos>}
    assert r"{\k40}Carro" in ass_viral
    assert r"{\k60}capotou" in ass_viral

    ass_minimal = engine.generate_ass(cues, preset=CaptionPreset.MINIMAL_CLEAN)
    assert "[Script Info]" in ass_minimal
    assert "Carro capotou" in ass_minimal
