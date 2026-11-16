"""
Testes automatizados para os endpoints REST e Control Plane da API FastAPI.
"""

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from fastclipper.api.app import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]


def test_api_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_api_status():
    res = client.get("/api/v1/status")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "hardware_encoder" in data
    assert "ffmpeg" in data


def test_api_dashboard_html():
    res = client.get("/")
    assert res.status_code == 200
    assert "Fast" in res.text
    assert "Clipper" in res.text
    assert "Control Plane" in res.text
    assert "text/html" in res.headers["content-type"]


def test_api_media_precheck():
    valid_video = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    res = client.post(
        "/api/v1/precheck",
        json={"video_path": str(valid_video), "min_duration_s": 2.0, "require_audio": True},
    )
    assert res.status_code == 200
    assert res.json()["is_valid"] is True
    assert res.json()["status"] == "valid"


def test_api_moments_flow_and_optimistic_locking():
    headers = {"X-Org-ID": "org_default", "X-User-ID": "user_admin", "X-Role": "administrador"}

    # 1. Criação de Momento
    create_payload = {
        "fonte_id": "fonte_gta_01",
        "inicio_s": 50.0,
        "fim_s": 75.0,
        "timestamp_fonte": 60.0,
        "descricao": "Perseguição policial de alta velocidade",
        "spoiler": False,
    }
    res_create = client.post("/api/v1/moments", json=create_payload, headers=headers)
    assert res_create.status_code == 200
    momento = res_create.json()
    assert momento["estado"] == "novo"
    assert momento["versao"] == 1
    mom_id = momento["id"]

    # 2. Atribuição Exclusiva
    assign_payload = {
        "momento_id": mom_id,
        "usuario_id": "user_admin",
        "versao_esperada": 1,
    }
    res_assign = client.post("/api/v1/moments/assign", json=assign_payload, headers=headers)
    assert res_assign.status_code == 200
    assert res_assign.json()["estado"] == "em_edicao"
    assert res_assign.json()["versao"] == 2

    # 3. Tentativa concorrente com versão defasada (deve falhar com 409 Conflict)
    res_conflito = client.post("/api/v1/moments/assign", json=assign_payload, headers=headers)
    assert res_conflito.status_code == 409

    # 4. Transição para Aprovado com versão correta (v=2)
    trans_payload = {
        "momento_id": mom_id,
        "novo_estado": "aprovado",
        "versao_esperada": 2,
    }
    res_trans = client.post("/api/v1/moments/transition", json=trans_payload, headers=headers)
    assert res_trans.status_code == 200
    assert res_trans.json()["estado"] == "aprovado"
    assert res_trans.json()["versao"] == 3


def test_api_shift_handoff():
    headers = {"X-Org-ID": "org_default", "X-User-ID": "user_admin"}
    res = client.get("/api/v1/shifts/handoff?hours=8", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["organizacao_id"] == "org_default"
    assert "pendentes_revisao" in data


def test_api_queue_idempotent():
    headers = {"X-Org-ID": "org_default"}
    payload = {
        "job_type": "render_9x16",
        "params": {"clip_id": "c1"},
        "idempotency_key": "api_test_idemp_01",
    }
    res1 = client.post("/api/v1/queue/jobs", json=payload, headers=headers)
    assert res1.status_code == 200
    job_id = res1.json()["job_id"]

    # Reenvio idêntico deve retornar o mesmo job_id
    res2 = client.post("/api/v1/queue/jobs", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["job_id"] == job_id

    # Consulta de status
    res_get = client.get(f"/api/v1/queue/jobs/{job_id}")
    assert res_get.status_code == 200
    assert res_get.json()["job_id"] == job_id


def test_api_social_and_kill_switch():
    headers = {"X-Org-ID": "org_default"}
    acc_payload = {
        "platform": "youtube_shorts",
        "account_name": "Canal YouTube Oficial",
        "access_token": "ya29.test_api_token",
        "scopes": ["upload"],
        "autopost_enabled": False,
    }
    res_acc = client.post("/api/v1/social/accounts", json=acc_payload, headers=headers)
    assert res_acc.status_code == 200
    assert res_acc.json()["account_name"] == "Canal YouTube Oficial"

    # Aciona Kill Switch
    res_kill = client.post("/api/v1/social/kill-switch", json={"active": True}, headers=headers)
    assert res_kill.status_code == 200
    assert res_kill.json()["kill_switch_active"] is True
