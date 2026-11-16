"""
Motor de alinhamento temporal, controle de autorização e gestão de faixas de reação.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

from fastclipper.multicreator.models import (
    CreatorReactionTrack,
    CrossCreatorEvent,
)

logger = logging.getLogger(__name__)


class MissingAuthorizationError(PermissionError):
    """Lançada quando uma fonte não possui autorização válida para comparação."""
    pass


class InvalidTimelineError(ValueError):
    """Lançada quando a configuração de sincronização temporal é inconsistente."""
    pass


class EventSyncManager:
    """Gerenciador de sincronização temporal e autorizações de eventos multi-criador."""

    def __init__(self):
        self._events: Dict[str, CrossCreatorEvent] = {}

    def create_event(self, event_id: str, title: str, description: str) -> CrossCreatorEvent:
        event = CrossCreatorEvent(event_id=event_id, title=title, description=description)
        self._events[event_id] = event
        return event

    def get_event(self, event_id: str) -> Optional[CrossCreatorEvent]:
        return self._events.get(event_id)

    def add_track(self, event_id: str, track: CreatorReactionTrack) -> CrossCreatorEvent:
        event = self._events.get(event_id)
        if not event:
            raise KeyError(f"Evento {event_id} não encontrado.")

        # Validação de caminho
        if not Path(track.video_path).is_file():
            raise FileNotFoundError(f"Arquivo de vídeo da faixa não encontrado: {track.video_path}")

        if track.duration_s <= 0:
            raise InvalidTimelineError("A duração da reação deve ser maior que zero.")

        # Substitui faixa existente se mesmo track_id ou adiciona
        event.tracks = [t for t in event.tracks if t.track_id != track.track_id]
        event.tracks.append(track)
        event.tracks.sort(key=lambda t: t.order)
        return event

    def remove_track(self, event_id: str, track_id: str) -> CrossCreatorEvent:
        event = self._events.get(event_id)
        if not event:
            raise KeyError(f"Evento {event_id} não encontrado.")
        event.tracks = [t for t in event.tracks if t.track_id != track_id]
        return event

    def adjust_offset(self, event_id: str, track_id: str, new_offset_s: float) -> CreatorReactionTrack:
        event = self._events.get(event_id)
        if not event:
            raise KeyError(f"Evento {event_id} não encontrado.")
        track = next((t for t in event.tracks if t.track_id == track_id), None)
        if not track:
            raise KeyError(f"Faixa {track_id} não encontrada no evento {event_id}.")
        track.stream_offset_s = round(float(new_offset_s), 3)
        return track

    def reorder_tracks(self, event_id: str, ordered_track_ids: List[str]) -> CrossCreatorEvent:
        event = self._events.get(event_id)
        if not event:
            raise KeyError(f"Evento {event_id} não encontrado.")

        id_to_order = {tid: idx + 1 for idx, tid in enumerate(ordered_track_ids)}
        for t in event.tracks:
            if t.track_id in id_to_order:
                t.order = id_to_order[t.track_id]

        event.tracks.sort(key=lambda t: t.order)
        return event

    def validate_event_readiness(self, event_id: str) -> dict:
        """Verifica autorização de todas as fontes e consistência temporal antes de renderizar."""
        event = self._events.get(event_id)
        if not event:
            raise KeyError(f"Evento {event_id} não encontrado.")

        if not event.tracks:
            raise InvalidTimelineError("O evento não possui nenhuma faixa de criador associada.")

        # Verificação rigorosa de direitos e autorizações
        nao_autorizados = [t.creator_name for t in event.tracks if not t.has_authorization]
        if nao_autorizados:
            raise MissingAuthorizationError(
                f"Impossível comparar ou exportar: fontes não autorizadas detectadas: {', '.join(nao_autorizados)}. "
                f"Todas as mídias devem ter autorização confirmada no manifesto."
            )

        spoilers = [t.creator_name for t in event.tracks if t.has_spoiler]

        return {
            "ready": True,
            "total_tracks": len(event.tracks),
            "tracks": [t.creator_name for t in event.tracks],
            "spoilers_detected": spoilers,
            "has_spoiler_warning": len(spoilers) > 0,
        }
