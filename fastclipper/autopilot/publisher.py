"""
Publicação após aprovação humana: sobe o MP4 para um armazenamento público (S3/Cloudflare R2)
e cria o post no Buffer (GraphQL, chave pessoal) para cada rede configurada.

Buffer não aceita upload de arquivo: exige uma URL HTTPS pública e permanente do MP4.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

BUFFER_ENDPOINT = "https://api.buffer.com"

CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}
"""

GET_ORGS = "query { account { organizations { id name } } }"
GET_CHANNELS = """
query GetChannels($org: OrganizationId!) {
  channels(input: { organizationId: $org }) { id name displayName service isQueuePaused }
}
"""


class PublishError(RuntimeError):
    pass


def platform_caption(platform: str, caption: str, hashtags: List[str]) -> str:
    tags = [t if t.startswith("#") else f"#{t}" for t in hashtags if t]
    if platform == "x":
        return caption  # sem hashtags no X
    if platform == "reels":
        tags = tags[:5]
    else:
        tags = tags[:3]
    return caption + ("\n\n" + " ".join(tags) if tags else "")


def platform_metadata(platform: str, title: str) -> Optional[Dict[str, Any]]:
    if platform == "reels":
        return {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    if platform == "shorts":
        return {"youtube": {"title": title[:100] or "GTA 6"}}
    if platform == "tiktok":
        return {"tiktok": {"title": title[:90]}}
    return None


class BufferClient:
    def __init__(self, api_key: str, endpoint: str = BUFFER_ENDPOINT,
                 transport: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None):
        if not api_key:
            raise PublishError("Chave do Buffer não configurada (buffer.api_key).")
        self.api_key = api_key
        self.endpoint = endpoint
        self._transport = transport

    def _gql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        if self._transport:
            return self._transport(payload)
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise PublishError(f"Buffer HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}") from exc
        if data.get("errors"):
            raise PublishError(f"Buffer: {data['errors'][0].get('message')}")
        return data.get("data") or {}

    def list_channels(self) -> List[Dict[str, Any]]:
        orgs = (self._gql(GET_ORGS).get("account") or {}).get("organizations") or []
        out: List[Dict[str, Any]] = []
        for org in orgs:
            for ch in self._gql(GET_CHANNELS, {"org": org["id"]}).get("channels") or []:
                out.append({**ch, "organization": org.get("name")})
        return out

    def create_video_post(self, channel_id: str, text: str, video_url: str, mode: str,
                          metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        inp: Dict[str, Any] = {
            "channelId": channel_id,
            "text": text,
            "schedulingType": "automatic",
            "mode": "addToQueue",
            "assets": [{"video": {"url": video_url}}],
        }
        if mode == "draft":
            inp["saveToDraft"] = True
        if metadata:
            inp["metadata"] = metadata
        res = self._gql(CREATE_POST, {"input": inp}).get("createPost") or {}
        if res.get("message"):
            raise PublishError(res["message"])
        return res.get("post") or {}


def upload_public(clip_path: Path, media_host: Dict[str, Any]) -> str:
    """Sobe para S3/R2 e devolve a URL pública. Exige boto3 e um bucket com acesso público."""
    if not media_host or media_host.get("type") != "s3":
        raise PublishError(
            "media_host não configurado. O Buffer precisa de uma URL pública do MP4 "
            "(configure um bucket Cloudflare R2/S3 em autopilot.json)."
        )
    try:
        import boto3  # type: ignore
    except ImportError as exc:
        raise PublishError("Instale boto3: .venv\\Scripts\\pip install boto3") from exc
    key = f"{media_host.get('prefix', 'clips').strip('/')}/{clip_path.name}"
    s3 = boto3.client(
        "s3",
        endpoint_url=media_host.get("endpoint_url") or None,
        aws_access_key_id=media_host.get("access_key_id"),
        aws_secret_access_key=media_host.get("secret_access_key"),
        region_name=media_host.get("region", "auto"),
    )
    s3.upload_file(str(clip_path), media_host["bucket"], key, ExtraArgs={"ContentType": "video/mp4"})
    return f"{media_host['public_base_url'].rstrip('/')}/{key}"


def publish_candidate(
    candidate: Dict[str, Any],
    buffer_cfg: Dict[str, Any],
    media_host: Dict[str, Any],
    platforms: List[str],
    buffer_client: Optional[BufferClient] = None,
    uploader: Callable[[Path, Dict[str, Any]], str] = upload_public,
) -> Dict[str, Any]:
    """Retorna um log por rede. Lança PublishError só se nada puder ser feito."""
    channels: Dict[str, str] = (buffer_cfg or {}).get("channels") or {}
    targets = [p for p in platforms if channels.get(p)]
    if not targets:
        raise PublishError("Nenhum canal do Buffer mapeado em buffer.channels para as redes escolhidas.")
    clip = Path(candidate["clip_path"])
    if not clip.is_file():
        raise PublishError(f"Arquivo do clipe não existe mais: {clip}")

    client = buffer_client or BufferClient(buffer_cfg.get("api_key", ""))
    video_url = uploader(clip, media_host)
    mode = buffer_cfg.get("mode", "queue")
    title = candidate.get("hook") or "GTA 6"
    log: Dict[str, Any] = {"video_url": video_url, "results": {}}
    for plat in targets:
        try:
            post = client.create_video_post(
                channel_id=channels[plat],
                text=platform_caption(plat, candidate.get("caption") or title, candidate.get("hashtags") or []),
                video_url=video_url,
                mode=mode,
                metadata=platform_metadata(plat, title),
            )
            log["results"][plat] = {"ok": True, "post_id": post.get("id"), "due_at": post.get("dueAt")}
        except Exception as exc:
            log["results"][plat] = {"ok": False, "error": str(exc)}
    return log
