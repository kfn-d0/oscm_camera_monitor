from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

@dataclass
class CameraConfig:
    id: str
    name: str
    host: str
    location: str
    ports: List[int] = field(default_factory=lambda: [554])
    enabled: bool = True
    icmp_only: bool = False
    lat: Optional[float] = None
    lng: Optional[float] = None

@dataclass
class AppConfig:
    cameras: List[CameraConfig]
    check_interval: int = 30
    timeout: float = 2.0
    failure_threshold: int = 3
    recovery_threshold: int = 2
    timezone: str = "America/Fortaleza"
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    log_path: str = "/var/log/camera-monitor/camera-monitor.log"
    database_url: str = "sqlite:///camera_monitor.db"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_msg_format_offline: str = (
        "🚨 <b>CÂMERA OFFLINE</b>\\n"
        "Nome: <b>{name}</b>\\n"
        "Host: <code>{host}</code>\\n"
        "Hora: {timestamp}\\n"
        "Motivo: {detail}\\n"
        "Localização: {location}"
    )
    telegram_msg_format_online: str = (
        "✅ <b>CÂMERA ONLINE</b>\\n"
        "Nome: <b>{name}</b>\\n"
        "Host: <code>{host}</code>\\n"
        "Hora: {timestamp}\\n"
        "Tempo offline: {duration_str}"
    )
    retention_days: int = 90
    vacuum_interval_days: int = 7
    zabbix_api_key: str = ""  

    @property
    def enabled_cameras(self) -> List[CameraConfig]:
        return [c for c in self.cameras if c.enabled]

def load_config(config_path: str) -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    cameras: List[CameraConfig] = []
    for cam in raw.get("cameras", []):
        cameras.append(
            CameraConfig(
                id=str(cam["id"]),
                name=cam["name"],
                host=cam["host"],
                location=cam.get("location", ""),
                ports=cam.get("ports", [554]),
                enabled=cam.get("enabled", True),
                icmp_only=cam.get("icmp_only", False),
                lat=cam.get("lat"),
                lng=cam.get("lng"),
            )
        )

    monitoring = raw.get("monitoring", {})
    web        = raw.get("web", {})
    logging_cfg = raw.get("logging", {})
    retention  = raw.get("retention", {})
    telegram   = raw.get("telegram", {})

    telegram_token = os.environ.get(
        "TELEGRAM_BOT_TOKEN", telegram.get("bot_token", "")
    )
    telegram_chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID", telegram.get("chat_id", "")
    )
    database_url = os.environ.get(
        "DATABASE_URL", raw.get("database_url", "sqlite:///camera_monitor.db")
    )

    return AppConfig(
        cameras=cameras,
        check_interval=monitoring.get("check_interval", 30),
        timeout=monitoring.get("timeout", 2.0),
        failure_threshold=monitoring.get("failure_threshold", 3),
        recovery_threshold=monitoring.get("recovery_threshold", 2),
        timezone=raw.get("timezone", "America/Fortaleza"),
        web_host=web.get("host", "127.0.0.1"),
        web_port=web.get("port", 8080),
        log_path=logging_cfg.get("path", "/var/log/camera-monitor/camera-monitor.log"),
        database_url=database_url,
        telegram_bot_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        telegram_msg_format_offline=telegram.get("msg_format_offline", AppConfig.telegram_msg_format_offline),
        telegram_msg_format_online=telegram.get("msg_format_online", AppConfig.telegram_msg_format_online),
        retention_days=retention.get("checks_days", 90),
        vacuum_interval_days=retention.get("vacuum_interval_days", 7),
        zabbix_api_key=os.environ.get("ZABBIX_API_KEY", raw.get("zabbix_api_key", "")),
    )