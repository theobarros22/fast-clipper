"""
Descoberta de vídeos novos: canais/playlists via yt-dlp (YouTube, Twitch, Kick) e pasta de entrada local.

Regra anti-enxurrada: na PRIMEIRA vez que um canal aparece, só o vídeo mais recente entra na fila;
o restante é marcado como 'baseline' (histórico, nunca processado).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}


@dataclass
class VideoCandidate:
    key: str
    url: str
    title: str
    duration_s: Optional[float]
    is_local: bool = False
    local_path: Optional[Path] = None


def _flat_list(url: str, limit: int) -> List[Dict[str, Any]]:
    import yt_dlp  # dependência opcional [transcribe]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    entries = info.get("entries")
    if entries is None:  # URL de um vídeo único
        return [info]
    out: List[Dict[str, Any]] = []
    for e in entries:
        if not e:
            continue
        if e.get("_type") == "playlist" and e.get("entries"):  # canal com abas (Vídeos/Lives)
            out.extend(x for x in e["entries"] if x)
        else:
            out.append(e)
    return out[:limit]


def _full_info(url: str) -> Dict[str, Any]:
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _entry_url(e: Dict[str, Any]) -> str:
    u = e.get("webpage_url") or e.get("url") or ""
    if u and not u.startswith("http"):
        extractor = (e.get("ie_key") or e.get("extractor_key") or "").lower()
        if "youtube" in extractor:
            u = f"https://www.youtube.com/watch?v={u}"
    return u


def list_source_videos(
    url: str,
    limit: int = 6,
    lister: Callable[[str, int], List[Dict[str, Any]]] = _flat_list,
) -> List[VideoCandidate]:
    out: List[VideoCandidate] = []
    for e in lister(url, limit):
        if e.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue  # live em andamento ainda não tem VOD final
        vurl = _entry_url(e)
        vid = e.get("id") or vurl
        if not vurl or not vid:
            continue
        extractor = (e.get("ie_key") or e.get("extractor_key") or e.get("extractor") or "web").lower()
        out.append(
            VideoCandidate(
                key=f"{extractor}:{vid}",
                url=vurl,
                title=str(e.get("title") or vid),
                duration_s=float(e["duration"]) if e.get("duration") else None,
            )
        )
    return out


def resolve_duration(
    cand: VideoCandidate, info_fn: Callable[[str], Dict[str, Any]] = _full_info
) -> Optional[float]:
    if cand.duration_s:
        return cand.duration_s
    try:
        info = info_fn(cand.url)
        if info.get("live_status") in ("is_live", "is_upcoming"):
            return None
        return float(info["duration"]) if info.get("duration") else None
    except Exception as exc:  # pragma: no cover - depende de rede
        logger.warning("Não consegui ler a duração de %s: %s", cand.url, exc)
        return None


def list_inbox(inbox: Path) -> List[VideoCandidate]:
    out: List[VideoCandidate] = []
    for p in sorted(inbox.iterdir(), key=lambda x: x.stat().st_mtime):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("."):
            st = p.stat()
            out.append(
                VideoCandidate(
                    key=f"local:{p.name}:{st.st_size}",
                    url=str(p.resolve()),
                    title=p.stem,
                    duration_s=None,
                    is_local=True,
                    local_path=p.resolve(),
                )
            )
    return out
