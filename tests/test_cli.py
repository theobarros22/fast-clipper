"""
Testes automatizados para a CLI unificada do Fast Clipper.
"""

import sys
from pathlib import Path
from unittest.mock import patch
import pytest

from fastclipper.cli import (
    cmd_status,
    cmd_analyze,
    cmd_collab,
    cmd_compare,
    cmd_queue,
    cmd_vault,
    main,
)


class DummyArgs:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_cli_status():
    args = DummyArgs()
    ret = cmd_status(args)
    assert ret == 0


def test_cli_precheck_valid():
    from fastclipper.cli import cmd_precheck
    root = Path(__file__).resolve().parents[1]
    video = root / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    args = DummyArgs(video=str(video), min_duration=2.0, no_audio_ok=False)
    ret = cmd_precheck(args)
    assert ret == 0


def test_cli_precheck_invalid():
    from fastclipper.cli import cmd_precheck
    root = Path(__file__).resolve().parents[1]
    video = root / "tests" / "fixtures" / "synthetic" / "sintetico_sem_audio.mp4"
    args = DummyArgs(video=str(video), min_duration=1.0, no_audio_ok=False)
    ret = cmd_precheck(args)
    assert ret == 1


def test_cli_analyze():
    args = DummyArgs(audio_threshold=2.5, chat_threshold=2.0)
    ret = cmd_analyze(args)
    assert ret == 0


def test_cli_collab_create():
    args = DummyArgs(
        subcmd="create",
        org="org_test",
        user="user_test",
        ts=150.0,
        titulo="Momento de Teste CLI",
        hours=8,
    )
    ret = cmd_collab(args)
    assert ret == 0


def test_cli_collab_handoff():
    args = DummyArgs(
        subcmd="handoff",
        org="org_test",
        user="user_test",
        ts=None,
        titulo=None,
        hours=8,
    )
    ret = cmd_collab(args)
    assert ret == 0


def test_cli_queue_enqueue_and_worker():
    args_enq = DummyArgs(
        subcmd="enqueue",
        org="org_test",
        key="idemp_key_cli_01",
        worker_id="w-01",
        db=":memory:",
    )
    ret_enq = cmd_queue(args_enq)
    assert ret_enq == 0

    args_worker = DummyArgs(
        subcmd="worker",
        org="org_test",
        key=None,
        worker_id="w-01",
        db=":memory:",
    )
    ret_worker = cmd_queue(args_worker)
    assert ret_worker == 0


def test_cli_simulate(tmp_path):
    root = Path(__file__).resolve().parents[1]
    video = root / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    buf_dir = tmp_path / "buffer"
    out_dir = tmp_path / "clips"

    args = DummyArgs(
        video=str(video),
        speed=10.0,
        pre=1.0,
        post=1.0,
        layout="solo",
        mark_at=[1.5],
        buffer_dir=str(buf_dir),
        output_dir=str(out_dir),
    )
    from fastclipper.cli import cmd_simulate
    ret = cmd_simulate(args)
    assert ret == 0


def test_cli_vault_store_and_kill_switch():
    args_store = DummyArgs(
        subcmd="store",
        org="org_test",
        autopost=True,
    )
    ret_store = cmd_vault(args_store)
    assert ret_store == 0

    args_kill = DummyArgs(
        subcmd="kill-switch",
        org="org_test",
        autopost=False,
    )
    ret_kill = cmd_vault(args_kill)
    assert ret_kill == 0

    args_reset = DummyArgs(
        subcmd="reset-kill-switch",
        org="org_test",
        autopost=False,
    )
    ret_reset = cmd_vault(args_reset)
    assert ret_reset == 0


def test_cli_compare_authorized(tmp_path):
    root = Path(__file__).resolve().parents[1]
    p1 = root / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    p2 = root / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_puro.mp4"

    args = DummyArgs(
        video1=str(p1),
        video2=str(p2),
        mode="split",
        start=0.5,
        end=2.0,
        offset=0.0,
        unauthorized=False,
        output_dir=str(tmp_path),
    )
    ret = cmd_compare(args)
    assert ret == 0


def test_cli_compare_unauthorized_blocks(tmp_path):
    root = Path(__file__).resolve().parents[1]
    p1 = root / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_facecam.mp4"
    p2 = root / "tests" / "fixtures" / "synthetic" / "sintetico_gameplay_puro.mp4"

    args = DummyArgs(
        video1=str(p1),
        video2=str(p2),
        mode="split",
        start=0.5,
        end=2.0,
        offset=0.0,
        unauthorized=True,
        output_dir=str(tmp_path),
    )
    ret = cmd_compare(args)
    assert ret == 1
