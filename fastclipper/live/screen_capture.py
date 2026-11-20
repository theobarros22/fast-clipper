"""
Captura de tela como fonte do Clipping Ao Vivo.

Implementação própria sobre FFmpeg (gdigrab / ddagrab no Windows, x11grab no Linux).
Não usa nem copia código de terceiros: a única referência é a ideia de produto
(gravador de tela que alimenta o fluxo de cortes).

A fonte é representada por uma pseudo-URL ``screen://?target=...&fps=...`` para
reaproveitar todo o fluxo da sessão ao vivo (buffer segmentado, marcação, auto-clip).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional

SCREEN_SCHEME = "screen://"
DESKTOP_TARGET = "desktop"
VALID_BACKENDS = ("auto", "gdigrab", "ddagrab", "x11grab", "testsrc")
MIN_FPS = 10
MAX_FPS = 60

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ScreenCaptureError(ValueError):
    """Erro de configuração ou plataforma da captura de tela."""


@dataclass(frozen=True)
class ScreenSpec:
    target: str = DESKTOP_TARGET  # "desktop" ou título exato da janela
    fps: int = 30
    audio_device: Optional[str] = None
    backend: str = "auto"
    draw_mouse: bool = True


def _clean_text(value: Optional[str], field: str, max_len: int = 200) -> str:
    text = (value or "").strip()
    if len(text) > max_len:
        raise ScreenCaptureError(f"{field} muito longo (máximo {max_len} caracteres).")
    if _CONTROL_CHARS.search(text):
        raise ScreenCaptureError(f"{field} contém caracteres inválidos.")
    return text


def normalize_spec(spec: ScreenSpec) -> ScreenSpec:
    target = _clean_text(spec.target, "Alvo da captura") or DESKTOP_TARGET
    audio = _clean_text(spec.audio_device, "Dispositivo de áudio") or None
    backend = (spec.backend or "auto").strip().lower()
    if backend not in VALID_BACKENDS:
        raise ScreenCaptureError(f"Backend de captura inválido: {backend}.")
    fps = int(spec.fps)
    if not (MIN_FPS <= fps <= MAX_FPS):
        raise ScreenCaptureError(f"FPS deve estar entre {MIN_FPS} e {MAX_FPS}.")
    return ScreenSpec(target=target, fps=fps, audio_device=audio, backend=backend, draw_mouse=bool(spec.draw_mouse))


def is_screen_url(url: str) -> bool:
    return (url or "").lower().startswith(SCREEN_SCHEME)


def build_screen_url(spec: ScreenSpec) -> str:
    s = normalize_spec(spec)
    params = {"target": s.target, "fps": str(s.fps), "backend": s.backend, "mouse": "1" if s.draw_mouse else "0"}
    if s.audio_device:
        params["audio"] = s.audio_device
    return f"{SCREEN_SCHEME}?{urllib.parse.urlencode(params)}"


def parse_screen_url(url: str) -> ScreenSpec:
    if not is_screen_url(url):
        raise ScreenCaptureError("URL de captura de tela inválida.")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query, keep_blank_values=False)

    def first(key: str, default: Optional[str] = None) -> Optional[str]:
        vals = query.get(key)
        return vals[0] if vals else default

    try:
        fps = int(first("fps", "30") or 30)
    except ValueError as exc:
        raise ScreenCaptureError("FPS inválido.") from exc
    return normalize_spec(
        ScreenSpec(
            target=first("target", DESKTOP_TARGET) or DESKTOP_TARGET,
            fps=fps,
            audio_device=first("audio"),
            backend=first("backend", "auto") or "auto",
            draw_mouse=(first("mouse", "1") != "0"),
        )
    )


@lru_cache(maxsize=4)
def _ffmpeg_has_filter(ffmpeg_bin: str, name: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return re.search(rf"\b{re.escape(name)}\b", proc.stdout or "") is not None
    except Exception:
        return False


def resolve_backend(spec: ScreenSpec, ffmpeg_bin: str = "ffmpeg", platform_name: Optional[str] = None) -> str:
    """Escolhe o backend efetivo. No Windows prefere ddagrab (GPU, pega a maioria dos jogos) para a tela inteira."""
    plat = platform_name or sys.platform
    if spec.backend != "auto":
        if spec.backend == "ddagrab" and spec.target != DESKTOP_TARGET:
            raise ScreenCaptureError("ddagrab captura apenas a tela inteira. Use gdigrab para uma janela específica.")
        return spec.backend
    if plat.startswith("win"):
        if spec.target == DESKTOP_TARGET and _ffmpeg_has_filter(ffmpeg_bin, "ddagrab"):
            return "ddagrab"
        return "gdigrab"
    if plat.startswith("linux"):
        return "x11grab"
    raise ScreenCaptureError("Captura de tela suportada apenas no Windows e no Linux (X11).")


def _video_input_args(spec: ScreenSpec, backend: str) -> List[str]:
    mouse = "1" if spec.draw_mouse else "0"
    if backend == "gdigrab":
        src = DESKTOP_TARGET if spec.target == DESKTOP_TARGET else f"title={spec.target}"
        return ["-thread_queue_size", "512", "-f", "gdigrab", "-framerate", str(spec.fps), "-draw_mouse", mouse, "-i", src]
    if backend == "ddagrab":
        graph = f"ddagrab=framerate={spec.fps}:draw_mouse={mouse},hwdownload,format=bgra"
        return ["-f", "lavfi", "-i", graph]
    if backend == "x11grab":
        display = os.environ.get("DISPLAY", ":0.0") if spec.target == DESKTOP_TARGET else spec.target
        return ["-thread_queue_size", "512", "-f", "x11grab", "-framerate", str(spec.fps), "-draw_mouse", mouse, "-i", display]
    if backend == "testsrc":
        return ["-re", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={spec.fps}"]
    raise ScreenCaptureError(f"Backend sem suporte: {backend}.")


def _audio_input_args(spec: ScreenSpec, backend: str, platform_name: Optional[str] = None) -> List[str]:
    plat = platform_name or sys.platform
    if backend == "testsrc":
        return ["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    if spec.audio_device:
        if plat.startswith("win"):
            return ["-thread_queue_size", "512", "-f", "dshow", "-i", f"audio={spec.audio_device}"]
        return ["-thread_queue_size", "512", "-f", "pulse", "-i", spec.audio_device]
    # Sem dispositivo: trilha silenciosa, para todo segmento ter áudio (legendas/normalização esperam um stream de áudio).
    return ["-re", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]


def build_capture_command(
    spec: ScreenSpec,
    ffmpeg_bin: str,
    segment_pattern: str,
    segment_time_s: float,
    backend: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> List[str]:
    """Monta o comando FFmpeg que grava a tela em segmentos MPEG-TS (H.264 + AAC)."""
    spec = normalize_spec(spec)
    eff = backend or resolve_backend(spec, ffmpeg_bin, platform_name)
    cmd: List[str] = [ffmpeg_bin, "-hide_banner", "-loglevel", "error"]
    cmd += _video_input_args(spec, eff)
    cmd += _audio_input_args(spec, eff, platform_name)
    cmd += [
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-r", str(spec.fps),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-g", str(spec.fps * 2),
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "48000",
        "-f", "segment",
        "-segment_time", str(segment_time_s),
        "-reset_timestamps", "1",
        segment_pattern,
    ]
    return cmd


# ---------------------------------------------------------------------------
# Descoberta de fontes (janelas e dispositivos de áudio)
# ---------------------------------------------------------------------------

_DSHOW_NAMED = re.compile(r'"([^"]+)"\s+\((audio|video|none)\)')
_DSHOW_PLAIN = re.compile(r'\]\s+"([^"]+)"\s*$')


def parse_dshow_audio_devices(stderr_text: str) -> List[str]:
    """Extrai nomes de dispositivos de áudio da saída de ``ffmpeg -list_devices true -f dshow -i dummy``.

    Aceita o formato novo (``"Nome" (audio)``) e o antigo (seções "DirectShow audio devices").
    """
    devices: List[str] = []
    in_audio_section = False
    saw_named = False
    for line in (stderr_text or "").splitlines():
        if "Alternative name" in line:
            continue
        m = _DSHOW_NAMED.search(line)
        if m:
            saw_named = True
            if m.group(2) == "audio" and m.group(1) not in devices:
                devices.append(m.group(1))
            continue
        low = line.lower()
        if "directshow audio devices" in low:
            in_audio_section = True
            continue
        if "directshow video devices" in low:
            in_audio_section = False
            continue
        if in_audio_section and not saw_named:
            p = _DSHOW_PLAIN.search(line)
            if p and p.group(1) not in devices:
                devices.append(p.group(1))
    return devices


def _list_windows_audio_devices(ffmpeg_bin: str) -> List[str]:
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        return parse_dshow_audio_devices(proc.stderr or "")
    except Exception:
        return []


def _list_windows_titles() -> List[str]:
    """Títulos de janelas visíveis (Win32 EnumWindows via ctypes). Retorna [] fora do Windows ou em falha."""
    if not sys.platform.startswith("win"):
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        titles: List[str] = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            try:
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        title = buf.value.strip()
                        if title and title not in titles:
                            titles.append(title)
            except Exception:
                pass
            return True

        user32.EnumWindows(enum_proc(_cb), 0)
        return sorted(titles, key=str.lower)
    except Exception:
        return []


def list_screen_sources(ffmpeg_bin: Optional[str] = None) -> Dict[str, Any]:
    ffmpeg = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
    is_win = sys.platform.startswith("win")
    return {
        "platform": "windows" if is_win else ("linux" if sys.platform.startswith("linux") else sys.platform),
        "supported": is_win or sys.platform.startswith("linux"),
        "backends": {
            "ddagrab": bool(is_win and _ffmpeg_has_filter(ffmpeg, "ddagrab")),
            "gdigrab": is_win,
            "x11grab": sys.platform.startswith("linux"),
        },
        "windows": _list_windows_titles(),
        "audio_devices": _list_windows_audio_devices(ffmpeg) if is_win else [],
    }
