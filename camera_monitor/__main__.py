from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import uvicorn

from .config import load_config
from .database import Database
from .utils import setup_logging, Notifier
from .monitor import run_monitor_loop
from .web.app import create_app

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Camera Monitor – IP Camera Availability Monitor")
    parser.add_argument("--config",  default="data/config.yaml", help="Path to config.yaml")
    parser.add_argument("--once",    action="store_true",          help="One check cycle then exit")
    parser.add_argument("--no-web",  action="store_true",          help="Disable web dashboard")
    parser.add_argument("--debug",   action="store_true",          help="Enable DEBUG logging")
    return parser.parse_args()


def _run_web_server(app, host: str, port: int) -> None:
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


def _sync_cameras_to_db(cfg, db: Database) -> None:
    for cam in cfg.cameras:
        db.upsert_camera(
            cam.id, cam.name, cam.host, cam.location,
            cam.ports, cam.enabled, cam.icmp_only, cam.lat, cam.lng,
        )
        db.ensure_state(cam.id)
    logger.info("Synced %d camera(s) to database.", len(cfg.cameras))


async def _main_async(args: argparse.Namespace) -> None:
    """Ponto de entrada assíncrono — inicia o Notifier worker antes do loop."""
    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(cfg.log_path, level=logging.DEBUG if args.debug else logging.INFO)

    logger.info("=" * 40)
    logger.info("Camera Monitor starting up")
    logger.info("=" * 40)

    db = Database(cfg.database_url)
    notifier = Notifier(
        cfg.telegram_bot_token,
        cfg.telegram_chat_id,
        fmt_offline=cfg.telegram_msg_format_offline,
        fmt_online=cfg.telegram_msg_format_online,
    )

    await notifier.start()

    _sync_cameras_to_db(cfg, db)

    if not args.no_web and not args.once:
        app = create_app(db, cfg, notifier, config_path=args.config)
        threading.Thread(
            target=_run_web_server,
            args=(app, cfg.web_host, cfg.web_port),
            daemon=True,
        ).start()
        logger.info("Web dashboard: http://%s:%d", cfg.web_host, cfg.web_port)

    try:
        await run_monitor_loop(cfg, db, notifier, once=args.once)
    finally:
        await notifier.stop()
        logger.info("Shutdown complete.")


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")


if __name__ == "__main__":
    main()