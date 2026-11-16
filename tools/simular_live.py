"""
CLI de Simulação de Live e Marcação de Clipes (M1 Fast Clipper).

Permite executar uma gravação como se fosse uma live em tempo real ou acelerada,
marcar momentos manuais com janela anterior/posterior, revisar e renderizar
clipes verticais 1080x1920 sem reprocessar o vídeo completo.
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastclipper.ingest.buffer import SegmentedBuffer
from fastclipper.ingest.clock import VirtualClock
from fastclipper.ingest.marker import CandidateStatus, ManualMarker
from fastclipper.ingest.source import LocalRecordingSource
from fastclipper.rendering.adapter import IsolatedRenderAdapter


def main():
    parser = argparse.ArgumentParser(description="Simulador de Live do Fast Clipper (M1)")
    parser.add_argument("--video", "-v", type=str, help="Caminho do arquivo de vídeo local")
    parser.add_argument("--speed", "-s", type=float, default=2.0, help="Velocidade da simulação (padrão: 2.0x)")
    parser.add_argument("--pre", type=float, default=2.0, help="Janela anterior em segundos (padrão: 2.0s)")
    parser.add_argument("--post", type=float, default=2.0, help="Janela posterior em segundos (padrão: 2.0s)")
    parser.add_argument("--mark-at", type=float, nargs="*", default=[3.0], help="Timestamps para simular marcação automática")
    args = parser.parse_args()

    caminho_video = Path(args.video) if args.video else ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    if not caminho_video.is_file():
        print(f"Vídeo de teste não encontrado: {caminho_video}")
        sys.exit(1)

    pasta_buffer = ROOT / "data" / "simulacao_buffer"
    pasta_saida = ROOT / "data" / "clipes_renderizados"

    print("=" * 60)
    print("FAST CLIPPER — SIMULAÇÃO DE LIVE & BUFFER SEGMENTADO (M1)")
    print("=" * 60)
    print(f"Vídeo de Origem: {caminho_video}")
    print(f"Velocidade: {args.speed}x | Janela Pre: {args.pre}s | Janela Post: {args.post}s")
    print(f"Buffer em: {pasta_buffer}")
    print(f"Clipes em: {pasta_saida}")
    print("-" * 60)

    clock = VirtualClock(speed=args.speed)
    buffer = SegmentedBuffer(buffer_dir=pasta_buffer, retention_seconds=120.0)
    source = LocalRecordingSource(
        video_path=caminho_video,
        buffer=buffer,
        clock=clock,
        segment_duration_s=2.0,
    )
    marker = ManualMarker(buffer=buffer, clock=clock, pre_window_s=args.pre, post_window_s=args.post)
    renderer = IsolatedRenderAdapter(output_dir=pasta_saida)

    marcas_agendadas = sorted(args.mark_at)
    candidatos_gerados = []

    source.start()
    print("Simulação iniciada.")

    try:
        while not source.is_finished() or any(c.status == CandidateStatus.AGUARDANDO_POST_WINDOW for c in candidatos_gerados):
            tempo_atual = clock.current_time()

            # Dispara marcas agendadas
            if marcas_agendadas and tempo_atual >= marcas_agendadas[0]:
                ts_marca = marcas_agendadas.pop(0)
                cand = marker.mark(timestamp=ts_marca, titulo=f"Momento em t={ts_marca:.1f}s")
                candidatos_gerados.append(cand)
                print(f"⏱️ [t={tempo_atual:.1f}s] MARCA MANUAL REGISTRADA: {cand.id} (Janela: [{cand.inicio_s}s - {cand.fim_s}s])")

            # Atualiza candidatos pendentes
            prontos = marker.check_pending_candidates(tempo_atual, stream_finished=source.is_finished())
            for c in prontos:
                print(f"✅ [t={tempo_atual:.1f}s] CANDIDATO PRONTO PARA REVISÃO: {c.id}")
                # Simula aprovação imediata do operador
                marker.approve(c.id)
                print(f"👍 [t={tempo_atual:.1f}s] Candidato {c.id} APROVADO pelo operador. Iniciando renderização 9:16...")

                res = renderer.render_candidate(
                    candidate=c,
                    buffer=buffer,
                    source_fallback_path=caminho_video,
                    require_audio=True,
                )

                if res.ok:
                    print(f"🎉 CLIPE PRONTO: {res.output_path} ({res.duration:.2f}s) [Renderizado em {res.metadata.get('render_time_s')}s]")
                else:
                    print(f"⚠️ CLIPE GEROU ALERTA: status={res.status.value} | Erros: {res.errors}")

            time.sleep(0.05)

    finally:
        source.stop()

    print("\n" + "=" * 60)
    print("SIMULAÇÃO CONCLUÍDA COM SUCESSO!")
    print(f"Total de segmentos no buffer: {len(buffer)}")
    print(f"Total de clipes processados: {len(candidatos_gerados)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
