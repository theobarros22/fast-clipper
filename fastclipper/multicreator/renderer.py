"""
Renderizador de sequências comparativas verticais (9:16) entre múltiplos criadores.

Garante roteamento estrito de áudio (uma fonte de voz ativa por vez, sem sobreposição),
exibição do nome de cada criador em banner visual e validação rigorosa por FFprobe.
"""

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastclipper.multicreator.models import (
    ComparisonMode,
    CreatorReactionTrack,
    CrossCreatorEvent,
)
from fastclipper.multicreator.sync import EventSyncManager, MissingAuthorizationError

logger = logging.getLogger(__name__)


class ComparisonRenderer:
    """Motor de renderização de comparações de reações em 1080x1920."""

    def __init__(
        self,
        output_dir: str | Path,
        ffmpeg_bin: Optional[str] = None,
        ffprobe_bin: Optional[str] = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_bin = ffprobe_bin or shutil.which("ffprobe") or "ffprobe"
        from fastclipper.rendering.encoder import HardwareEncoderManager
        self.encoder_manager = HardwareEncoderManager(ffmpeg_bin=self.ffmpeg_bin)

    def render_event_comparison(
        self,
        event: CrossCreatorEvent,
        mode: ComparisonMode = ComparisonMode.SEQUENTIAL,
        output_filename: Optional[str] = None,
        audio_source_track_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Renderiza a sequência comparativa completa do evento em 9:16."""
        # 1. Validação estrita de autorização
        sync_mgr = EventSyncManager()
        sync_mgr._events[event.event_id] = event
        validacao_prontidao = sync_mgr.validate_event_readiness(event.event_id)

        nome_saida = output_filename or f"comparacao_{event.event_id}_{mode.value}_9x16.mp4"
        caminho_saida = self.output_dir / nome_saida

        t0 = time.time()

        if mode == ComparisonMode.SEQUENTIAL:
            sucesso = self._render_sequential(event, caminho_saida)
        elif mode == ComparisonMode.SPLIT_VERTICAL:
            sucesso = self._render_split_vertical(event, caminho_saida, audio_source_track_id)
        else:
            raise ValueError(f"Modo de comparação não suportado: {mode}")

        if not sucesso or not caminho_saida.is_file():
            return {
                "ok": False,
                "error": "Falha na execução do processo de renderização no FFmpeg.",
                "output_path": None,
            }

        tempo_render = round(time.time() - t0, 3)

        # 2. Validação estrita via FFprobe e decodificação total
        validacao = self._validar_ffprobe(caminho_saida)

        return {
            "ok": validacao["valid"],
            "output_path": str(caminho_saida),
            "duration": validacao.get("duration", 0.0),
            "render_time_s": tempo_render,
            "mode": mode.value,
            "tracks_included": [t.creator_name for t in event.tracks],
            "spoilers_warning": validacao_prontidao["spoilers_detected"],
            "errors": validacao.get("errors", []),
        }

    def _render_sequential(self, event: CrossCreatorEvent, output_path: Path) -> bool:
        """Renderiza cada reação em sequência (uma após a outra, áudio de cada respectivo criador)."""
        temp_dir = output_path.parent / f"_temp_seq_{event.event_id}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        trechos_gerados = []

        try:
            for idx, track in enumerate(event.tracks):
                trecho_path = temp_dir / f"clip_{idx:02d}_{track.track_id}.mp4"
                
                # Gera arquivo SRT temporário para evitar dependência de Fontconfig no Windows
                sub_path = temp_dir / f"label_{idx}.srt"
                sub_path.write_text(
                    f"1\n00:00:00,000 --> 00:01:00,000\nREACAO: {track.creator_name}\n",
                    encoding="utf-8",
                )
                sub_escapado = sub_path.as_posix().replace(":", r"\:")

                filtro = (
                    f"scale=1080:1920:force_original_aspect_ratio=increase,"
                    f"crop=1080:1920,"
                    f"drawbox=y=60:color=black@0.6:width=iw:height=90:t=fill,"
                    f"subtitles='{sub_escapado}'"
                )

                enc = self.encoder_manager.get_encoder()
                enc_args = self.encoder_manager.get_encoder_args(enc, width=1080, height=1920)

                cmd = [
                    self.ffmpeg_bin,
                    "-loglevel", "error",
                    "-y",
                    "-ss", str(track.start_s),
                    "-t", str(track.duration_s),
                    "-i", str(track.video_path),
                    "-vf", filtro,
                ] + enc_args + [
                    "-c:a", "aac",
                    "-b:a", "128k",
                    str(trecho_path),
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if res.returncode != 0 or not trecho_path.is_file():
                    logger.error("Falha ao gerar trecho %s: %s", track.track_id, res.stderr)
                    return False
                trechos_gerados.append(trecho_path)

            # Concatenação sequencial dos trechos
            concat_txt = temp_dir / "concat_list.txt"
            linhas = [f"file '{p.resolve().as_posix()}'" for p in trechos_gerados]
            concat_txt.write_text("\n".join(linhas), encoding="utf-8")

            cmd_concat = [
                self.ffmpeg_bin,
                "-loglevel", "error",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_txt),
                "-c", "copy",
                str(output_path),
            ]
            res_concat = subprocess.run(cmd_concat, capture_output=True, text=True, timeout=300)
            return res_concat.returncode == 0
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _render_split_vertical(
        self,
        event: CrossCreatorEvent,
        output_path: Path,
        audio_source_track_id: Optional[str] = None,
    ) -> bool:
        """Renderiza duas perspectivas empilhadas (1080x960 cada) com áudio de apenas UMA fonte ativa."""
        if len(event.tracks) < 2:
            # Fallback para sequencial se houver apenas 1 faixa
            return self._render_sequential(event, output_path)

        track1 = event.tracks[0]
        track2 = event.tracks[1]
        duracao = min(track1.duration_s, track2.duration_s)

        # Determina qual áudio usar para evitar cacofonia
        audio_track_idx = 0
        if audio_source_track_id and audio_source_track_id == track2.track_id:
            audio_track_idx = 1

        # Gera legenda SRT para identificação no topo/base sem dependência de Fontconfig
        sub_path = output_path.with_suffix(".split.srt")
        try:
            sub_path.write_text(
                f"1\n00:00:00,000 --> 00:01:00,000\n{track1.creator_name}  |  {track2.creator_name}\n",
                encoding="utf-8",
            )
            sub_escapado = sub_path.as_posix().replace(":", r"\:")

            # Painel superior (Track 1) e Painel inferior (Track 2)
            filtro_complexo = (
                f"[0:v] scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,"
                f"drawbox=y=30:color=blue@0.6:width=iw:height=70:t=fill [top]; "
                f"[1:v] scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,"
                f"drawbox=y=30:color=green@0.6:width=iw:height=70:t=fill [bot]; "
                f"[top][bot] vstack=inputs=2,subtitles='{sub_escapado}' [outv]"
            )

            enc = self.encoder_manager.get_encoder()
            enc_args = self.encoder_manager.get_encoder_args(enc, width=1080, height=1920)

            cmd = [
                self.ffmpeg_bin,
                "-loglevel", "error",
                "-y",
                "-ss", str(track1.start_s),
                "-t", str(duracao),
                "-i", str(track1.video_path),
                "-ss", str(track2.start_s),
                "-t", str(duracao),
                "-i", str(track2.video_path),
                "-filter_complex", filtro_complexo,
                "-map", "[outv]",
                "-map", f"{audio_track_idx}:a",  # ÁUDIO ESTRITAMENTE DE UMA FONTE ATIVA
            ] + enc_args + [
                "-c:a", "aac",
                "-b:a", "128k",
                str(output_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                logger.error("Falha split vertical: %s", res.stderr)
                return False
            return True
        finally:
            if sub_path.exists():
                try:
                    sub_path.unlink()
                except OSError:
                    pass

    def _validar_ffprobe(self, caminho_arquivo: Path) -> Dict[str, Any]:
        erros: List[str] = []
        if not caminho_arquivo.is_file() or caminho_arquivo.stat().st_size == 0:
            return {"valid": False, "errors": ["Arquivo gerado não existe ou está vazio."]}

        cmd = [
            self.ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json",
            str(caminho_arquivo),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return {"valid": False, "errors": [f"FFprobe falhou: {res.stderr}"]}

        dados = json.loads(res.stdout)
        duracao = float(dados.get("format", {}).get("duration", 0.0))
        streams = dados.get("streams", [])

        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)

        if not v:
            erros.append("Vídeo ausente no arquivo comparativo.")
        elif int(v.get("width", 0)) != 1080 or int(v.get("height", 0)) != 1920:
            erros.append(f"Resolução incorreta: {v.get('width')}x{v.get('height')}.")

        if not a:
            erros.append("Áudio ausente no arquivo comparativo.")

        # Teste de decodificação completa
        decode_cmd = [self.ffmpeg_bin, "-v", "error", "-i", str(caminho_arquivo), "-f", "null", "-"]
        res_decode = subprocess.run(decode_cmd, capture_output=True, text=True, timeout=180)
        if res_decode.returncode != 0:
            erros.append(f"Decodificação falhou: {res_decode.stderr.strip()}")

        return {
            "valid": len(erros) == 0,
            "duration": duracao,
            "errors": erros,
        }
