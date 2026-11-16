"""
Ingestão de VODs locais e URLs autorizadas (yt-dlp) com registro obrigatório de autorização.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from fastclipper.ingest.precheck import MediaPrechecker
from fastclipper.live.youtube_live import SecuritySSRFError, YouTubeLiveIngestor
from fastclipper.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def compute_file_sha256(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Calcula SHA-256 em streaming sem carregar o vídeo inteiro na memória."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class VodIngestor:
    """Importa arquivos locais ou baixa VODs autorizados via yt-dlp."""

    def __init__(
        self,
        store: SQLiteStore,
        projects_root: str | Path,
        prechecker: Optional[MediaPrechecker] = None,
    ):
        self.store = store
        self.projects_root = Path(projects_root)
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.prechecker = prechecker or MediaPrechecker(min_duration_s=1.0)
        self.store.ensure_default_project()

    def _validate_auth_ref(self, authorization_ref: str) -> str:
        ref = (authorization_ref or "").strip()
        if not ref:
            raise ValueError("Referência de autorização (authorization_ref) é obrigatória para qualquer fonte.")
        return ref

    def _validate_project_id(self, project_id: str) -> str:
        pid = (project_id or "").strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", pid):
            raise ValueError("project_id inválido: use apenas letras, números, '-' ou '_' (1 a 64 caracteres).")
        return pid

    def import_local_file(
        self,
        file_path: str | Path,
        authorization_ref: str,
        project_id: str = "proj_default",
        title: Optional[str] = None,
        copy_to_project: bool = False,
    ) -> Dict[str, Any]:
        """Valida e registra um arquivo de vídeo local no SQLite."""
        ref = self._validate_auth_ref(authorization_ref)
        project_id = self._validate_project_id(project_id)
        src = Path(file_path).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Arquivo de vídeo não encontrado: {src}")

        probe = self.prechecker.probe(src, require_audio=False)
        if not probe.is_valid:
            raise ValueError(f"Arquivo reprovado no pre-check: {probe.message}")

        target_path = src
        if copy_to_project:
            dest_dir = self.projects_root / project_id / "source"
            dest_dir.mkdir(parents=True, exist_ok=True)
            target_path = dest_dir / src.name
            if target_path.resolve() != src:
                shutil.copy2(src, target_path)

        sha256 = compute_file_sha256(target_path)
        return self.store.save_source(
            project_id=project_id,
            title=title or target_path.stem,
            origin_type="local_file",
            file_path=str(target_path),
            file_sha256=sha256,
            authorization_ref=ref,
            duration_s=probe.duration_s,
            width=probe.width,
            height=probe.height,
            has_audio=probe.has_audio,
        )

    def download_from_url(
        self,
        url: str,
        authorization_ref: str,
        project_id: str = "proj_default",
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Baixa um VOD autorizado via yt-dlp com proteção SSRF e salva no projeto."""
        ref = self._validate_auth_ref(authorization_ref)
        project_id = self._validate_project_id(project_id)
        dest_dir = (self.projects_root / project_id / "source").resolve()
        if not dest_dir.is_relative_to(self.projects_root.resolve()):
            raise ValueError("Caminho de projeto fora da pasta permitida.")
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Reaproveita o SSRF guard já validado
        YouTubeLiveIngestor(stream_url=url, buffer_dir=dest_dir)

        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("yt-dlp não está instalado neste ambiente (.venv).") from exc

        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded = Path(ydl.prepare_filename(info))
            if not downloaded.exists():
                downloaded = downloaded.with_suffix(".mp4")

        if not downloaded.is_file():
            raise RuntimeError("Download concluído, mas o arquivo MP4 final não foi localizado.")

        probe = self.prechecker.probe(downloaded, require_audio=False)
        if not probe.is_valid:
            raise ValueError(f"VOD baixado reprovado no pre-check: {probe.message}")

        sha256 = compute_file_sha256(downloaded)
        return self.store.save_source(
            project_id=project_id,
            title=title or info.get("title") or downloaded.stem,
            origin_type="yt_dlp",
            origin_url=url,
            file_path=str(downloaded.resolve()),
            file_sha256=sha256,
            authorization_ref=ref,
            duration_s=probe.duration_s,
            width=probe.width,
            height=probe.height,
            has_audio=probe.has_audio,
        )
