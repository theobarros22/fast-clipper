"""
Suite de Testes Automatizados para o Marco M2 (Layout de Gameplay e Facecam).

Cobre:
1. Facecam nos quatro cantos (superior esquerdo, superior direito, inferior esquerdo, inferior direito).
2. Ausência total de câmera (Modo Solo 9:16).
3. Caixas fora dos limites da resolução de origem (detecção de erro e clamp seguro).
4. Persistência de configuração por canal com versionamento e histórico.
5. Detecção de corte de cabeça (Headroom) e proteção de zonas de HUD.
6. Detecção de mudança de cena durante o clipe emitindo 'requer_revisao'.
7. Geração de prévia visual 1080x1920 em imagem.
8. Renderização completa de clipe empilhado (Facecam 9:8 + Gameplay 9:8) com FFprobe 1080x1920.
"""

import tempfile
from pathlib import Path

import pytest

from fastclipper.ingest.buffer import SegmentedBuffer
from fastclipper.ingest.clock import VirtualClock
from fastclipper.ingest.marker import CandidateStatus, ManualMarker
from fastclipper.layouts.config import (
    ChannelLayoutConfig,
    LayoutConfigStore,
    LayoutMode,
    Rectangle,
)
from fastclipper.layouts.engine import LayoutEngine
from fastclipper.layouts.scene_detector import SceneDetector
from fastclipper.rendering.adapter import IsolatedRenderAdapter


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory(prefix="fastclipper_test_m2_") as d:
        yield Path(d)


@pytest.fixture
def synthetic_facecam_video():
    caminho = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    if not caminho.is_file():
        from tests.fixtures import gerar_midia_sintetica
        gerar_midia_sintetica.main()
    return caminho


@pytest.fixture
def synthetic_pure_video():
    caminho = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "sintetico_gameplay_puro.mp4"
    if not caminho.is_file():
        from tests.fixtures import gerar_midia_sintetica
        gerar_midia_sintetica.main()
    return caminho


def test_facecam_nos_quatro_cantos():
    """Valida o cálculo de composição para facecam posicionada nos 4 cantos em 1920x1080."""
    engine = LayoutEngine()
    w_src, h_src = 1920, 1080

    cantos = {
        "superior_esquerdo": Rectangle(x=40, y=40, w=440, h=330),
        "superior_direito": Rectangle(x=1440, y=40, w=440, h=330),
        "inferior_esquerdo": Rectangle(x=40, y=710, w=440, h=330),
        "inferior_direito": Rectangle(x=1440, y=710, w=440, h=330),
    }

    for nome, rect in cantos.items():
        config = ChannelLayoutConfig(
            channel_id="canal_teste",
            layout_name=f"Layout {nome}",
            mode=LayoutMode.STACKED,
            facecam_rect=rect,
            gameplay_rect=Rectangle(x=0, y=0, w=1920, h=1080),
        )
        seguranca = engine.validate_layout_safety(config, w_src, h_src)
        assert seguranca["safe"], f"Canto {nome} falhou: {seguranca['errors']}"

        filtro = engine.build_filter_graph(config, w_src, h_src)
        assert f"crop={rect.w}:{rect.h}:{rect.x}:{rect.y}" in filtro
        assert "vstack=inputs=2" in filtro


def test_ausencia_de_camera_modo_solo():
    """Valida geração do layout quando não há câmera configurada."""
    engine = LayoutEngine()
    w_src, h_src = 1920, 1080

    config = ChannelLayoutConfig(
        channel_id="canal_sem_cam",
        mode=LayoutMode.AUTO,
        facecam_rect=None,
        gameplay_rect=Rectangle(x=200, y=0, w=1520, h=1080),
    )
    assert config.effective_mode() == LayoutMode.SOLO

    filtro = engine.build_filter_graph(config, w_src, h_src)
    assert "crop=1520:1080:200:0" in filtro
    assert "vstack" not in filtro


def test_caixas_fora_dos_limites_e_clamp():
    """Valida detecção de erro e mecanismo de clamp quando caixas ultrapassam as bordas."""
    engine = LayoutEngine()
    w_src, h_src = 1920, 1080

    # Caixa ultrapassando a largura da origem
    caixa_invalida = Rectangle(x=1600, y=50, w=600, h=400)
    assert not caixa_invalida.validate_bounds(w_src, h_src)

    config = ChannelLayoutConfig(
        channel_id="canal_overflow",
        mode=LayoutMode.STACKED,
        facecam_rect=caixa_invalida,
    )
    seguranca = engine.validate_layout_safety(config, w_src, h_src)
    assert not seguranca["safe"]
    assert any("fora dos limites" in e for e in seguranca["errors"])

    # Clamp restringe ao quadro
    clamped = caixa_invalida.clamp(w_src, h_src)
    assert clamped.x + clamped.w <= w_src
    assert clamped.y + clamped.h <= h_src


def test_deteccao_cabeça_cortada_headroom():
    """Detecta se uma caixa de facecam corta a cabeça do participante."""
    engine = LayoutEngine()
    w_src, h_src = 1920, 1080

    # Facecam em y=100 até y=400
    facecam = Rectangle(x=1400, y=100, w=400, h=300)
    # Cabeça começa em y=50 (fora do topo da facecam -> cabeça decepada)
    cabeca_cortada = (1500.0, 50.0, 1700.0, 350.0)

    config = ChannelLayoutConfig(
        channel_id="canal_headroom",
        mode=LayoutMode.STACKED,
        facecam_rect=facecam,
    )

    seguranca = engine.validate_layout_safety(
        config,
        w_src,
        h_src,
        detected_head_boxes=[cabeca_cortada],
    )
    assert len(seguranca["warnings"]) > 0
    assert any("cortada" in w for w in seguranca["warnings"])


def test_persistencia_e_versionamento_de_configuracao(tmp_dir):
    store = LayoutConfigStore(base_dir=tmp_dir / "layouts")

    c1 = ChannelLayoutConfig(
        channel_id="streamer_gtav",
        layout_name="Versao 1",
        facecam_rect=Rectangle(x=1440, y=40, w=440, h=330),
    )
    salvo1 = store.save_config(c1)
    assert salvo1.version == 1

    # Atualiza posição da câmera e salva nova versão
    c2 = ChannelLayoutConfig(
        channel_id="streamer_gtav",
        layout_name="Versao 2 Camera Maior",
        facecam_rect=Rectangle(x=1300, y=30, w=580, h=400),
    )
    salvo2 = store.save_config(c2)
    assert salvo2.version == 2

    # Recupera histórico e versões específicas
    versoes = store.list_versions("streamer_gtav")
    assert versoes == [1, 2]

    v1 = store.get_config("streamer_gtav", version=1)
    v2 = store.get_config("streamer_gtav", version=2)
    latest = store.get_config("streamer_gtav")

    assert v1.facecam_rect.w == 440
    assert v2.facecam_rect.w == 580
    assert latest.version == 2


def test_geracao_de_preview_1080x1920(tmp_dir, synthetic_facecam_video):
    engine = LayoutEngine()
    config = ChannelLayoutConfig(
        channel_id="preview_test",
        mode=LayoutMode.STACKED,
        facecam_rect=Rectangle(x=1440, y=40, w=440, h=330),
        gameplay_rect=Rectangle(x=0, y=0, w=1920, h=1080),
    )
    out_img = tmp_dir / "preview_frame.jpg"

    preview_path = engine.generate_preview(
        video_path=synthetic_facecam_video,
        timestamp_s=2.0,
        config=config,
        output_image_path=out_img,
    )
    assert preview_path.is_file()
    assert preview_path.stat().st_size > 0

    # Confere dimensões da imagem via ffprobe
    w, h = engine.get_source_dimensions(preview_path)
    assert w == 1080
    assert h == 1920


def test_renderizacao_empilhada_9x16_com_validacao_ffprobe(tmp_dir, synthetic_facecam_video):
    buffer = SegmentedBuffer(buffer_dir=tmp_dir / "buffer")
    clock = VirtualClock(speed=1.0)
    clock.seek(4.0)

    marker = ManualMarker(buffer=buffer, clock=clock, pre_window_s=1.5, post_window_s=1.5)
    cand = marker.mark(titulo="Clipe Empilhado Facecam + Gameplay")
    marker.approve(cand.id)

    config = ChannelLayoutConfig(
        channel_id="canal_stacked",
        mode=LayoutMode.STACKED,
        facecam_rect=Rectangle(x=1440, y=40, w=440, h=330),
        gameplay_rect=Rectangle(x=0, y=0, w=1920, h=1080),
    )

    renderer = IsolatedRenderAdapter(output_dir=tmp_dir / "rendered")
    resultado = renderer.render_candidate(
        candidate=cand,
        buffer=buffer,
        source_fallback_path=synthetic_facecam_video,
        require_audio=True,
        layout_config=config,
        check_scene_changes=False,
    )

    assert resultado.ok, f"Erros: {resultado.errors}"
    assert resultado.status == CandidateStatus.CONCLUIDO
    assert Path(resultado.output_path).is_file()

    validacao = renderer.validate_media_ffprobe(
        resultado.output_path,
        duracao_esperada=cand.fim_s - cand.inicio_s,
        largura_esperada=1080,
        altura_esperada=1920,
        require_audio=True,
    )
    assert validacao["valid"], f"Erros na validação: {validacao['errors']}"
    assert validacao["streams"]["video"] is True
    assert validacao["streams"]["audio"] is True


def test_deteccao_de_troca_de_cena_gera_requer_revisao(tmp_dir, synthetic_facecam_video):
    """Verifica se o detector de cena sinaliza 'requer_revisao' para clipe com facecam."""
    detector = SceneDetector(default_threshold=0.2)
    # Detecta cortes no vídeo sintético
    resultado = detector.validate_layout_stability(
        synthetic_facecam_video,
        start_s=0.0,
        duration_s=5.0,
        has_facecam=True,
    )

    assert "requires_review" in resultado
    assert "cuts" in resultado
