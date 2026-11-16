"""
Suite de Testes Automatizados para o Marco M4 (Sugestões Assistidas e Sinais Multimodais).

Cobre:
1. Detecção de picos de energia de áudio com z-score normalizado de RMS.
2. Detecção de picos e aceleração de chat de live.
3. Indexação de transcrição com separação de eventos de jogo vs reações do criador.
4. Agregação multimodal com explicabilidade transparente e versionamento do método.
5. Possibilidade estrita de desativar cada detector de forma independente (toggle).
6. Comparador de benchmark contra conjunto de avaliação humano do M0 (Precisão, Recall e Momentos Perdidos).
"""

from pathlib import Path
import pytest

from fastclipper.signals.aggregator import MultimodalSignalAggregator
from fastclipper.signals.audio_analyzer import AudioPeakDetector
from fastclipper.signals.chat_analyzer import ChatSpikeDetector
from fastclipper.signals.evaluator import BenchmarkEvaluator
from fastclipper.signals.models import (
    GamingEditorialProfile,
    SignalEvent,
    SignalType,
    SignalWeights,
)
from fastclipper.signals.transcription_indexer import TranscriptionIndexer


@pytest.fixture
def synthetic_audio_video():
    caminho = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    if not caminho.is_file():
        from tests.fixtures import gerar_midia_sintetica
        gerar_midia_sintetica.main()
    return caminho


def test_audio_peak_detection_zscore(synthetic_audio_video):
    detector = AudioPeakDetector(zscore_threshold=1.5, window_duration_s=0.5)
    picos = detector.analyze_audio(synthetic_audio_video)

    assert isinstance(picos, list)
    for p in picos:
        assert p.signal_type == SignalType.AUDIO_PEAK
        assert p.intensity > 0
        assert "zscore" in p.metadata


def test_chat_spike_detection():
    detector = ChatSpikeDetector(bucket_duration_s=2.0, zscore_threshold=1.8)

    # Simula 60 segundos de chat com taxa normal (1 msg a cada 2s) e um pico em t=20s (15 msgs)
    mensagens = []
    for t in range(0, 60, 2):
        mensagens.append({"timestamp_s": float(t), "user": f"user_{t}", "message": "jogando bem"})

    # Injeta pico em t=20s a t=22s
    for i in range(15):
        mensagens.append({"timestamp_s": 20.5 + (i * 0.08), "user": f"fan_{i}", "message": "KKKKKKKK MEU DEUS"})

    picos = detector.analyze_chat(mensagens)
    assert len(picos) >= 1
    pico_principal = max(picos, key=lambda p: p.intensity)
    assert 19.0 <= pico_principal.timestamp_s <= 23.0
    assert pico_principal.signal_type == SignalType.CHAT_SPIKE
    assert pico_principal.metadata["msg_por_segundo"] > 5.0


def test_transcription_gaming_profile_separation():
    perfil = GamingEditorialProfile()
    indexer = TranscriptionIndexer(profile=perfil)

    segmentos = [
        {"start": 5.0, "end": 8.0, "text": "Entrando no carro e acelerando na autoestrada"},
        {"start": 15.0, "end": 18.0, "text": "A polícia começou a perseguição agora"},  # Apenas evento do jogo
        {"start": 25.0, "end": 28.0, "text": "Mano do céu olha isso não acredito"},    # Apenas reação do streamer
        {"start": 35.0, "end": 39.0, "text": "O helicóptero explodiu caraca socorro"},  # Coincidência: evento + reação
    ]

    eventos = indexer.index_transcription(segmentos)
    assert len(eventos) == 3

    # Evento 1: apenas jogo
    assert eventos[0].label == "Evento do Jogo"
    assert "polícia" in eventos[0].metadata["eventos_jogo"]

    # Evento 2: apenas reação
    assert eventos[1].label == "Reação Vocal do Criador"
    assert len(eventos[1].metadata["reacoes_criador"]) >= 1

    # Evento 3: Coincidência com bônus de intensidade
    assert eventos[2].label == "Reação ao Jogo"
    assert eventos[2].metadata["coincidencia_evento_reacao"] is True
    assert eventos[2].intensity > eventos[0].intensity


def test_multimodal_aggregation_and_explainability():
    pesos = SignalWeights(
        audio_weight=0.35,
        chat_weight=0.30,
        transcription_weight=0.25,
        visual_weight=0.10,
        min_candidate_score=0.50,
    )
    aggregator = MultimodalSignalAggregator(weights=pesos, cluster_window_s=6.0)

    # Eventos convergentes por volta de t=30s
    eventos = [
        SignalEvent(SignalType.AUDIO_PEAK, timestamp_s=29.5, intensity=0.9, label="Pico Áudio"),
        SignalEvent(SignalType.CHAT_SPIKE, timestamp_s=30.2, intensity=0.85, label="Pico Chat"),
        SignalEvent(SignalType.TRANSCRIPTION_KEYWORD, timestamp_s=30.0, intensity=0.85, label="Fala"),
    ]

    candidatos = aggregator.aggregate_signals(eventos)
    assert len(candidatos) == 1

    cand = candidatos[0]
    assert 28.0 <= cand.timestamp_s <= 31.0
    assert cand.score >= 0.70
    assert cand.method_version == "m4_multimodal_v1.0"
    assert "Score" in cand.explanation_text
    assert cand.signals_breakdown["audio"]["weighted_score"] > 0
    assert cand.signals_breakdown["chat"]["weighted_score"] > 0
    assert cand.signals_breakdown["transcription"]["weighted_score"] > 0


def test_toggle_individual_detectors():
    """Valida a opção de desligar cada detector de forma independente."""
    pesos_sem_chat = SignalWeights(enable_chat=False, min_candidate_score=0.20)
    aggregator = MultimodalSignalAggregator(weights=pesos_sem_chat)

    eventos = [
        SignalEvent(SignalType.CHAT_SPIKE, timestamp_s=15.0, intensity=1.5, label="Pico Chat Isolado"),
        SignalEvent(SignalType.AUDIO_PEAK, timestamp_s=40.0, intensity=0.8, label="Pico Áudio"),
    ]

    candidatos = aggregator.aggregate_signals(eventos)
    # O pico de chat em t=15s foi ignorado porque o detector estava desligado!
    assert len(candidatos) == 1
    assert candidatos[0].timestamp_s == 40.0
    assert candidatos[0].signals_breakdown["chat"]["weighted_score"] == 0.0


def test_benchmark_evaluator_metrics():
    evaluator = BenchmarkEvaluator()

    # Ground truth anotado por humano (2 úteis e 1 inútil)
    ground_truth = [
        {"inicio_s": 20.0, "fim_s": 40.0, "tipo": "util", "descricao": "Perseguição com capotamento"},
        {"inicio_s": 70.0, "fim_s": 90.0, "tipo": "util", "descricao": "Salto de paraquedas épico"},
        {"inicio_s": 120.0, "fim_s": 150.0, "tipo": "inutil", "descricao": "Menu de pausa"},
    ]

    # Sugestões da IA
    from fastclipper.signals.models import CandidateSuggestion
    sugestoes = [
        # Acerta o primeiro intervalo útil
        CandidateSuggestion("s1", timestamp_s=30.0, start_s=18.0, end_s=42.0, score=0.85, category="highlight"),
        # Cai no intervalo inútil (Falso Positivo)
        CandidateSuggestion("s2", timestamp_s=130.0, start_s=118.0, end_s=145.0, score=0.65, category="noise"),
    ]

    # Intervalo 2 (70s-90s) não recebeu nenhuma sugestão (Falso Negativo / Momento Perdido)
    metricas = evaluator.evaluate(ground_truth, sugestoes)

    assert metricas.total_intervalos_uteis == 2
    assert metricas.total_intervalos_inuteis == 1
    assert metricas.verdadeiros_positivos == 1
    assert metricas.falsos_positivos == 1
    assert metricas.momentos_perdidos == 1
    assert metricas.precisao == 0.5   # 1 / (1 + 1)
    assert metricas.recall == 0.5     # 1 / 2
    assert metricas.taxa_momentos_perdidos == 0.5
