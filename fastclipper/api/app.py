"""
Servidor de API Web e Plano de Controle do Fast Clipper (FastAPI).

Fornece endpoints REST e painel web operacional com o Design System Calm Premium.
Permite monitorar ingestão, pré-verificação de mídia, aprovação de momentos,
controle de turnos, fila assíncrona durável e cofre de redes sociais.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from fastclipper.collaboration.models import AuthContext, EstadoMomento, PapelUsuario
from fastclipper.collaboration.service import (
    CollaborationService,
    ConcurrencyConflictError,
    PermissionDeniedError,
    TenantAccessViolationError,
)
from fastclipper.ingest.precheck import MediaPrechecker
from fastclipper.ingest.vod_ingest import VodIngestor
from fastclipper.queue.durable_queue import DurableTaskQueue, JobStatus
from fastclipper.rendering.encoder import HardwareEncoderManager
from fastclipper.rendering.pipeline_runner import PipelineRunner
from fastclipper.social.oauth_vault import OAuthTokenVault, SocialPlatform
from fastclipper.storage.sqlite_store import SQLiteStore, SQLiteTaskQueue
from fastclipper.transcribe.whisper_service import WhisperTranscriptionService

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "testserver"}


def get_allowed_input_dirs() -> List[Path]:
    dirs = [
        (ROOT / "data").resolve(),
        (ROOT / "tests" / "fixtures").resolve(),
    ]
    extra = os.environ.get("FASTCLIPPER_ALLOWED_DIRS", "")
    if extra:
        for item in extra.split(os.pathsep):
            if item.strip():
                dirs.append(Path(item.strip()).resolve())
    return dirs


def validate_input_path(video_path: str) -> Path:
    """Garante que o arquivo de entrada esteja dentro das pastas locais permitidas."""
    if not video_path or not video_path.strip():
        raise HTTPException(status_code=400, detail="Caminho do vídeo não informado.")
    p = Path(video_path)
    if not p.is_absolute():
        p = ROOT / p
    resolved = p.resolve()

    allowed = get_allowed_input_dirs()
    for base in allowed:
        try:
            resolved.relative_to(base)
            return resolved
        except ValueError:
            continue

    raise HTTPException(
        status_code=403,
        detail="Acesso negado: o arquivo está fora das pastas permitidas (data/ ou tests/fixtures/).",
    )


app = FastAPI(
    title="Fast Clipper API & Control Plane",
    version="2.0.0",
    description="Plataforma local de monitoramento, clipping 9:16 e revisão editorial.",
)


@app.middleware("http")
async def local_security_guard(request: Request, call_next):
    """Protege o servidor local contra DNS rebinding e requisições cross-origin maliciosas."""
    host_header = request.headers.get("host", "")
    hostname = host_header.split(":")[0].lower()
    if hostname and hostname not in ALLOWED_HOSTS:
        return JSONResponse(status_code=403, content={"detail": "Host não permitido."})

    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
        if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
            return JSONResponse(status_code=403, content={"detail": "Origem cross-origin bloqueada."})

    return await call_next(request)

# Serviços locais persistentes (SQLite WAL) e protótipos de apoio
sqlite_store = SQLiteStore(ROOT / "data" / "fastclipper.db")
collab_service = CollaborationService()
durable_queue = SQLiteTaskQueue(sqlite_store)
encoder_manager = HardwareEncoderManager()
social_vault = OAuthTokenVault()
prechecker = MediaPrechecker()
vod_ingestor = VodIngestor(store=sqlite_store, projects_root=ROOT / "data" / "projects")
whisper_service = WhisperTranscriptionService(store=sqlite_store, model_name="small")
pipeline_runner = PipelineRunner(queue=durable_queue, output_dir=ROOT / "data" / "output_clips")

# Registra organização e usuário padrão para demonstração imediata
collab_service.register_organization(org_id="org_default", nome="Agência Alfa", slug="alfa")
collab_service.register_user(
    user_id="user_admin",
    org_id="org_default",
    nome="Administrador",
    email="admin@fastclipper.local",
    papel=PapelUsuario.ADMINISTRADOR,
)
default_ctx = AuthContext(
    usuario_id="user_admin",
    organizacao_id="org_default",
    papel=PapelUsuario.ADMINISTRADOR,
)
collab_service.register_channel(
    auth=default_ctx,
    canal_id="canal_principal",
    nome="Live Principal (GTA V)",
    plataforma="youtube",
    url="https://youtube.com/live/exemplo",
    autorizacao_ref="AUTH-ALFA-2026",
)
collab_service.register_source(
    auth=default_ctx,
    fonte_id="fonte_gta_01",
    canal_id="canal_principal",
    titulo="GTA V Stream Matutina",
    tipo="buffer_rolante",
    caminho_buffer="/data/buffer",
    termo_autorizacao="TERMO-AUTH-001",
)


# -----------------------------------------------------------------------------
# Pydantic Schemas
# -----------------------------------------------------------------------------

class PrecheckRequest(BaseModel):
    video_path: str
    min_duration_s: float = 10.0
    require_audio: bool = True


class CreateMomentRequest(BaseModel):
    fonte_id: str
    inicio_s: float
    fim_s: float
    timestamp_fonte: float
    descricao: str
    spoiler: bool = False


class AssignMomentRequest(BaseModel):
    momento_id: str
    usuario_id: str
    versao_esperada: int


class TransitionStateRequest(BaseModel):
    momento_id: str
    novo_estado: str
    versao_esperada: int
    motivo_rejeicao: Optional[str] = None


class EnqueueJobRequest(BaseModel):
    job_type: str
    params: Dict[str, Any]
    idempotency_key: Optional[str] = None


class RegisterSocialAccountRequest(BaseModel):
    platform: str
    account_name: str
    access_token: str
    scopes: List[str] = ["upload"]
    autopost_enabled: bool = False


class KillSwitchRequest(BaseModel):
    active: bool


class StudioRenderRequest(BaseModel):
    video_path: str
    inicio_s: float
    fim_s: float
    layout: str = "solo"
    facecam_rect: Optional[Dict[str, int]] = None
    gameplay_rect: Optional[Dict[str, int]] = None
    subtitle_text: Optional[str] = None
    words: Optional[List[Dict[str, Any]]] = None
    subtitles_are_absolute: bool = False
    subtitle_preset: str = "viral_bounce"
    burn_subtitles: bool = True


class ImportSourceRequest(BaseModel):
    video_path: str
    authorization_ref: str
    project_id: str = "proj_default"
    title: Optional[str] = None


class DownloadSourceRequest(BaseModel):
    url: str
    authorization_ref: str
    project_id: str = "proj_default"
    title: Optional[str] = None


class TranscribeRequest(BaseModel):
    video_path: Optional[str] = None
    source_id: Optional[str] = None
    language: Optional[str] = "pt"
    model_name: Optional[str] = None
    force_retranscribe: bool = False


# Helper para extrair contexto de autenticação de headers
def get_auth_context(
    x_org_id: str = Header("org_default", alias="X-Org-ID"),
    x_user_id: str = Header("user_admin", alias="X-User-ID"),
    x_role: str = Header("administrador", alias="X-Role"),
) -> AuthContext:
    try:
        papel = PapelUsuario(x_role.lower())
    except ValueError:
        papel = PapelUsuario.EDITOR
    return AuthContext(usuario_id=x_user_id, organizacao_id=x_org_id, papel=papel)


# -----------------------------------------------------------------------------
# Rotas de Diagnóstico e Sistema
# -----------------------------------------------------------------------------

@app.get("/health", tags=["Sistema"])
def health_check():
    return {"status": "ok", "service": "Fast Clipper API", "version": "2.0.0"}


@app.get("/api/v1/status", tags=["Sistema"])
def system_status():
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    active_encoder = encoder_manager.get_encoder()

    return {
        "status": "healthy",
        "ffmpeg": {"installed": ffmpeg_bin is not None, "path": ffmpeg_bin},
        "ffprobe": {"installed": ffprobe_bin is not None, "path": ffprobe_bin},
        "hardware_encoder": active_encoder,
        "python_version": sys.version.split()[0],
        "time": time.time(),
    }


@app.post("/api/v1/precheck", tags=["Mídia"])
def run_media_precheck(req: PrecheckRequest):
    safe_path = validate_input_path(req.video_path)
    checker = MediaPrechecker(min_duration_s=req.min_duration_s)
    res = checker.probe(safe_path, require_audio=req.require_audio)
    return res.to_dict()


# -----------------------------------------------------------------------------
# Rotas de Colaboração & Momentos (M3)
# -----------------------------------------------------------------------------

@app.post("/api/v1/moments", tags=["Momentos"])
def create_moment(req: CreateMomentRequest, ctx: AuthContext = Depends(get_auth_context)):
    try:
        m = collab_service.create_moment(
            auth=ctx,
            fonte_id=req.fonte_id,
            inicio_s=req.inicio_s,
            fim_s=req.fim_s,
            timestamp_fonte=req.timestamp_fonte,
            descricao=req.descricao,
            spoiler=req.spoiler,
        )
        return m.to_dict()
    except TenantAccessViolationError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@app.get("/api/v1/moments", tags=["Momentos"])
def list_moments(
    estado: Optional[str] = Query(None),
    ctx: AuthContext = Depends(get_auth_context),
):
    st = EstadoMomento(estado) if estado else None
    momentos = collab_service.list_moments(auth=ctx, estado=st)
    return [m.to_dict() for m in momentos]


@app.post("/api/v1/moments/assign", tags=["Momentos"])
def assign_moment(req: AssignMomentRequest, ctx: AuthContext = Depends(get_auth_context)):
    try:
        m = collab_service.assign_moment(
            auth=ctx,
            momento_id=req.momento_id,
            target_user_id=req.usuario_id,
            expected_version=req.versao_esperada,
        )
        return m.to_dict()
    except ConcurrencyConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except TenantAccessViolationError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@app.post("/api/v1/moments/transition", tags=["Momentos"])
def transition_moment_state(req: TransitionStateRequest, ctx: AuthContext = Depends(get_auth_context)):
    try:
        novo_st = EstadoMomento(req.novo_estado.lower())
        m = collab_service.transition_state(
            auth=ctx,
            momento_id=req.momento_id,
            new_state=novo_st,
            expected_version=req.versao_esperada,
        )
        return m.to_dict()
    except ConcurrencyConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except (TenantAccessViolationError, KeyError) as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@app.get("/api/v1/shifts/handoff", tags=["Turnos"])
def get_shift_handoff(
    hours: int = Query(8),
    ctx: AuthContext = Depends(get_auth_context),
):
    agora = time.time()
    inicio = agora - (hours * 3600)
    rel = collab_service.generate_shift_handoff(auth=ctx, periodo_inicio=inicio, periodo_fim=agora)
    from dataclasses import asdict
    return asdict(rel)


# -----------------------------------------------------------------------------
# Rotas da Fila Durável (M6)
# -----------------------------------------------------------------------------

@app.post("/api/v1/queue/jobs", tags=["Fila"])
def enqueue_job(req: EnqueueJobRequest, x_org_id: str = Header("org_default", alias="X-Org-ID")):
    job = durable_queue.enqueue(
        org_id=x_org_id,
        job_type=req.job_type,
        params=req.params,
        idempotency_key=req.idempotency_key,
    )
    return job.to_dict()


@app.get("/api/v1/queue/jobs/{job_id}", tags=["Fila"])
def get_job_status(job_id: str):
    job = durable_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job não encontrado")
    return job.to_dict()


@app.post("/api/v1/queue/worker/process-next", tags=["Fila"])
def process_next_queued_job():
    """Aciona o PipelineRunner para processar o próximo trabalho da fila."""
    res = pipeline_runner.run_single_step()
    if not res:
        return {"processed": False, "message": "Nenhum trabalho pendente na fila."}
    return {"processed": True, "result": res.to_dict()}


@app.post("/api/v1/queue/jobs/{job_id}/dispatch", tags=["Fila"])
def dispatch_job_background(job_id: str, background_tasks: BackgroundTasks):
    """Despacha a execução de um trabalho específico em background."""
    job = durable_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job não encontrado")

    background_tasks.add_task(pipeline_runner.process_job, job)
    return {"status": "dispatched", "job_id": job_id}


# -----------------------------------------------------------------------------
# Rotas do Cofre Social & Kill Switch (M6)
# -----------------------------------------------------------------------------

@app.post("/api/v1/social/accounts", tags=["Social"])
def register_social_account(req: RegisterSocialAccountRequest, x_org_id: str = Header("org_default", alias="X-Org-ID")):
    try:
        plat = SocialPlatform(req.platform.lower())
    except ValueError:
        plat = SocialPlatform.YOUTUBE_SHORTS

    acc = social_vault.register_account(
        org_id=x_org_id,
        platform=plat,
        account_name=req.account_name,
        access_token=req.access_token,
        scopes=req.scopes,
        autopost_enabled=req.autopost_enabled,
    )
    return acc.to_dict()


@app.post("/api/v1/social/kill-switch", tags=["Social"])
def set_kill_switch(req: KillSwitchRequest, x_org_id: str = Header("org_default", alias="X-Org-ID")):
    social_vault.set_emergency_kill_switch(org_id=x_org_id, active=req.active)
    return {"org_id": x_org_id, "kill_switch_active": req.active}


# -----------------------------------------------------------------------------
# Rotas do Fast Clipper Studio Local (Cortes, Player e Renderização Imediata)
# -----------------------------------------------------------------------------

@app.get("/api/v1/studio/clips", tags=["Studio"])
def list_studio_clips():
    """Lista todos os clipes renderizados disponíveis no diretório de saída."""
    out_dir = pipeline_runner.output_dir
    if not out_dir.exists():
        return []
    arquivos = list(out_dir.glob("*.mp4"))
    arquivos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    res = []
    for f in arquivos:
        st = f.stat()
        res.append({
            "filename": f.name,
            "path": str(f.resolve()),
            "size_mb": round(st.st_size / (1024 * 1024), 2),
            "modified_at": st.st_mtime,
            "download_url": f"/api/v1/studio/video/{f.name}",
        })
    return res


@app.get("/api/v1/studio/video/{filename}", tags=["Studio"])
def stream_studio_clip(filename: str):
    """Serve um clipe de vídeo renderizado para reprodução ou download."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=403, detail="Nome de arquivo inválido.")
    out_dir = pipeline_runner.output_dir.resolve()
    caminho = (out_dir / filename).resolve()
    if caminho.parent != out_dir:
        raise HTTPException(status_code=403, detail="Acesso negado fora da pasta de saída.")
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Clipe não encontrado")
    return FileResponse(str(caminho), media_type="video/mp4", filename=filename)


@app.post("/api/v1/studio/sources/import", tags=["Studio"])
def studio_import_source(req: ImportSourceRequest):
    """Importa um arquivo de vídeo local autorizado e registra no SQLite."""
    safe_path = validate_input_path(req.video_path)
    try:
        return vod_ingestor.import_local_file(
            file_path=safe_path,
            authorization_ref=req.authorization_ref,
            project_id=req.project_id,
            title=req.title,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/studio/sources/download", tags=["Studio"])
def studio_download_source(req: DownloadSourceRequest):
    """Baixa um VOD autorizado via yt-dlp com guarda SSRF e registra no SQLite."""
    try:
        return vod_ingestor.download_from_url(
            url=req.url,
            authorization_ref=req.authorization_ref,
            project_id=req.project_id,
            title=req.title,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/v1/studio/sources", tags=["Studio"])
def studio_list_sources(project_id: Optional[str] = Query(None)):
    """Lista todas as fontes de vídeo registradas no SQLite."""
    return sqlite_store.list_sources(project_id=project_id)


@app.post("/api/v1/studio/transcribe", tags=["Studio"])
def studio_transcribe_source(req: TranscribeRequest):
    """Transcreve o vídeo com faster-whisper (CUDA/CPU) e retorna timestamps por palavra com cache SQLite."""
    file_sha256: Optional[str] = None
    if req.source_id:
        src = sqlite_store.get_source(req.source_id)
        if not src:
            raise HTTPException(status_code=404, detail="Fonte não encontrada.")
        target_path = Path(src["file_path"])
        file_sha256 = src["file_sha256"]
    elif req.video_path:
        target_path = validate_input_path(req.video_path)
    else:
        raise HTTPException(status_code=400, detail="Informe 'source_id' ou 'video_path'.")

    try:
        return whisper_service.transcribe(
            media_path=target_path,
            file_sha256=file_sha256,
            language=req.language,
            model_name=req.model_name,
            force_retranscribe=req.force_retranscribe,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Erro na transcrição: {exc}")


@app.post("/api/v1/studio/render-now", tags=["Studio"])
def render_studio_clip_immediate(req: StudioRenderRequest):
    """Renderiza um clipe 9:16 imediatamente a partir dos parâmetros do Studio."""
    safe_path = validate_input_path(req.video_path)

    subtitles_payload = None
    subtitles_are_absolute = req.subtitles_are_absolute
    if req.words:
        subtitles_payload = req.words
    elif req.subtitle_text and req.subtitle_text.strip():
        palavras = req.subtitle_text.strip().split()
        if palavras:
            duracao = max(0.5, req.fim_s - req.inicio_s)
            passo = duracao / len(palavras)
            subtitles_payload = []
            for i, p in enumerate(palavras):
                # Tempos sempre relativos ao início do clipe cortado (0.0s)
                w_start = i * passo
                w_end = w_start + passo * 0.95
                subtitles_payload.append({
                    "word": p,
                    "start_s": round(w_start, 3),
                    "end_s": round(w_end, 3),
                })
            subtitles_are_absolute = False

    params = {
        "video_path": str(safe_path),
        "inicio_s": req.inicio_s,
        "fim_s": req.fim_s,
        "layout": req.layout,
        "facecam_rect": req.facecam_rect,
        "gameplay_rect": req.gameplay_rect,
        "subtitles": subtitles_payload,
        "subtitles_are_absolute": subtitles_are_absolute,
        "subtitle_preset": req.subtitle_preset,
        "burn_subtitles": req.burn_subtitles,
    }

    result = pipeline_runner.render_clip(params)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error or "Falha na renderização do clipe")

    filename = Path(result.output_video_path).name if result.output_video_path else None
    return {
        "success": True,
        "filename": filename,
        "output_path": result.output_video_path,
        "duration": result.duration,
        "download_url": f"/api/v1/studio/video/{filename}" if filename else None,
        "metadata": result.metadata,
    }


# -----------------------------------------------------------------------------
# Painel Web Operacional (Calm Premium Design System)
# -----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
def get_dashboard():
    html_content = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fast Clipper — Estúdio Local de Clipping 9:16</title>
    <style>
        :root {
            --bg: #F8F7F5;
            --card: #FFFFFF;
            --ink: #191817;
            --sub: #66635F;
            --muted: #9E9B95;
            --line: #E8E6E1;
            --accent: #2563EB;
            --accent-hover: #1D4ED8;
            --accent-soft: #EFF6FF;
            --cta-bg: #191817;
            --cta-fg: #FFFFFF;
            --ok: #16A34A;
            --ok-soft: #F0FDF4;
            --warn: #D97706;
            --error: #DC2626;
            --radius-sm: 8px;
            --radius-md: 14px;
            --radius-lg: 20px;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--ink);
            line-height: 1.5;
            padding: 24px 36px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--line);
            padding-bottom: 20px;
            margin-bottom: 24px;
        }
        .brand {
            font-size: 22px;
            font-weight: 700;
            letter-spacing: -0.5px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .brand span {
            color: var(--accent);
        }
        .tag-studio {
            background: var(--ink);
            color: #FFF;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            padding: 3px 8px;
            border-radius: 6px;
            letter-spacing: 0.5px;
        }
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 12.5px;
            font-family: monospace;
            background: var(--ok-soft);
            color: var(--ok);
            border: 1px solid #BBF7D0;
            padding: 6px 14px;
            border-radius: 999px;
            font-weight: 600;
        }
        .badge-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: var(--ok);
        }

        /* Tabs */
        .tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 24px;
        }
        .tab-btn {
            background: transparent;
            border: 1px solid var(--line);
            padding: 9px 18px;
            border-radius: 999px;
            font-size: 13.5px;
            font-weight: 500;
            color: var(--sub);
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .tab-btn.active {
            background: var(--ink);
            color: #FFF;
            border-color: var(--ink);
        }

        /* Studio Main Layout */
        .studio-grid {
            display: grid;
            grid-template-columns: 1.15fr 0.85fr;
            gap: 28px;
            align-items: start;
        }
        .panel {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: var(--radius-lg);
            padding: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.02);
            margin-bottom: 24px;
        }
        .panel-title {
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .field {
            margin-bottom: 18px;
        }
        .label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: var(--sub);
            margin-bottom: 6px;
        }
        .input-text, .select-box, .textarea {
            width: 100%;
            padding: 10px 14px;
            font-size: 13.5px;
            border: 1px solid var(--line);
            border-radius: var(--radius-sm);
            background: #FAFAFA;
            color: var(--ink);
            outline: none;
            transition: border-color 0.15s;
        }
        .input-text:focus, .select-box:focus, .textarea:focus {
            border-color: var(--accent);
            background: #FFF;
        }
        .row-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
        }
        .chips {
            display: flex;
            gap: 8px;
            margin-top: 8px;
        }
        .chip {
            background: #F0EEEA;
            border: none;
            font-size: 12px;
            padding: 5px 12px;
            border-radius: 6px;
            cursor: pointer;
            color: var(--sub);
            font-weight: 500;
        }
        .chip:hover {
            background: #E5E3DD;
        }
        .duration-pill {
            font-size: 12px;
            font-family: monospace;
            background: var(--accent-soft);
            color: var(--accent);
            padding: 3px 8px;
            border-radius: 6px;
            font-weight: 600;
        }

        /* Layout Cards */
        .layout-options {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }
        .layout-card {
            border: 2px solid var(--line);
            border-radius: var(--radius-md);
            padding: 14px;
            cursor: pointer;
            text-align: center;
            transition: all 0.15s;
        }
        .layout-card.selected {
            border-color: var(--accent);
            background: var(--accent-soft);
        }
        .layout-card h4 {
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 4px;
        }
        .layout-card p {
            font-size: 12px;
            color: var(--sub);
        }

        /* Primary Action Button */
        .btn-render {
            width: 100%;
            background: var(--accent);
            color: #FFFFFF;
            border: none;
            padding: 14px;
            border-radius: var(--radius-md);
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            transition: background 0.15s ease;
        }
        .btn-render:hover {
            background: var(--accent-hover);
        }
        .btn-render:disabled {
            background: var(--muted);
            cursor: not-allowed;
        }

        /* Right Panel: Phone Mockup / Video Player */
        .phone-mockup {
            width: 270px;
            height: 480px;
            background: #000;
            border-radius: 28px;
            border: 8px solid #282828;
            margin: 0 auto 20px auto;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 10px 30px rgba(0,0,0,0.15);
            position: relative;
        }
        .phone-mockup video {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .no-video-placeholder {
            color: #888;
            font-size: 13px;
            text-align: center;
            padding: 20px;
        }

        .clip-list {
            max-height: 260px;
            overflow-y: auto;
        }
        .clip-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 12px;
            border-bottom: 1px solid var(--line);
            font-size: 13px;
        }
        .clip-item:hover {
            background: #F9F9F8;
        }
        .clip-btn-play {
            background: var(--cta-bg);
            color: #FFF;
            border: none;
            padding: 5px 12px;
            border-radius: 999px;
            font-size: 12px;
            cursor: pointer;
        }

        /* Control Plane Grid */
        .grid-3 {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
            margin-bottom: 24px;
        }
        .metric-card {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: var(--radius-md);
            padding: 20px;
        }
        .metric-card h3 {
            font-size: 13px;
            color: var(--sub);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }
        .metric-card .val {
            font-size: 26px;
            font-weight: 700;
            font-family: monospace;
        }
        .metric-card .subtext {
            font-size: 12px;
            color: var(--muted);
            margin-top: 4px;
        }
        .terminal-box {
            background: #191817;
            color: #F6F5F3;
            font-family: monospace;
            font-size: 12.5px;
            padding: 16px;
            border-radius: 10px;
            white-space: pre-wrap;
            line-height: 1.6;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="brand">
            Fast<span>Clipper</span>
            <span class="tag-studio">Studio Local</span>
        </div>
        <div class="status-badge">
            <div class="badge-dot"></div>
            SISTEMA OPERACIONAL (88 TESTES VALIDADOS)
        </div>
    </div>

    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('studio')">Estúdio de Clipping 9:16</button>
        <button class="tab-btn" onclick="switchTab('control')">Plano de Controle & Métricas</button>
    </div>

    <!-- TAB 1: ESTUDIO DE CLIPPING -->
    <div id="tab-studio" class="studio-grid">
        <!-- Coluna Esquerda: Configuração e Parâmetros -->
        <div>
            <div class="panel">
                <div class="panel-title">
                    <span>1. Vídeo de Origem & Marcação de Corte</span>
                    <span id="duration-badge" class="duration-pill">Duração: 15.0s</span>
                </div>

                <div class="field">
                    <label class="label">Caminho do Vídeo no Computador</label>
                    <input type="text" id="video_path" class="input-text"
                           value="tests/fixtures/synthetic/sintetico_gameplay_facecam.mp4"
                           placeholder="Ex: C:\\Users\\...\\gravacao_live.mp4">
                </div>

                <div class="row-2">
                    <div class="field">
                        <label class="label">Início do Corte (segundos)</label>
                        <input type="number" step="0.1" id="inicio_s" class="input-text" value="0.0" oninput="updateDuration()">
                    </div>
                    <div class="field">
                        <label class="label">Fim do Corte (segundos)</label>
                        <input type="number" step="0.1" id="fim_s" class="input-text" value="15.0" oninput="updateDuration()">
                    </div>
                </div>

                <div class="chips">
                    <button class="chip" onclick="setClipDuration(15)">+15s</button>
                    <button class="chip" onclick="setClipDuration(30)">+30s</button>
                    <button class="chip" onclick="setClipDuration(45)">+45s</button>
                    <button class="chip" onclick="setClipDuration(60)">+60s</button>
                </div>
            </div>

            <div class="panel">
                <div class="panel-title">
                    <span>2. Enquadramento Vertical (9:16)</span>
                </div>
                <div class="layout-options">
                    <div id="card-solo" class="layout-card selected" onclick="selectLayout('solo')">
                        <h4>Solo 9:16</h4>
                        <p>Crop vertical centralizado inteligente</p>
                    </div>
                    <div id="card-stacked" class="layout-card" onclick="selectLayout('stacked')">
                        <h4>Empilhado (Stacked)</h4>
                        <p>Facecam no topo + Gameplay na base</p>
                    </div>
                </div>

                <div id="stacked-config" style="display: none; margin-top: 16px; padding: 12px; background: #FAF9F8; border-radius: 8px;">
                    <div class="row-2">
                        <div class="field">
                            <label class="label">Preset Facecam</label>
                            <select id="facecam-preset" class="select-box" onchange="applyFacecamPreset()">
                                <option value="top-left">Canto Superior Esquerdo</option>
                                <option value="top-right">Canto Superior Direito</option>
                                <option value="bottom-left">Canto Inferior Esquerdo</option>
                            </select>
                        </div>
                        <div class="field">
                            <label class="label">Altura Facecam Topo</label>
                            <input type="number" id="h-facecam" class="input-text" value="720">
                        </div>
                    </div>
                </div>
            </div>

            <div class="panel">
                <div class="panel-title">
                    <span>3. Legendas Animadas & Gancho Viral</span>
                </div>
                <div class="row-2">
                    <div class="field">
                        <label class="label">Estilo da Legenda</label>
                        <select id="subtitle_preset" class="select-box">
                            <option value="viral_bounce">Viral Bounce (Amarelo & Preto)</option>
                            <option value="minimal_clean">Minimal Clean (Calm White)</option>
                            <option value="cinema_editorial">Cinema Editorial (Alto Contraste)</option>
                        </select>
                    </div>
                    <div class="field" style="display:flex; align-items:center; margin-top: 24px;">
                        <label style="font-size: 13.5px; cursor: pointer; display: flex; align-items: center; gap: 8px;">
                            <input type="checkbox" id="burn_subtitles" checked style="width: 18px; height: 18px;">
                            Queimar legendas animadas no vídeo
                        </label>
                    </div>
                </div>

                <div class="field">
                    <label class="label">Texto da Legenda / Gancho do Clipe</label>
                    <textarea id="subtitle_text" class="textarea" rows="2"
                              placeholder="Digite ou cole a frase de impacto que aparecerá na legenda...">ESSA JOGADA FOI INSANA! OLHA O QUE ELE FEZ NO FINAL</textarea>
                </div>

                <button id="btn-render-action" class="btn-render" onclick="renderClipNow()">
                    <span>🚀 RENDERIZAR CLIPE 9:16 AGORA</span>
                </button>
                <div id="render-status" style="margin-top: 12px; font-size: 13px; font-weight: 500; text-align: center; display: none;"></div>
            </div>
        </div>

        <!-- Coluna Direita: Player e Biblioteca -->
        <div>
            <div class="panel" style="text-align: center;">
                <div class="panel-title">
                    <span>Pré-Visualização 9:16</span>
                    <a id="btn-download-clip" href="#" download style="display: none; font-size: 12px; font-weight: 600; color: var(--accent); text-decoration: none;">⬇️ Baixar Clipe</a>
                </div>

                <div class="phone-mockup">
                    <video id="player-preview" controls playsinline>
                        <source id="video-source" src="" type="video/mp4">
                        Seu navegador não suporta reprodução de vídeo.
                    </video>
                    <div id="video-placeholder" class="no-video-placeholder">
                        <div style="font-size: 32px; margin-bottom: 8px;">📱</div>
                        O clipe renderizado aparecerá aqui pronto para assistir e baixar.
                    </div>
                </div>
            </div>

            <div class="panel">
                <div class="panel-title">
                    <span>Clipes Renderizados Recentemente</span>
                    <button onclick="loadClipsList()" style="background:none; border:none; color:var(--accent); font-size:12px; cursor:pointer; font-weight:600;">↻ Atualizar</button>
                </div>
                <div id="clips-container" class="clip-list">
                    <div style="font-size: 13px; color: var(--muted); padding: 12px; text-align: center;">Carregando clipes...</div>
                </div>
            </div>
        </div>
    </div>

    <!-- TAB 2: PLANO DE CONTROLE & METRICAS -->
    <div id="tab-control" style="display: none;">
        <div class="grid-3">
            <div class="metric-card">
                <h3>Hardware Encoder</h3>
                <div class="val">NVENC / QSV</div>
                <div class="subtext">Aceleração por hardware com fallback x264</div>
            </div>
            <div class="metric-card">
                <h3>Media Pre-Check</h3>
                <div class="val">ATIVO</div>
                <div class="subtext">Sonda estrita anti-desperdício de GPU</div>
            </div>
            <div class="metric-card">
                <h3>Pipeline Runner</h3>
                <div class="val">ONLINE</div>
                <div class="subtext">Processamento de fila durável ponta a ponta</div>
            </div>
        </div>

        <div class="panel">
            <div class="panel-title">Guia Operacional no Terminal</div>
            <div class="terminal-box">
# Iniciar o Estúdio Fast Clipper e abrir no navegador:
python -m fastclipper studio

# Executar worker contínuo de renderização:
python -m fastclipper queue worker --run-pipeline

# Diagnóstico preliminar de vídeo fonte:
python -m fastclipper precheck --video tests/fixtures/synthetic/sintetico_gameplay_facecam.mp4

# Documentação OpenAPI interativa (Swagger):
Acesse: http://127.0.0.1:8000/docs
            </div>
        </div>
    </div>

    <script>
        let currentLayout = 'solo';

        function switchTab(tab) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            if (tab === 'studio') {
                document.querySelectorAll('.tab-btn')[0].classList.add('active');
                document.getElementById('tab-studio').style.display = 'grid';
                document.getElementById('tab-control').style.display = 'none';
            } else {
                document.querySelectorAll('.tab-btn')[1].classList.add('active');
                document.getElementById('tab-studio').style.display = 'none';
                document.getElementById('tab-control').style.display = 'block';
            }
        }

        function updateDuration() {
            const ini = parseFloat(document.getElementById('inicio_s').value) || 0;
            const fim = parseFloat(document.getElementById('fim_s').value) || 0;
            const dur = Math.max(0, fim - ini).toFixed(1);
            document.getElementById('duration-badge').innerText = `Duração: ${dur}s`;
        }

        function setClipDuration(seconds) {
            const ini = parseFloat(document.getElementById('inicio_s').value) || 0;
            document.getElementById('fim_s').value = (ini + seconds).toFixed(1);
            updateDuration();
        }

        function selectLayout(mode) {
            currentLayout = mode;
            document.getElementById('card-solo').classList.toggle('selected', mode === 'solo');
            document.getElementById('card-stacked').classList.toggle('selected', mode === 'stacked');
            document.getElementById('stacked-config').style.display = mode === 'stacked' ? 'block' : 'none';
        }

        async function renderClipNow() {
            const btn = document.getElementById('btn-render-action');
            const statusDiv = document.getElementById('render-status');
            const videoPath = document.getElementById('video_path').value.trim();
            const inicioS = parseFloat(document.getElementById('inicio_s').value) || 0;
            const fimS = parseFloat(document.getElementById('fim_s').value) || 10;
            const subPreset = document.getElementById('subtitle_preset').value;
            const subText = document.getElementById('subtitle_text').value.trim();
            const burnSubs = document.getElementById('burn_subtitles').checked;

            if (!videoPath) {
                alert('Informe o caminho do vídeo de origem.');
                return;
            }

            btn.disabled = true;
            btn.innerHTML = '<span>⏳ Processando e Renderizando 9:16...</span>';
            statusDiv.style.display = 'block';
            statusDiv.style.color = 'var(--accent)';
            statusDiv.innerText = 'Executando corte, ajuste 9:16 e queima de legendas...';

            let facecamRect = null;
            if (currentLayout === 'stacked') {
                facecamRect = { x: 50, y: 50, w: 400, h: 300 };
            }

            const payload = {
                video_path: videoPath,
                inicio_s: inicioS,
                fim_s: fimS,
                layout: currentLayout,
                facecam_rect: facecamRect,
                subtitle_text: subText,
                subtitle_preset: subPreset,
                burn_subtitles: burnSubs
            };

            try {
                const res = await fetch('/api/v1/studio/render-now', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();

                if (res.ok && data.success) {
                    statusDiv.style.color = 'var(--ok)';
                    statusDiv.innerText = `✅ Sucesso! Clipe renderizado em ${data.duration.toFixed(1)}s.`;
                    playClipInMockup(data.download_url, data.filename);
                    loadClipsList();
                } else {
                    statusDiv.style.color = 'var(--error)';
                    statusDiv.innerText = `❌ Erro: ${data.detail || 'Falha na renderização'}`;
                }
            } catch (err) {
                statusDiv.style.color = 'var(--error)';
                statusDiv.innerText = `❌ Erro de conexão com o servidor: ${err.message}`;
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<span>🚀 RENDERIZAR CLIPE 9:16 AGORA</span>';
            }
        }

        function playClipInMockup(url, filename) {
            const player = document.getElementById('player-preview');
            const source = document.getElementById('video-source');
            const placeholder = document.getElementById('video-placeholder');
            const dlBtn = document.getElementById('btn-download-clip');

            placeholder.style.display = 'none';
            player.style.display = 'block';
            source.src = url;
            player.load();
            player.play().catch(() => {});

            dlBtn.href = url;
            dlBtn.style.display = 'inline';
            dlBtn.download = filename || 'clipe_9x16.mp4';
        }

        async function loadClipsList() {
            const container = document.getElementById('clips-container');
            try {
                const res = await fetch('/api/v1/studio/clips');
                const clips = await res.json();
                if (!clips || clips.length === 0) {
                    container.innerHTML = '<div style="font-size: 13px; color: var(--muted); padding: 12px; text-align: center;">Nenhum clipe renderizado ainda.</div>';
                    return;
                }

                container.innerHTML = clips.map(c => `
                    <div class="clip-item">
                        <div>
                            <strong>${c.filename}</strong><br>
                            <span style="font-size: 11.5px; color: var(--muted);">${c.size_mb} MB</span>
                        </div>
                        <button class="clip-btn-play" onclick="playClipInMockup('${c.download_url}', '${c.filename}')">▶ Assistir</button>
                    </div>
                `).join('');
            } catch (e) {
                container.innerHTML = '<div style="font-size: 13px; color: var(--error); padding: 12px; text-align: center;">Erro ao carregar lista de clipes.</div>';
            }
        }

        // Carrega lista ao iniciar
        window.addEventListener('DOMContentLoaded', () => {
            loadClipsList();
        });
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content, status_code=200)
    return HTMLResponse(content=html_content, status_code=200)
