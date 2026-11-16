"""
Suite de Testes Automatizados para o Marco M3 (Mapa de Momentos e Turnos).

Cobre:
1. Concorrência na atribuição com bloqueio otimista (prevenção de conflito de operadores).
2. Isolamento multi-tenant estrito entre organizações (proibição de acesso cruzado).
3. Ciclo de vida de estados com versionamento atômico (novo -> em_edicao -> em_revisao -> aprovado).
4. Relatório estruturado de passagem de turno com filtros, pendências e alertas de spoiler.
5. Trilha imutável de auditoria com histórico completo de alterações e responsáveis.
6. Controle de permissões por papel de usuário (RBAC).
"""

import time
import pytest

from fastclipper.collaboration.models import (
    AuthContext,
    EstadoMomento,
    PapelUsuario,
)
from fastclipper.collaboration.service import (
    CollaborationService,
    ConcurrencyConflictError,
    PermissionDeniedError,
    TenantAccessViolationError,
)


@pytest.fixture
def service():
    svc = CollaborationService()
    # Cria duas organizações independentes para testes de multi-tenant
    svc.register_organization("org_alfa", "Equipe Alfa Produções", "alfa-prod")
    svc.register_organization("org_beta", "Equipe Beta Clipes", "beta-clipes")

    # Usuários Org Alfa
    svc.register_user("usr_alfa_owner", "org_alfa", "Carlos Dono", "carlos@alfa.com", PapelUsuario.PROPRIETARIO)
    svc.register_user("usr_alfa_ed1", "org_alfa", "Ana Editora 1", "ana@alfa.com", PapelUsuario.EDITOR)
    svc.register_user("usr_alfa_ed2", "org_alfa", "Bruno Editor 2", "bruno@alfa.com", PapelUsuario.EDITOR)
    svc.register_user("usr_alfa_rev", "org_alfa", "Clara Revisora", "clara@alfa.com", PapelUsuario.REVISOR)

    # Usuários Org Beta
    svc.register_user("usr_beta_ed1", "org_beta", "Daniel Editor", "daniel@beta.com", PapelUsuario.EDITOR)

    return svc


@pytest.fixture
def setup_alfa_stream(service):
    auth_owner = AuthContext("usr_alfa_owner", "org_alfa", PapelUsuario.PROPRIETARIO)
    canal = service.register_channel(
        auth=auth_owner,
        canal_id="canal_gtav_alfa",
        nome="Streamer Oficial GTA",
        plataforma="youtube",
        url="https://youtube.com/@gtav_streamer",
        autorizacao_ref="TERMO-AUTH-ALFA-001",
    )
    fonte = service.register_source(
        auth=auth_owner,
        fonte_id="fonte_live_01",
        canal_id=canal.id,
        titulo="GTA V Sessão 01",
        tipo="live_simulada",
        caminho_buffer="data/buffer_alfa_01",
        termo_autorizacao="TERMO-AUTH-ALFA-001",
    )
    return auth_owner, canal, fonte


def test_concorrencia_na_atribuicao_com_bloqueio_otimista(service, setup_alfa_stream):
    auth_owner, canal, fonte = setup_alfa_stream
    auth_ed1 = AuthContext("usr_alfa_ed1", "org_alfa", PapelUsuario.EDITOR)
    auth_ed2 = AuthContext("usr_alfa_ed2", "org_alfa", PapelUsuario.EDITOR)

    # Cria momento na versão 1
    momento = service.create_moment(
        auth=auth_ed1,
        fonte_id=fonte.id,
        inicio_s=10.0,
        fim_s=40.0,
        timestamp_fonte=25.0,
        descricao="Momento de perseguição policial",
    )
    assert momento.versao == 1
    assert momento.estado == EstadoMomento.NOVO

    # Editor 1 assume a tarefa esperando versão 1 -> Sucesso!
    service.assign_moment(
        auth=auth_ed1,
        momento_id=momento.id,
        target_user_id="usr_alfa_ed1",
        expected_version=1,
    )
    mom_atualizado = service.get_moment(auth_ed1, momento.id)
    assert mom_atualizado.versao == 2
    assert mom_atualizado.responsavel_id == "usr_alfa_ed1"
    assert mom_atualizado.estado == EstadoMomento.EM_EDICAO

    # Editor 2 tenta assumir com a versão defasada (v1) -> Conflito de versão!
    with pytest.raises(ConcurrencyConflictError) as exc_info:
        service.assign_moment(
            auth=auth_ed2,
            momento_id=momento.id,
            target_user_id="usr_alfa_ed2",
            expected_version=1,
        )
    assert "Conflito de versão" in str(exc_info.value)

    # Editor 2 tenta assumir mesmo sabendo que está v2, mas sem ser admin -> Conflito de tarefa em edição ativa!
    with pytest.raises(ConcurrencyConflictError) as exc_info_edit:
        service.assign_moment(
            auth=auth_ed2,
            momento_id=momento.id,
            target_user_id="usr_alfa_ed2",
            expected_version=2,
        )
    assert "sob edição exclusiva" in str(exc_info_edit.value)

    # Administrador/Owner pode reatribuir forçadamente com a versão correta
    service.assign_moment(
        auth=auth_owner,
        momento_id=momento.id,
        target_user_id="usr_alfa_ed2",
        expected_version=2,
    )
    mom_reatribuido = service.get_moment(auth_owner, momento.id)
    assert mom_reatribuido.versao == 3
    assert mom_reatribuido.responsavel_id == "usr_alfa_ed2"


def test_isolamento_multi_tenant_estrito(service, setup_alfa_stream):
    auth_owner_alfa, _, fonte_alfa = setup_alfa_stream
    auth_beta = AuthContext("usr_beta_ed1", "org_beta", PapelUsuario.EDITOR)

    # Cria momento na Org Alfa
    momento_alfa = service.create_moment(
        auth=auth_owner_alfa,
        fonte_id=fonte_alfa.id,
        inicio_s=15.0,
        fim_s=45.0,
        timestamp_fonte=30.0,
        descricao="Momento Confidencial Alfa",
    )

    # Usuário da Org Beta tenta ler o momento da Org Alfa -> Acesso Negado!
    with pytest.raises(TenantAccessViolationError):
        service.get_moment(auth=auth_beta, momento_id=momento_alfa.id)

    # Usuário da Org Beta tenta editar o momento da Org Alfa -> Acesso Negado!
    with pytest.raises(TenantAccessViolationError):
        service.update_moment_boundaries(
            auth=auth_beta,
            momento_id=momento_alfa.id,
            expected_version=1,
            new_start_s=20.0,
            new_end_s=40.0,
        )

    # Listagem de momentos para Org Beta não deve retornar nenhum item de Alfa
    momentos_beta = service.list_moments(auth=auth_beta)
    assert len(momentos_beta) == 0


def test_ciclo_completo_de_estados_e_versionamento(service, setup_alfa_stream):
    auth_owner, _, fonte = setup_alfa_stream
    auth_ed = AuthContext("usr_alfa_ed1", "org_alfa", PapelUsuario.EDITOR)

    # 1. Criação (v1, NOVO)
    m = service.create_moment(
        auth=auth_ed,
        fonte_id=fonte.id,
        inicio_s=5.0,
        fim_s=25.0,
        timestamp_fonte=15.0,
        descricao="Queda de moto",
    )
    assert m.versao == 1
    assert m.estado == EstadoMomento.NOVO

    # 2. Atribuição (v2, EM_EDICAO)
    m2 = service.assign_moment(auth_ed, m.id, target_user_id="usr_alfa_ed1", expected_version=1)
    assert m2.versao == 2
    assert m2.estado == EstadoMomento.EM_EDICAO

    # 3. Ajuste de Limites (v3, EM_EDICAO)
    m3 = service.update_moment_boundaries(auth_ed, m.id, expected_version=2, new_start_s=8.0, new_end_s=24.0)
    assert m3.versao == 3
    assert m3.inicio_s == 8.0

    # 4. Envio para Revisão (v4, EM_REVISAO)
    m4 = service.transition_state(auth_ed, m.id, new_state=EstadoMomento.EM_REVISAO, expected_version=3)
    assert m4.versao == 4
    assert m4.estado == EstadoMomento.EM_REVISAO

    # 5. Aprovação Final (v5, APROVADO)
    m5 = service.transition_state(auth_owner, m.id, new_state=EstadoMomento.APROVADO, expected_version=4)
    assert m5.versao == 5
    assert m5.estado == EstadoMomento.APROVADO


def test_passagem_de_turno_com_pendencias_e_spoilers(service, setup_alfa_stream):
    auth_owner, canal, fonte = setup_alfa_stream
    auth_ed1 = AuthContext("usr_alfa_ed1", "org_alfa", PapelUsuario.EDITOR)

    t_inicio_turno = time.time() - 3600  # 1 hora atrás

    # Cria 4 momentos com estados variados
    m1 = service.create_moment(auth_ed1, fonte.id, 10, 30, 20, "Momento Aprovado")
    service.transition_state(auth_owner, m1.id, EstadoMomento.APROVADO, expected_version=1)

    m2 = service.create_moment(auth_ed1, fonte.id, 40, 60, 50, "Momento Pendente de Revisão")
    service.transition_state(auth_owner, m2.id, EstadoMomento.EM_REVISAO, expected_version=1)

    m3 = service.create_moment(auth_ed1, fonte.id, 70, 90, 80, "Momento Sob Edição")
    service.assign_moment(auth_ed1, m3.id, "usr_alfa_ed1", expected_version=1)

    m4 = service.create_moment(auth_ed1, fonte.id, 100, 120, 110, "Momento com Revelação de Final", spoiler=True)

    t_fim_turno = time.time() + 10

    # Gera relatório de passagem de turno
    relatorio = service.generate_shift_handoff(
        auth=auth_ed1,
        periodo_inicio=t_inicio_turno,
        periodo_fim=t_fim_turno,
        notas_operacionais="Atenção para o clipe com spoiler no final da perseguição.",
        canal_id=canal.id,
    )

    assert relatorio.total_momentos_periodo == 4
    assert relatorio.aprovados == 1
    assert len(relatorio.pendentes_revisao) == 1
    assert len(relatorio.em_edicao) == 1
    assert len(relatorio.novos) == 1
    assert len(relatorio.alertas_spoiler) == 1
    assert relatorio.alertas_spoiler[0]["id"] == m4.id
    assert "spoiler" in relatorio.notas_operacionais


def test_historico_de_auditoria_imutavel(service, setup_alfa_stream):
    auth_owner, _, fonte = setup_alfa_stream
    auth_ed = AuthContext("usr_alfa_ed1", "org_alfa", PapelUsuario.EDITOR)

    m = service.create_moment(auth_ed, fonte.id, 10, 30, 20, "Momento Auditado")
    service.assign_moment(auth_ed, m.id, "usr_alfa_ed1", expected_version=1)
    service.add_comment(auth_ed, m.id, "Ajustei o corte para remover ruído inicial")
    service.update_moment_boundaries(auth_ed, m.id, expected_version=2, new_start_s=12, new_end_s=28)

    # Consulta trilha de auditoria do momento
    trilha = service.get_audit_trail(auth=auth_ed, momento_id=m.id)
    assert len(trilha) == 4

    acoes = [log.acao for log in trilha]
    assert acoes == ["CRIAR_MOMENTO", "ATRIBUIR_MOMENTO", "ADICIONAR_COMENTARIO", "AJUSTAR_LIMITES"]

    for log in trilha:
        assert log.organizacao_id == "org_alfa"
        assert log.usuario_id == "usr_alfa_ed1"
        assert log.timestamp > 0


def test_permissao_de_papeis(service, setup_alfa_stream):
    _, _, fonte = setup_alfa_stream
    auth_rev = AuthContext("usr_alfa_rev", "org_alfa", PapelUsuario.REVISOR)

    # Revisor não tem permissão para criar momentos
    with pytest.raises(PermissionDeniedError):
        service.create_moment(auth_rev, fonte.id, 10, 20, 15, "Momento Invalido")
