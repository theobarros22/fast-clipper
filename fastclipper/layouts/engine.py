"""
Motor de composição de layout 9:16 (Solo e Empilhado Facecam + Gameplay).

Gera grafos de filtros complexos do FFmpeg, prévias visuais e validação de segurança
contra corte de rostos (headroom) e violação de áreas de HUD.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastclipper.layouts.config import ChannelLayoutConfig, LayoutMode, Rectangle

logger = logging.getLogger(__name__)


class LayoutEngine:
    """Motor de cálculo e renderização de layouts 9:16."""

    def __init__(self, ffmpeg_bin: Optional[str] = None, ffprobe_bin: Optional[str] = None):
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_bin = ffprobe_bin or shutil.which("ffprobe") or "ffprobe"

    def get_source_dimensions(self, video_path: str | Path) -> Tuple[int, int]:
        cmd = [
            self.ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(video_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        dados = json.loads(res.stdout)
        stream = dados["streams"][0]
        return int(stream["width"]), int(stream["height"])

    def validate_layout_safety(
        self,
        config: ChannelLayoutConfig,
        source_w: int,
        source_h: int,
        detected_head_boxes: Optional[List[Tuple[float, float, float, float]]] = None,
    ) -> Dict[str, Any]:
        """Valida se as caixas estão contidas no quadro e se cabeças não são decepadas."""
        errors: List[str] = []
        warnings: List[str] = []

        mode = config.effective_mode()

        if config.facecam_rect is not None:
            if not config.facecam_rect.validate_bounds(source_w, source_h):
                errors.append(
                    f"Caixa de Facecam fora dos limites da origem ({source_w}x{source_h}): "
                    f"[{config.facecam_rect.x}, {config.facecam_rect.y}, {config.facecam_rect.w}, {config.facecam_rect.h}]."
                )

        if config.gameplay_rect is not None:
            if not config.gameplay_rect.validate_bounds(source_w, source_h):
                errors.append(
                    f"Caixa de Gameplay fora dos limites da origem ({source_w}x{source_h}): "
                    f"[{config.gameplay_rect.x}, {config.gameplay_rect.y}, {config.gameplay_rect.w}, {config.gameplay_rect.h}]."
                )

        # Validação contra corte de cabeça (Headroom)
        if detected_head_boxes and config.facecam_rect is not None:
            fc = config.facecam_rect
            for idx, (hx0, hy0, hx1, hy1) in enumerate(detected_head_boxes):
                # Se a cabeça está dentro da região geral da câmera, confere se as bordas cortam a cabeça
                if (hx0 >= fc.x and hx1 <= fc.x + fc.w) or (hy0 >= fc.y and hy1 <= fc.y + fc.h):
                    # Cabeça intercepta a borda?
                    corta_topo = hy0 < fc.y
                    corta_base = hy1 > (fc.y + fc.h)
                    corta_esq = hx0 < fc.x
                    corta_dir = hx1 > (fc.x + fc.w)

                    if corta_topo or corta_esq or corta_dir:
                        warnings.append(
                            f"Cabeça #{idx} pode estar cortada pelas bordas da Facecam: "
                            f"Cabeça=[{hx0:.0f}, {hy0:.0f}, {hx1:.0f}, {hy1:.0f}], Facecam=[{fc.x}, {fc.y}, {fc.w}, {fc.h}]."
                        )

        # Validação de zonas protegidas de HUD
        if config.hud_protected_zones:
            for hud in config.hud_protected_zones:
                if not hud.validate_bounds(source_w, source_h):
                    warnings.append(f"Zona de HUD fora dos limites: {hud}")

        return {
            "safe": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "effective_mode": mode.value,
        }

    def build_filter_graph(
        self,
        config: ChannelLayoutConfig,
        source_w: int,
        source_h: int,
    ) -> str:
        """Gera a expressão de filtro do FFmpeg para a composição 9:16."""
        mode = config.effective_mode()
        target_w = config.target_width
        target_h = config.target_height

        if mode == LayoutMode.BLUR_FIT:
            return (
                f"[0:v] scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h},boxblur=20:5,eq=brightness=-0.15 [bg]; "
                f"[0:v] scale={target_w}:{target_h}:force_original_aspect_ratio=decrease [fg]; "
                f"[bg][fg] overlay=(W-w)/2:(H-h)/2 [outv]"
            )

        if mode == LayoutMode.SOLO or config.facecam_rect is None:
            # Modo Solo (9:16 ou 16:9)
            if config.gameplay_rect:
                gp = config.gameplay_rect.clamp(source_w, source_h)
                return (
                    f"crop={gp.w}:{gp.h}:{gp.x}:{gp.y},"
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                    f"crop={target_w}:{target_h}"
                )
            if target_w > target_h:
                return (
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2"
                )
            return (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h}"
            )

        # Modo Empilhado (Stacked: Facecam superior + Gameplay inferior)
        fc = config.facecam_rect.clamp(source_w, source_h)
        h_cam = config.stacked_facecam_height
        h_game = target_h - h_cam

        if config.gameplay_rect:
            gp = config.gameplay_rect.clamp(source_w, source_h)
            crop_game = f"crop={gp.w}:{gp.h}:{gp.x}:{gp.y}"
        else:
            crop_game = f"crop={source_w}:{source_h}:0:0"

        # Painel superior (Câmera): crop -> scale mantendo proporção -> crop para largura target_w e altura h_cam
        # Painel inferior (Gameplay): crop -> scale mantendo proporção -> crop para largura target_w e altura h_game
        # vstack une os dois painéis formando target_w x target_h
        filtro_complexo = (
            f"[0:v] crop={fc.w}:{fc.h}:{fc.x}:{fc.y}, "
            f"scale={target_w}:{h_cam}:force_original_aspect_ratio=increase, "
            f"crop={target_w}:{h_cam} [cam]; "
            f"[0:v] {crop_game}, "
            f"scale={target_w}:{h_game}:force_original_aspect_ratio=increase, "
            f"crop={target_w}:{h_game} [game]; "
            f"[cam][game] vstack=inputs=2 [outv]"
        )
        return filtro_complexo

    def generate_preview(
        self,
        video_path: str | Path,
        timestamp_s: float,
        config: ChannelLayoutConfig,
        output_image_path: str | Path,
    ) -> Path:
        """Gera uma imagem de prévia (1080x1920) aplicando o layout selecionado."""
        source_w, source_h = self.get_source_dimensions(video_path)
        out_p = Path(output_image_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)

        mode = config.effective_mode()
        filtro = self.build_filter_graph(config, source_w, source_h)

        cmd = [
            self.ffmpeg_bin,
            "-loglevel", "error",
            "-y",
            "-ss", str(timestamp_s),
            "-i", str(video_path),
        ]

        if mode == LayoutMode.STACKED and config.facecam_rect is not None:
            cmd.extend([
                "-filter_complex", filtro,
                "-map", "[outv]",
            ])
        else:
            cmd.extend([
                "-vf", filtro,
            ])

        cmd.extend([
            "-vframes", "1",
            "-q:v", "2",
            str(out_p),
        ])

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg falhou ao gerar preview do layout: {res.stderr}")

        return out_p

    def extract_source_frame(
        self,
        video_path: str | Path,
        timestamp_s: float,
        output_image_path: str | Path,
    ) -> Dict[str, Any]:
        """Extrai um frame original da origem (sem crop) e retorna suas dimensões."""
        source_w, source_h = self.get_source_dimensions(video_path)
        out_p = Path(output_image_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.ffmpeg_bin,
            "-loglevel", "error",
            "-y",
            "-ss", str(max(0.0, float(timestamp_s))),
            "-i", str(video_path),
            "-vframes", "1",
            "-q:v", "2",
            str(out_p),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0 or not out_p.is_file():
            raise RuntimeError(f"FFmpeg falhou ao extrair frame de origem: {res.stderr}")

        return {
            "image_path": str(out_p),
            "source_width": source_w,
            "source_height": source_h,
            "timestamp_s": float(timestamp_s),
        }
