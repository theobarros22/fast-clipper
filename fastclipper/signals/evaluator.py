"""
Avaliador estatístico de sugestões contra o conjunto de avaliação humano do M0.

Mede Precisão, Cobertura (Recall), taxa de momentos perdidos e latência média,
fornecendo relatórios quantitativos sem alegações vazias de viralização.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from fastclipper.signals.models import CandidateSuggestion


@dataclass
class MetricasAvaliacao:
    total_intervalos_uteis: int
    total_intervalos_inuteis: int
    total_sugestoes_geradas: int
    verdadeiros_positivos: int  # TP: sugestões que acertaram intervalo útil
    falsos_positivos: int       # FP: sugestões que caíram em intervalo inútil ou vazio
    momentos_perdidos: int      # FN: intervalos úteis ignorados pela IA
    precisao: float             # TP / (TP + FP)
    recall: float               # TP / (TP + FN)
    taxa_momentos_perdidos: float  # FN / (TP + FN)
    latencia_media_s: float
    detalhes_correspondencia: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class BenchmarkEvaluator:
    """Motor de cálculo de métricas de acerto contra anotações humanas."""

    def __init__(self, tolerancia_sobreposicao_s: float = 3.0):
        self.tolerancia_sobreposicao_s = tolerancia_sobreposicao_s

    def evaluate(
        self,
        ground_truth: List[Dict[str, Any]],
        suggestions: List[CandidateSuggestion],
    ) -> MetricasAvaliacao:
        """Avalia a suite de sugestões contra os intervalos rotulados pelo avaliador humano."""
        uteis = [i for i in ground_truth if i.get("tipo") == "util"]
        inuteis = [i for i in ground_truth if i.get("tipo") == "inutil"]

        tp = 0
        fp = 0
        uteis_acertados = set()
        detalhes = []

        for sug in suggestions:
            acertou_util = False
            for idx, u in enumerate(uteis):
                # Verifica sobreposição temporal
                if not (sug.end_s < u["inicio_s"] or sug.start_s > u["fim_s"]):
                    tp += 1
                    uteis_acertados.add(idx)
                    acertou_util = True
                    detalhes.append({
                        "sugestao_id": sug.id,
                        "status": "TP",
                        "intervalo_rotulado": u.get("descricao"),
                        "score": sug.score,
                    })
                    break

            if not acertou_util:
                fp += 1
                detalhes.append({
                    "sugestao_id": sug.id,
                    "status": "FP",
                    "intervalo_rotulado": "Nenhum ou Inútil",
                    "score": sug.score,
                })

        fn = len(uteis) - len(uteis_acertados)
        precisao = round(tp / max(1, (tp + fp)), 3)
        recall = round(len(uteis_acertados) / max(1, len(uteis)), 3)
        taxa_perdidos = round(fn / max(1, len(uteis)), 3)

        latencias = [sug.end_s - sug.timestamp_s for sug in suggestions]
        latencia_media = round(sum(latencias) / max(1, len(latencias)), 2)

        return MetricasAvaliacao(
            total_intervalos_uteis=len(uteis),
            total_intervalos_inuteis=len(inuteis),
            total_sugestoes_geradas=len(suggestions),
            verdadeiros_positivos=tp,
            falsos_positivos=fp,
            momentos_perdidos=fn,
            precisao=precisao,
            recall=recall,
            taxa_momentos_perdidos=taxa_perdidos,
            latencia_media_s=latencia_media,
            detalhes_correspondencia=detalhes,
        )
