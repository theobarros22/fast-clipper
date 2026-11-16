"""
Exportador de pacotes de publicação locais (MP4 + SRT + capa.jpg + metadados).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastclipper.subtitles.engine import SubtitleEngine
from fastclipper.subtitles.models import WordTiming


def _slugify(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "_", (value or "clipe").strip()).strip("_")
    return clean[:48] or "clipe"


class PackageExporter:
    """Empacota clipe renderizado com legenda SRT, capa e textos prontos para postar."""

    def __init__(self, exports_root: str | Path, ffmpeg_bin: Optional[str] = None):
        self.exports_root = Path(exports_root)
        self.exports_root.mkdir(parents=True, exist_ok=True)
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.subtitle_engine = SubtitleEngine()

    def export_package(
        self,
        clip_path: str | Path,
        platform: str,
        title: str,
        description: str,
        hashtags: List[str],
        words: Optional[List[Dict[str, Any]]] = None,
        cover_timestamp_s: float = 0.5,
        inicio_s: float = 0.0,
        fim_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        src = Path(clip_path).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Clipe renderizado não encontrado: {src}")

        day_folder = datetime.now().strftime("%Y-%m-%d")
        slug = _slugify(title) or src.stem
        pkg_dir = self.exports_root / day_folder / f"{slug}_{src.stem[:8]}"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # 1. Copia MP4
        out_mp4 = pkg_dir / f"{slug}.mp4"
        shutil.copy2(src, out_mp4)

        # 2. Gera SRT se houver palavras (filtrando e deslocando para o tempo relativo do clipe)
        out_srt: Optional[Path] = None
        if words:
            t_ini = max(0.0, float(inicio_s or 0.0))
            t_fim = float(fim_s) if fim_s is not None else None
            wt = []
            for w in words:
                ws = float(w.get("start_s", 0.0))
                we = float(w.get("end_s", 0.0))
                if we <= t_ini:
                    continue
                if t_fim is not None and ws >= t_fim:
                    continue
                rel_s = max(0.0, ws - t_ini)
                rel_e = max(rel_s + 0.05, (min(t_fim, we) if t_fim is not None else we) - t_ini)
                wt.append(WordTiming(word=str(w.get("word", "")), start_s=rel_s, end_s=rel_e))
            cues = self.subtitle_engine.chunk_words(wt)
            if cues:
                out_srt = pkg_dir / f"{slug}.srt"
                out_srt.write_text(self.subtitle_engine.generate_srt(cues), encoding="utf-8")

        # 3. Extrai capa.jpg do clipe
        out_cover = pkg_dir / "capa.jpg"
        cmd = [
            self.ffmpeg_bin,
            "-loglevel", "error",
            "-y",
            "-ss", str(max(0.0, float(cover_timestamp_s))),
            "-i", str(out_mp4),
            "-vframes", "1",
            "-q:v", "2",
            str(out_cover),
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        # 4. Metadados JSON e arquivo texto pronto para copiar e colar
        hashtags_str = " ".join(hashtags)
        meta = {
            "platform": platform.lower(),
            "title": title,
            "description": description,
            "hashtags": hashtags,
            "video_file": str(out_mp4),
            "srt_file": str(out_srt) if out_srt else None,
            "cover_file": str(out_cover) if out_cover.is_file() else None,
            "exported_at": datetime.now().isoformat(),
        }
        (pkg_dir / "metadados.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (pkg_dir / "LEIA_PARA_POSTAR.txt").write_text(
            f"PLATAFORMA: {platform.upper()}\n"
            f"TÍTULO:\n{title}\n\n"
            f"DESCRIÇÃO:\n{description}\n\n"
            f"HASHTAGS:\n{hashtags_str}\n",
            encoding="utf-8",
        )

        zip_base = pkg_dir.parent / pkg_dir.name
        zip_path = Path(shutil.make_archive(str(zip_base), "zip", root_dir=str(pkg_dir)))
        rel_zip = f"{day_folder}/{zip_path.name}"

        return {
            "package_dir": str(pkg_dir.resolve()),
            "zip_file": str(zip_path.resolve()),
            "zip_url": f"/api/v1/studio/export-zip/{rel_zip}",
            "video_file": str(out_mp4.resolve()),
            "srt_file": str(out_srt.resolve()) if out_srt else None,
            "cover_file": str(out_cover.resolve()) if out_cover.is_file() else None,
            "platform": platform.lower(),
            "title": title,
        }
