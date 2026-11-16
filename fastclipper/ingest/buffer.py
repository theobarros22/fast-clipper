"""
Buffer rolante circular segmentado com manifesto persistido e proteção de retenção.

Garante persistência de cada segmento gravado, retenção por TTL ou contagem,
e proteção estrita (lock com contagem de referências) para segmentos vinculados
a clipes pendentes de revisão ou renderização.
"""

import hashlib
import json
import logging
import os
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class Segment:
    segment_id: int
    filename: str
    timestamp_inicio: float
    timestamp_fim: float
    duracao: float
    checksum_sha256: Optional[str] = None
    locked_by: Set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["locked_by"] = sorted(list(self.locked_by))
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Segment":
        locked = set(data.get("locked_by", []))
        return cls(
            segment_id=data["segment_id"],
            filename=data["filename"],
            timestamp_inicio=float(data["timestamp_inicio"]),
            timestamp_fim=float(data["timestamp_fim"]),
            duracao=float(data["duracao"]),
            checksum_sha256=data.get("checksum_sha256"),
            locked_by=locked,
        )


class SegmentedBuffer:
    """Gerenciador thread-safe de buffer circular de mídia."""

    def __init__(
        self,
        buffer_dir: str | Path,
        stream_id: str = "default_stream",
        retention_seconds: float = 3600.0,
        max_segments: Optional[int] = None,
    ):
        self.buffer_dir = Path(buffer_dir)
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.stream_id = stream_id
        self.retention_seconds = float(retention_seconds)
        self.max_segments = max_segments
        self.manifest_path = self.buffer_dir / "buffer_manifest.json"

        self._segments: Dict[int, Segment] = {}
        self._lock = threading.Lock()

        if self.manifest_path.exists():
            self.load_manifest()

    def __len__(self) -> int:
        with self._lock:
            return len(self._segments)

    def add_segment(
        self,
        segment_id: int,
        filename: str,
        timestamp_inicio: float,
        timestamp_fim: float,
        checksum_sha256: Optional[str] = None,
    ) -> Segment:
        caminho_arquivo = self.buffer_dir / filename
        if not checksum_sha256 and caminho_arquivo.exists():
            checksum_sha256 = self._calcular_hash(caminho_arquivo)

        duracao = round(timestamp_fim - timestamp_inicio, 4)
        seg = Segment(
            segment_id=segment_id,
            filename=filename,
            timestamp_inicio=timestamp_inicio,
            timestamp_fim=timestamp_fim,
            duracao=duracao,
            checksum_sha256=checksum_sha256,
        )

        with self._lock:
            self._segments[segment_id] = seg
            self._save_manifest_locked()

        return seg

    def lock_segment(self, segment_id: int, lock_id: str) -> bool:
        """Protege um segmento contra remoção por política de retenção."""
        with self._lock:
            seg = self._segments.get(segment_id)
            if not seg:
                return False
            seg.locked_by.add(lock_id)
            self._save_manifest_locked()
            return True

    def unlock_segment(self, segment_id: int, lock_id: str) -> bool:
        """Libera a proteção de um segmento."""
        with self._lock:
            seg = self._segments.get(segment_id)
            if not seg:
                return False
            seg.locked_by.discard(lock_id)
            self._save_manifest_locked()
            return True

    def lock_range(self, start_s: float, end_s: float, lock_id: str) -> List[int]:
        """Bloqueia todos os segmentos que interceptam o intervalo [start_s, end_s]."""
        with self._lock:
            locked_ids = []
            for seg in self._segments.values():
                if seg.timestamp_fim >= start_s and seg.timestamp_inicio <= end_s:
                    seg.locked_by.add(lock_id)
                    locked_ids.append(seg.segment_id)
            if locked_ids:
                self._save_manifest_locked()
            return locked_ids

    def unlock_range(self, lock_id: str) -> int:
        """Remove determinado lock_id de todos os segmentos protegidos."""
        with self._lock:
            count = 0
            for seg in self._segments.values():
                if lock_id in seg.locked_by:
                    seg.locked_by.remove(lock_id)
                    count += 1
            if count > 0:
                self._save_manifest_locked()
            return count

    def get_segments_in_range(self, start_s: float, end_s: float) -> List[Segment]:
        with self._lock:
            candidatos = [
                seg
                for seg in self._segments.values()
                if seg.timestamp_fim >= start_s and seg.timestamp_inicio <= end_s
            ]
            return sorted(candidatos, key=lambda s: s.timestamp_inicio)

    def prune_expired(self, current_time: float) -> List[Segment]:
        """Remove segmentos expirados que NÃO estejam protegidos por locks."""
        limite_tempo = current_time - self.retention_seconds
        removidos: List[Segment] = []

        with self._lock:
            ordenados = sorted(self._segments.values(), key=lambda s: s.timestamp_inicio)
            for seg in ordenados:
                # Verifica se expirou por tempo
                expirou_tempo = seg.timestamp_fim < limite_tempo
                # Verifica se expirou por cota máxima
                excedeu_cota = False
                if self.max_segments and len(self._segments) - len(removidos) > self.max_segments:
                    excedeu_cota = True

                if (expirou_tempo or excedeu_cota) and len(seg.locked_by) == 0:
                    removidos.append(seg)

            for seg in removidos:
                del self._segments[seg.segment_id]
                caminho = self.buffer_dir / seg.filename
                if caminho.exists():
                    try:
                        caminho.unlink()
                    except OSError as e:
                        logger.warning("Falha ao excluir arquivo do segmento %s: %e", caminho, e)

            if removidos:
                self._save_manifest_locked()

        return removidos

    def load_manifest(self) -> None:
        with self._lock:
            if not self.manifest_path.exists():
                return
            try:
                conteudo = self.manifest_path.read_text(encoding="utf-8")
                dados = json.loads(conteudo)
                self.stream_id = dados.get("stream_id", self.stream_id)
                self.retention_seconds = float(dados.get("retention_seconds", self.retention_seconds))
                self._segments = {
                    seg_data["segment_id"]: Segment.from_dict(seg_data)
                    for seg_data in dados.get("segments", [])
                }
            except Exception as e:
                logger.error("Erro ao carregar manifesto do buffer: %s", e)

    def save_manifest(self) -> None:
        with self._lock:
            self._save_manifest_locked()

    def _save_manifest_locked(self) -> None:
        dados = {
            "stream_id": self.stream_id,
            "retention_seconds": self.retention_seconds,
            "total_segments": len(self._segments),
            "segments": [seg.to_dict() for seg in sorted(self._segments.values(), key=lambda s: s.timestamp_inicio)],
        }
        temp_path = self.manifest_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(dados, indent=2), encoding="utf-8")
        shutil.move(str(temp_path), str(self.manifest_path))

    @staticmethod
    def _calcular_hash(caminho: Path) -> str:
        h = hashlib.sha256()
        with open(caminho, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
