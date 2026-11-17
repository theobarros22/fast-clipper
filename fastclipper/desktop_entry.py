"""
Ponto de entrada desktop do Fast Clipper Studio (.exe).

Sobe o servidor FastAPI/Uvicorn localmente em 127.0.0.1, configura PATH para
binários empacotados (ffmpeg, ffprobe, yt-dlp) e abre o Studio no navegador.
"""

from __future__ import annotations

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


def _pick_free_port(preferred: int = 8000) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
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

    import uvicorn
    from fastclipper.api.app import app

    port = int(os.environ.get("FASTCLIPPER_PORT", _pick_free_port(8000)))
    url = f"http://127.0.0.1:{port}"
    print(f"Fast Clipper Studio iniciando em {url} ...")
    print(f"Dados salvos em: {os.environ.get('FASTCLIPPER_DATA_DIR', 'data/')}")

    threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
