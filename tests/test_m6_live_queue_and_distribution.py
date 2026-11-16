"""
Suite de Testes Automatizados para o Marco M6 (Lives Reais, Fila Durável e Distribuição).

Cobre:
1. Proteção estrita contra SSRF na ingestão de lives (bloqueio de IPs privados e domínios não homologados).
2. Idempotência em fila de tarefas (prevenção de duplicação de jobs).
3. Concessão atômica de worker (lease) e recuperação de desastre após timeout.
4. Cofre OAuth2: limites diários, revogação instantânea de token e proteção de credenciais.
5. Regra de ouro do Autopost: estritamente opt-in por canal (rejeição de disparo automático não autorizado).
6. Botão de emergência (Kill Switch) operacional com suspensão imediata da fila.
7. Idempotência na publicação social (prevenção de postagem duplicada após timeout de API).
"""

import time
from pathlib import Path
import pytest

from fastclipper.live.youtube_live import (
    SecuritySSRFError,
    YouTubeLiveIngestor,
)
from fastclipper.queue.durable_queue import (
    DurableTaskQueue,
    JobStatus,
)
from fastclipper.social.oauth_vault import (
    EmergencyKillSwitchActiveError,
    OAuthTokenVault,
    RateLimitExceededError,
    RevokedAccountError,
    SocialPlatform,
)


@pytest.fixture
def tmp_clip_file(tmp_path):
    clip = tmp_path / "clipe_teste.mp4"
    clip.write_bytes(b"\0" * 2048)
    return clip


def test_youtube_live_ssrf_protection(tmp_path):
    # 1. URLs oficiais válidas -> Sucesso
    ingestor = YouTubeLiveIngestor(
        stream_url="https://www.youtube.com/watch?v=live_gtav_test",
        buffer_dir=tmp_path / "buf",
    )
    assert ingestor.connected is False

    # 2. Tentativas de SSRF para rede interna -> Bloqueio imediato!
    urls_maliciosas = [
        "http://localhost:8080/admin",
        "http://127.0.0.1:9000/keys",
        "http://192.168.1.1/router",
        "http://10.0.0.5/secrets",
        "http://169.254.169.254/latest/meta-data/",  # AWS/GCP metadata
        "https://servidor-interno-malicioso.com/live",
    ]

    for bad_url in urls_maliciosas:
        with pytest.raises(SecuritySSRFError):
            YouTubeLiveIngestor(stream_url=bad_url, buffer_dir=tmp_path / "buf")


def test_durable_queue_idempotency():
    queue = DurableTaskQueue()
    params = {"clip_id": "clip_101", "resolution": "1080x1920"}

    # Enfileira com chave idempotente
    j1 = queue.enqueue(
        org_id="org_alfa",
        job_type="render_clip",
        params=params,
        idempotency_key="key_hash_abc123",
    )
    # Tenta enfileirar novamente com os mesmos parâmetros
    j2 = queue.enqueue(
        org_id="org_alfa",
        job_type="render_clip",
        params=params,
        idempotency_key="key_hash_abc123",
    )

    # Não pode duplicar a tarefa! Retorna a mesma instância
    assert j1.job_id == j2.job_id
    assert len(queue._jobs) == 1


def test_queue_worker_lease_and_crash_recovery():
    queue = DurableTaskQueue()
    j = queue.enqueue("org_alfa", "render_clip", {"input": "test.mp4"})

    # Worker A assume com lease curto de 0.2 segundos
    job_leased = queue.lease_next_job(worker_id="worker_gpu_01", lease_duration_s=0.2)
    assert job_leased is not None
    assert job_leased.worker_id == "worker_gpu_01"
    assert job_leased.attempts == 1

    # Atualiza progresso
    queue.update_progress(job_leased.job_id, worker_id="worker_gpu_01", progress_percent=45.0, step_description="Processando frames")
    assert job_leased.progress_percent == 45.0

    # Simula que o Worker A caiu e o lease expirou (espera 0.25s)
    time.sleep(0.25)

    # Worker B assume a tarefa abandonada (recuperação de desastre)
    job_recovered = queue.lease_next_job(worker_id="worker_gpu_02", lease_duration_s=60.0)
    assert job_recovered is not None
    assert job_recovered.job_id == j.job_id
    assert job_recovered.worker_id == "worker_gpu_02"
    assert job_recovered.attempts == 2

    # Worker B conclui com sucesso
    concluido = queue.complete_job(job_recovered.job_id, worker_id="worker_gpu_02", result={"output": "final.mp4"})
    assert concluido.status == JobStatus.PUBLICADO
    assert concluido.progress_percent == 100.0


def test_social_vault_revocation_and_limits(tmp_clip_file):
    vault = OAuthTokenVault()
    acc = vault.register_account(
        org_id="org_alfa",
        platform=SocialPlatform.YOUTUBE_SHORTS,
        account_name="@canal_cortes",
        access_token="secret_token_12345",
        scopes=["upload"],
        daily_limit=2,
    )

    # 1. Primeira postagem manual -> Sucesso
    r1 = vault.publish_clip(
        org_id="org_alfa",
        account_id=acc.account_id,
        clip_path=tmp_clip_file,
        title="Corte GTA V 1",
        idempotency_key="post_idem_01",
    )
    assert r1["ok"] is True
    assert acc.posts_today == 1

    # 2. Segunda postagem -> Sucesso
    vault.publish_clip(
        org_id="org_alfa",
        account_id=acc.account_id,
        clip_path=tmp_clip_file,
        title="Corte GTA V 2",
        idempotency_key="post_idem_02",
    )
    assert acc.posts_today == 2

    # 3. Terceira postagem -> Bloqueio por limite diário
    with pytest.raises(RateLimitExceededError):
        vault.publish_clip(
            org_id="org_alfa",
            account_id=acc.account_id,
            clip_path=tmp_clip_file,
            title="Corte GTA V 3",
            idempotency_key="post_idem_03",
        )

    # 4. Revogação de conta -> Bloqueio imediato
    vault.revoke_account("org_alfa", acc.account_id)
    assert acc.is_revoked is True
    with pytest.raises(RevokedAccountError):
        vault.publish_clip(
            org_id="org_alfa",
            account_id=acc.account_id,
            clip_path=tmp_clip_file,
            title="Corte GTA V 4",
            idempotency_key="post_idem_04",
        )


def test_social_autopost_strict_opt_in(tmp_clip_file):
    vault = OAuthTokenVault()
    # Conta criada com autopost_enabled=False (padrão obrigatório)
    acc = vault.register_account(
        org_id="org_beta",
        platform=SocialPlatform.TIKTOK,
        account_name="@tiktok_beta",
        access_token="tok_123",
        scopes=["video.upload"],
        autopost_enabled=False,
    )

    # Disparo automático não autorizado pelo operador -> Bloqueio com PermissionError
    with pytest.raises(PermissionError) as exc_info:
        vault.publish_clip(
            org_id="org_beta",
            account_id=acc.account_id,
            clip_path=tmp_clip_file,
            title="Corte Automático",
            idempotency_key="auto_post_01",
            is_automatic_post=True,
        )
    assert "Autopost desabilitado" in str(exc_info.value)

    # Operador habilita opt-in explicitamente
    acc.autopost_enabled = True
    r = vault.publish_clip(
        org_id="org_beta",
        account_id=acc.account_id,
        clip_path=tmp_clip_file,
        title="Corte Automático Permitido",
        idempotency_key="auto_post_01",
        is_automatic_post=True,
    )
    assert r["ok"] is True


def test_emergency_kill_switch(tmp_clip_file):
    vault = OAuthTokenVault()
    acc = vault.register_account(
        org_id="org_alfa",
        platform=SocialPlatform.INSTAGRAM_REELS,
        account_name="@reels_oficial",
        access_token="tok_insta",
        scopes=["reels_publish"],
    )

    # Ativa botão de emergência
    vault.set_emergency_kill_switch("org_alfa", active=True)
    assert vault.is_kill_switch_active("org_alfa") is True

    # Qualquer publicação deve ser suspensa imediatamente
    with pytest.raises(EmergencyKillSwitchActiveError):
        vault.publish_clip(
            org_id="org_alfa",
            account_id=acc.account_id,
            clip_path=tmp_clip_file,
            title="Post de Emergência",
            idempotency_key="emg_01",
        )

    # Desativação do Kill Switch restabelece o fluxo
    vault.set_emergency_kill_switch("org_alfa", active=False)
    res = vault.publish_clip(
        org_id="org_alfa",
        account_id=acc.account_id,
        clip_path=tmp_clip_file,
        title="Post Liberado",
        idempotency_key="emg_01",
    )
    assert res["ok"] is True


def test_duplicate_publish_idempotency(tmp_clip_file):
    vault = OAuthTokenVault()
    acc = vault.register_account(
        org_id="org_alfa",
        platform=SocialPlatform.YOUTUBE_SHORTS,
        account_name="@shorts",
        access_token="tok",
        scopes=["upload"],
    )

    # Primeira chamada
    r1 = vault.publish_clip(
        org_id="org_alfa",
        account_id=acc.account_id,
        clip_path=tmp_clip_file,
        title="Shorts 1",
        idempotency_key="idem_retry_100",
    )
    assert acc.posts_today == 1

    # Retry por timeout simulado com a mesma chave idempotente
    r2 = vault.publish_clip(
        org_id="org_alfa",
        account_id=acc.account_id,
        clip_path=tmp_clip_file,
        title="Shorts 1 Retry",
        idempotency_key="idem_retry_100",
    )

    # Deve retornar o mesmo post_id sem postar novamente e sem consumir contagem
    assert r1["post_id"] == r2["post_id"]
    assert acc.posts_today == 1
