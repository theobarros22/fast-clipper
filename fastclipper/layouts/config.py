"""
Gerenciamento de configurações de layout por canal e versão.

Permite salvar e recuperar retângulos de gameplay, facecam e zonas de proteção
de HUD por canal, com histórico versionado e validação de limites.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class LayoutMode(str, Enum):
    AUTO = "auto"
    SOLO = "solo"
    STACKED = "stacked"


@dataclass
class Rectangle:
    x: int
    y: int
    w: int
    h: int

    def __post_init__(self):
        self.x = int(self.x)
        self.y = int(self.y)
        self.w = int(self.w)
        self.h = int(self.h)

    def validate_bounds(self, source_w: int, source_h: int) -> bool:
        if self.w <= 0 or self.h <= 0:
            return False
        if self.x < 0 or self.y < 0:
            return False
        if self.x + self.w > source_w or self.y + self.h > source_h:
            return False
        return True

    def clamp(self, source_w: int, source_h: int) -> "Rectangle":
        x = max(0, min(self.x, source_w - 1))
        y = max(0, min(self.y, source_h - 1))
        w = max(1, min(self.w, source_w - x))
        h = max(1, min(self.h, source_h - y))
        return Rectangle(x, y, w, h)

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d: dict) -> "Rectangle":
        return cls(x=d["x"], y=d["y"], w=d["w"], h=d["h"])


@dataclass
class ChannelLayoutConfig:
    channel_id: str
    layout_name: str = "Padrão"
    version: int = 1
    mode: LayoutMode = LayoutMode.AUTO
    facecam_rect: Optional[Rectangle] = None
    gameplay_rect: Optional[Rectangle] = None
    hud_protected_zones: List[Rectangle] = field(default_factory=list)
    target_width: int = 1080
    target_height: int = 1920
    stacked_facecam_height: int = 720  # 720px para câmera e 1200px para gameplay

    def effective_mode(self) -> LayoutMode:
        """Determina o modo real caso mode seja AUTO."""
        if self.mode != LayoutMode.AUTO:
            return self.mode
        if self.facecam_rect is not None:
            return LayoutMode.STACKED
        return LayoutMode.SOLO

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "layout_name": self.layout_name,
            "version": self.version,
            "mode": self.mode.value,
            "facecam_rect": self.facecam_rect.to_dict() if self.facecam_rect else None,
            "gameplay_rect": self.gameplay_rect.to_dict() if self.gameplay_rect else None,
            "hud_protected_zones": [r.to_dict() for r in self.hud_protected_zones],
            "target_width": self.target_width,
            "target_height": self.target_height,
            "stacked_facecam_height": self.stacked_facecam_height,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelLayoutConfig":
        facecam = Rectangle.from_dict(data["facecam_rect"]) if data.get("facecam_rect") else None
        gameplay = Rectangle.from_dict(data["gameplay_rect"]) if data.get("gameplay_rect") else None
        huds = [Rectangle.from_dict(r) for r in data.get("hud_protected_zones", [])]
        mode = LayoutMode(data.get("mode", LayoutMode.AUTO.value))

        return cls(
            channel_id=data["channel_id"],
            layout_name=data.get("layout_name", "Padrão"),
            version=int(data.get("version", 1)),
            mode=mode,
            facecam_rect=facecam,
            gameplay_rect=gameplay,
            hud_protected_zones=huds,
            target_width=int(data.get("target_width", 1080)),
            target_height=int(data.get("target_height", 1920)),
            stacked_facecam_height=int(data.get("stacked_facecam_height", 720)),
        )


class LayoutConfigStore:
    """Repositório de persistência e versionamento de configurações de layout."""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _channel_dir(self, channel_id: str) -> Path:
        p = self.base_dir / channel_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def save_config(self, config: ChannelLayoutConfig) -> ChannelLayoutConfig:
        cdir = self._channel_dir(config.channel_id)
        latest_file = cdir / "latest.json"

        # Calcula a próxima versão se for 1 ou se já houver versões existentes
        versao = config.version
        if latest_file.exists():
            try:
                antigo = json.loads(latest_file.read_text(encoding="utf-8"))
                versao = max(versao, int(antigo.get("version", 0)) + 1)
            except Exception:
                pass

        config.version = versao
        dados_json = json.dumps(config.to_dict(), indent=2)

        # Salva versão específica e latest
        version_file = cdir / f"v{versao}.json"
        version_file.write_text(dados_json, encoding="utf-8")
        latest_file.write_text(dados_json, encoding="utf-8")
        logger.info("Layout salvo para o canal %s (v%d)", config.channel_id, versao)
        return config

    def get_config(self, channel_id: str, version: Optional[int] = None) -> Optional[ChannelLayoutConfig]:
        cdir = self._channel_dir(channel_id)
        if version is not None:
            v_file = cdir / f"v{version}.json"
            if v_file.is_file():
                return ChannelLayoutConfig.from_dict(json.loads(v_file.read_text(encoding="utf-8")))
            return None

        latest_file = cdir / "latest.json"
        if latest_file.is_file():
            return ChannelLayoutConfig.from_dict(json.loads(latest_file.read_text(encoding="utf-8")))
        return None

    def list_versions(self, channel_id: str) -> List[int]:
        cdir = self._channel_dir(channel_id)
        versoes = []
        for f in cdir.glob("v*.json"):
            try:
                num = int(f.stem.replace("v", ""))
                versoes.append(num)
            except ValueError:
                pass
        return sorted(versoes)
