"""
Ponto de entrada desktop do Fast Clipper Studio (.exe).

Sobe o servidor FastAPI/Uvicorn localmente em 127.0.0.1, configura PATH para
binários empacotados (ffmpeg, ffprobe, yt-dlp) e abre o Studio no navegador.
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _configure_frozen_environment() -> None:
    """Configura diretórios de dados graváveis e PATH de binários quando empacotado."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        meipass = Path(getattr(sys, "_MEIPASS", exe_dir)).resolve()

        # Prioriza binários empacotados (ffmpeg, ffprobe, yt-dlp) no PATH
        extra_bins = [str(meipass), str(exe_dir)]
        internal_dir = exe_dir / "_internal"
        if internal_dir.is_dir():
            extra_bins.append(str(internal_dir))
        os.environ["PATH"] = os.pathsep.join(extra_bins) + os.pathsep + os.environ.get("PATH", "")

        # Pasta de dados gravável (usa %LOCALAPPDATA%\FastClipper\data por padrão)
        if not os.environ.get("FASTCLIPPER_DATA_DIR"):
            local_app = Path(os.environ.get("LOCALAPPDATA", Path.home()))
            data_dir = (local_app / "FastClipper" / "data").resolve()
            data_dir.mkdir(parents=True, exist_ok=True)
            os.environ["FASTCLIPPER_DATA_DIR"] = str(data_dir)

        # Garante que sys.stdout/sys.stderr sejam válidos mesmo se executado oculto/sem console
        try:
            if sys.stdout is None or not hasattr(sys.stdout, "write"):
                log_file = (Path(os.environ["FASTCLIPPER_DATA_DIR"]) / "fastclipper.log").open("a", encoding="utf-8")
                sys.stdout = log_file
        except Exception:
            try:
                sys.stdout = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                pass
        try:
            if sys.stderr is None or not hasattr(sys.stderr, "write"):
                sys.stderr = sys.stdout or open(os.devnull, "w", encoding="utf-8")
        except Exception:
            pass


def _is_server_running(port: int = 8000) -> bool:
    """Verifica se o Fast Clipper Studio já está rodando e respondendo na porta."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/status",
            headers={"User-Agent": "FastClipperDesktop/1.0"},
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def _pick_free_port(preferred: int = 8000) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _open_browser_when_ready(url: str, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/v1/status", timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.25)
    if os.environ.get("FASTCLIPPER_NO_BROWSER") != "1":
        webbrowser.open(url)


def main() -> None:
    _configure_frozen_environment()

    # Porta padrão preferencial: 8000
    preferred_port = int(os.environ.get("FASTCLIPPER_PORT", 8000))

    # Se o servidor já estiver rodando nessa porta, apenas traz o navegador à frente
    if _is_server_running(preferred_port):
        url = f"http://127.0.0.1:{preferred_port}"
        print(f"[INFO] Fast Clipper Studio ja esta ativo em {url}")
        print("[INFO] Abrindo o Estudio no seu navegador...")
        webbrowser.open(url)
        return

    port = int(os.environ.get("FASTCLIPPER_PORT", _pick_free_port(preferred_port)))
    url = f"http://127.0.0.1:{port}"
    print(f"Fast Clipper Studio iniciando em {url} ...")
    print(f"Dados salvos em: {os.environ.get('FASTCLIPPER_DATA_DIR', 'data/')}")

    threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

    import uvicorn
    from fastclipper.api.app import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

