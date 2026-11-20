"""
Configuração do Piloto Automático (watchlist → corte → fila de aprovação → Buffer).

O arquivo real fica em data/autopilot/autopilot.json (fora do git, pode conter chaves).
O modelo comentado está em autopilot.example.json na raiz do projeto.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    return Path(os.environ.get("FASTCLIPPER_DATA_DIR", str(project_root() / "data"))).resolve()


def autopilot_dir() -> Path:
    d = data_dir() / "autopilot"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_config_path() -> Path:
    return autopilot_dir() / "autopilot.json"


@dataclass
class WatchSource:
    name: str
    url: str
    authorization_ref: str
    layout: Optional[str] = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WatchSource":
        name = str(d.get("name") or d.get("url") or "").strip()
        url = str(d.get("url") or "").strip()
        auth = str(d.get("authorization_ref") or "").strip()
        if not url:
            raise ValueError(f"Fonte '{name}' sem 'url'.")
        if not auth:
            raise ValueError(
                f"Fonte '{name}' sem 'authorization_ref'. Registre a permissão do criador "
                "(ex.: 'DM aprovada 19/11' ou 'campanha Whop X')."
            )
        return cls(
            name=name,
            url=url,
            authorization_ref=auth,
            layout=d.get("layout"),
            enabled=bool(d.get("enabled", True)),
        )


@dataclass
class AutopilotConfig:
    studio_url: str = "http://127.0.0.1:8000"
    page_handle: str = ""
    language: str = "pt"
    max_video_minutes: float = 90.0
    max_new_per_source: int = 1
    clips_per_video: int = 3
    clip_seconds: float = 25.0
    min_score: float = 0.0
    layout: str = "solo"
    subtitle_preset: str = "viral_bounce"
    normalize_audio: bool = True
    platforms: List[str] = field(default_factory=lambda: ["x", "reels", "tiktok", "shorts"])
    inbox_authorization_ref: str = "arquivo próprio ou autorizado (pasta de entrada)"
    watchlist: List[WatchSource] = field(default_factory=list)
    buffer: Dict[str, Any] = field(default_factory=dict)
    media_host: Dict[str, Any] = field(default_factory=dict)

    @property
    def inbox_dir(self) -> Path:
        d = autopilot_dir() / "entrada"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return autopilot_dir() / "autopilot.db"

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AutopilotConfig":
        cfg = cls()
        for key in (
            "studio_url", "page_handle", "language", "max_video_minutes", "max_new_per_source",
            "clips_per_video", "clip_seconds", "min_score", "layout", "subtitle_preset",
            "normalize_audio", "platforms", "inbox_authorization_ref", "buffer", "media_host",
        ):
            if key in raw and raw[key] is not None:
                setattr(cfg, key, raw[key])
        cfg.watchlist = [WatchSource.from_dict(s) for s in raw.get("watchlist", []) or []]
        cfg.studio_url = str(cfg.studio_url).rstrip("/")
        cfg.clips_per_video = max(1, min(10, int(cfg.clips_per_video)))
        cfg.max_new_per_source = max(1, min(5, int(cfg.max_new_per_source)))
        return cfg


def load_config(path: Optional[Path] = None) -> AutopilotConfig:
    p = Path(path) if path else default_config_path()
    if not p.is_file():
        example = project_root() / "autopilot.example.json"
        raise FileNotFoundError(
            f"Configuração não encontrada em {p}. Copie {example.name} para esse caminho e preencha."
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    return AutopilotConfig.from_dict(raw)
