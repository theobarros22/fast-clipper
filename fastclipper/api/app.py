"""
Servidor de API Web e Plano de Controle do Fast Clipper (FastAPI).

Fornece endpoints REST e painel web operacional com o Design System Calm Premium.
Permite monitorar ingestão, pré-verificação de mídia, aprovação de momentos,
controle de turnos, fila assíncrona durável e cofre de redes sociais.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from fastclipper.collaboration.models import AuthContext, EstadoMomento, PapelUsuario
from fastclipper.collaboration.service import (
    CollaborationService,
    ConcurrencyConflictError,
    PermissionDeniedError,
    TenantAccessViolationError,
)
from fastclipper.export import PackageExporter, generate_post_copy
from fastclipper.ingest.precheck import MediaPrechecker
from fastclipper.ingest.vod_ingest import VodIngestor
from fastclipper.live.live_manager import LiveManager
from fastclipper.live.youtube_live import SecuritySSRFError
from fastclipper.layouts.config import ChannelLayoutConfig, LayoutMode, Rectangle
from fastclipper.layouts.engine import LayoutEngine
from fastclipper.layouts.facecam_detector import FacecamDetector
from fastclipper.queue.durable_queue import DurableTaskQueue, JobStatus
from fastclipper.rendering.encoder import HardwareEncoderManager
from fastclipper.rendering.pipeline_runner import PipelineRunner
from fastclipper.signals.moment_suggester import MomentSuggester
from fastclipper.social.oauth_vault import OAuthTokenVault, SocialPlatform
from fastclipper.storage.sqlite_store import SQLiteStore, SQLiteTaskQueue
from fastclipper.subtitles.engine import SubtitleEngine
from fastclipper.subtitles.models import WordTiming
from fastclipper.transcribe.whisper_service import WhisperTranscriptionService

BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])).resolve()
ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[2]
)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "testserver"}


def get_allowed_input_dirs() -> List[Path]:
    dirs = [
        DATA_DIR.resolve(),
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
DATA_DIR = Path(os.environ.get("FASTCLIPPER_DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
sqlite_store = SQLiteStore(DATA_DIR / "fastclipper.db")
collab_service = CollaborationService()
durable_queue = SQLiteTaskQueue(sqlite_store)
encoder_manager = HardwareEncoderManager()
social_vault = OAuthTokenVault()
prechecker = MediaPrechecker()
vod_ingestor = VodIngestor(store=sqlite_store, projects_root=DATA_DIR / "projects")
whisper_service = WhisperTranscriptionService(store=sqlite_store, model_name="small")
pipeline_runner = PipelineRunner(queue=durable_queue, output_dir=DATA_DIR / "output_clips")
live_manager = LiveManager(
    projects_root=DATA_DIR / "projects",
    pipeline_runner=pipeline_runner,
    transcribe_fn=lambda path, lang: whisper_service.transcribe(media_path=path, language=lang, save_to_cache=False),
)
package_exporter = PackageExporter(exports_root=DATA_DIR / "exports")
subtitle_engine = SubtitleEngine()

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


VALID_SUBTITLE_PRESETS = {"viral_bounce", "minimal_clean", "cinema_editorial"}


def _check_even_facecam_height(v: int) -> int:
    if v < 200 or v > 1720 or v % 2 != 0:
        raise ValueError("stacked_facecam_height deve ser um número par entre 200 e 1720.")
    return v


def _check_subtitle_preset(v: str) -> str:
    val = (v or "").strip().lower()
    if val not in VALID_SUBTITLE_PRESETS:
        raise ValueError(f"subtitle_preset inválido: '{v}'. Use: {sorted(VALID_SUBTITLE_PRESETS)}.")
    return val


class StudioRenderRequest(BaseModel):
    video_path: str
    inicio_s: float = Field(ge=0.0)
    fim_s: float = Field(gt=0.0)
    layout: str = "solo"
    facecam_rect: Optional[Dict[str, int]] = None
    gameplay_rect: Optional[Dict[str, int]] = None
    stacked_facecam_height: int = 720
    aspect_ratio: str = "9:16"
    solo_align: Literal["left", "center", "right"] = "center"
    subtitle_text: Optional[str] = None
    edited_clip_text: Optional[str] = None
    hook_title: Optional[str] = None
    words: Optional[List[Dict[str, Any]]] = None
    subtitles_are_absolute: bool = False
    subtitle_preset: str = "viral_bounce"
    burn_subtitles: bool = True
    progress_bar: bool = False
    enhance_voice: bool = False
    normalize_audio: bool = False
    watermark_text: Optional[str] = None

    @field_validator("stacked_facecam_height")
    @classmethod
    def _val_h(cls, v: int) -> int:
        return _check_even_facecam_height(v)

    @field_validator("subtitle_preset")
    @classmethod
    def _val_preset(cls, v: str) -> str:
        return _check_subtitle_preset(v)

    @model_validator(mode="after")
    def _val_bounds(self) -> "StudioRenderRequest":
        if self.fim_s <= self.inicio_s:
            raise ValueError("fim_s deve ser maior que inicio_s.")
        return self


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


class LiveStartRequest(BaseModel):
    url: str
    segment_duration_s: float = 10.0
    retention_seconds: float = 1800.0
    auto_clip: bool = False
    auto_clip_mode: Literal["auto_render", "suggest_only"] = "auto_render"
    auto_clip_duration_s: float = 25.0
    auto_clip_threshold_db: float = -12.0
    auto_clip_keywords: List[str] = []
    layout: str = "stacked"
    facecam_rect: Optional[Dict[str, Any]] = None
    gameplay_rect: Optional[Dict[str, Any]] = None
    stacked_facecam_height: int = 720
    aspect_ratio: str = "9:16"
    solo_align: Literal["left", "center", "right"] = "center"
    burn_subtitles: bool = False
    hook_title: Optional[str] = None
    progress_bar: bool = False
    enhance_voice: bool = False
    subtitle_preset: str = "minimal_clean"
    language: str = "pt"
    normalize_audio: bool = False
    watermark_text: Optional[str] = None

    @field_validator("stacked_facecam_height")
    @classmethod
    def _val_h(cls, v: int) -> int:
        return _check_even_facecam_height(v)

    @field_validator("subtitle_preset")
    @classmethod
    def _val_preset(cls, v: str) -> str:
        return _check_subtitle_preset(v)


class LiveMarkRequest(BaseModel):
    pre_s: float = 25.0
    post_s: float = 0.0
    titulo: str = "Corte ao Vivo"
    layout: Optional[str] = None
    facecam_rect: Optional[Dict[str, Any]] = None
    gameplay_rect: Optional[Dict[str, Any]] = None
    stacked_facecam_height: Optional[int] = None
    aspect_ratio: Optional[str] = None
    solo_align: Optional[Literal["left", "center", "right"]] = None
    burn_subtitles: Optional[bool] = None
    hook_title: Optional[str] = None
    progress_bar: Optional[bool] = None
    enhance_voice: Optional[bool] = None
    subtitle_preset: Optional[str] = None
    language: Optional[str] = None
    normalize_audio: Optional[bool] = None
    watermark_text: Optional[str] = None

    @field_validator("stacked_facecam_height")
    @classmethod
    def _val_h(cls, v: Optional[int]) -> Optional[int]:
        return _check_even_facecam_height(v) if v is not None else None

    @field_validator("subtitle_preset")
    @classmethod
    def _val_preset(cls, v: Optional[str]) -> Optional[str]:
        return _check_subtitle_preset(v) if v is not None else None


class LiveSettingsRequest(BaseModel):
    auto_clip: Optional[bool] = None
    auto_clip_mode: Optional[Literal["auto_render", "suggest_only"]] = None
    auto_clip_duration_s: Optional[float] = None
    auto_clip_threshold_db: Optional[float] = None
    auto_clip_keywords: Optional[List[str]] = None
    layout: Optional[str] = None
    facecam_rect: Optional[Dict[str, Any]] = None
    gameplay_rect: Optional[Dict[str, Any]] = None
    stacked_facecam_height: Optional[int] = None
    aspect_ratio: Optional[str] = None
    solo_align: Optional[Literal["left", "center", "right"]] = None
    burn_subtitles: Optional[bool] = None
    hook_title: Optional[str] = None
    progress_bar: Optional[bool] = None
    enhance_voice: Optional[bool] = None
    subtitle_preset: Optional[str] = None
    language: Optional[str] = None
    normalize_audio: Optional[bool] = None
    watermark_text: Optional[str] = None


class RealignWordsRequest(BaseModel):
    words: List[Dict[str, Any]]
    edited_text: str
    inicio_s: float = Field(0.0, ge=0.0)
    fim_s: Optional[float] = Field(None, gt=0.0)


class LiveReplayRequest(BaseModel):
    inicio_s: Optional[float] = Field(None, ge=0.0)
    fim_s: Optional[float] = Field(None, gt=0.0)
    duration_s: float = Field(45.0, ge=3.0, le=1800.0)


class TranscribeRequest(BaseModel):
    video_path: Optional[str] = None
    source_id: Optional[str] = None
    language: Optional[str] = "pt"
    model_name: Optional[str] = None
    force_retranscribe: bool = False


class PreviewFrameRequest(BaseModel):
    video_path: str
    timestamp_s: float = 0.0
    detect_facecam: bool = True


class PreviewLayoutRequest(BaseModel):
    video_path: str
    timestamp_s: float = 0.0
    layout: str = "solo"
    facecam_rect: Optional[Dict[str, int]] = None
    gameplay_rect: Optional[Dict[str, int]] = None
    stacked_facecam_height: int = 720


class ChannelPresetRequest(BaseModel):
    channel_id: str
    name: str
    layout_mode: str = "solo"
    facecam_rect: Optional[Dict[str, int]] = None
    gameplay_rect: Optional[Dict[str, int]] = None
    stacked_facecam_height: int = 720


class SuggestMomentsRequest(BaseModel):
    video_path: str
    words: Optional[List[Dict[str, Any]]] = None
    target_duration_s: float = Field(25.0, ge=1.0)
    top_k: int = Field(5, ge=1, le=50)


class BatchRenderItem(BaseModel):
    inicio_s: float = Field(ge=0.0)
    fim_s: float = Field(gt=0.0)
    hook_title: Optional[str] = None
    edited_clip_text: Optional[str] = None

    @model_validator(mode="after")
    def _val_bounds(self) -> "BatchRenderItem":
        if self.fim_s <= self.inicio_s:
            raise ValueError("fim_s deve ser maior que inicio_s.")
        return self


class BatchRenderRequest(BaseModel):
    video_path: str
    items: List[BatchRenderItem]
    layout: str = "solo"
    facecam_rect: Optional[Dict[str, int]] = None
    gameplay_rect: Optional[Dict[str, int]] = None
    stacked_facecam_height: int = 720
    aspect_ratio: str = "9:16"
    solo_align: Literal["left", "center", "right"] = "center"
    words: Optional[List[Dict[str, Any]]] = None
    subtitles_are_absolute: bool = False
    subtitle_preset: str = "viral_bounce"
    burn_subtitles: bool = True
    hook_title: Optional[str] = None
    progress_bar: bool = False
    enhance_voice: bool = False
    normalize_audio: bool = False
    watermark_text: Optional[str] = None

    @field_validator("stacked_facecam_height")
    @classmethod
    def _val_h(cls, v: int) -> int:
        return _check_even_facecam_height(v)

    @field_validator("subtitle_preset")
    @classmethod
    def _val_preset(cls, v: str) -> str:
        return _check_subtitle_preset(v)


class GenerateCopyRequest(BaseModel):
    transcript_text: str
    platform: str = "tiktok"
    channel_name: Optional[str] = None
    use_llm: bool = False


class ExportPackageRequest(BaseModel):
    clip_filename: str
    platform: str = "tiktok"
    title: str
    description: str
    hashtags: List[str] = []
    words: Optional[List[Dict[str, Any]]] = None
    cover_timestamp_s: float = Field(0.5, ge=0.0)
    inicio_s: float = Field(0.0, ge=0.0)
    fim_s: Optional[float] = Field(None, gt=0.0)


class ClipResultRequest(BaseModel):
    clip_filename: str
    platform: Literal["tiktok", "shorts", "reels", "youtube", "x"]
    post_url: Optional[str] = None
    views_24h: int = Field(0, ge=0)
    views_7d: int = Field(0, ge=0)
    revenue_brl: float = Field(0.0, ge=0.0)
    layout_mode: Optional[str] = None
    subtitle_preset: Optional[str] = None
    channel_id: Optional[str] = None
    notes: Optional[str] = None


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
    cuda_available = False
    try:
        import ctranslate2

        cuda_available = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        pass

    return {
        "status": "healthy",
        "ffmpeg": {"installed": ffmpeg_bin is not None, "path": ffmpeg_bin},
        "ffprobe": {"installed": ffprobe_bin is not None, "path": ffprobe_bin},
        "hardware_encoder": active_encoder,
        "nvenc_available": "nvenc" in str(active_encoder).lower(),
        "cuda_available": cuda_available,
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


@app.delete("/api/v1/studio/clips/{filename}", tags=["Studio"])
def delete_studio_clip(filename: str):
    """Remove um clipe renderizado do diretório de saída (e limpa registro da live se houver)."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=403, detail="Nome de arquivo inválido.")
    out_dir = pipeline_runner.output_dir.resolve()
    caminho = (out_dir / filename).resolve()
    if caminho.parent != out_dir:
        raise HTTPException(status_code=403, detail="Acesso negado fora da pasta de saída.")
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Clipe não encontrado.")
    caminho.unlink()
    ass_companion = (out_dir / f"{caminho.stem}_subs.ass").resolve()
    if ass_companion.parent == out_dir and ass_companion.is_file():
        ass_companion.unlink(missing_ok=True)
    live_manager.remove_clip_entry(filename)
    return {"deleted": True, "filename": filename}


@app.get("/api/v1/studio/source-video", tags=["Studio"])
def stream_source_video(path: str = Query(...)):
    """Serve o vídeo fonte autorizado para reprodução e marcação I/O no Studio."""
    safe_path = validate_input_path(path)
    if not safe_path.is_file():
        raise HTTPException(status_code=404, detail="Vídeo fonte não encontrado.")
    return FileResponse(str(safe_path), media_type="video/mp4", filename=safe_path.name)


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


@app.post("/api/v1/studio/sources/upload", tags=["Studio"])
async def studio_upload_source(
    request: Request,
    filename: str = Query(...),
    authorization_ref: str = Query("ARQUIVO-LOCAL"),
    project_id: str = Query("proj_default"),
):
    """Recebe upload binário em streaming do computador, valida e registra no projeto."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", (project_id or "").strip()):
        raise HTTPException(status_code=400, detail="project_id inválido.")

    raw_name = Path(filename).name
    if not raw_name or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Nome de arquivo inválido.")

    ext = Path(raw_name).suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".ts"}:
        raise HTTPException(status_code=400, detail=f"Formato não suportado para upload: {ext}")

    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw_name).strip("._") or f"upload{ext}"
    if not safe_name.lower().endswith(ext):
        safe_name = f"{safe_name}{ext}"

    projects_root = vod_ingestor.projects_root.resolve()
    dest_dir = (projects_root / project_id / "source").resolve()
    if not dest_dir.is_relative_to(projects_root):
        raise HTTPException(status_code=403, detail="Destino de upload fora da pasta de projetos.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = (dest_dir / f"{int(time.time())}_{safe_name}").resolve()
    if dest_path.parent != dest_dir or not dest_path.is_relative_to(projects_root):
        raise HTTPException(status_code=403, detail="Destino de upload inválido.")

    max_bytes = int(os.environ.get("FASTCLIPPER_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
    part_path = dest_path.with_suffix(dest_path.suffix + ".part")
    total_bytes = 0
    try:
        with open(part_path, "wb") as out_f:
            async for chunk in request.stream():
                if chunk:
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise HTTPException(status_code=413, detail="Arquivo excede o limite máximo de upload.")
                    out_f.write(chunk)

        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Arquivo enviado está vazio.")

        part_path.replace(dest_path)
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise

    try:
        return vod_ingestor.import_local_file(
            file_path=dest_path,
            authorization_ref=authorization_ref,
            project_id=project_id,
            title=Path(raw_name).stem,
        )
    except (ValueError, FileNotFoundError) as exc:
        dest_path.unlink(missing_ok=True)
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


@app.post("/api/v1/studio/preview-frame", tags=["Studio"])
def studio_preview_frame(req: PreviewFrameRequest):
    """Extrai um frame original da mídia e sugere o retângulo inicial de Facecam."""
    safe_path = validate_input_path(req.video_path)
    previews_dir = pipeline_runner.output_dir / "_previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    img_name = f"src_{int(time.time() * 1000)}.jpg"
    img_path = previews_dir / img_name

    engine = LayoutEngine()
    try:
        info = engine.extract_source_frame(safe_path, req.timestamp_s, img_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    suggestion = None
    if req.detect_facecam:
        detector = FacecamDetector()
        suggestion = detector.detect_from_image(img_path, info["source_width"], info["source_height"])

    return {
        "image_url": f"/api/v1/studio/preview-image/{img_name}",
        "source_width": info["source_width"],
        "source_height": info["source_height"],
        "timestamp_s": info["timestamp_s"],
        "facecam_suggestion": suggestion,
    }


@app.post("/api/v1/studio/preview-layout", tags=["Studio"])
def studio_preview_layout(req: PreviewLayoutRequest):
    """Gera uma prévia 1080x1920 aplicando o layout Solo ou Empilhado com as caixas atuais."""
    safe_path = validate_input_path(req.video_path)
    previews_dir = pipeline_runner.output_dir / "_previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    img_name = f"layout_{int(time.time() * 1000)}.jpg"
    img_path = previews_dir / img_name

    mode = LayoutMode.STACKED if req.layout.lower() == "stacked" else LayoutMode.SOLO
    try:
        fc = Rectangle(**req.facecam_rect) if req.facecam_rect else None
        gp = Rectangle(**req.gameplay_rect) if req.gameplay_rect else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Retângulo de enquadramento inválido: {exc}")
    cfg = ChannelLayoutConfig(
        channel_id="preview",
        mode=mode,
        facecam_rect=fc,
        gameplay_rect=gp,
        stacked_facecam_height=req.stacked_facecam_height,
    )

    engine = LayoutEngine()
    try:
        engine.generate_preview(safe_path, req.timestamp_s, cfg, img_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"image_url": f"/api/v1/studio/preview-image/{img_name}"}


@app.get("/api/v1/studio/preview-image/{filename}", tags=["Studio"])
def studio_get_preview_image(filename: str):
    """Serve uma imagem de prévia gerada pelo Studio."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=403, detail="Nome de arquivo inválido.")
    previews_dir = (pipeline_runner.output_dir / "_previews").resolve()
    caminho = (previews_dir / filename).resolve()
    if caminho.parent != previews_dir:
        raise HTTPException(status_code=403, detail="Acesso negado fora da pasta de prévias.")
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Imagem de prévia não encontrada.")
    return FileResponse(str(caminho), media_type="image/jpeg", filename=filename)


@app.get("/api/v1/studio/presets", tags=["Studio"])
def studio_list_presets():
    """Lista presets de layout salvos por canal no SQLite."""
    return sqlite_store.list_channel_presets()


@app.post("/api/v1/studio/presets", tags=["Studio"])
def studio_save_preset(req: ChannelPresetRequest):
    """Salva ou atualiza o preset de enquadramento (Facecam/Gameplay) de um canal."""
    return sqlite_store.save_channel_preset(
        channel_id=req.channel_id,
        name=req.name,
        layout_mode=req.layout_mode,
        facecam_rect=req.facecam_rect,
        gameplay_rect=req.gameplay_rect,
        stacked_facecam_height=req.stacked_facecam_height,
    )


@app.get("/api/v1/studio/presets/{channel_id}", tags=["Studio"])
def studio_get_preset(channel_id: str):
    """Recupera o preset de layout de um canal específico."""
    preset = sqlite_store.get_channel_preset(channel_id)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset de canal não encontrado.")
    return preset


@app.post("/api/v1/studio/suggest-moments", tags=["Studio"])
def studio_suggest_moments(req: SuggestMomentsRequest):
    """Sugere e ranqueia momentos com base em áudio, palavras-chave e fronteiras de frase."""
    safe_path = validate_input_path(req.video_path)
    probe = MediaPrechecker(min_duration_s=1.0).probe(safe_path, require_audio=False)
    if not probe.is_valid:
        raise HTTPException(status_code=400, detail=probe.message)

    suggester = MomentSuggester()
    try:
        suggestions = suggester.suggest(
            video_path=safe_path,
            duration_s=probe.duration_s or 10.0,
            words=req.words,
            target_duration_s=req.target_duration_s,
            top_k=req.top_k,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "suggestions": suggestions,
        "duration_s": probe.duration_s,
        "warning": suggester.last_warning,
    }


@app.post("/api/v1/studio/realign-words", tags=["Studio"])
def studio_realign_words(req: RealignWordsRequest):
    """Realinha timestamps de palavras quando o operador edita o texto transcrito."""
    parsed_words = [
        WordTiming(
            word=str(w.get("word", "")),
            start_s=float(w.get("start_s", 0.0)),
            end_s=float(w.get("end_s", 0.0)),
        )
        for w in req.words
        if w.get("word") is not None
    ]
    realigned = subtitle_engine.realign_edited_words(
        words=parsed_words,
        edited_text=req.edited_text,
        clip_start_s=req.inicio_s,
        clip_end_s=req.fim_s,
    )
    return {
        "words": [w.to_dict() for w in realigned],
        "count": len(realigned),
    }


@app.post("/api/v1/studio/render-batch", tags=["Studio"])
def studio_render_batch(req: BatchRenderRequest, background_tasks: BackgroundTasks):
    """Enfileira vários cortes na fila SQLite e dispara o worker em background."""
    safe_path = validate_input_path(req.video_path)
    if not req.items:
        raise HTTPException(status_code=400, detail="Nenhum corte informado para o lote.")

    job_ids: List[str] = []
    for idx, item in enumerate(req.items):
        params = {
            "video_path": str(safe_path),
            "inicio_s": item.inicio_s,
            "fim_s": item.fim_s,
            "layout": req.layout,
            "facecam_rect": req.facecam_rect,
            "gameplay_rect": req.gameplay_rect,
            "stacked_facecam_height": req.stacked_facecam_height,
            "aspect_ratio": req.aspect_ratio,
            "solo_align": req.solo_align,
            "subtitles": req.words,
            "subtitles_are_absolute": req.subtitles_are_absolute,
            "subtitle_preset": req.subtitle_preset,
            "burn_subtitles": req.burn_subtitles,
            "hook_title": item.hook_title or req.hook_title,
            "edited_clip_text": item.edited_clip_text,
            "progress_bar": req.progress_bar,
            "enhance_voice": req.enhance_voice,
            "normalize_audio": req.normalize_audio,
            "watermark_text": req.watermark_text,
        }
        job = durable_queue.enqueue(
            org_id="org_default",
            job_type="render_clip",
            params=params,
            idempotency_key=f"batch_{int(time.time()*1000)}_{idx}_{item.inicio_s}_{item.fim_s}",
        )
        job_ids.append(job.job_id)

    batch_cancel = threading.Event()
    with _active_cancel_lock:
        _active_cancel_events.add(batch_cancel)

    def _run_batch(target_ids: List[str], ev: threading.Event):
        try:
            pipeline_runner.run_jobs(target_ids, cancel_event=ev)
        finally:
            with _active_cancel_lock:
                _active_cancel_events.discard(ev)

    background_tasks.add_task(_run_batch, list(job_ids), batch_cancel)
    return {"enqueued": len(job_ids), "job_ids": job_ids}


@app.post("/api/v1/studio/live/start", tags=["Studio Live"])
def studio_live_start(req: LiveStartRequest):
    """Inicia captura contínua ao vivo (YouTube, X, Twitch ou Kick) em buffer segmentado."""
    try:
        render_opts = {
            "layout": req.layout,
            "facecam_rect": req.facecam_rect,
            "gameplay_rect": req.gameplay_rect,
            "stacked_facecam_height": req.stacked_facecam_height,
            "aspect_ratio": req.aspect_ratio,
            "solo_align": req.solo_align,
            "burn_subtitles": req.burn_subtitles,
            "subtitle_preset": req.subtitle_preset,
            "language": req.language,
            "normalize_audio": req.normalize_audio,
            "watermark_text": req.watermark_text,
        }
        session = live_manager.start_session(
            url=req.url,
            segment_duration_s=req.segment_duration_s,
            retention_seconds=req.retention_seconds,
            auto_clip=req.auto_clip,
            auto_clip_mode=req.auto_clip_mode,
            auto_clip_duration_s=req.auto_clip_duration_s,
            auto_clip_threshold_db=req.auto_clip_threshold_db,
            auto_clip_keywords=req.auto_clip_keywords,
            default_render_opts=render_opts,
        )
        return session.get_status()
    except SecuritySSRFError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao iniciar live: {exc}")


@app.get("/api/v1/studio/live/status", tags=["Studio Live"])
def studio_live_status():
    """Retorna o estado atual da transmissão ao vivo, fila ativa e clipes gerados."""
    return live_manager.get_status()


@app.get("/api/v1/studio/live/stream.mp4", tags=["Studio Live"])
def studio_live_monitor_stream():
    """Transmite a live em MP4 fragmentado (fMP4) para o monitor integrado no Studio."""
    session = live_manager.current_session
    if not session or not session.running:
        raise HTTPException(status_code=404, detail="Nenhuma transmissão ao vivo ativa no momento.")
    return StreamingResponse(
        session.iter_preview_fmp4(),
        media_type="video/mp4",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.patch("/api/v1/studio/live/settings", tags=["Studio Live"])
def studio_live_update_settings(req: LiveSettingsRequest):
    """Atualiza modo de auto-clipping, gatilhos e opções de renderização sem interromper a captura."""
    render_opts: Dict[str, Any] = {}
    for key in (
        "layout",
        "facecam_rect",
        "gameplay_rect",
        "stacked_facecam_height",
        "aspect_ratio",
        "solo_align",
        "burn_subtitles",
        "hook_title",
        "progress_bar",
        "enhance_voice",
        "subtitle_preset",
        "language",
        "normalize_audio",
        "watermark_text",
    ):
        val = getattr(req, key)
        if val is not None:
            render_opts[key] = val
    try:
        return live_manager.update_settings(
            auto_clip=req.auto_clip,
            auto_clip_mode=req.auto_clip_mode,
            auto_clip_duration_s=req.auto_clip_duration_s,
            auto_clip_threshold_db=req.auto_clip_threshold_db,
            auto_clip_keywords=req.auto_clip_keywords,
            default_render_opts=render_opts or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/studio/live/mark", tags=["Studio Live"])
def studio_live_mark(req: LiveMarkRequest):
    """Cria um corte instantâneo dos últimos segundos do buffer ou agenda marcação com janela posterior."""
    render_opts: Dict[str, Any] = {}
    for key in (
        "layout",
        "facecam_rect",
        "gameplay_rect",
        "stacked_facecam_height",
        "aspect_ratio",
        "solo_align",
        "burn_subtitles",
        "hook_title",
        "progress_bar",
        "enhance_voice",
        "subtitle_preset",
        "language",
        "normalize_audio",
        "watermark_text",
    ):
        val = getattr(req, key)
        if val is not None:
            render_opts[key] = val

    try:
        return live_manager.mark_clip(
            pre_s=req.pre_s,
            post_s=req.post_s,
            titulo=req.titulo,
            render_opts=render_opts or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/studio/live/pending/{candidate_id}/cancel", tags=["Studio Live"])
def studio_live_cancel_pending(candidate_id: str):
    """Cancela uma marcação ao vivo que estava aguardando janela posterior (+15s)."""
    session = live_manager.current_session
    if not session:
        raise HTTPException(status_code=404, detail="Nenhuma sessão ao vivo encontrada.")
    if not session.cancel_pending_mark(candidate_id):
        raise HTTPException(status_code=404, detail="Marcação pendente não encontrada ou já processada.")
    return {"cancelled": True, "candidate_id": candidate_id}


@app.post("/api/v1/studio/live/suggestions/{suggestion_id}/approve", tags=["Studio Live"])
def studio_live_approve_suggestion(suggestion_id: str, req: Optional[LiveSettingsRequest] = None):
    """Aprova uma sugestão de corte automático e enfileira sua renderização."""
    session = live_manager.current_session
    if not session:
        raise HTTPException(status_code=404, detail="Nenhuma sessão ao vivo encontrada.")
    render_opts: Dict[str, Any] = {}
    if req is not None:
        for key in (
            "layout",
            "facecam_rect",
            "gameplay_rect",
            "stacked_facecam_height",
            "aspect_ratio",
            "solo_align",
            "burn_subtitles",
            "hook_title",
            "progress_bar",
            "enhance_voice",
            "subtitle_preset",
            "language",
            "normalize_audio",
            "watermark_text",
        ):
            val = getattr(req, key)
            if val is not None:
                render_opts[key] = val
    try:
        return session.approve_suggestion(suggestion_id, render_opts=render_opts or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/studio/live/suggestions/{suggestion_id}/dismiss", tags=["Studio Live"])
def studio_live_dismiss_suggestion(suggestion_id: str):
    """Descarta uma sugestão automática da live sem renderizar."""
    session = live_manager.current_session
    if not session or not session.dismiss_suggestion(suggestion_id):
        raise HTTPException(status_code=404, detail="Sugestão não encontrada.")
    return {"dismissed": True, "suggestion_id": suggestion_id}


@app.post("/api/v1/studio/live/clips/{filename}/approve", tags=["Studio Live"])
def studio_live_approve_clip(filename: str):
    """Marca um clipe gerado na live como aprovado/mantido."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=403, detail="Nome de arquivo inválido.")
    session = live_manager.current_session
    if not session or not session.approve_clip(filename):
        raise HTTPException(status_code=404, detail="Clipe não encontrado na sessão ao vivo.")
    return {"approved": True, "filename": filename}


@app.post("/api/v1/studio/live/replay", tags=["Studio Live"])
def studio_live_extract_replay(req: LiveReplayRequest):
    """Extrai um trecho bruto do buffer da live para o player principal do Studio (ajuste fino I/O)."""
    session = live_manager.current_session
    if not session:
        raise HTTPException(status_code=404, detail="Nenhuma sessão ao vivo disponível.")
    dest_dir = (vod_ingestor.projects_root / "proj_default" / "source").resolve()
    try:
        info = session.export_buffer_replay(
            dest_dir=dest_dir,
            inicio_s=req.inicio_s,
            fim_s=req.fim_s,
            duration_s=req.duration_s,
        )
        try:
            vod_ingestor.import_local_file(
                file_path=Path(info["file_path"]),
                authorization_ref="LIVE-BUFFER-REPLAY",
                project_id="proj_default",
                title=f"Replay Live ({info['inicio_s']}s–{info['fim_s']}s)",
            )
        except Exception:
            pass
        return info
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/studio/live/stop", tags=["Studio Live"])
def studio_live_stop():
    """Encerra a captura ao vivo atual."""
    return live_manager.stop_session()


@app.post("/api/v1/studio/live/cleanup", tags=["Studio Live"])
def studio_live_cleanup():
    """Remove buffers temporários de transmissões encerradas para liberar espaço."""
    return live_manager.cleanup_old_buffers()


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return round(total / (1024 * 1024), 2)


@app.get("/api/v1/studio/storage", tags=["Studio"])
def studio_storage_stats():
    """Retorna uso de disco dos clipes gerados, exportações e buffers ao vivo."""
    return {
        "output_clips_mb": _dir_size_mb(pipeline_runner.output_dir),
        "exports_mb": _dir_size_mb(package_exporter.exports_root),
        "live_buffers_mb": _dir_size_mb(DATA_DIR / "projects" / "proj_default" / "live"),
    }


_active_cancel_lock = threading.Lock()
_active_cancel_events: set[threading.Event] = set()


@app.post("/api/v1/studio/cancel", tags=["Studio"])
def studio_cancel_active_render():
    """Solicita cancelamento imediato dos renders manuais ou lotes ativos (sem afetar a live)."""
    with _active_cancel_lock:
        for ev in list(_active_cancel_events):
            ev.set()
    return {"status": "cancelling", "cancelled": True}


@app.get("/api/v1/studio/batch-status", tags=["Studio"])
def studio_batch_status(ids: str = Query("")):
    """Consulta o estado consolidado de uma lista de job_ids separados por vírgula."""
    job_ids = [x.strip() for x in ids.split(",") if x.strip()]
    jobs = []
    done = 0
    failed = 0
    for jid in job_ids:
        job = durable_queue.get_job(jid)
        if not job:
            continue
        d = job.to_dict()
        jobs.append(d)
        if job.status in (JobStatus.APROVADO, JobStatus.PUBLICADO):
            done += 1
        elif job.status in (JobStatus.FALHOU, JobStatus.CANCELADO):
            failed += 1
    total = len(jobs)
    active = max(0, total - done - failed)
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "active": active,
        "cancelled": any(j.get("status") == JobStatus.CANCELADO.value for j in jobs),
        "jobs": jobs,
    }


@app.post("/api/v1/studio/render-now", tags=["Studio"])
def render_studio_clip_immediate(req: StudioRenderRequest):
    """Renderiza um clipe imediatamente a partir dos parâmetros do Studio."""
    safe_path = validate_input_path(req.video_path)
    job_cancel = threading.Event()
    with _active_cancel_lock:
        _active_cancel_events.add(job_cancel)

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
        "stacked_facecam_height": req.stacked_facecam_height,
        "aspect_ratio": req.aspect_ratio,
        "solo_align": req.solo_align,
        "subtitles": subtitles_payload,
        "subtitles_are_absolute": subtitles_are_absolute,
        "subtitle_preset": req.subtitle_preset,
        "burn_subtitles": req.burn_subtitles,
        "edited_clip_text": req.edited_clip_text,
        "hook_title": req.hook_title,
        "progress_bar": req.progress_bar,
        "enhance_voice": req.enhance_voice,
        "normalize_audio": req.normalize_audio,
        "watermark_text": req.watermark_text,
    }

    try:
        result = pipeline_runner.render_clip(params, cancel_event=job_cancel)
    finally:
        with _active_cancel_lock:
            _active_cancel_events.discard(job_cancel)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error or "Falha na renderização do clipe")

    filename = Path(result.output_video_path).name if result.output_video_path else None
    extra_outputs = [
        {
            **ex,
            "download_url": f"/api/v1/studio/video/{ex['filename']}",
        }
        for ex in (result.metadata or {}).get("extra_outputs", [])
        if ex.get("filename")
    ]
    return {
        "success": True,
        "filename": filename,
        "output_path": result.output_video_path,
        "duration": result.duration,
        "download_url": f"/api/v1/studio/video/{filename}" if filename else None,
        "extra_outputs": extra_outputs,
        "metadata": result.metadata,
    }


# -----------------------------------------------------------------------------
# Painel Web Operacional (Calm Premium Design System)
# -----------------------------------------------------------------------------

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
def get_dashboard():
    studio_html = BUNDLE_ROOT / "fastclipper" / "web" / "studio.html"
    return HTMLResponse(content=studio_html.read_text(encoding="utf-8"), status_code=200)


# -----------------------------------------------------------------------------
# Rotas de Exportação de Pacotes e Métricas de Receita (Sprint 4)
# -----------------------------------------------------------------------------

@app.post("/api/v1/studio/generate-copy", tags=["Exportação & Receita"])
def studio_generate_copy(req: GenerateCopyRequest):
    """Gera título, descrição, gancho e hashtags por plataforma (offline ou LLM opcional)."""
    return generate_post_copy(
        transcript_text=req.transcript_text,
        platform=req.platform,
        channel_name=req.channel_name,
        use_llm=req.use_llm,
    )


@app.post("/api/v1/studio/export-package", tags=["Exportação & Receita"])
def studio_export_package(req: ExportPackageRequest):
    """Empacota MP4 + SRT + capa.jpg + texto pronto para postar em data/exports/."""
    if not req.clip_filename or "/" in req.clip_filename or "\\" in req.clip_filename or ".." in req.clip_filename:
        raise HTTPException(status_code=403, detail="Nome de arquivo inválido.")
    out_dir = pipeline_runner.output_dir.resolve()
    clip_path = (out_dir / req.clip_filename).resolve()
    if clip_path.parent != out_dir or not clip_path.is_file():
        raise HTTPException(status_code=404, detail="Clipe renderizado não encontrado.")

    return package_exporter.export_package(
        clip_path=clip_path,
        platform=req.platform,
        title=req.title,
        description=req.description,
        hashtags=req.hashtags,
        words=req.words,
        cover_timestamp_s=req.cover_timestamp_s,
        inicio_s=req.inicio_s,
        fim_s=req.fim_s,
    )


@app.get("/api/v1/studio/export-zip/{day}/{zip_name}", tags=["Exportação & Receita"])
def studio_download_export_zip(day: str, zip_name: str):
    """Baixa o pacote completo (.zip) gerado em data/exports/."""
    for part in (day, zip_name):
        if not part or "/" in part or "\\" in part or ".." in part:
            raise HTTPException(status_code=403, detail="Caminho de pacote inválido.")
    exports_root = package_exporter.exports_root.resolve()
    target = (exports_root / day / zip_name).resolve()
    if target.parent.parent != exports_root or not target.is_file() or target.suffix.lower() != ".zip":
        raise HTTPException(status_code=404, detail="Pacote ZIP não encontrado.")
    return FileResponse(str(target), media_type="application/zip", filename=target.name)


@app.post("/api/v1/studio/results", tags=["Exportação & Receita"])
def studio_save_result(req: ClipResultRequest):
    """Registra views e receita de um clipe publicado no SQLite."""
    return sqlite_store.save_clip_result(
        clip_filename=req.clip_filename,
        platform=req.platform,
        post_url=req.post_url,
        views_24h=req.views_24h,
        views_7d=req.views_7d,
        revenue_brl=req.revenue_brl,
        layout_mode=req.layout_mode,
        subtitle_preset=req.subtitle_preset,
        channel_id=req.channel_id,
        notes=req.notes,
    )


@app.get("/api/v1/studio/results", tags=["Exportação & Receita"])
def studio_get_results():
    """Lista resultados registrados e resumo acumulado de views e receita."""
    return {
        "results": sqlite_store.list_clip_results(),
        "summary": sqlite_store.get_results_summary(),
    }
