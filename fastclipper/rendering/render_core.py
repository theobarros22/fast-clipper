"""
Núcleo único de renderização FFmpeg 9:16 do Fast Clipper.

Centraliza a construção do grafo de filtros (Solo / Empilhado), queima de
legendas ASS em passo único e fallback automático de encoder de hardware para x264.
Utilizado tanto pelo IsolatedRenderAdapter quanto pelo PipelineRunner.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from fastclipper.layouts.config import ChannelLayoutConfig, LayoutMode
from fastclipper.layouts.engine import LayoutEngine
from fastclipper.rendering.encoder import HardwareEncoderManager, SOFTWARE_ENCODER

logger = logging.getLogger(__name__)


def render_vertical_ffmpeg(
    output_path: Path,
    start_s: float,
    duration_s: float,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    encoder_manager: HardwareEncoderManager,
    source_path: Optional[Path] = None,
    concat_file: Optional[Path] = None,
    layout_config: Optional[ChannelLayoutConfig] = None,
    ass_path: Optional[Path] = None,
    timeout_s: float = 600.0,
) -> bool:
    """Executa um único passo FFmpeg para corte, layout 9:16 e queima opcional de legenda ASS."""
    if not source_path and not concat_file:
        raise ValueError("Informe source_path ou concat_file para renderização.")

    layout_engine = LayoutEngine(ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)

    if source_path is not None:
        source_w, source_h = layout_engine.get_source_dimensions(source_path)
        input_args = ["-ss", str(start_s), "-t", str(duration_s), "-i", str(source_path)]
    else:
        assert concat_file is not None
        # Lê a primeira linha do concat para obter dimensões da origem
        first_line = next(
            (ln for ln in concat_file.read_text(encoding="utf-8").splitlines() if ln.startswith("file '")),
            "",
        )
        first_seg = Path(first_line.removeprefix("file '").removesuffix("'"))
        source_w, source_h = layout_engine.get_source_dimensions(first_seg)
        input_args = ["-f", "concat", "-safe", "0", "-i", str(concat_file), "-ss", str(start_s), "-t", str(duration_s)]

    cmd = [ffmpeg_bin, "-loglevel", "error", "-y"] + input_args

    if layout_config is not None:
        filtro_base = layout_engine.build_filter_graph(layout_config, source_w, source_h)
        is_stacked = (
            layout_config.effective_mode() == LayoutMode.STACKED
            and layout_config.facecam_rect is not None
        )
    else:
        filtro_base = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
        is_stacked = False

    cwd: Optional[str] = None
    if ass_path is not None and ass_path.is_file():
        # Usa cwd na pasta do .ass e nome relativo para evitar problemas com ':' no Windows
        cwd = str(ass_path.parent.resolve())
        ass_name = ass_path.name
        if is_stacked:
            filtro_mod = filtro_base.replace("[outv]", "[pre_sub]")
            filtro_final = f"{filtro_mod}; [pre_sub] ass={ass_name} [outv]"
            cmd.extend(["-filter_complex", filtro_final, "-map", "[outv]", "-map", "0:a?"])
        else:
            cmd.extend(["-vf", f"{filtro_base},ass={ass_name}"])
    else:
        if is_stacked:
            cmd.extend(["-filter_complex", filtro_base, "-map", "[outv]", "-map", "0:a?"])
        else:
            cmd.extend(["-vf", filtro_base])

    enc = encoder_manager.get_encoder()
    enc_args = encoder_manager.get_encoder_args(enc, width=1080, height=1920)
    full_cmd = list(cmd) + enc_args + ["-c:a", "aac", "-b:a", "128k", str(output_path.resolve())]
    try:
        res = subprocess.run(full_cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg excedeu o tempo limite de %.0fs.", timeout_s)
        return False

    if res.returncode != 0 and enc != SOFTWARE_ENCODER:
        logger.warning("Hardware encoder '%s' falhou. Recorrendo ao %s.", enc, SOFTWARE_ENCODER)
        encoder_manager.mark_broken(enc)
        sw_args = encoder_manager.get_encoder_args(SOFTWARE_ENCODER, width=1080, height=1920)
        full_cmd = list(cmd) + sw_args + ["-c:a", "aac", "-b:a", "128k", str(output_path.resolve())]
        try:
            res = subprocess.run(full_cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg (fallback x264) excedeu o tempo limite de %.0fs.", timeout_s)
            return False

    if res.returncode != 0:
        logger.error("Erro no FFmpeg: %s", res.stderr)
        return False
    return True
