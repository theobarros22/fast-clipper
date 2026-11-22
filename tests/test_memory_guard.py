"""Testes unitários para a guarda de memória (fastclipper/process/memory_guard.py)."""

from __future__ import annotations

import logging

import pytest

from fastclipper.process import memory_guard
from fastclipper.process.memory_guard import (
    MINIMOS_GB,
    InsufficientMemoryError,
    ensure_free_memory,
    free_memory_gb,
    is_out_of_memory_error,
)


def test_minimos_gb_padrao():
    assert MINIMOS_GB == {"transcricao": 2.5, "render": 2.0, "pacote_ia": 3.0}


def test_ensure_free_memory_acima_do_minimo(monkeypatch):
    monkeypatch.delenv("FASTCLIPPER_MIN_FREE_GB", raising=False)
    monkeypatch.setattr(memory_guard, "free_memory_gb", lambda: 5.4)

    livre = ensure_free_memory(2.0, "render")
    assert livre == 5.4


def test_ensure_free_memory_abaixo_do_minimo_mensagem_e_atributos(monkeypatch):
    monkeypatch.delenv("FASTCLIPPER_MIN_FREE_GB", raising=False)
    monkeypatch.setattr(memory_guard, "free_memory_gb", lambda: 0.9)

    with pytest.raises(InsufficientMemoryError) as exc_info:
        ensure_free_memory(2.0, "render")

    err = exc_info.value
    assert err.etapa == "render"
    assert err.livre_gb == 0.9
    assert err.minimo_gb == 2.0
    assert (
        str(err)
        == "Memória livre 0,9 GB; a etapa 'render' precisa de 2,0 GB. Feche programas e tente de novo."
    )


def test_ensure_free_memory_none_nao_levanta_e_avisa_uma_vez(monkeypatch, caplog):
    monkeypatch.delenv("FASTCLIPPER_MIN_FREE_GB", raising=False)
    monkeypatch.setattr(memory_guard, "free_memory_gb", lambda: None)
    monkeypatch.setattr(memory_guard, "_aviso_indisponivel_emitido", False)

    with caplog.at_level(logging.WARNING, logger=memory_guard.__name__):
        primeiro = ensure_free_memory(2.0, "render")
        segundo = ensure_free_memory(3.0, "pacote_ia")

    assert primeiro == -1.0
    assert segundo == -1.0
    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(avisos) == 1


def test_ensure_free_memory_env_zero_desliga_guarda(monkeypatch):
    monkeypatch.setenv("FASTCLIPPER_MIN_FREE_GB", "0")
    monkeypatch.setattr(memory_guard, "free_memory_gb", lambda: 0.1)

    livre = ensure_free_memory(2.5, "transcricao")
    assert livre == 0.1


def test_ensure_free_memory_env_substitui_minimo(monkeypatch):
    monkeypatch.setenv("FASTCLIPPER_MIN_FREE_GB", "4.0")
    monkeypatch.setattr(memory_guard, "free_memory_gb", lambda: 3.2)

    with pytest.raises(InsufficientMemoryError) as exc_info:
        ensure_free_memory(2.0, "render")

    assert exc_info.value.minimo_gb == 4.0
    assert exc_info.value.livre_gb == 3.2


def test_free_memory_gb_real_sem_excecao():
    valor = free_memory_gb()
    assert valor is None or (isinstance(valor, float) and valor > 0.0)


@pytest.mark.parametrize(
    "texto",
    [
        "FFmpeg falhou: Cannot allocate memory",
        "Intel MKL ERROR: mkl_malloc: failed to allocate memory",
        "RuntimeError: CUDA out of memory",
        "terminate called after throwing an instance of 'std::bad_alloc'",
        "Traceback: MemoryError",
        "cudaErrorMemoryAllocation ao inicializar contexto",
    ],
)
def test_is_out_of_memory_error_positivos(texto):
    assert is_out_of_memory_error(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "Invalid argument",
        "",
        "No such file",
    ],
)
def test_is_out_of_memory_error_negativos(texto):
    assert is_out_of_memory_error(texto) is False
