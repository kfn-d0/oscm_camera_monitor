from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

@dataclass
class CheckResult:
    ok: bool
    rtt_ms: Optional[float]
    method: str
    detail: str
    port: Optional[int]
    icmp_ok: Optional[bool] = None
    icmp_rtt_ms: Optional[float] = None

def tcp_connect(host: str, port: int, timeout: float) -> CheckResult:
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            rtt = (time.monotonic() - start) * 1000
            return CheckResult(
                ok=True,
                rtt_ms=round(rtt, 2),
                method="tcp",
                detail=f"TCP:{port} OK",
                port=port,
            )
    except socket.timeout:
        rtt = (time.monotonic() - start) * 1000
        return CheckResult(
            ok=False,
            rtt_ms=round(rtt, 2),
            method="tcp",
            detail=f"TCP:{port} TIMEOUT ({timeout}s)",
            port=port,
        )
    except ConnectionRefusedError:
        rtt = (time.monotonic() - start) * 1000
        return CheckResult(
            ok=False,
            rtt_ms=round(rtt, 2),
            method="tcp",
            detail=f"TCP:{port} RECUSADO",
            port=port,
        )
    except OSError as exc:
        return CheckResult(
            ok=False,
            rtt_ms=None,
            method="tcp",
            detail=f"TCP:{port} ERRO: {exc}",
            port=port,
        )

def icmp_ping(host: str, timeout: float) -> tuple[bool, Optional[float]]:

    start = time.monotonic()
    try:
        if sys.platform.startswith("win"):
            cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
        else:
            cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout + 2,
        )
        rtt = (time.monotonic() - start) * 1000
        return result.returncode == 0, round(rtt, 2)
    except subprocess.TimeoutExpired:
        return False, None
    except FileNotFoundError:
        logger.debug("ping binary not found, ICMP unavailable")
        return False, None
    except Exception as exc:
        logger.debug("ICMP ping error: %s", exc)
        return False, None

def check_camera(host: str, ports: List[int], timeout: float, icmp_only: bool = False) -> CheckResult:

    if icmp_only:
        icmp_ok, icmp_rtt = icmp_ping(host, timeout)
        return CheckResult(
            ok=icmp_ok,
            rtt_ms=icmp_rtt,
            method="ping",
            detail=f"ICMP OK ({icmp_rtt:.1f}ms)" if icmp_ok else "ICMP sem resposta (Host offline)",
            port=None,
            icmp_ok=icmp_ok,
            icmp_rtt_ms=icmp_rtt,
        )

    primary_port = 554 if 554 in ports else (ports[0] if ports else 554)
    tcp_result = tcp_connect(host, primary_port, timeout)

    if tcp_result.ok:
        icmp_ok, icmp_rtt = icmp_ping(host, timeout)
        logger.debug(
            "Camera %s TCP:%d OK (%.1f ms) | ICMP: %s",
            host, primary_port, tcp_result.rtt_ms or 0, "OK" if icmp_ok else "FAIL",
        )
        tcp_result.icmp_ok = icmp_ok
        tcp_result.icmp_rtt_ms = icmp_rtt
        tcp_result.method = "tcp554"
        return tcp_result

    logger.debug("Camera %s TCP:%d FAIL: %s — testing ICMP...", host, primary_port, tcp_result.detail)
    icmp_ok, icmp_rtt = icmp_ping(host, timeout)

    if icmp_ok:
        detail = (
            f"TCP:{primary_port} inacessível (RTSP down) — ICMP OK ({icmp_rtt:.0f}ms, host vivo)"
        )
        return CheckResult(
            ok=False,
            rtt_ms=icmp_rtt,
            method="tcp554+ping",
            detail=detail,
            port=primary_port,
            icmp_ok=True,
            icmp_rtt_ms=icmp_rtt,
        )
    else:
        detail = f"ICMP sem resposta + TCP:{primary_port} falhou — host inacessível"
        return CheckResult(
            ok=False,
            rtt_ms=None,
            method="tcp554+ping",
            detail=detail,
            port=primary_port,
            icmp_ok=False,
            icmp_rtt_ms=None,
        )