"""
Gerenciador de captura e clipping ao vivo (YouTube Live e X/Twitter Live).

Conecta a ingestão contínua em segmentos MPEG-TS (via yt-dlp + FFmpeg) ao
SegmentedBuffer e ao ManualMarker, permitindo cortes instantâneos ou com janela
posterior e auto-clipping por picos de áudio enquanto a live acontece.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastclipper.ingest.buffer import SegmentedBuffer
from fastclipper.ingest.clock import VirtualClock
from fastclipper.ingest.marker import CandidateStatus, ManualMarker
from fastclipper.live.youtube_live import SecuritySSRFError, YouTubeLiveIngestor

logger = logging.getLogger(__name__)


def _validate_public_http_url(url: str) -> None:
    """Garante que uma URL resolvida (ex.: CDN HLS) seja HTTP/HTTPS pública (anti-SSRF)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecuritySSRFError(f"Esquema inválido na URL de stream: {parsed.scheme}")
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise SecuritySSRFError(f"Host interno bloqueado na URL de stream: {hostname}")
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise SecuritySSRFError(f"IP não público bloqueado na URL de stream: {ip}")
    except ValueError:
        pass


def resolve_live_stream_url(page_url: str) -> str:
    """Resolve a URL de mídia (HLS/manifest) de uma live pública via yt-dlp."""
    try:
        import yt_dlp  # type: ignore

        ydl_opts = {
            "format": "best[ext=mp4]/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
            if not info:
                raise RuntimeError("Não foi possível extrair informações da transmissão.")
            stream_url = info.get("url")
            if not stream_url and info.get("requested_formats"):
                stream_url = info["requested_formats"][0].get("url")
            if not stream_url:
                raise RuntimeError("URL de stream ao vivo não encontrada para este link.")
            _validate_public_http_url(stream_url)
            return stream_url
    except ImportError:
        yt_dlp_bin = shutil.which("yt-dlp")
        if not yt_dlp_bin:
            raise RuntimeError("yt-dlp não encontrado no ambiente (.venv nem PATH).")
        proc = subprocess.run(
            [
                yt_dlp_bin,
                "-g",
                "--no-playlist",
                "--no-warnings",
                "-f",
                "best[ext=mp4]/best",
                page_url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"Falha ao resolver stream ao vivo via yt-dlp: {err}")
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            raise RuntimeError("yt-dlp não retornou URL de stream para esta live.")
        _validate_public_http_url(lines[0])
        return lines[0]


def _measure_segment_volume_db(ffmpeg_bin: str, seg_path: Path) -> Optional[float]:
    """Mede o volume máximo (dB) de um segmento para detecção simples de pico ao vivo."""
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", str(seg_path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr or "")
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return None


class LiveClipSession:
    """Sessão ativa de ingestão contínua e clipping 9:16 em tempo real."""

    def __init__(
        self,
        session_id: str,
        url: str,
        session_dir: Path,
        pipeline_runner: Any,
        segment_duration_s: float = 10.0,
        retention_seconds: float = 1800.0,
        auto_clip: bool = False,
        auto_clip_duration_s: float = 25.0,
        auto_clip_threshold_db: float = -12.0,
        auto_clip_keywords: Optional[List[str]] = None,
        default_render_opts: Optional[Dict[str, Any]] = None,
        stream_resolver: Optional[Callable[[str], str]] = None,
        transcribe_fn: Optional[Callable[[Path, str], Dict[str, Any]]] = None,
    ):
        self.session_id = session_id
        self.url = url
        self.session_dir = Path(session_dir)
        self.segments_dir = self.session_dir / "segments"
        self.windows_dir = self.session_dir / "windows"
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        self.windows_dir.mkdir(parents=True, exist_ok=True)

        # Valida SSRF e plataforma antes de qualquer operação de rede
        YouTubeLiveIngestor(stream_url=url, buffer_dir=self.segments_dir)
        self.platform = YouTubeLiveIngestor.detect_platform(url)

        self.pipeline_runner = pipeline_runner
        self.segment_duration_s = max(2.0, float(segment_duration_s))
        self.retention_seconds = max(60.0, float(retention_seconds))
        self.auto_clip = bool(auto_clip)
        self.auto_clip_duration_s = max(5.0, float(auto_clip_duration_s))
        self.auto_clip_threshold_db = float(auto_clip_threshold_db)
        self.auto_clip_keywords = [k.strip().lower() for k in (auto_clip_keywords or []) if k and k.strip()]
        self.auto_clip_cooldown_s = max(20.0, self.auto_clip_duration_s)
        self.default_render_opts = dict(default_render_opts or {})
        self.stream_resolver = stream_resolver or resolve_live_stream_url
        self.transcribe_fn = transcribe_fn
        self.last_transcript_snippet: str = ""

        self.buffer = SegmentedBuffer(
            buffer_dir=self.segments_dir,
            stream_id=session_id,
            retention_seconds=self.retention_seconds,
        )
        self.clock = VirtualClock(start_time=0.0)
        self.marker = ManualMarker(
            buffer=self.buffer,
            clock=self.clock,
            pre_window_s=self.auto_clip_duration_s,
            post_window_s=15.0,
        )

        self.ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
        self._proc: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self.running = False
        self.started_at = time.time()
        self.last_error: Optional[str] = None
        self.registered_indices: set[int] = set()
        self._next_t_start: float = 0.0
        self._candidate_render_opts: Dict[str, Dict[str, Any]] = {}
        self.clips_generated: List[Dict[str, Any]] = []
        self._last_auto_clip_s: float = -999.0

    def start(self) -> None:
        """Resolve o stream e inicia captura contínua em segmentos via FFmpeg."""
        resolved_url = self.stream_resolver(self.url)
        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
        ]
        if resolved_url.startswith(("http://", "https://")):
            cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"])

        cmd.extend([
            "-i",
            resolved_url,
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_duration_s),
            "-reset_timestamps",
            "1",
            str(self.segments_dir / "seg_%05d.ts"),
        ])

        self._stop_event.clear()
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.running = True
        self.started_at = time.time()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name=f"LiveMonitor-{self.session_id}",
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        """Encerra captura ao vivo de forma limpa e processa o último segmento gravado."""
        self._stop_event.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=3)
        self._scan_new_segments(include_last=True)
        self.running = False

    def _probe_segment_duration(self, seg_path: Path) -> float:
        try:
            proc = subprocess.run(
                [
                    self.ffprobe_bin,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(seg_path),
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            val = float((proc.stdout or "").strip())
            if val > 0.1:
                return val
        except Exception:
            pass
        return self.segment_duration_s

    def _scan_new_segments(self, include_last: bool = False) -> List[int]:
        """Registra no SegmentedBuffer os arquivos seg_XXXXX.ts já finalizados."""
        files = sorted(self.segments_dir.glob("seg_*.ts"))
        if not files:
            return []

        ready_files = files if include_last else files[:-1]
        novos: List[int] = []
        for f in ready_files:
            m = re.match(r"seg_(\d+)\.ts$", f.name)
            if not m:
                continue
            idx = int(m.group(1))
            if idx in self.registered_indices or not f.exists() or f.stat().st_size == 0:
                continue
            dur = self._probe_segment_duration(f)
            t_ini = self._next_t_start
            t_fim = round(t_ini + dur, 3)
            self._next_t_start = t_fim
            self.buffer.add_segment(
                segment_id=idx,
                filename=f.name,
                timestamp_inicio=t_ini,
                timestamp_fim=t_fim,
            )
            self.registered_indices.add(idx)
            novos.append(idx)
            if t_fim > self.clock.current_time():
                self.clock.seek(t_fim)
        return novos

    def _evaluate_auto_clip_trigger(self, seg_path: Path) -> Optional[str]:
        """Avalia pico de áudio e, se configurado, palavras-chave transcritas no segmento."""
        max_db = _measure_segment_volume_db(self.ffmpeg_bin, seg_path)
        if max_db is not None and max_db > self.auto_clip_threshold_db:
            return f"Auto Pico ({max_db:.1f} dB)"

        if self.auto_clip_keywords and self.transcribe_fn is not None:
            try:
                lang = str(self.default_render_opts.get("language", "pt") or "pt")
                tr = self.transcribe_fn(seg_path, lang) or {}
                txt = str(tr.get("full_text", "") or "").strip()
                if txt:
                    self.last_transcript_snippet = txt[-140:]
                    low = txt.lower()
                    for kw in self.auto_clip_keywords:
                        if kw in low:
                            return f"Auto Palavra: {kw}"
            except Exception as exc:
                logger.warning("Erro ao avaliar palavras-chave no segmento ao vivo: %s", exc)
        return None

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                novos = self._scan_new_segments(include_last=False)
                now_t = self.clock.current_time()

                # Verifica marcações que aguardavam post_window
                prontos = self.marker.check_pending_candidates(current_time=now_t, stream_finished=False)
                for cand in prontos:
                    opts = self._candidate_render_opts.pop(cand.id, self.default_render_opts)
                    threading.Thread(
                        target=self._render_window_worker,
                        args=(cand.inicio_s, cand.fim_s, cand.titulo, opts, cand.id),
                        daemon=True,
                    ).start()

                # Auto-clipping por pico de áudio ou palavras-chave nos novos segmentos
                if self.auto_clip and novos and now_t >= 4.0 and (now_t - self._last_auto_clip_s >= self.auto_clip_cooldown_s):
                    last_seg_file = self.segments_dir / f"seg_{novos[-1]:05d}.ts"
                    triggered_title = self._evaluate_auto_clip_trigger(last_seg_file)
                    if triggered_title:
                        self._last_auto_clip_s = now_t
                        ini = max(0.0, now_t - self.auto_clip_duration_s)
                        threading.Thread(
                            target=self._render_window_worker,
                            args=(ini, now_t, triggered_title, self.default_render_opts, None),
                            daemon=True,
                        ).start()

                self.buffer.prune_expired(now_t)

                # Verifica se o processo FFmpeg encerrou inesperadamente
                if self._proc and self._proc.poll() is not None:
                    self._scan_new_segments(include_last=True)
                    prontos_fim = self.marker.check_pending_candidates(
                        current_time=self.clock.current_time(),
                        stream_finished=True,
                    )
                    for cand in prontos_fim:
                        opts = self._candidate_render_opts.pop(cand.id, self.default_render_opts)
                        self._render_window_worker(cand.inicio_s, cand.fim_s, cand.titulo, opts, cand.id)
                    if self._proc.returncode not in (0, None) and not self._stop_event.is_set():
                        stderr_txt = ""
                        try:
                            stderr_txt = (self._proc.stderr.read() or "").strip()
                        except Exception:
                            pass
                        self.last_error = stderr_txt or f"FFmpeg encerrou com código {self._proc.returncode}"
                    self.running = False
                    break
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Erro no monitor da live %s", self.session_id)

            self._stop_event.wait(1.0)

    def extract_window(self, inicio_s: float, fim_s: float, out_mp4: Path) -> Path:
        """Extrai o intervalo [inicio_s, fim_s] do buffer circular para um MP4 temporário."""
        segs = self.buffer.get_segments_in_range(inicio_s, fim_s)
        if not segs:
            raise RuntimeError("Nenhum segmento disponível no buffer para este intervalo.")

        lock_id = f"extract_{uuid.uuid4().hex[:8]}"
        self.buffer.lock_range(inicio_s, fim_s, lock_id)
        try:
            concat_file = self.windows_dir / f"concat_{lock_id}.txt"
            lines = []
            for seg in segs:
                seg_abs = (self.segments_dir / seg.filename).resolve().as_posix().replace("'", "'\\''")
                lines.append(f"file '{seg_abs}'")
            concat_file.write_text("\n".join(lines), encoding="utf-8")

            rel_start = max(0.0, inicio_s - segs[0].timestamp_inicio)
            duration = max(1.0, fim_s - inicio_s)

            cmd_reenc = [
                self.ffmpeg_bin,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-ss",
                f"{rel_start:.3f}",
                "-t",
                f"{duration:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(out_mp4),
            ]
            res = subprocess.run(cmd_reenc, capture_output=True, text=True, timeout=120)
            if res.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size == 0:
                raise RuntimeError(f"Falha ao extrair janela do buffer: {res.stderr}")
            return out_mp4
        finally:
            self.buffer.unlock_range(lock_id)
            try:
                concat_file.unlink(missing_ok=True)
            except Exception:
                pass

    def _render_window_worker(
        self,
        inicio_s: float,
        fim_s: float,
        titulo: str,
        render_opts: Dict[str, Any],
        candidate_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        window_mp4 = self.windows_dir / f"win_{uuid.uuid4().hex[:8]}.mp4"
        try:
            self.extract_window(inicio_s, fim_s, window_mp4)
            dur = max(1.0, fim_s - inicio_s)
            opts = {**self.default_render_opts, **(render_opts or {})}
            burn_subs = bool(opts.get("burn_subtitles", False))
            subtitles_words = opts.get("subtitles")
            transcript_text = ""

            if burn_subs and not subtitles_words and self.transcribe_fn is not None:
                try:
                    lang = str(opts.get("language", "pt") or "pt")
                    tr = self.transcribe_fn(window_mp4, lang) or {}
                    subtitles_words = tr.get("words") or None
                    transcript_text = str(tr.get("full_text", "") or "")
                except Exception as exc:
                    logger.warning("Falha na transcrição automática do clipe ao vivo: %s", exc)

            params = {
                "video_path": str(window_mp4),
                "inicio_s": 0.0,
                "fim_s": dur,
                "layout": opts.get("layout", "stacked"),
                "facecam_rect": opts.get("facecam_rect"),
                "gameplay_rect": opts.get("gameplay_rect"),
                "stacked_facecam_height": int(opts.get("stacked_facecam_height", 720)),
                "aspect_ratio": opts.get("aspect_ratio", "9:16"),
                "subtitles": subtitles_words,
                "subtitles_are_absolute": False,
                "subtitle_preset": opts.get("subtitle_preset", "viral_clean"),
                "burn_subtitles": burn_subs and bool(subtitles_words),
            }
            result = self.pipeline_runner.render_clip(params)
            if result.success and result.output_video_path:
                fname = Path(result.output_video_path).name
                entry = {
                    "filename": fname,
                    "titulo": titulo,
                    "inicio_s": round(inicio_s, 2),
                    "fim_s": round(fim_s, 2),
                    "download_url": f"/api/v1/studio/video/{fname}",
                    "transcript_text": transcript_text,
                    "created_at": time.time(),
                }
                extra_entries = []
                for ex in (result.metadata or {}).get("extra_outputs", []):
                    ex_name = ex.get("filename")
                    if ex_name:
                        extra_entries.append({
                            "filename": ex_name,
                            "titulo": f"{titulo} ({ex.get('aspect_ratio', '16:9')})",
                            "inicio_s": round(inicio_s, 2),
                            "fim_s": round(fim_s, 2),
                            "download_url": f"/api/v1/studio/video/{ex_name}",
                            "created_at": time.time(),
                        })
                with self._lock:
                    self.clips_generated.append(entry)
                    self.clips_generated.extend(extra_entries)
                if candidate_id and candidate_id in self.marker._candidates:
                    self.marker._candidates[candidate_id].status = CandidateStatus.CONCLUIDO
                return entry
            else:
                self.last_error = result.error or "Falha ao renderizar clipe ao vivo."
                return {"error": self.last_error}
        except Exception as exc:
            self.last_error = str(exc)
            return {"error": self.last_error}
        finally:
            try:
                window_mp4.unlink(missing_ok=True)
            except Exception:
                pass

    def mark_and_clip(
        self,
        pre_s: float = 25.0,
        post_s: float = 0.0,
        titulo: str = "Corte ao Vivo",
        render_opts: Optional[Dict[str, Any]] = None,
        background: bool = True,
    ) -> Dict[str, Any]:
        """Cria um corte instantâneo do buffer ou agenda marcação aguardando post_s."""
        self._scan_new_segments(include_last=False)
        now_t = self.clock.current_time()
        if len(self.buffer) == 0 or now_t <= 0.5:
            raise RuntimeError("Buffer ao vivo ainda não possui segmentos suficientes. Aguarde alguns segundos.")

        if post_s <= 0.0:
            inicio_s = max(0.0, now_t - max(2.0, float(pre_s)))
            fim_s = now_t
            if background:
                threading.Thread(
                    target=self._render_window_worker,
                    args=(inicio_s, fim_s, titulo, render_opts or {}, None),
                    daemon=True,
                ).start()
                return {
                    "mode": "instant",
                    "status": "rendering",
                    "inicio_s": round(inicio_s, 2),
                    "fim_s": round(fim_s, 2),
                }
            res = self._render_window_worker(inicio_s, fim_s, titulo, render_opts or {}, None)
            return {"mode": "instant", "status": "done" if "filename" in res else "error", **res}

        cand = self.marker.mark(
            timestamp=now_t,
            titulo=titulo,
            pre_s=max(2.0, float(pre_s)),
            post_s=float(post_s),
        )
        self._candidate_render_opts[cand.id] = dict(render_opts or {})
        return {
            "mode": "post_window",
            "candidate_id": cand.id,
            "status": cand.status.value,
            "inicio_s": cand.inicio_s,
            "fim_s": cand.fim_s,
        }

    def get_status(self) -> Dict[str, Any]:
        pending = [
            c.to_dict()
            for c in self.marker._candidates.values()
            if c.status == CandidateStatus.AGUARDANDO_POST_WINDOW
        ]
        with self._lock:
            clips = list(self.clips_generated)
        return {
            "active": self.running,
            "session_id": self.session_id,
            "url": self.url,
            "platform": self.platform,
            "running": self.running,
            "elapsed_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
            "segments_count": len(self.buffer),
            "buffer_duration_s": round(self.clock.current_time(), 1),
            "auto_clip": self.auto_clip,
            "auto_clip_keywords": list(self.auto_clip_keywords),
            "last_transcript_snippet": self.last_transcript_snippet,
            "clips_generated": len(clips),
            "recent_clips": clips[-10:],
            "pending_marks": len(pending),
            "last_error": self.last_error,
        }


class LiveManager:
    """Controlador singleton de sessão ao vivo do Studio."""

    def __init__(
        self,
        projects_root: Path,
        pipeline_runner: Any,
        transcribe_fn: Optional[Callable[[Path, str], Dict[str, Any]]] = None,
    ):
        self.projects_root = Path(projects_root)
        self.pipeline_runner = pipeline_runner
        self.transcribe_fn = transcribe_fn
        self._lock = threading.Lock()
        self.current_session: Optional[LiveClipSession] = None

    def start_session(
        self,
        url: str,
        segment_duration_s: float = 10.0,
        retention_seconds: float = 1800.0,
        auto_clip: bool = False,
        auto_clip_duration_s: float = 25.0,
        auto_clip_threshold_db: float = -12.0,
        auto_clip_keywords: Optional[List[str]] = None,
        default_render_opts: Optional[Dict[str, Any]] = None,
        stream_resolver: Optional[Callable[[str], str]] = None,
    ) -> LiveClipSession:
        with self._lock:
            if self.current_session and self.current_session.running:
                self.current_session.stop()

            session_id = f"live_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            session_dir = self.projects_root / "proj_default" / "live" / session_id
            session = LiveClipSession(
                session_id=session_id,
                url=url,
                session_dir=session_dir,
                pipeline_runner=self.pipeline_runner,
                segment_duration_s=segment_duration_s,
                retention_seconds=retention_seconds,
                auto_clip=auto_clip,
                auto_clip_duration_s=auto_clip_duration_s,
                auto_clip_threshold_db=auto_clip_threshold_db,
                auto_clip_keywords=auto_clip_keywords,
                default_render_opts=default_render_opts,
                stream_resolver=stream_resolver,
                transcribe_fn=self.transcribe_fn,
            )
            session.start()
            self.current_session = session
            return session

    def stop_session(self) -> Dict[str, Any]:
        with self._lock:
            if not self.current_session:
                return {"stopped": False, "active": False}
            self.current_session.stop()
            return {"stopped": True, **self.current_session.get_status()}

    def mark_clip(
        self,
        pre_s: float = 25.0,
        post_s: float = 0.0,
        titulo: str = "Corte ao Vivo",
        render_opts: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        session = self.current_session
        if not session:
            raise RuntimeError("Nenhuma transmissão ao vivo iniciada no momento.")
        return session.mark_and_clip(pre_s=pre_s, post_s=post_s, titulo=titulo, render_opts=render_opts)

    def get_status(self) -> Dict[str, Any]:
        session = self.current_session
        if not session:
            return {
                "active": False,
                "running": False,
                "segments_count": 0,
                "buffer_duration_s": 0.0,
                "clips_generated": 0,
                "recent_clips": [],
                "pending_marks": 0,
                "last_error": None,
            }
        return session.get_status()
