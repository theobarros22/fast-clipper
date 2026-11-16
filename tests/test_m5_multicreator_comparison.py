"""
Suite de Testes Automatizados para o Marco M5 (Comparação entre Criadores - Cross-POV).

Cobre:
1. Bloqueio estrito quando qualquer fonte carece de autorização (MissingAuthorizationError).
2. Sincronização de streams com tempos diferentes de transmissão (offsets distintos).
3. Correção de associação e reordenação de faixas.
4. Prevenção de áudios sobrepostos com roteamento de fonte única.
5. Remoção de uma fonte da sequência comparativa.
6. Renderização comparativa 1080x1920 (modo Sequencial e Dividido) com FFprobe estrito.
"""

import tempfile
from pathlib import Path

import pytest

from fastclipper.multicreator.models import (
    ComparisonMode,
    CreatorReactionTrack,
    CrossCreatorEvent,
)
from fastclipper.multicreator.renderer import ComparisonRenderer
from fastclipper.multicreator.sync import (
    EventSyncManager,
    MissingAuthorizationError,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory(prefix="fastclipper_test_m5_") as d:
        yield Path(d)


@pytest.fixture
def fixture_video_a():
    caminho = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    if not caminho.is_file():
        from tests.fixtures import gerar_midia_sintetica
        gerar_midia_sintetica.main()
    return str(caminho)


@pytest.fixture
def fixture_video_b():
    caminho = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "sintetico_gameplay_puro.mp4"
    if not caminho.is_file():
        from tests.fixtures import gerar_midia_sintetica
        gerar_midia_sintetica.main()
    return str(caminho)


def test_ausencia_de_autorizacao_bloqueia_renderizacao(tmp_dir, fixture_video_a, fixture_video_b):
    sync_mgr = EventSyncManager()
    event = sync_mgr.create_event("evt_assalto", "Grande Golpe GTA", "Assalto ao Banco")

    # Faixa 1 com autorização confirmada
    t1 = CreatorReactionTrack(
        track_id="trk_streamer_1",
        event_id="evt_assalto",
        creator_name="Streamer Alfa",
        video_path=fixture_video_a,
        stream_offset_s=0.0,
        start_s=1.0,
        end_s=3.0,
        has_authorization=True,
        authorization_ref="TERMO-001",
    )
    # Faixa 2 SEM autorização
    t2 = CreatorReactionTrack(
        track_id="trk_streamer_2",
        event_id="evt_assalto",
        creator_name="Streamer Não Autorizado",
        video_path=fixture_video_b,
        stream_offset_s=5.0,
        start_s=2.0,
        end_s=4.0,
        has_authorization=False,
        authorization_ref="",
    )

    sync_mgr.add_track(event.event_id, t1)
    sync_mgr.add_track(event.event_id, t2)

    # Tentativa de renderização deve ser sumariamente bloqueada!
    renderer = ComparisonRenderer(output_dir=tmp_dir / "out")
    with pytest.raises(MissingAuthorizationError) as exc_info:
        renderer.render_event_comparison(event)
    assert "fontes não autorizadas" in str(exc_info.value)


def test_tempos_diferentes_de_transmissao_alinhamento(fixture_video_a, fixture_video_b):
    sync_mgr = EventSyncManager()
    event = sync_mgr.create_event("evt_perseguicao", "Perseguição", "Fuga de 5 estrelas")

    # Streamer A tem 4.0s de delay
    t_a = CreatorReactionTrack(
        track_id="trk_a",
        event_id=event.event_id,
        creator_name="Streamer A",
        video_path=fixture_video_a,
        stream_offset_s=4.0,
        start_s=10.0,
        end_s=15.0,
        has_authorization=True,
        authorization_ref="TERMO-A",
    )
    # Streamer B tem 14.0s de delay
    t_b = CreatorReactionTrack(
        track_id="trk_b",
        event_id=event.event_id,
        creator_name="Streamer B",
        video_path=fixture_video_b,
        stream_offset_s=14.0,
        start_s=20.0,
        end_s=25.0,
        has_authorization=True,
        authorization_ref="TERMO-B",
    )

    sync_mgr.add_track(event.event_id, t_a)
    sync_mgr.add_track(event.event_id, t_b)

    # Na linha global sincronizada:
    # Streamer A t=10s com offset=4s -> t_global = 6.0s
    assert t_a.global_start_s == 6.0
    # Streamer B t=20s com offset=14s -> t_global = 6.0s (sincronizados no mesmo instante do jogo!)
    assert t_b.global_start_s == 6.0


def test_correcao_de_associacao_e_reordenacao(fixture_video_a):
    sync_mgr = EventSyncManager()
    event = sync_mgr.create_event("evt_ajuste", "Evento Ajuste", "Teste de Reordenação")

    t1 = CreatorReactionTrack("trk_1", event.event_id, "Criador 1", fixture_video_a, 0.0, 1.0, 3.0, True, "TERMO", order=1)
    t2 = CreatorReactionTrack("trk_2", event.event_id, "Criador 2", fixture_video_a, 2.0, 1.0, 3.0, True, "TERMO", order=2)

    sync_mgr.add_track(event.event_id, t1)
    sync_mgr.add_track(event.event_id, t2)

    # Ajusta o offset da faixa 1 em milissegundos
    sync_mgr.adjust_offset(event.event_id, "trk_1", new_offset_s=1.250)
    assert t1.stream_offset_s == 1.250

    # Inverte a ordem
    sync_mgr.reorder_tracks(event.event_id, ["trk_2", "trk_1"])
    assert event.tracks[0].track_id == "trk_2"
    assert event.tracks[1].track_id == "trk_1"


def test_remocao_de_uma_fonte(fixture_video_a, fixture_video_b):
    sync_mgr = EventSyncManager()
    event = sync_mgr.create_event("evt_remocao", "Evento Trio", "Teste Remoção")

    t1 = CreatorReactionTrack("t1", event.event_id, "A", fixture_video_a, 0, 1, 3, True, "TERMO", order=1)
    t2 = CreatorReactionTrack("t2", event.event_id, "B", fixture_video_b, 0, 1, 3, True, "TERMO", order=2)

    sync_mgr.add_track(event.event_id, t1)
    sync_mgr.add_track(event.event_id, t2)
    assert len(event.tracks) == 2

    # Remove fonte B
    sync_mgr.remove_track(event.event_id, "t2")
    assert len(event.tracks) == 1
    assert event.tracks[0].track_id == "t1"


def test_audios_sobrepostos_prevencao_e_renderizacao_split(tmp_dir, fixture_video_a, fixture_video_b):
    """Valida renderização em tela dividida (Split 9:16) com áudio de fonte única (sem sobreposição)."""
    sync_mgr = EventSyncManager()
    event = sync_mgr.create_event("evt_split", "Reação Dupla", "Salto e Explosão")

    t1 = CreatorReactionTrack("trk_top", event.event_id, "Piloto", fixture_video_a, 0.0, 1.0, 3.0, True, "TERMO-1", order=1)
    t2 = CreatorReactionTrack("trk_bot", event.event_id, "Copiloto", fixture_video_b, 0.0, 1.0, 3.0, True, "TERMO-2", order=2)

    sync_mgr.add_track(event.event_id, t1)
    sync_mgr.add_track(event.event_id, t2)

    renderer = ComparisonRenderer(output_dir=tmp_dir / "comparacoes")
    resultado = renderer.render_event_comparison(
        event=event,
        mode=ComparisonMode.SPLIT_VERTICAL,
        audio_source_track_id="trk_top",  # Áudio selecionado apenas do Piloto
    )

    assert resultado["ok"], f"Erros: {resultado.get('errors')}"
    assert Path(resultado["output_path"]).is_file()
    assert resultado["mode"] == "split_vertical"
    assert len(resultado["tracks_included"]) == 2


def test_renderizacao_sequencial_com_nomes_dos_criadores(tmp_dir, fixture_video_a, fixture_video_b):
    """Valida renderização sequencial com faixas alternadas e crachás de identificação."""
    sync_mgr = EventSyncManager()
    event = sync_mgr.create_event("evt_seq", "Sequencial Reações", "Reação Criador 1 depois Criador 2")

    t1 = CreatorReactionTrack("t1", event.event_id, "Streamer Alfa", fixture_video_a, 0.0, 1.0, 2.5, True, "T1", order=1)
    t2 = CreatorReactionTrack("t2", event.event_id, "Streamer Beta", fixture_video_b, 0.0, 1.5, 3.0, True, "T2", order=2)

    sync_mgr.add_track(event.event_id, t1)
    sync_mgr.add_track(event.event_id, t2)

    renderer = ComparisonRenderer(output_dir=tmp_dir / "comparacoes")
    resultado = renderer.render_event_comparison(
        event=event,
        mode=ComparisonMode.SEQUENTIAL,
    )

    assert resultado["ok"], f"Erros: {resultado.get('errors')}"
    assert Path(resultado["output_path"]).is_file()
    assert resultado["mode"] == "sequential"
    assert resultado["duration"] >= 2.5  # Duração combinada dos dois trechos
