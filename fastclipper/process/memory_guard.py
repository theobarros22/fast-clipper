"""
Guarda de memória livre antes de etapas pesadas (transcrição, render e pacote de IA).

Consulta a RAM física disponível sem dependências externas (sem psutil):
- Windows: ctypes + GlobalMemoryStatusEx (campo ullAvailPhys)
- Linux: MemAvailable em /proc/meminfo
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

MINIMOS_GB: dict[str, float] = {
    "transcricao": 2.5,
    "render": 2.0,
    "pacote_ia": 3.0,
}

_PADROES_OOM: tuple[str, ...] = (
    "cannot allocate memory",
    "mkl_malloc",
    "out of memory",
    "std::bad_alloc",
    "memoryerror",
    "cudaerrormemoryallocation",
)

_GIB: float = float(1024**3)
_MIB_EM_KB: float = float(1024**2)
_aviso_indisponivel_emitido: bool = False


def _formatar_gb_pt(valor: float) -> str:
    """Formata um valor em GB com 1 casa decimal e vírgula (padrão pt-BR)."""
    return f"{float(valor):.1f}".replace(".", ",")


class InsufficientMemoryError(RuntimeError):
    """Memória livre abaixo do mínimo para uma etapa."""

    def __init__(self, etapa: str, livre_gb: float, minimo_gb: float) -> None:
        self.etapa = str(etapa)
        self.livre_gb = float(livre_gb)
        self.minimo_gb = float(minimo_gb)
        mensagem = (
            f"Memória livre {_formatar_gb_pt(self.livre_gb)} GB; "
            f"a etapa '{self.etapa}' precisa de {_formatar_gb_pt(self.minimo_gb)} GB. "
            "Feche programas e tente de novo."
        )
        super().__init__(mensagem)


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _free_memory_gb_windows() -> float | None:
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    if not ok:
        return None
    return float(status.ullAvailPhys) / _GIB


def _free_memory_gb_linux() -> float | None:
    conteudo = Path("/proc/meminfo").read_text(encoding="utf-8")
    for linha in conteudo.splitlines():
        if linha.startswith("MemAvailable:"):
            partes = linha.split()
            if len(partes) >= 2:
                kb = float(partes[1])
                return kb / _MIB_EM_KB
    return None


def free_memory_gb() -> float | None:
    """Retorna a memória RAM livre em GB ou None se não for possível consultar."""
    try:
        if sys.platform == "win32":
            return _free_memory_gb_windows()
        if sys.platform.startswith("linux"):
            return _free_memory_gb_linux()
        return None
    except Exception:
        return None


def _resolver_minimo_efetivo(minimo_gb: float) -> float:
    valor_env = os.environ.get("FASTCLIPPER_MIN_FREE_GB")
    if valor_env is None or not valor_env.strip():
        return float(minimo_gb)
    try:
        return float(valor_env.strip().replace(",", "."))
    except ValueError:
        logger.warning(
            "Valor inválido em FASTCLIPPER_MIN_FREE_GB=%r; usando mínimo padrão %.1f GB.",
            valor_env,
            minimo_gb,
        )
        return float(minimo_gb)


def ensure_free_memory(minimo_gb: float, etapa: str) -> float:
    """Garante que há memória livre suficiente para a etapa informada.

    - Se FASTCLIPPER_MIN_FREE_GB estiver definida, substitui ``minimo_gb`` (``0`` desliga a guarda).
    - Se ``free_memory_gb()`` retornar ``None``, não bloqueia, emite ``logging.warning`` uma única vez
      por processo e devolve ``-1.0``.
    - Se a memória livre for menor que o mínimo efetivo, levanta ``InsufficientMemoryError``.
    - Caso contrário, devolve a memória livre em GB.
    """
    global _aviso_indisponivel_emitido

    minimo_efetivo = _resolver_minimo_efetivo(minimo_gb)
    livre = free_memory_gb()

    if livre is None:
        if not _aviso_indisponivel_emitido:
            _aviso_indisponivel_emitido = True
            logger.warning(
                "Não foi possível consultar a memória RAM livre; guarda de memória ignorada."
            )
        return -1.0

    if minimo_efetivo <= 0.0:
        return livre

    if livre < minimo_efetivo:
        raise InsufficientMemoryError(etapa=etapa, livre_gb=livre, minimo_gb=minimo_efetivo)

    return livre


def is_out_of_memory_error(texto: str) -> bool:
    """Retorna True se o texto indicar erro de falta de memória (OOM)."""
    if not texto:
        return False
    normalizado = str(texto).lower()
    return any(padrao in normalizado for padrao in _PADROES_OOM)
