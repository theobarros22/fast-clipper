"""
Modelos de domínio para colaboração multiusuário, turnos e auditoria.

Contém as entidades organizacao, usuario, canal, fonte, momento, atribuicao,
comentario, registro de auditoria e clipe, com suporte a bloqueio otimista.
"""

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class PapelUsuario(str, Enum):
    PROPRIETARIO = "proprietario"
    ADMINISTRADOR = "administrador"
    EDITOR = "editor"
    REVISOR = "revisor"
    PUBLICADOR = "publicador"


class EstadoMomento(str, Enum):
    NOVO = "novo"
    EM_EDICAO = "em_edicao"
    EM_REVISAO = "em_revisao"
    APROVADO = "aprovado"
    REJEITADO = "rejeitado"


@dataclass
class AuthContext:
    """Contexto de autenticação do usuário na requisição."""
    usuario_id: str
    organizacao_id: str
    papel: PapelUsuario

    def is_admin_or_owner(self) -> bool:
        return self.papel in (PapelUsuario.ADMINISTRADOR, PapelUsuario.PROPRIETARIO)

    def can_edit(self) -> bool:
        return self.papel in (PapelUsuario.PROPRIETARIO, PapelUsuario.ADMINISTRADOR, PapelUsuario.EDITOR)

    def can_approve(self) -> bool:
        return self.papel in (PapelUsuario.PROPRIETARIO, PapelUsuario.ADMINISTRADOR, PapelUsuario.EDITOR, PapelUsuario.REVISOR)


@dataclass
class Organizacao:
    id: str
    nome: str
    slug: str
    criado_em: float = field(default_factory=time.time)


@dataclass
class Usuario:
    id: str
    organizacao_id: str
    nome: str
    email: str
    papel: PapelUsuario
    criado_em: float = field(default_factory=time.time)


@dataclass
class Canal:
    id: str
    organizacao_id: str
    nome: str
    plataforma: str  # youtube, twitch, kick
    url: str
    autorizacao_ref: str
    criado_em: float = field(default_factory=time.time)


@dataclass
class Fonte:
    id: str
    canal_id: str
    organizacao_id: str
    titulo: str
    tipo: str  # live_simulada, live_real, vod
    caminho_buffer: str
    termo_autorizacao: str
    inicio_utc: float = field(default_factory=time.time)


@dataclass
class Momento:
    id: str
    organizacao_id: str
    fonte_id: str
    inicio_s: float
    fim_s: float
    timestamp_fonte: float
    descricao: str
    spoiler: bool = False
    autorizacao_confirmada: bool = True
    estado: EstadoMomento = EstadoMomento.NOVO
    responsavel_id: Optional[str] = None
    versao: int = 1
    criado_em: float = field(default_factory=time.time)
    atualizado_em: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["estado"] = self.estado.value
        return d


@dataclass
class Atribuicao:
    id: str
    momento_id: str
    usuario_id: str
    atribuido_em: float = field(default_factory=time.time)


@dataclass
class Comentario:
    id: str
    momento_id: str
    usuario_id: str
    texto: str
    criado_em: float = field(default_factory=time.time)


@dataclass
class RegistroAuditoria:
    id: str
    organizacao_id: str
    momento_id: str
    usuario_id: str
    acao: str
    dados_anteriores: Optional[Dict[str, Any]] = None
    dados_novos: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class Clipe:
    id: str
    organizacao_id: str
    momento_id: str
    versao: int
    caminho_arquivo: str
    duracao_s: float
    aprovado_por_id: str
    criado_em: float = field(default_factory=time.time)
    publicado_em: Optional[float] = None
