"""
Piloto Automático: pasta de entrada → corte real com FFmpeg → fila de aprovação → Buffer (simulado).
"""

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from fastclipper.api.app import app
from fastclipper.autopilot.config import AutopilotConfig, WatchSource, autopilot_dir
from fastclipper.autopilot.discovery import VideoCandidate, list_source_videos
from fastclipper.autopilot.publisher import BufferClient, platform_caption, publish_candidate
from fastclipper.autopilot.runner import AutopilotRunner
from fastclipper.autopilot.state import AutopilotState
from fastclipper.autopilot.studio_client import StudioError

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
tc = TestClient(app)


class TestClientStudio:
    """Adapta o TestClient do FastAPI à interface do StudioClient."""

    def health(self):
        return tc.get("/health").status_code == 200

    def get(self, path):
        r = tc.get(path)
        if r.status_code >= 400:
            raise StudioError(f"GET {path} → {r.status_code}: {r.text}")
        return r.json()

    def post(self, path, body):
        r = tc.post(path, json=body)
        if r.status_code >= 400:
            raise StudioError(f"POST {path} → {r.status_code}: {r.text}")
        return r.json()


def _cfg(**kw):
    cfg = AutopilotConfig(clips_per_video=2, clip_seconds=4.0, page_handle="@teste", **kw)
    return cfg


def test_inbox_video_becomes_pending_candidates(tmp_path):
    cfg = _cfg()
    inbox = cfg.inbox_dir
    target = inbox / "live_teste.mp4"
    shutil.copy2(SYNTHETIC, target)
    state = AutopilotState(tmp_path / "ap.db")
    runner = AutopilotRunner(cfg, client=TestClientStudio(), state=state, poll_interval_s=0.2, render_timeout_s=300)

    summary = runner.run_once()
    assert summary["new_candidates"] >= 1, summary
    pend = state.list_candidates("pendente")
    assert pend and all(Path(c["clip_path"]).is_file() for c in pend)
    assert pend[0]["hook"]

    # segundo ciclo não reprocessa o mesmo arquivo
    assert runner.run_once()["new_candidates"] == 0
    target.unlink()


def test_first_time_source_only_takes_newest(tmp_path):
    cfg = _cfg(max_new_per_source=1)
    cfg.watchlist = [WatchSource(name="Canal A", url="https://www.youtube.com/@a/videos", authorization_ref="ok")]
    state = AutopilotState(tmp_path / "ap.db")
    vids = [VideoCandidate(key=f"yt:{i}", url=f"https://youtu.be/{i}", title=f"v{i}", duration_s=600) for i in range(4)]
    vids.append(VideoCandidate(key="yt:longo", url="https://youtu.be/longo", title="longo", duration_s=4 * 3600))
    runner = AutopilotRunner(cfg, client=TestClientStudio(), state=state,
                             lister=lambda url, limit=6: vids, duration_fn=lambda v: v.duration_s)
    picked = runner._discover_source(cfg.watchlist[0])
    assert [p[0].key for p in picked] == ["yt:0"]
    assert state.is_seen("yt:1") and state.is_seen("yt:3")  # histórico vira baseline

    # vídeo novo publicado depois entra; vídeo longo demais é ignorado
    state.mark_video("yt:0", "concluido", "Canal A")
    vids.insert(0, VideoCandidate(key="yt:novo", url="https://youtu.be/novo", title="novo", duration_s=900))
    picked = runner._discover_source(cfg.watchlist[0])
    assert [p[0].key for p in picked] == ["yt:novo"]


def test_flat_listing_skips_live_and_builds_urls():
    entries = [
        {"id": "abc", "url": "abc", "ie_key": "Youtube", "title": "VOD", "duration": 1200},
        {"id": "live1", "url": "live1", "ie_key": "Youtube", "title": "AO VIVO", "live_status": "is_live"},
    ]
    vids = list_source_videos("https://www.youtube.com/@x/videos", lister=lambda u, n: entries)
    assert len(vids) == 1 and vids[0].url == "https://www.youtube.com/watch?v=abc"


def test_watchsource_requires_authorization():
    try:
        WatchSource.from_dict({"name": "x", "url": "https://twitch.tv/x"})
    except ValueError as exc:
        assert "authorization_ref" in str(exc)
    else:
        raise AssertionError("deveria exigir authorization_ref")


def test_publish_candidate_with_fake_buffer(tmp_path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"0")
    sent = []

    def transport(payload):
        sent.append(payload)
        return {"data": {"createPost": {"post": {"id": f"p{len(sent)}", "dueAt": "2026-11-20T15:00:00Z"}}}}

    cand = {"clip_path": str(clip), "hook": "O carro escondido", "caption": "O carro escondido\n\n🎥 Canal",
            "hashtags": ["#gta6", "#gtavi", "#vicecity", "#leonida", "#gaming", "#cortes"]}
    log = publish_candidate(
        cand,
        {"api_key": "k", "channels": {"x": "chX", "reels": "chIG"}},
        {"type": "s3"},
        ["x", "reels", "tiktok"],
        buffer_client=BufferClient("k", transport=transport),
        uploader=lambda p, h: "https://cdn.example/c.mp4",
    )
    assert log["results"]["x"]["ok"] and log["results"]["reels"]["ok"]
    assert "tiktok" not in log["results"]  # sem canal mapeado
    x_inp = sent[0]["variables"]["input"]
    assert "#" not in x_inp["text"] and x_inp["assets"][0]["video"]["url"].startswith("https://")
    ig_inp = sent[1]["variables"]["input"]
    assert ig_inp["metadata"]["instagram"]["type"] == "reel"
    assert platform_caption("reels", "a", cand["hashtags"]).count("#") == 5


def test_approval_routes_without_buffer(tmp_path):
    st = AutopilotState(autopilot_dir() / "autopilot.db")
    clip = tmp_path / "clip.mp4"
    shutil.copy2(SYNTHETIC, clip)
    c = st.add_candidate(video_key="local:x", source_name="Canal", clip_path=str(clip), hook="Gancho",
                         caption="Legenda", hashtags=["#gta6"], score=0.8, inicio_s=1, fim_s=5)
    assert tc.get("/aprovacao").status_code == 200
    assert any(x["candidate_id"] == c["candidate_id"] for x in tc.get("/api/v1/autopilot/candidates").json())
    assert tc.get(f"/api/v1/autopilot/video/{c['candidate_id']}").status_code == 200
    r = tc.post(f"/api/v1/autopilot/candidates/{c['candidate_id']}/approve", json={"caption": "Nova legenda"})
    assert r.status_code == 200 and r.json()["status"] == "aprovado"
    assert r.json()["caption"] == "Nova legenda"
    assert "manualmente" in json.dumps(r.json()["publish_log"], ensure_ascii=False)
    status = tc.get("/api/v1/autopilot/status").json()
    assert status["buffer_ready"] is False
