"""
Serviço de transcrição com timestamps por palavra usando faster-whisper (CUDA/CPU)
e cache persistente em SQLite indexado pelo SHA-256 do arquivo de mídia.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastclipper.ingest.vod_ingest import compute_file_sha256
from fastclipper.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class WhisperTranscriptionService:
    """Executa transcrição incremental com word_timestamps e cache por hash."""

    def __init__(
        self,
        store: Optional[SQLiteStore] = None,
        model_name: str = "small",
        default_language: Optional[str] = "pt",
    ):
        self.store = store
        self.model_name = model_name
        self.default_language = default_language
        self._loaded_models: Dict[tuple[str, str, str], Any] = {}

    def select_device_and_compute(self) -> tuple[str, str]:
        """Seleciona CUDA int8_float16 quando há GPU NVIDIA (ex: RTX 3050 6GB) ou CPU int8."""
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "int8_float16"
        except Exception:
            pass
        return "cpu", "int8"

    def _get_model(self, model_name: str, device: str, compute_type: str) -> Any:
        key = (model_name, device, compute_type)
        if key not in self._loaded_models:
            from faster_whisper import WhisperModel

            logger.info("Carregando WhisperModel(%s, device=%s, compute_type=%s)", model_name, device, compute_type)
            self._loaded_models[key] = WhisperModel(model_name, device=device, compute_type=compute_type)
        return self._loaded_models[key]

    def transcribe(
        self,
        media_path: str | Path,
        file_sha256: Optional[str] = None,
        language: Optional[str] = None,
        model_name: Optional[str] = None,
        force_retranscribe: bool = False,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> Dict[str, Any]:
        """Transcreve o áudio com timestamps por palavra, usando cache em SQLite se existir."""
        path = Path(media_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Arquivo de mídia não encontrado para transcrição: {path}")

        sha = file_sha256 or compute_file_sha256(path)
        if self.store and not force_retranscribe:
            cached = self.store.get_transcription(sha)
            if cached:
                if progress_callback:
                    progress_callback(100.0, "Transcrição carregada do cache SQLite")
                cached["from_cache"] = True
                return cached

        chosen_model = model_name or self.model_name
        lang = language if language is not None else self.default_language
        device, compute_type = self.select_device_and_compute()

        if progress_callback:
            progress_callback(5.0, f"Carregando modelo {chosen_model} ({device}/{compute_type})")

        model = self._get_model(chosen_model, device, compute_type)
        segments, info = model.transcribe(
            str(path),
            language=lang,
            word_timestamps=True,
            vad_filter=True,
        )

        total_dur = float(getattr(info, "duration", 0.0) or 0.0)
        words: List[Dict[str, Any]] = []
        texts: List[str] = []

        for seg in segments:
            seg_text = (seg.text or "").strip()
            if seg_text:
                texts.append(seg_text)
            if getattr(seg, "words", None):
                for w in seg.words:
                    token = (w.word or "").strip()
                    if not token:
                        continue
                    words.append(
                        {
                            "word": token,
                            "start_s": round(float(w.start), 3),
                            "end_s": round(float(w.end), 3),
                        }
                    )
            if progress_callback and total_dur > 0:
                pct = min(98.0, max(10.0, (float(seg.end) / total_dur) * 95.0))
                progress_callback(pct, f"Transcrevendo ({seg.end:.1f}s / {total_dur:.1f}s)")

        detected_lang = str(getattr(info, "language", lang or "pt"))
        full_text = " ".join(texts).strip()

        if self.store:
            saved = self.store.save_transcription(
                file_sha256=sha,
                language=detected_lang,
                model_name=chosen_model,
                duration_s=total_dur,
                words=words,
                full_text=full_text,
            )
            saved["from_cache"] = False
            saved["device"] = device
            if progress_callback:
                progress_callback(100.0, "Transcrição concluída e salva no cache")
            return saved

        if progress_callback:
            progress_callback(100.0, "Transcrição concluída")
        return {
            "file_sha256": sha,
            "language": detected_lang,
            "model_name": chosen_model,
            "duration_s": total_dur,
            "words": words,
            "full_text": full_text,
            "from_cache": False,
            "device": device,
        }
