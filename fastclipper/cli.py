"""
Interface de Linha de Comando (CLI) Unificada do Fast Clipper.

Permite operar todos os componentes da plataforma:
  - status: Diagnóstico de ambiente, GPU, FFmpeg e fixtures (M0)
  - simulate: Simulação de live e marcação com buffer rolante (M1/M2)
  - analyze: Análise multimodal de sinais explicáveis (M4)
  - collab: Colaboração multiusuário, bloqueio otimista e passagem de turno (M3)
  - compare: Comparação e sincronização multi-criador Cross-POV (M5)
  - queue: Gerenciamento e execução de workers da fila durável (M6)
  - vault: Gestão segura de tokens sociais, autopost e kill switch (M6)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastclipper.collaboration.models import AuthContext, PapelUsuario, EstadoMomento
from fastclipper.collaboration.service import CollaborationService
from fastclipper.ingest.buffer import SegmentedBuffer
from fastclipper.ingest.clock import VirtualClock
from fastclipper.ingest.marker import CandidateStatus, ManualMarker
from fastclipper.ingest.source import LocalRecordingSource
from fastclipper.layouts.config import ChannelLayoutConfig, LayoutMode, Rectangle
from fastclipper.multicreator.models import ComparisonMode, CreatorReactionTrack, CrossCreatorEvent
from fastclipper.multicreator.renderer import ComparisonRenderer
from fastclipper.multicreator.sync import EventSyncManager
from fastclipper.queue.durable_queue import DurableTaskQueue, JobStatus
from fastclipper.rendering.adapter import IsolatedRenderAdapter
from fastclipper.signals.aggregator import MultimodalSignalAggregator
from fastclipper.signals.audio_analyzer import AudioPeakDetector
from fastclipper.signals.chat_analyzer import ChatSpikeDetector
from fastclipper.signals.models import GamingEditorialProfile, SignalWeights
from fastclipper.signals.transcription_indexer import TranscriptionIndexer
from fastclipper.social.oauth_vault import OAuthTokenVault, SocialPlatform


def cmd_status(args: argparse.Namespace) -> int:
    """Verifica saúde do ambiente, dependências de sistema, GPU e fixtures."""
    print("=" * 65)
    print("FAST CLIPPER — STATUS E DIAGNÓSTICO DO SISTEMA (M0)")
    print("=" * 65)

    # 1. FFmpeg & FFprobe
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    print(f"[FFmpeg] : {'OK -> ' + ffmpeg_bin if ffmpeg_bin else 'ERRO: Não encontrado no PATH'}")
    print(f"[FFprobe]: {'OK -> ' + ffprobe_bin if ffprobe_bin else 'ERRO: Não encontrado no PATH'}")

    if ffmpeg_bin:
        try:
            ver = subprocess.run([ffmpeg_bin, "-version"], capture_output=True, text=True)
            linha_ver = ver.stdout.splitlines()[0] if ver.stdout else "Desconhecido"
            print(f"         {linha_ver}")
        except Exception as e:
            print(f"         Falha ao obter versão: {e}")

    # 2. Python e PyTorch / GPU
    print(f"[Python] : {sys.version.split()[0]} ({sys.executable})")
    try:
        import torch

        cuda_ok = torch.cuda.is_available()
        gpu_name = torch.cuda.get_device_name(0) if cuda_ok else "N/A"
        cuda_ver = torch.version.cuda if cuda_ok else "N/A"
        print(f"[PyTorch]: {torch.__version__}")
        print(f"[CUDA]   : {'Ativo -> ' + gpu_name + ' (CUDA ' + cuda_ver + ')' if cuda_ok else 'Indisponível (Modo CPU)'}")
    except ImportError:
        print("[PyTorch]: Módulo torch não instalado neste venv")

    # 3. Fixtures Sintéticas
    dir_fixtures = ROOT / "tests" / "fixtures" / "synthetic"
    fixtures = list(dir_fixtures.glob("*.mp4")) if dir_fixtures.exists() else []
    print(f"[Fixtures]: {len(fixtures)} mídias sintéticas prontas em tests/fixtures/synthetic")
    for f in fixtures:
        tamanho_mb = f.stat().st_size / (1024 * 1024)
        print(f"         - {f.name} ({tamanho_mb:.2f} MB)")

    print("=" * 65)
    return 0


def cmd_precheck(args: argparse.Namespace) -> int:
    """Executa diagnóstico preliminar de mídia antes de computação pesada (Media Pre-check)."""
    from fastclipper.ingest.precheck import MediaPrechecker

    video_path = Path(args.video) if args.video else ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    print("=" * 65)
    print("FAST CLIPPER — MEDIA PRE-CHECK GUARD (INSPIRADO EM AUTOCLIP)")
    print("=" * 65)
    print(f"Arquivo Alvo: {video_path}")
    print("-" * 65)

    checker = MediaPrechecker(min_duration_s=args.min_duration)
    res = checker.probe(video_path, require_audio=not args.no_audio_ok)

    print(f"Status Validação : {'[APROVADO]' if res.is_valid else '[REJEITADO]'}")
    print(f"Código Diagnóstico: {res.status.value}")
    print(f"Duração          : {res.duration_s:.2f}s" if res.duration_s else "Duração          : Indisponível")
    print(f"Vídeo Presente   : {res.has_video} ({res.width}x{res.height} @ {res.fps} fps)" if res.has_video else "Vídeo Presente   : Não")
    print(f"Áudio Presente   : {res.has_audio}")
    print(f"Mensagem         : {res.message}")
    print("=" * 65)

    return 0 if res.is_valid else 1


def cmd_simulate(args: argparse.Namespace) -> int:
    """Executa simulação de live com buffer rolante e renderização 9:16."""
    caminho_video = Path(args.video) if args.video else ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    if not caminho_video.is_file():
        print(f"Erro: Arquivo de vídeo não encontrado: {caminho_video}")
        return 1

    pasta_buffer = Path(args.buffer_dir) if args.buffer_dir else ROOT / "data" / "simulacao_buffer"
    pasta_saida = Path(args.output_dir) if args.output_dir else ROOT / "data" / "clipes_renderizados"

    print("=" * 65)
    print("FAST CLIPPER — SIMULADOR DE LIVE & BUFFER (M1/M2)")
    print("=" * 65)
    print(f"Origem   : {caminho_video.name}")
    print(f"Velocidade: {args.speed}x | Janela: [-{args.pre}s, +{args.post}s]")
    print(f"Layout   : {args.layout}")
    print("-" * 65)

    clock = VirtualClock(speed=args.speed)
    buffer = SegmentedBuffer(buffer_dir=pasta_buffer, retention_seconds=180.0)
    source = LocalRecordingSource(
        video_path=caminho_video,
        buffer=buffer,
        clock=clock,
        segment_duration_s=2.0,
    )
    marker = ManualMarker(buffer=buffer, clock=clock, pre_window_s=args.pre, post_window_s=args.post)
    renderer = IsolatedRenderAdapter(output_dir=pasta_saida)

    # Configuração de layout
    layout_cfg = None
    if args.layout == "stacked":
        layout_cfg = ChannelLayoutConfig(
            canal_id="simulacao_canal",
            versao=1,
            mode=LayoutMode.STACKED,
            facecam_box=Rectangle(1400, 40, 480, 360),
            gameplay_box=Rectangle(0, 0, 1920, 1080),
        )

    marcas_pendentes = sorted(args.mark_at) if args.mark_at else [3.0]
    candidatos = []

    source.start()
    print("Simulação em andamento...")

    try:
        while not source.is_finished() or any(c.status == CandidateStatus.AGUARDANDO_POST_WINDOW for c in candidatos):
            t = clock.current_time()

            if marcas_pendentes and t >= marcas_pendentes[0]:
                ts = marcas_pendentes.pop(0)
                cand = marker.mark(timestamp=ts, titulo=f"Momento em {ts:.1f}s")
                candidatos.append(cand)
                print(f"[t={t:.1f}s] Marca registrada: {cand.id} (janela: {cand.inicio_s:.1f}s - {cand.fim_s:.1f}s)")

            prontos = marker.check_pending_candidates(t, stream_finished=source.is_finished())
            for c in prontos:
                print(f"[t={t:.1f}s] Candidato pronto para revisão: {c.id}")
                marker.approve(c.id)
                print(f"[t={t:.1f}s] Aprovado. Renderizando clipe 9:16...")

                res = renderer.render_candidate(
                    candidate=c,
                    buffer=buffer,
                    source_fallback_path=caminho_video,
                    layout_config=layout_cfg,
                    require_audio=True,
                )

                if res.ok:
                    print(f"🎉 SUCESSO: Clipe gerado em {res.output_path} ({res.duration:.1f}s)")
                else:
                    print(f"⚠️ AVISO: Status {res.status.value} - Erros: {res.errors}")

            time.sleep(0.05)

    finally:
        source.stop()
        buffer.cleanup_all()

    print("Simulação concluída com sucesso.")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Executa análise multimodal sobre sinais de áudio, chat e transcrição."""
    print("=" * 65)
    print("FAST CLIPPER — ANÁLISE MULTIMODAL EXPLICÁVEL (M4)")
    print("=" * 65)

    video_path = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    audio_detector = AudioPeakDetector(zscore_threshold=args.audio_threshold)
    chat_detector = ChatSpikeDetector(zscore_threshold=args.chat_threshold)
    profile = GamingEditorialProfile()
    indexer = TranscriptionIndexer(profile=profile)

    # 1. Áudio da fixture sintética
    audio_events = []
    if video_path.is_file():
        try:
            audio_events = audio_detector.analyze_audio(video_path=video_path, start_s=0.0, duration_s=10.0)
        except Exception as e:
            print(f"Aviso ao analisar áudio: {e}")

    # Fallback sintético se áudio for senoidal estático
    if not audio_events:
        from fastclipper.signals.models import SignalEvent, SignalType
        audio_events = [
            SignalEvent(
                signal_type=SignalType.AUDIO_PEAK,
                timestamp_s=20.0,
                intensity=0.88,
                label="pico_audio",
                metadata={"zscore": 3.4, "rms": 1420.0},
            )
        ]

    # 2. Chat spike em t=20s sobre linha de base de 60s
    chat_messages = [
        {"timestamp_s": float(i) * 2.0, "user": f"user_{i}", "message": "gg"} for i in range(30)
    ] + [
        {"timestamp_s": 20.1, "user": "mod", "message": "MEU DEUS DO CEU"},
        {"timestamp_s": 20.2, "user": "fan1", "message": "KKKKKKKKKKKK"},
        {"timestamp_s": 20.3, "user": "fan2", "message": "CAPOTOU O CARRO"},
        {"timestamp_s": 20.4, "user": "fan3", "message": "CLIPAAA"},
        {"timestamp_s": 20.5, "user": "fan4", "message": "SOCORRO"},
        {"timestamp_s": 20.6, "user": "fan5", "message": "QUE TIRO FOI ESSE"},
        {"timestamp_s": 20.7, "user": "fan6", "message": "OMGGGG"},
    ]
    chat_events = chat_detector.analyze_chat(chat_messages)

    # 3. Transcrição com reação e evento do jogo em t=20s
    asr_segments = [
        {"start": 18.5, "end": 22.0, "text": "olha a polícia, ele capotou o carro meu deus kkkkk!"}
    ]
    asr_events = indexer.index_transcription(asr_segments)

    all_signals = audio_events + chat_events + asr_events

    aggregator = MultimodalSignalAggregator(
        weights=SignalWeights(audio_weight=0.35, chat_weight=0.35, transcription_weight=0.30),
        cluster_window_s=4.0,
    )

    candidatos = aggregator.aggregate_signals(all_signals)

    print(f"Total de sinais brutos detectados: {len(all_signals)}")
    print(f"Candidatos consolidados gerados: {len(candidatos)}\n")

    for i, c in enumerate(candidatos, 1):
        print(f"--- CANDIDATO #{i}: {c.id} ---")
        print(f"Janela Sugerida : [{c.start_s:.1f}s -> {c.end_s:.1f}s] (Duração: {c.end_s - c.start_s:.1f}s)")
        print(f"Score Composto  : {c.score:.2f} (Categoria: {c.category})")
        print(f"Explicação      : {c.explanation_text}")
        print("Sinais          :")
        for sig_name, sig_val in c.signals_breakdown.items():
            print(f"   * {sig_name}: {sig_val}")
        print()

    return 0


def cmd_collab(args: argparse.Namespace) -> int:
    """Gerencia estados de momentos, bloqueio otimista e passagens de turno."""
    print("=" * 65)
    print("FAST CLIPPER — COLABORAÇÃO E PASSAGEM DE TURNO (M3)")
    print("=" * 65)

    svc = CollaborationService()
    org_id = args.org or "org_alpha"
    user_id = args.user or "editor_1"

    svc.register_organization(org_id=org_id, nome="Organização Teste", slug=org_id)
    svc.register_user(user_id=user_id, org_id=org_id, nome="Editor Teste", email="editor@example.com", papel=PapelUsuario.EDITOR)

    ctx = AuthContext(
        usuario_id=user_id,
        organizacao_id=org_id,
        papel=PapelUsuario.EDITOR,
    )

    canal = svc.register_channel(
        auth=ctx,
        canal_id="canal_gta_v",
        nome="Canal GTA V Oficial",
        plataforma="youtube",
        url="https://youtube.com/live/exemplo",
        autorizacao_ref="AUTH-CANAL-2026-001",
    )

    fonte = svc.register_source(
        auth=ctx,
        fonte_id="fonte_gta_01",
        canal_id=canal.id,
        titulo="Gravação GTA V Live",
        tipo="buffer_rolante",
        caminho_buffer="/data/buffer",
        termo_autorizacao="TERMO-AUTH-2026-001",
    )

    if args.subcmd == "create":
        ts = args.ts or 120.0
        m = svc.create_moment(
            auth=ctx,
            fonte_id=fonte.id,
            inicio_s=ts - 10.0,
            fim_s=ts + 15.0,
            timestamp_fonte=ts,
            descricao=args.titulo or "Momento de Teste",
        )
        print(f"Momento criado com sucesso: ID={m.id} | Versão={m.versao} | Estado={m.estado.value}")

    elif args.subcmd == "handoff":
        agora = time.time()
        inicio_p = agora - (args.hours or 8) * 3600
        rel = svc.generate_shift_handoff(
            auth=ctx,
            periodo_inicio=inicio_p,
            periodo_fim=agora,
            notas_operacionais="Troca de turno padrão. Sem incidentes de segurança.",
        )
        from dataclasses import asdict
        print(json.dumps(asdict(rel), indent=2, ensure_ascii=False))

    else:
        print("Subcomando de colaboração não reconhecido. Use 'create' ou 'handoff'.")
        return 1

    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Renderiza vídeo comparativo 9:16 entre múltiplos criadores sincronizados."""
    print("=" * 65)
    print("FAST CLIPPER — COMPARAÇÃO ENTRE CRIADORES CROSS-POV (M5)")
    print("=" * 65)

    p1 = Path(args.video1) if args.video1 else ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    p2 = Path(args.video2) if args.video2 else ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_puro.mp4"
    out_dir = Path(args.output_dir) if args.output_dir else ROOT / "data" / "clipes_comparativos"

    sync_mgr = EventSyncManager()
    ev = sync_mgr.create_event(
        event_id="evento_policia_gta",
        title="Perseguição GTA V",
        description="Comparação de reações",
    )
    ev.comparison_mode = ComparisonMode.SPLIT_VERTICAL if args.mode == "split" else ComparisonMode.SEQUENTIAL

    track1 = CreatorReactionTrack(
        track_id="tr_alfa",
        event_id=ev.event_id,
        creator_name="Streamer Alfa",
        video_path=str(p1),
        stream_offset_s=0.0,
        start_s=args.start or 0.5,
        end_s=args.end or 3.0,
        has_authorization=not args.unauthorized,
        authorization_ref="TERMO-AUTH-ALFA",
    )
    track2 = CreatorReactionTrack(
        track_id="tr_beta",
        event_id=ev.event_id,
        creator_name="Streamer Beta",
        video_path=str(p2),
        stream_offset_s=args.offset or 0.0,
        start_s=args.start or 0.5,
        end_s=args.end or 3.0,
        has_authorization=True,
        authorization_ref="TERMO-AUTH-BETA",
    )

    sync_mgr.add_track(ev.event_id, track1)
    sync_mgr.add_track(ev.event_id, track2)

    renderer = ComparisonRenderer(output_dir=out_dir)

    print(f"Streamer 1 : {track1.creator_name} ({p1.name}) [Autorizado={track1.has_authorization}]")
    print(f"Streamer 2 : {track2.creator_name} ({p2.name}) [Offset={track2.stream_offset_s}s]")
    print(f"Modo       : {ev.comparison_mode.value}")
    print("-" * 65)

    try:
        res = renderer.render_event_comparison(event=ev)
        if res.get("ok"):
            print(f"🎉 SUCESSO: Vídeo comparativo gerado em {res.get('output_path')} ({res.get('duration', 0):.2f}s)")
        else:
            print(f"⚠️ AVISO: Erros na renderização: {res.get('errors') or res.get('error')}")
    except Exception as e:
        print(f"❌ ERRO na comparação: {e}")
        return 1

    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    """Gerencia tarefas na fila durável com tolerância a falhas."""
    print("=" * 65)
    print("FAST CLIPPER — FILA DURÁVEL E RECUPERAÇÃO DE TRABALHOS (M6)")
    print("=" * 65)

    queue = DurableTaskQueue()

    if args.subcmd == "enqueue":
        job = queue.enqueue(
            org_id=args.org or "org_alpha",
            job_type="render_clip_9x16",
            params={"clipe_id": "clip_101", "duracao": 30.0},
            idempotency_key=args.key or f"task_{int(time.time())}",
        )
        print(f"Tarefa enfileirada: ID={job.job_id} | Status={job.status.value} | Key={job.idempotency_key}")

    elif args.subcmd == "worker":
        print(f"Iniciando Worker ID: {args.worker_id or 'worker-01'}...")
        leased = queue.lease_next_job(worker_id=args.worker_id or "worker-01", lease_duration_s=15.0)
        if leased:
            print(f"Tarefa adquirida: ID={leased.job_id} | Tipo={leased.job_type} | Org={leased.org_id}")
            queue.complete_job(leased.job_id, result={"output": "clipe_finalizado.mp4"})
            print(f"Tarefa {leased.job_id} concluída com sucesso.")
        else:
            print("Nenhuma tarefa pendente na fila.")

    return 0


def cmd_vault(args: argparse.Namespace) -> int:
    """Gerencia cofre de autenticação OAuth2 e chave de emergência (Kill Switch)."""
    print("=" * 65)
    print("FAST CLIPPER — COFRE OAUTH2 E CONTROLE SOCIAL (M6)")
    print("=" * 65)

    vault = OAuthTokenVault(secret_key="super_secret_encryption_key_32b!")
    org_id = args.org or "org_alpha"

    if args.subcmd == "store":
        acc = vault.register_account(
            org_id=org_id,
            platform=SocialPlatform.YOUTUBE_SHORTS,
            account_name="Canal YouTube Principal",
            access_token="ya29.fake_token_for_demonstration",
            scopes=["https://www.googleapis.com/auth/youtube.upload"],
            autopost_enabled=args.autopost,
        )
        print(f"Conta OAuth registrada com sucesso: ID={acc.account_id} | Org={org_id} (Autopost: {args.autopost})")

    elif args.subcmd == "kill-switch":
        vault.set_emergency_kill_switch(org_id=org_id, active=True)
        print(f"🚨 KILL SWITCH ATIVADO: Todas as publicações para {org_id} estão bloqueadas!")

    elif args.subcmd == "reset-kill-switch":
        vault.set_emergency_kill_switch(org_id=org_id, active=False)
        print(f"✅ KILL SWITCH DESATIVADO: Operações normais retomadas para {org_id}.")

    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Inicia o servidor de API FastAPI e Control Plane Web."""
    import uvicorn
    print("=" * 65)
    print("FAST CLIPPER — CONTROL PLANE & REST API")
    print("=" * 65)
    print(f"Endereço: http://{args.host}:{args.port}")
    print(f"Documentação Swagger: http://{args.host}:{args.port}/docs")
    print("=" * 65)
    uvicorn.run("fastclipper.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="fastclipper",
        description="Fast Clipper — Plataforma de Clipping Inteligente e Colaborativo",
    )
    subparsers = parser.add_subparsers(dest="command", help="Comandos disponíveis")

    # status
    p_status = subparsers.add_parser("status", help="Diagnóstico do sistema, GPU e dependências (M0)")
    p_status.set_defaults(func=cmd_status)

    # precheck
    p_pre = subparsers.add_parser("precheck", help="Sonda preliminar estrita de mídia antes de computação (AutoClip)")
    p_pre.add_argument("--video", "-v", type=str, help="Caminho do arquivo de vídeo a ser verificado")
    p_pre.add_argument("--min-duration", type=float, default=5.0, help="Duração mínima em segundos")
    p_pre.add_argument("--no-audio-ok", action="store_true", help="Permitir mídia sem áudio")
    p_pre.set_defaults(func=cmd_precheck)

    # simulate
    p_sim = subparsers.add_parser("simulate", help="Simulação de live e marcação de buffer (M1/M2)")
    p_sim.add_argument("--video", "-v", type=str, help="Caminho do arquivo de vídeo local")
    p_sim.add_argument("--speed", "-s", type=float, default=2.0, help="Velocidade da simulação (padrão: 2.0x)")
    p_sim.add_argument("--pre", type=float, default=2.0, help="Janela anterior em segundos (padrão: 2.0s)")
    p_sim.add_argument("--post", type=float, default=2.0, help="Janela posterior em segundos (padrão: 2.0s)")
    p_sim.add_argument("--layout", choices=["solo", "stacked"], default="solo", help="Layout 9:16 de saída")
    p_sim.add_argument("--mark-at", type=float, nargs="*", default=[3.0], help="Timestamps de marcas de teste")
    p_sim.add_argument("--buffer-dir", type=str, help="Diretório temporário do buffer")
    p_sim.add_argument("--output-dir", type=str, help="Diretório de saída de clipes")
    p_sim.set_defaults(func=cmd_simulate)

    # analyze
    p_ana = subparsers.add_parser("analyze", help="Análise de sinais multimodais explicáveis (M4)")
    p_ana.add_argument("--audio-threshold", type=float, default=2.5, help="Z-score de pico de áudio")
    p_ana.add_argument("--chat-threshold", type=float, default=2.0, help="Z-score de spike de chat")
    p_ana.set_defaults(func=cmd_analyze)

    # collab
    p_col = subparsers.add_parser("collab", help="Colaboração multiusuário e passagem de turno (M3)")
    p_col.add_argument("subcmd", choices=["create", "handoff"], help="Ação de colaboração")
    p_col.add_argument("--org", type=str, default="org_alpha", help="ID da organização")
    p_col.add_argument("--user", type=str, default="editor_1", help="ID do usuário")
    p_col.add_argument("--ts", type=float, help="Timestamp da fonte")
    p_col.add_argument("--titulo", type=str, help="Título do momento")
    p_col.add_argument("--hours", type=int, default=8, help="Horas do período de passagem de turno")
    p_col.set_defaults(func=cmd_collab)

    # compare
    p_cmp = subparsers.add_parser("compare", help="Comparação e sincronização multi-criador Cross-POV (M5)")
    p_cmp.add_argument("--video1", type=str, help="Vídeo do criador 1")
    p_cmp.add_argument("--video2", type=str, help="Vídeo do criador 2")
    p_cmp.add_argument("--mode", choices=["split", "seq"], default="split", help="Modo de comparação 9:16")
    p_cmp.add_argument("--start", type=float, default=1.0, help="Início em segundos")
    p_cmp.add_argument("--end", type=float, default=4.0, help="Fim em segundos")
    p_cmp.add_argument("--offset", type=float, default=0.0, help="Offset de sincronização do criador 2")
    p_cmp.add_argument("--unauthorized", action="store_true", help="Simular criador sem autorização")
    p_cmp.add_argument("--output-dir", type=str, help="Diretório de saída")
    p_cmp.set_defaults(func=cmd_compare)

    # queue
    p_q = subparsers.add_parser("queue", help="Fila durável e gerenciamento de tarefas (M6)")
    p_q.add_argument("subcmd", choices=["enqueue", "worker"], help="Ação da fila")
    p_q.add_argument("--org", type=str, default="org_alpha", help="ID da organização")
    p_q.add_argument("--key", type=str, help="Chave de idempotência")
    p_q.add_argument("--worker-id", type=str, default="worker-01", help="ID do worker")
    p_q.add_argument("--db", type=str, default=":memory:", help="Caminho do banco SQLite da fila")
    p_q.set_defaults(func=cmd_queue)

    # vault
    p_v = subparsers.add_parser("vault", help="Cofre OAuth2, autopost e kill switch (M6)")
    p_v.add_argument("subcmd", choices=["store", "kill-switch", "reset-kill-switch"], help="Ação do cofre")
    p_v.add_argument("--org", type=str, default="org_alpha", help="ID da organização")
    p_v.add_argument("--autopost", action="store_true", help="Habilitar autopost na conta")
    p_v.set_defaults(func=cmd_vault)

    # serve
    p_serve = subparsers.add_parser("serve", help="Inicia servidor FastAPI e Dashboard Web")
    p_serve.add_argument("--host", type=str, default="127.0.0.1", help="Endereço de bind (padrão: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000, help="Porta HTTP (padrão: 8000)")
    p_serve.add_argument("--reload", action="store_true", help="Habilitar autoreload")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    if hasattr(args, "func"):
        return args.func(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
