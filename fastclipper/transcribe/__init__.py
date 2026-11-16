"""
Adaptador de transcrição local com faster-whisper (CUDA/CPU) e cache em SQLite.
"""

from fastclipper.transcribe.whisper_service import WhisperTranscriptionService

__all__ = ["WhisperTranscriptionService"]
