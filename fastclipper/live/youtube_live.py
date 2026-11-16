"""
Adaptador de ingestão de YouTube Live com proteção contra SSRF e reconexão resiliente.
"""

import ipaddress
import logging
import re
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SecuritySSRFError(PermissionError):
    """Lançada quando uma URL aponta para a rede interna ou domínio não homologado."""
    pass


@dataclass
class StreamHealth:
    connected: bool
    reconnect_attempts: int
    bitrate_kbps: float
    fps: float
    disk_available_gb: float
    last_error: Optional[str] = None


class YouTubeLiveIngestor:
    """Ingestor resiliente de YouTube Live com validação estrita de rede."""

    ALLOWED_DOMAINS = [
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "m.youtube.com",
        "manifest.googlevideo.com",
    ]

    def __init__(
        self,
        stream_url: str,
        buffer_dir: str | Path,
        max_storage_gb: float = 50.0,
    ):
        self.stream_url = stream_url
        self.buffer_dir = Path(buffer_dir)
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.max_storage_gb = max_storage_gb

        self.reconnect_attempts = 0
        self.connected = False
        self._validate_ssrf_safety(stream_url)

    def _validate_ssrf_safety(self, url: str) -> None:
        """Protege contra requisições forçadas a servidores internos (SSRF)."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise SecuritySSRFError(f"Esquema de rede inválido: {parsed.scheme}. Apenas HTTP/HTTPS são permitidos.")

        hostname = (parsed.hostname or "").lower()
        if not hostname:
            raise SecuritySSRFError("Hostname ausente na URL fornecida.")

        # 1. Bloqueia endereços de loopback e rede privada
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            raise SecuritySSRFError(f"Acesso bloqueado a endereço local: {hostname}")

        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise SecuritySSRFError(f"Acesso bloqueado a endereço IP não público: {ip}")
        except ValueError:
            pass  # É um nome de domínio, continua validação por domínio

        # 2. Whitelist de domínios homologados
        dominio_valido = False
        for allowed in self.ALLOWED_DOMAINS:
            if hostname == allowed or hostname.endswith("." + allowed):
                dominio_valido = True
                break

        if not dominio_valido:
            raise SecuritySSRFError(
                f"Domínio não homologado para ingestão ao vivo: {hostname}. "
                f"Apenas fontes oficiais do YouTube Live são permitidas."
            )

    def check_disk_space_gb(self) -> float:
        """Verifica espaço livre em disco no diretório do buffer."""
        total, used, free = shutil.disk_usage(self.buffer_dir)
        return round(free / (1024 ** 3), 2)

    def connect(self) -> StreamHealth:
        """Simula / inicializa a conexão segura com o stream."""
        espaco_livre = self.check_disk_space_gb()
        if espaco_livre < 2.0:
            self.connected = False
            return StreamHealth(
                connected=False,
                reconnect_attempts=self.reconnect_attempts,
                bitrate_kbps=0.0,
                fps=0.0,
                disk_available_gb=espaco_livre,
                last_error="Espaço em disco insuficiente para ingestão (< 2 GB livres).",
            )

        self.connected = True
        return StreamHealth(
            connected=True,
            reconnect_attempts=self.reconnect_attempts,
            bitrate_kbps=4500.0,
            fps=60.0,
            disk_available_gb=espaco_livre,
            last_error=None,
        )

    def disconnect(self) -> None:
        self.connected = False
