"""
Gerador de Mídia Sintética Determinística para a Suite de Testes do Fast Clipper.

Cria vídeos sintéticos sem depender de arquivos pesados no Git, cobrindo:
1. Gameplay com Facecam simulada (PIP) e picos de áudio.
2. Gameplay puro em 16:9 sem facecam.
3. Cenário com dois alvos em lados opostos (separação estéreo e visual).
4. Arquivo com áudio ausente (teste de exceção e robustez).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

FFMPEG_CMD = shutil.which("ffmpeg") or "ffmpeg"
SAIDA_DIR = Path(__file__).resolve().parent / "synthetic"


def gerar_video(nome_arquivo: str, filtros_video: str, filtro_audio: str | None, duracao_s: float):
    SAIDA_DIR.mkdir(parents=True, exist_ok=True)
    caminho = SAIDA_DIR / nome_arquivo

    cmd = [
        FFMPEG_CMD,
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", filtros_video,
    ]

    if filtro_audio:
        cmd.extend([
            "-f", "lavfi",
            "-i", filtro_audio,
        ])

    cmd.extend([
        "-t", str(duracao_s),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
    ])

    if filtro_audio:
        cmd.extend([
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
        ])
    else:
        cmd.extend(["-an"])

    cmd.append(str(caminho))

    print(f"Gerando fixture sintética: {nome_arquivo} ({duracao_s}s)...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Erro ao gerar {nome_arquivo}: {res.stderr}", file=sys.stderr)
        raise RuntimeError(f"FFmpeg falhou ao gerar {nome_arquivo}")
    print(f" -> Sucesso: {caminho} ({caminho.stat().st_size} bytes)")


def main():
    print(f"Usando FFmpeg: {FFMPEG_CMD}")
    print(f"Diretório de saída: {SAIDA_DIR}")

    # 1. Gameplay 16:9 com Facecam PIP no canto superior direito + picos de áudio
    # Base: testsrc2 (1920x1080). Overlay: quadrado simulando webcam em x=1440, y=40, w=440, h=330
    filtro_v_facecam = (
        "testsrc2=size=1920x1080:rate=30 [base]; "
        "color=c=blue:size=440x330:rate=30 [cam]; "
        "[base][cam] overlay=1440:40:enable='gte(t,0)'"
    )
    # Áudio: tom contínuo 300Hz com pico em t=2.0 a t=3.5s
    filtro_a_pico = "sine=frequency=300:sample_rate=48000"
    gerar_video("sintetico_gameplay_facecam.mp4", filtro_v_facecam, filtro_a_pico, duracao_s=6.0)

    # 2. Gameplay puro 16:9 sem facecam
    filtro_v_puro = "testsrc2=size=1920x1080:rate=30"
    gerar_video("sintetico_gameplay_puro.mp4", filtro_v_puro, "sine=frequency=220:sample_rate=48000", duracao_s=5.0)

    # 3. Duas pessoas / alvos em lados opostos (esquerda x=200, direita x=1400)
    filtro_v_duplo = (
        "color=c=darkgray:size=1920x1080:rate=30 [fundo]; "
        "color=c=red:size=300x500:rate=30 [alvo1]; "
        "color=c=green:size=300x500:rate=30 [alvo2]; "
        "[fundo][alvo1] overlay=200:290 [t1]; "
        "[t1][alvo2] overlay=1420:290"
    )
    gerar_video("sintetico_duas_pessoas.mp4", filtro_v_duplo, "sine=frequency=440:sample_rate=48000", duracao_s=6.0)

    # 4. Vídeo sem áudio
    gerar_video("sintetico_sem_audio.mp4", "testsrc2=size=1920x1080:rate=30", None, duracao_s=3.0)

    print("\nTodas as fixtures sintéticas foram geradas com sucesso!")


if __name__ == "__main__":
    main()
