"""
Suite de Testes Automatizados para o Marco M1 (Gravação Simulando Live).

Cobre:
1. Controle do Relógio Virtual (avanço, pausa, retomada, seek).
2. Marcação no início do vídeo (pre_window truncada segura sem valores negativos).
3. Janela futura ainda indisponível (espera pelo término de post_window_s).
4. Buffer sem espaço e proteção de retenção de segmentos vinculados.
5. Reinício e retomada do buffer a partir do manifesto persistido.
6. Ajuste de início/fim e ciclo de aprovação/rejeição.
7. Arquivo com áudio ausente gerando 'requer_revisao' (nunca sucesso silencioso).
8. Renderização completa de clipe 1080x1920 com áudio sincronizado e decodificação total.
"""

import shutil
import tempfile
from pathlib import Path

import pytest

from fastclipper.ingest.buffer import SegmentedBuffer
from fastclipper.ingest.clock import ClockState, VirtualClock
from fastclipper.ingest.marker import CandidateStatus, ManualMarker
from fastclipper.ingest.source import LocalRecordingSource
from fastclipper.rendering.adapter import IsolatedRenderAdapter


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory(prefix="fastclipper_test_m1_") as d:
        yield Path(d)


@pytest.fixture
def synthetic_video():
    caminho = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    if not caminho.is_file():
        # Gera sob demanda se ainda não existir
        from tests.fixtures import gerar_midia_sintetica
        gerar_midia_sintetica.main()
    return caminho


@pytest.fixture
def synthetic_no_audio_video():
    caminho = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "sintetico_sem_audio.mp4"
    if not caminho.is_file():
        from tests.fixtures import gerar_midia_sintetica
        gerar_midia_sintetica.main()
    return caminho


def test_virtual_clock_controls():
    clock = VirtualClock(speed=2.0)
    assert clock.state == ClockState.STOPPED
    assert clock.current_time() == 0.0

    clock.start()
    assert clock.state == ClockState.RUNNING

    clock.pause()
    assert clock.state == ClockState.PAUSED
    t_pausado = clock.current_time()

    clock.resume()
    assert clock.state == ClockState.RUNNING

    clock.seek(50.0)
    assert clock.current_time() >= 50.0

    clock.stop()
    assert clock.state == ClockState.STOPPED


def test_marca_no_inicio_do_video(tmp_dir):
    buffer = SegmentedBuffer(buffer_dir=tmp_dir / "buffer")
    clock = VirtualClock(speed=1.0)
    clock.seek(1.5)

    marker = ManualMarker(buffer=buffer, clock=clock, pre_window_s=10.0, post_window_s=5.0)
    cand = marker.mark(titulo="Marca Inicial")

    # Início não pode ser negativo
    assert cand.inicio_s == 0.0
    assert cand.fim_s == 6.5
    assert cand.timestamp_marca == 1.5


def test_janela_futura_ainda_indisponivel(tmp_dir):
    buffer = SegmentedBuffer(buffer_dir=tmp_dir / "buffer")
    clock = VirtualClock(speed=1.0)
    clock.seek(2.0)

    marker = ManualMarker(buffer=buffer, clock=clock, pre_window_s=2.0, post_window_s=4.0)
    cand = marker.mark(titulo="Momento Tenso")

    # fim_s é 6.0s, enquanto o relógio ainda está em 2.0s -> deve aguardar
    assert cand.status == CandidateStatus.AGUARDANDO_POST_WINDOW

    # Relógio avança para 4.0s (ainda antes do fim de 6.0s)
    clock.seek(4.0)
    prontos = marker.check_pending_candidates(current_time=4.0)
    assert len(prontos) == 0
    assert cand.status == CandidateStatus.AGUARDANDO_POST_WINDOW

    # Relógio atinge e ultrapassa 6.0s
    clock.seek(6.1)
    prontos = marker.check_pending_candidates(current_time=6.1)
    assert len(prontos) == 1
    assert cand.status == CandidateStatus.PRONTO_PARA_REVISAO


def test_buffer_retention_and_protection(tmp_dir):
    buffer = SegmentedBuffer(buffer_dir=tmp_dir / "buffer", retention_seconds=10.0, max_segments=3)

    # Adiciona 4 segmentos
    s0 = buffer.add_segment(0, "seg_00.mp4", 0.0, 2.0)
    s1 = buffer.add_segment(1, "seg_01.mp4", 2.0, 4.0)
    s2 = buffer.add_segment(2, "seg_02.mp4", 4.0, 6.0)
    s3 = buffer.add_segment(3, "seg_03.mp4", 6.0, 8.0)

    # Cria arquivo físico falso para testar remoção em disco
    for i in range(4):
        (tmp_dir / "buffer" / f"seg_0{i}.mp4").write_text("dummy")

    # Bloqueia o s0 através de um candidato
    buffer.lock_segment(0, lock_id="cand_vip")

    # Executa poda em t=20.0s (todos expiraram por tempo e por cota de 3)
    removidos = buffer.prune_expired(current_time=20.0)

    # s0 estava protegido pelo lock, logo NÃO pode ter sido removido!
    removidos_ids = [s.segment_id for s in removidos]
    assert 0 not in removidos_ids
    assert 0 in buffer._segments

    # Libera o lock e poda novamente
    buffer.unlock_segment(0, lock_id="cand_vip")
    removidos_pos_unlock = buffer.prune_expired(current_time=20.0)
    assert any(s.segment_id == 0 for s in removidos_pos_unlock)
    assert 0 not in buffer._segments


def test_buffer_persistence_reinicio_e_retomada(tmp_dir):
    buffer_dir = tmp_dir / "buffer_persistente"
    b1 = SegmentedBuffer(buffer_dir=buffer_dir, stream_id="live_gtav", retention_seconds=120.0)
    b1.add_segment(10, "seg_00010.mp4", 20.0, 22.0)
    b1.lock_segment(10, lock_id="cand_alpha")

    # Simula reinício: recria a instância no mesmo diretório
    b2 = SegmentedBuffer(buffer_dir=buffer_dir)
    assert len(b2) == 1
    assert 10 in b2._segments
    assert b2._segments[10].timestamp_inicio == 20.0
    assert "cand_alpha" in b2._segments[10].locked_by


def test_ajustar_inicio_fim_e_ciclo_aprovacao(tmp_dir):
    buffer = SegmentedBuffer(buffer_dir=tmp_dir / "buffer")
    clock = VirtualClock(speed=1.0)
    clock.seek(10.0)

    marker = ManualMarker(buffer=buffer, clock=clock, pre_window_s=5.0, post_window_s=5.0)
    cand = marker.mark(titulo="Momento Inicial")

    # Ajusta início e fim
    versao_inicial = cand.versao
    cand_ajustado = marker.adjust_boundaries(cand.id, new_start_s=6.0, new_end_s=14.0)
    assert cand_ajustado.inicio_s == 6.0
    assert cand_ajustado.fim_s == 14.0
    assert cand_ajustado.versao > versao_inicial

    # Aprova o candidato
    aprovado = marker.approve(cand.id)
    assert aprovado.status == CandidateStatus.APROVADO

    # Rejeita outro candidato e verifica liberação de locks
    cand2 = marker.mark(titulo="Momento Rejeitado")
    rejeitado = marker.reject(cand2.id)
    assert rejeitado.status == CandidateStatus.REJEITADO
    assert len(rejeitado.locked_segment_ids) == 0


def test_renderizacao_clipe_1080x1920_com_audio_e_validacao_ffprobe(tmp_dir, synthetic_video):
    buffer = SegmentedBuffer(buffer_dir=tmp_dir / "buffer")
    clock = VirtualClock(speed=1.0)
    clock.seek(4.0)

    marker = ManualMarker(buffer=buffer, clock=clock, pre_window_s=2.0, post_window_s=2.0)
    cand = marker.mark(titulo="Clipe Renderizado")
    marker.approve(cand.id)

    renderer = IsolatedRenderAdapter(output_dir=tmp_dir / "rendered")
    resultado = renderer.render_candidate(
        candidate=cand,
        buffer=buffer,
        source_fallback_path=synthetic_video,
        require_audio=True,
    )

    assert resultado.ok, f"Erros na renderização: {resultado.errors}"
    assert resultado.status == CandidateStatus.CONCLUIDO
    assert Path(resultado.output_path).is_file()

    # Confere FFprobe estrito
    validacao = renderer.validate_media_ffprobe(
        resultado.output_path,
        duracao_esperada=cand.fim_s - cand.inicio_s,
        largura_esperada=1080,
        altura_esperada=1920,
        require_audio=True,
    )
    assert validacao["valid"], f"Falha na validação de saída: {validacao['errors']}"
    assert validacao["streams"]["video"] is True
    assert validacao["streams"]["audio"] is True


def test_arquivo_com_audio_ausente_gera_requer_revisao(tmp_dir, synthetic_no_audio_video):
    buffer = SegmentedBuffer(buffer_dir=tmp_dir / "buffer")
    clock = VirtualClock(speed=1.0)
    clock.seek(1.5)

    marker = ManualMarker(buffer=buffer, clock=clock, pre_window_s=1.0, post_window_s=1.0)
    cand = marker.mark(titulo="Clipe Mudo")
    marker.approve(cand.id)

    renderer = IsolatedRenderAdapter(output_dir=tmp_dir / "rendered")
    resultado = renderer.render_candidate(
        candidate=cand,
        buffer=buffer,
        source_fallback_path=synthetic_no_audio_video,
        require_audio=True,
    )

    # Nunca sucesso silencioso quando o áudio é obrigatório e está ausente!
    assert not resultado.ok
    assert resultado.status == CandidateStatus.REQUER_REVISAO
    assert any("áudio" in e.lower() for e in resultado.errors)


def test_source_adaptador_simulacao_live(tmp_dir, synthetic_video):
    buffer = SegmentedBuffer(buffer_dir=tmp_dir / "buffer")
    clock = VirtualClock(speed=10.0)  # 10x mais rápido para o teste

    source = LocalRecordingSource(
        video_path=synthetic_video,
        buffer=buffer,
        clock=clock,
        segment_duration_s=2.0,
    )

    assert source.total_duration_s >= 5.0
    assert source.total_segments >= 3

    # Emite segmentos manualmente ou inicia o loop
    source.emit_segment_now(0)
    source.emit_segment_now(1)

    status = source.get_status()
    assert status["segments_emitted"] >= 2
    assert len(buffer) == 2
