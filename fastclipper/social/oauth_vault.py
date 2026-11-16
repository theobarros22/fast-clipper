"""
PROTÓTIPO CONGELADO — Cofre de tokens OAuth2, publicação idempotente e Kill Switch.

Aviso: módulo congelado (Gate C do plano-mestre-v3). O método de ofuscação atual usa
base64 + SHA-256 (não é criptografia autenticada) e NÃO deve armazenar tokens reais de produção
até ser substituído por AES-GCM/DPAPI/KMS.
"""

import base64
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SocialPlatform(str, Enum):
    YOUTUBE_SHORTS = "youtube_shorts"
    TIKTOK = "tiktok"
    INSTAGRAM_REELS = "instagram_reels"


class RateLimitExceededError(RuntimeError):
    pass


class EmergencyKillSwitchActiveError(PermissionError):
    pass


class RevokedAccountError(PermissionError):
    pass


@dataclass
class SocialAccount:
    account_id: str
    org_id: str
    platform: SocialPlatform
    account_name: str
    encrypted_token: str
    scopes: List[str]
    autopost_enabled: bool = False  # Estritamente OPT-IN por padrão
    daily_post_limit: int = 15
    posts_today: int = 0
    is_revoked: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "org_id": self.org_id,
            "platform": self.platform.value,
            "account_name": self.account_name,
            "scopes": self.scopes,
            "autopost_enabled": self.autopost_enabled,
            "daily_post_limit": self.daily_post_limit,
            "posts_today": self.posts_today,
            "is_revoked": self.is_revoked,
            "created_at": self.created_at,
        }


class OAuthTokenVault:
    """Cofre criptográfico e despachante controlado de publicação social."""

    def __init__(self, secret_key: str = "fastclipper_secret_salt_2026"):
        self._secret_key = secret_key.encode("utf-8")
        self._accounts: Dict[str, SocialAccount] = {}
        self._published_idempotency_keys: Dict[str, Dict[str, Any]] = {}
        self._kill_switches: Dict[str, bool] = {}  # org_id -> bool
        self._lock = threading.Lock()

    def _encrypt(self, token: str) -> str:
        # Obfuscação criptográfica leve para isolamento em memória/disco
        h = hashlib.sha256(self._secret_key + token.encode("utf-8")).digest()
        dados = base64.b64encode(token.encode("utf-8") + b"::" + h).decode("ascii")
        return dados

    def set_emergency_kill_switch(self, org_id: str, active: bool) -> None:
        """Ativa ou desativa o botão de emergência operacional para suspender a fila de postagens."""
        with self._lock:
            self._kill_switches[org_id] = bool(active)
            if active:
                logger.warning("🚨 KILL SWITCH DE EMERGÊNCIA ATIVADO PARA A ORGANIZAÇÃO: %s", org_id)

    def is_kill_switch_active(self, org_id: str) -> bool:
        with self._lock:
            return self._kill_switches.get(org_id, False)

    def register_account(
        self,
        org_id: str,
        platform: SocialPlatform,
        account_name: str,
        access_token: str,
        scopes: List[str],
        autopost_enabled: bool = False,
        daily_limit: int = 15,
    ) -> SocialAccount:
        with self._lock:
            account_id = f"acc_{uuid.uuid4().hex[:8]}"
            account = SocialAccount(
                account_id=account_id,
                org_id=org_id,
                platform=platform,
                account_name=account_name,
                encrypted_token=self._encrypt(access_token),
                scopes=scopes,
                autopost_enabled=bool(autopost_enabled),
                daily_post_limit=daily_limit,
            )
            self._accounts[account_id] = account
            return account

    def revoke_account(self, org_id: str, account_id: str) -> bool:
        """Revoga imediatamente as credenciais de uma conta."""
        with self._lock:
            acc = self._accounts.get(account_id)
            if not acc or acc.org_id != org_id:
                raise KeyError("Conta não encontrada na organização.")
            acc.is_revoked = True
            acc.autopost_enabled = False
            acc.encrypted_token = ""
            logger.info("Conta social %s revogada com sucesso.", account_id)
            return True

    def publish_clip(
        self,
        org_id: str,
        account_id: str,
        clip_path: str | Path,
        title: str,
        idempotency_key: str,
        is_automatic_post: bool = False,
    ) -> Dict[str, Any]:
        """Publica um clipe em ambiente controlado respeitando limites, revogação e idempotência."""
        # 1. Checagem do Kill Switch
        if self.is_kill_switch_active(org_id):
            raise EmergencyKillSwitchActiveError("Publicação suspensa: Kill Switch de emergência está ativado.")

        with self._lock:
            # 2. Checagem de Idempotência (evita postagem duplicada após timeout ou retry de worker)
            if idempotency_key in self._published_idempotency_keys:
                logger.info("Chave idempotente já publicada anteriormente: %s", idempotency_key)
                return self._published_idempotency_keys[idempotency_key]

            acc = self._accounts.get(account_id)
            if not acc or acc.org_id != org_id:
                raise KeyError("Conta social não pertence a esta organização.")

            if acc.is_revoked:
                raise RevokedAccountError("Tentativa de publicação em conta com token revogado.")

            # 3. Validação de Autopost (se a publicação partiu de robô automático, exige opt-in)
            if is_automatic_post and not acc.autopost_enabled:
                raise PermissionError(
                    f"Autopost desabilitado para a conta {acc.account_name}. "
                    f"Publicação automática exige autorização opt-in explícita nas configurações."
                )

            # 4. Checagem de Limite Diário de Postagens
            if acc.posts_today >= acc.daily_post_limit:
                raise RateLimitExceededError(
                    f"Limite diário de postagens atingido para {acc.account_name} ({acc.daily_post_limit} posts/dia)."
                )

            # 5. Validação de Arquivo
            arquivo = Path(clip_path)
            if not arquivo.is_file():
                raise FileNotFoundError(f"Vídeo para publicação não encontrado: {clip_path}")

            # Simulação de publicação oficial bem-sucedida
            post_id = f"post_{uuid.uuid4().hex[:10]}"
            url_publicada = f"https://{acc.platform.value}.com/watch?v={post_id}"

            resultado = {
                "ok": True,
                "post_id": post_id,
                "platform": acc.platform.value,
                "account_name": acc.account_name,
                "url": url_publicada,
                "published_at": time.time(),
                "idempotency_key": idempotency_key,
                "status": "publicado",
            }

            acc.posts_today += 1
            self._published_idempotency_keys[idempotency_key] = resultado
            return resultado
