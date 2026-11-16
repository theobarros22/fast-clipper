"""
Supervisor de processos filhos e proteção contra processos zumbis (Child Process Guard).

Inspirado e adaptado da engenharia de processos do AutoClip (child_guard.py).
No Windows: atribui todos os subprocessos (FFmpeg, transcritores) a um Windows Job Object
com JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x00002000). Caso o worker ou CLI termine
inesperadamente, o kernel do Windows encerra instantaneamente todas as instâncias do FFmpeg,
evitando vazamento de memória, CPU e travas de arquivos de mídia.
"""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_live_procs: Dict[int, subprocess.Popen] = {}
_job_handle = None
_hooks_installed = False


def _get_windows_job():
    """Cria e retorna o Job Object do Windows configurado com KILL_ON_JOB_CLOSE."""
    global _job_handle
    if sys.platform != "win32":
        return None

    if _job_handle is not None:
        return _job_handle

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

        _job_handle = (kernel32, handle)
        logger.debug("Windows Job Object inicializado com sucesso (KILL_ON_JOB_CLOSE).")
        return _job_handle
    except Exception as err:
        logger.warning("Job Object indisponível no Windows: %s", err)
        _job_handle = False
        return None


def register_process(proc: subprocess.Popen) -> None:
    """Registra um subprocesso e o acopla ao Job Object de contenção."""
    global _hooks_installed
    with _lock:
        if not _hooks_installed:
            atexit.register(kill_all_guarded)
            _hooks_installed = True

        _live_procs[proc.pid] = proc

        if sys.platform == "win32":
            job = _get_windows_job()
            if job:
                kernel32, handle = job
                try:
                    kernel32.AssignProcessToJobObject(handle, int(proc._handle))  # type: ignore[attr-defined]
                except Exception as e:
                    logger.debug("Falha ao associar processo %s ao job: %s", proc.pid, e)


def unregister_process(proc: subprocess.Popen) -> None:
    """Desregistra processo que terminou normalmente."""
    with _lock:
        _live_procs.pop(proc.pid, None)


def kill_all_guarded() -> None:
    """Encerra graciosamente ou força finalização de todos os processos vivos registrados."""
    with _lock:
        for pid, proc in list(_live_procs.items()):
            try:
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
            except OSError:
                pass
            _live_procs.pop(pid, None)
