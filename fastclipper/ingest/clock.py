"""
Relógio virtual controlável para simulação de transmissões ao vivo.

Permite avanço em tempo real (1.0x), pausa, retomada, busca temporal (seek)
e avanço acelerado (para testes rápidos em lote).
"""

import threading
import time
from enum import Enum


class ClockState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


class VirtualClock:
    """Relógio virtual thread-safe para controle temporal de streams e gravações."""

    def __init__(self, speed: float = 1.0, start_time: float = 0.0):
        if speed <= 0:
            raise ValueError("A velocidade do relógio deve ser maior que zero.")
        self._speed = speed
        self._current_time = float(start_time)
        self._state = ClockState.STOPPED
        self._last_tick_wall = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> ClockState:
        with self._lock:
            return self._state

    @property
    def speed(self) -> float:
        with self._lock:
            return self._speed

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError("A velocidade do relógio deve ser maior que zero.")
        with self._lock:
            self._update_internal_time()
            self._speed = float(speed)

    def start(self) -> None:
        with self._lock:
            if self._state == ClockState.RUNNING:
                return
            self._state = ClockState.RUNNING
            self._last_tick_wall = time.perf_counter()

    def pause(self) -> None:
        with self._lock:
            if self._state != ClockState.RUNNING:
                return
            self._update_internal_time()
            self._state = ClockState.PAUSED

    def resume(self) -> None:
        with self._lock:
            if self._state != ClockState.PAUSED:
                return
            self._state = ClockState.RUNNING
            self._last_tick_wall = time.perf_counter()

    def stop(self) -> None:
        with self._lock:
            self._update_internal_time()
            self._state = ClockState.STOPPED

    def seek(self, target_time: float) -> None:
        if target_time < 0:
            raise ValueError("Tempo alvo não pode ser negativo.")
        with self._lock:
            self._current_time = float(target_time)
            if self._state == ClockState.RUNNING:
                self._last_tick_wall = time.perf_counter()

    def current_time(self) -> float:
        with self._lock:
            self._update_internal_time()
            return self._current_time

    def _update_internal_time(self) -> None:
        if self._state == ClockState.RUNNING:
            now_wall = time.perf_counter()
            delta_wall = now_wall - self._last_tick_wall
            self._current_time += delta_wall * self._speed
            self._last_tick_wall = now_wall
