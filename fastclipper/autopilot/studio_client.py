"""Cliente HTTP mínimo (stdlib) para a API local do Fast Clipper Studio."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class StudioError(RuntimeError):
    pass


class StudioClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout_s: float = 3600.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _req(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise StudioError(f"{method} {path} → HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise StudioError(
                f"Studio inacessível em {self.base_url} ({exc.reason}). Abra o iniciar.bat antes do piloto."
            ) from exc

    def get(self, path: str) -> Any:
        return self._req("GET", path)

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        return self._req("POST", path, body)

    def health(self) -> bool:
        try:
            self.get("/health")
            return True
        except StudioError:
            return False
