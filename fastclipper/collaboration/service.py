"""
Serviço de colaboração em equipe, bloqueio otimista, turnos e isolamento multi-tenant.
"""

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from fastclipper.collaboration.models import (
    Atribuicao,
    AuthContext,
    Canal,
    Clipe,
    Comentario,
    EstadoMomento,
    Fonte,
    Momento,
    Organizacao,
    PapelUsuario,
    RegistroAuditoria,
    Usuario,
)

logger = logging.getLogger(__name__)


class TenantAccessViolationError(PermissionError):
    """Lançada quando um usuário tenta acessar recursos de outra organização."""
    pass


class ConcurrencyConflictError(RuntimeError):
    """Lançada quando uma mutação ocorre sobre uma versão desatualizada (bloqueio otimista)."""
    pass


class PermissionDeniedError(PermissionError):
    """Lançada quando um usuário não tem permissão de papel para a ação."""
    pass


@dataclass
class RelatorioPassagemTurno:
    organizacao_id: str
    gerado_em: float
    periodo_inicio: float
    periodo_fim: float
    responsavel_saida_id: str
    total_momentos_periodo: int
    aprovados: int
    rejeitados: int
    pendentes_revisao: List[Dict[str, Any]]
    em_edicao: List[Dict[str, Any]]
    novos: List[Dict[str, Any]]
    alertas_spoiler: List[Dict[str, Any]]
    notas_operacionais: str = ""


class CollaborationService:
    """Motor de orquestração multiusuário e controle de turnos."""

    def __init__(self):
        self._lock = threading.Lock()
        self._organizacoes: Dict[str, Organizacao] = {}
        self._usuarios: Dict[str, Usuario] = {}
        self._canais: Dict[str, Canal] = {}
        self._fontes: Dict[str, Fonte] = {}
        self._momentos: Dict[str, Momento] = {}
        self._comentarios: Dict[str, List[Comentario]] = {}
        self._auditoria: List[RegistroAuditoria] = []
        self._clipes: Dict[str, Clipe] = {}

    # --- Métodos de Cadastro Auxiliar ---

    def register_organization(self, org_id: str, nome: str, slug: str) -> Organizacao:
        with self._lock:
            org = Organizacao(id=org_id, nome=nome, slug=slug)
            self._organizacoes[org_id] = org
            return org

    def register_user(self, user_id: str, org_id: str, nome: str, email: str, papel: PapelUsuario) -> Usuario:
        with self._lock:
            if org_id not in self._organizacoes:
                raise KeyError(f"Organização inexistente: {org_id}")
            user = Usuario(id=user_id, organizacao_id=org_id, nome=nome, email=email, papel=papel)
            self._usuarios[user_id] = user
            return user

    def register_channel(self, auth: AuthContext, canal_id: str, nome: str, plataforma: str, url: str, autorizacao_ref: str) -> Canal:
        with self._lock:
            canal = Canal(
                id=canal_id,
                organizacao_id=auth.organizacao_id,
                nome=nome,
                plataforma=plataforma,
                url=url,
                autorizacao_ref=autorizacao_ref,
            )
            self._canais[canal_id] = canal
            return canal

    def register_source(self, auth: AuthContext, fonte_id: str, canal_id: str, titulo: str, tipo: str, caminho_buffer: str, termo_autorizacao: str) -> Fonte:
        with self._lock:
            canal = self._canais.get(canal_id)
            if not canal or canal.organizacao_id != auth.organizacao_id:
                raise TenantAccessViolationError("Canal não pertence à organização autenticada.")
            fonte = Fonte(
                id=fonte_id,
                canal_id=canal_id,
                organizacao_id=auth.organizacao_id,
                titulo=titulo,
                tipo=tipo,
                caminho_buffer=caminho_buffer,
                termo_autorizacao=termo_autorizacao,
            )
            self._fontes[fonte_id] = fonte
            return fonte

    # --- Gestão de Momentos & Bloqueio Otimista ---

    def create_moment(
        self,
        auth: AuthContext,
        fonte_id: str,
        inicio_s: float,
        fim_s: float,
        timestamp_fonte: float,
        descricao: str,
        spoiler: bool = False,
        autorizacao_confirmada: bool = True,
    ) -> Momento:
        if not auth.can_edit():
            raise PermissionDeniedError("Papel de usuário não possui permissão para criar momentos.")

        with self._lock:
            fonte = self._fontes.get(fonte_id)
            if not fonte or fonte.organizacao_id != auth.organizacao_id:
                raise TenantAccessViolationError("Fonte não encontrada ou de outra organização.")

            momento_id = f"mom_{uuid.uuid4().hex[:8]}"
            momento = Momento(
                id=momento_id,
                organizacao_id=auth.organizacao_id,
                fonte_id=fonte_id,
                inicio_s=round(inicio_s, 2),
                fim_s=round(fim_s, 2),
                timestamp_fonte=round(timestamp_fonte, 2),
                descricao=descricao,
                spoiler=spoiler,
                autorizacao_confirmada=autorizacao_confirmada,
                estado=EstadoMomento.NOVO,
                versao=1,
            )
            self._momentos[momento_id] = momento
            self._registrar_auditoria_locked(auth, momento_id, "CRIAR_MOMENTO", None, momento.to_dict())
            return momento

    def get_moment(self, auth: AuthContext, momento_id: str) -> Momento:
        with self._lock:
            momento = self._momentos.get(momento_id)
            if not momento or momento.organizacao_id != auth.organizacao_id:
                raise TenantAccessViolationError(f"Momento {momento_id} não encontrado na organização.")
            return momento

    def assign_moment(
        self,
        auth: AuthContext,
        momento_id: str,
        target_user_id: str,
        expected_version: int,
    ) -> Momento:
        """Atribuição exclusiva com versão otimista."""
        if not auth.can_edit():
            raise PermissionDeniedError("Papel não autorizado para atribuir tarefas.")

        with self._lock:
            momento = self._momentos.get(momento_id)
            if not momento or momento.organizacao_id != auth.organizacao_id:
                raise TenantAccessViolationError("Acesso negado ao momento.")

            target_user = self._usuarios.get(target_user_id)
            if not target_user or target_user.organizacao_id != auth.organizacao_id:
                raise TenantAccessViolationError("Usuário alvo pertence a outra organização.")

            # Validação do bloqueio otimista
            if momento.versao != expected_version:
                raise ConcurrencyConflictError(
                    f"Conflito de versão no momento {momento_id}: esperado v{expected_version}, atual v{momento.versao}. "
                    f"Outro operador pode ter assumido ou alterado esta tarefa."
                )

            # Se já estiver atribuído a outro operador em edição ativa
            if momento.responsavel_id and momento.responsavel_id != target_user_id and momento.estado == EstadoMomento.EM_EDICAO:
                if not auth.is_admin_or_owner():
                    raise ConcurrencyConflictError(
                        f"Tarefa já está sob edição exclusiva de outro operador ({momento.responsavel_id}). "
                        f"Apenas um administrador pode reatribuir forçadamente."
                    )

            dados_antigos = momento.to_dict()
            momento.responsavel_id = target_user_id
            momento.estado = EstadoMomento.EM_EDICAO
            momento.versao += 1
            momento.atualizado_em = time.time()

            self._registrar_auditoria_locked(
                auth,
                momento_id,
                "ATRIBUIR_MOMENTO",
                dados_antigos,
                momento.to_dict(),
            )
            return momento

    def update_moment_boundaries(
        self,
        auth: AuthContext,
        momento_id: str,
        expected_version: int,
        new_start_s: float,
        new_end_s: float,
        new_description: Optional[str] = None,
    ) -> Momento:
        """Ajusta limites de corte do momento com bloqueio otimista."""
        if not auth.can_edit():
            raise PermissionDeniedError("Papel não autorizado para editar limites.")

        if new_start_s < 0 or new_end_s <= new_start_s:
            raise ValueError("Limites de tempo inválidos.")

        with self._lock:
            momento = self._momentos.get(momento_id)
            if not momento or momento.organizacao_id != auth.organizacao_id:
                raise TenantAccessViolationError("Acesso negado ao momento.")

            if momento.versao != expected_version:
                raise ConcurrencyConflictError(
                    f"Conflito de versão no momento {momento_id}: esperado v{expected_version}, atual v{momento.versao}."
                )

            dados_antigos = momento.to_dict()
            momento.inicio_s = round(new_start_s, 2)
            momento.fim_s = round(new_end_s, 2)
            if new_description is not None:
                momento.descricao = new_description

            momento.versao += 1
            momento.atualizado_em = time.time()

            self._registrar_auditoria_locked(
                auth,
                momento_id,
                "AJUSTAR_LIMITES",
                dados_antigos,
                momento.to_dict(),
            )
            return momento

    def transition_state(
        self,
        auth: AuthContext,
        momento_id: str,
        new_state: EstadoMomento,
        expected_version: int,
    ) -> Momento:
        """Transiciona o estado do momento (ex: EM_EDICAO -> EM_REVISAO -> APROVADO)."""
        if new_state in (EstadoMomento.APROVADO, EstadoMomento.REJEITADO) and not auth.can_approve():
            raise PermissionDeniedError("Papel não autorizado para aprovar ou rejeitar momentos.")

        with self._lock:
            momento = self._momentos.get(momento_id)
            if not momento or momento.organizacao_id != auth.organizacao_id:
                raise TenantAccessViolationError("Acesso negado ao momento.")

            if momento.versao != expected_version:
                raise ConcurrencyConflictError(
                    f"Conflito de versão no momento {momento_id}: esperado v{expected_version}, atual v{momento.versao}."
                )

            dados_antigos = momento.to_dict()
            momento.estado = new_state
            momento.versao += 1
            momento.atualizado_em = time.time()

            self._registrar_auditoria_locked(
                auth,
                momento_id,
                f"TRANSICAO_ESTADO_{new_state.value.upper()}",
                dados_antigos,
                momento.to_dict(),
            )
            return momento

    def add_comment(self, auth: AuthContext, momento_id: str, texto: str) -> Comentario:
        with self._lock:
            momento = self._momentos.get(momento_id)
            if not momento or momento.organizacao_id != auth.organizacao_id:
                raise TenantAccessViolationError("Acesso negado ao momento.")

            comentario = Comentario(
                id=f"com_{uuid.uuid4().hex[:8]}",
                momento_id=momento_id,
                usuario_id=auth.usuario_id,
                texto=texto,
            )
            self._comentarios.setdefault(momento_id, []).append(comentario)
            self._registrar_auditoria_locked(
                auth,
                momento_id,
                "ADICIONAR_COMENTARIO",
                None,
                {"texto": texto, "comentario_id": comentario.id},
            )
            return comentario

    def list_moments(
        self,
        auth: AuthContext,
        canal_id: Optional[str] = None,
        responsavel_id: Optional[str] = None,
        estado: Optional[EstadoMomento] = None,
        apenas_spoiler: bool = False,
    ) -> List[Momento]:
        with self._lock:
            resultado = []
            for m in self._momentos.values():
                if m.organizacao_id != auth.organizacao_id:
                    continue
                if canal_id:
                    fonte = self._fontes.get(m.fonte_id)
                    if not fonte or fonte.canal_id != canal_id:
                        continue
                if responsavel_id and m.responsavel_id != responsavel_id:
                    continue
                if estado and m.estado != estado:
                    continue
                if apenas_spoiler and not m.spoiler:
                    continue
                resultado.append(m)
            return sorted(resultado, key=lambda x: x.timestamp_fonte)

    def generate_shift_handoff(
        self,
        auth: AuthContext,
        periodo_inicio: float,
        periodo_fim: float,
        notas_operacionais: str = "",
        canal_id: Optional[str] = None,
    ) -> RelatorioPassagemTurno:
        """Gera o relatório estruturado de passagem de turno com filtros e pendências."""
        with self._lock:
            momentos_org = [
                m for m in self._momentos.values()
                if m.organizacao_id == auth.organizacao_id and periodo_inicio <= m.criado_em <= periodo_fim
            ]

            if canal_id:
                momentos_org = [
                    m for m in momentos_org
                    if self._fontes.get(m.fonte_id) and self._fontes[m.fonte_id].canal_id == canal_id
                ]

            aprovados = sum(1 for m in momentos_org if m.estado == EstadoMomento.APROVADO)
            rejeitados = sum(1 for m in momentos_org if m.estado == EstadoMomento.REJEITADO)

            pendentes_rev = [m.to_dict() for m in momentos_org if m.estado == EstadoMomento.EM_REVISAO]
            em_edicao = [m.to_dict() for m in momentos_org if m.estado == EstadoMomento.EM_EDICAO]
            novos = [m.to_dict() for m in momentos_org if m.estado == EstadoMomento.NOVO]
            spoilers = [m.to_dict() for m in momentos_org if m.spoiler]

            relatorio = RelatorioPassagemTurno(
                organizacao_id=auth.organizacao_id,
                gerado_em=time.time(),
                periodo_inicio=periodo_inicio,
                periodo_fim=periodo_fim,
                responsavel_saida_id=auth.usuario_id,
                total_momentos_periodo=len(momentos_org),
                aprovados=aprovados,
                rejeitados=rejeitados,
                pendentes_revisao=pendentes_rev,
                em_edicao=em_edicao,
                novos=novos,
                alertas_spoiler=spoilers,
                notas_operacionais=notas_operacionais,
            )
            return relatorio

    def get_audit_trail(self, auth: AuthContext, momento_id: Optional[str] = None) -> List[RegistroAuditoria]:
        with self._lock:
            logs = [
                log for log in self._auditoria
                if log.organizacao_id == auth.organizacao_id
                and (momento_id is None or log.momento_id == momento_id)
            ]
            return sorted(logs, key=lambda l: l.timestamp)

    def _registrar_auditoria_locked(
        self,
        auth: AuthContext,
        momento_id: str,
        acao: str,
        anteriores: Optional[dict],
        novos: Optional[dict],
    ) -> None:
        reg = RegistroAuditoria(
            id=f"aud_{uuid.uuid4().hex[:8]}",
            organizacao_id=auth.organizacao_id,
            momento_id=momento_id,
            usuario_id=auth.usuario_id,
            acao=acao,
            dados_anteriores=anteriores,
            dados_novos=novos,
            timestamp=time.time(),
        )
        self._auditoria.append(reg)
