"""Módulo de processamento de legendas animadas."""
from fastclipper.subtitles.models import CaptionCue, CaptionPreset, WordTiming
from fastclipper.subtitles.engine import SubtitleEngine, to_srt_time, to_ass_time

__all__ = ["CaptionCue", "CaptionPreset", "WordTiming", "SubtitleEngine", "to_srt_time", "to_ass_time"]
