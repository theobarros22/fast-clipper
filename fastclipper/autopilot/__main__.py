"""
CLI do Piloto Automático.

  python -m fastclipper.autopilot run-once          # um ciclo (é o que o Agendador do Windows chama)
  python -m fastclipper.autopilot loop --minutos 15 # ciclo contínuo nesta janela
  python -m fastclipper.autopilot status
  python -m fastclipper.autopilot canais-buffer     # lista IDs dos canais do Buffer para o autopilot.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from fastclipper.autopilot.config import autopilot_dir, load_config


def _setup_logging() -> None:
    log_file = autopilot_dir() / "autopilot.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")],
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fastclipper.autopilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-once")
    lp = sub.add_parser("loop")
    lp.add_argument("--minutos", type=float, default=15.0)
    sub.add_parser("status")
    sub.add_parser("canais-buffer")
    args = ap.parse_args(argv)
    _setup_logging()
    log = logging.getLogger("fastclipper.autopilot")

    if args.cmd == "canais-buffer":
        from fastclipper.autopilot.publisher import BufferClient
        cfg = load_config()
        for ch in BufferClient((cfg.buffer or {}).get("api_key", "")).list_channels():
            print(f"{ch['service']:<12} {ch['id']}  {ch.get('displayName') or ch.get('name')}")
        return 0

    if args.cmd == "status":
        from fastclipper.autopilot.state import AutopilotState
        cfg = load_config()
        st = AutopilotState(cfg.db_path)
        print(json.dumps({"fila": st.counts(), "videos": st.list_videos(10)}, ensure_ascii=False, indent=2))
        return 0

    from fastclipper.autopilot.runner import AutopilotRunner

    lock = autopilot_dir() / ".ciclo.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 3 * 3600:
        log.info("Outro ciclo ainda está rodando (lock %s). Saindo.", lock)
        return 0

    def _cycle() -> int:
        lock.write_text(str(time.time()))
        try:
            summary = AutopilotRunner(load_config()).run_once()
            log.info("Ciclo concluído: %s", json.dumps(summary, ensure_ascii=False))
            return 1 if summary["errors"] and not summary["processed"] else 0
        except Exception as exc:
            log.error("Ciclo abortado: %s", exc)
            return 2
        finally:
            try:
                lock.unlink()
            except OSError:
                lock.write_text("0")  # sem permissão de apagar: zera o lock
                import os
                os.utime(lock, (0, 0))

    if args.cmd == "run-once":
        return _cycle()
    while True:
        _cycle()
        log.info("Próximo ciclo em %.0f min.", args.minutos)
        time.sleep(args.minutos * 60)


if __name__ == "__main__":
    raise SystemExit(main())
