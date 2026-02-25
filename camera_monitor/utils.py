from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def setup_logging(log_path: str, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(filename=str(log_file), encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:
        print(f"[WARNING] Cannot write logs to {log_path}: {e}", file=sys.stderr)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

_TELEGRAM_MIN_INTERVAL_S: float = 1.1   
_TELEGRAM_QUEUE_MAXSIZE:  int   = 50    


class Notifier:
    """
    Notificador Telegram com:
    - Rate limiting (mínimo de 1,1 s entre mensagens)
    - Fila assíncrona não-bloqueante (asyncio.Queue)
    - Worker background que consome a fila
    - Fallback síncrono (para chamadas fora de contexto async)
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        fmt_offline: Optional[str] = None,
        fmt_online:  Optional[str] = None,
    ) -> None:
        self._token   = bot_token
        self._chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)

        self._fmt_offline = fmt_offline or (
            "🚨 <b>CÂMERA OFFLINE</b>\n"
            "Nome: <b>{name}</b>\n"
            "Host: <code>{host}</code>\n"
            "Hora: {timestamp}\n"
            "Motivo: {detail}\n"
            "Localização: {location}"
        )
        self._fmt_online = fmt_online or (
            "✅ <b>CÂMERA ONLINE</b>\n"
            "Nome: <b>{name}</b>\n"
            "Host: <code>{host}</code>\n"
            "Hora: {timestamp}\n"
            "Tempo offline: {duration_str}"
        )

        self._last_sent_at: float = 0.0

        self._queue:  Optional[asyncio.Queue[str]] = None
        self._worker: Optional[asyncio.Task]       = None


    async def start(self) -> None:
        """Inicia o worker de envio em background. Chamar uma vez no startup."""
        if self._queue is not None:
            return  
        self._queue = asyncio.Queue(maxsize=_TELEGRAM_QUEUE_MAXSIZE)
        self._worker = asyncio.create_task(self._send_worker(), name="telegram-worker")
        logger.debug("Telegram worker iniciado (enabled=%s)", self._enabled)

    async def stop(self) -> None:
        """Para o worker graciosamente."""
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        self._queue = None


    async def _send_worker(self) -> None:
        """Consome a fila e envia mensagens respeitando o rate limit."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                try:
                    text = await self._queue.get()  
                except asyncio.CancelledError:
                    break

                try:
                    await self._send_async(client, text)
                except Exception as exc:
                    logger.error("Telegram worker error: %s", exc)
                finally:
                    self._queue.task_done()  

    async def _send_async(self, client: httpx.AsyncClient, text: str) -> None:
        """Envia uma mensagem respeitando o intervalo mínimo entre envios."""
        if not self._enabled:
            return

        elapsed = time.monotonic() - self._last_sent_at
        if elapsed < _TELEGRAM_MIN_INTERVAL_S:
            await asyncio.sleep(_TELEGRAM_MIN_INTERVAL_S - elapsed)

        url  = TELEGRAM_API.format(token=self._token)
        text = text.replace("\\n", "\n")
        try:
            resp = await client.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
            )
            self._last_sent_at = time.monotonic()
            if not resp.is_success:
                logger.error("Telegram error %d: %s", resp.status_code, resp.text[:200])
        except httpx.TimeoutException:
            logger.warning("Telegram timeout ao enviar mensagem")
        except Exception as exc:
            logger.error("Failed to send Telegram: %s", exc)

    def _enqueue(self, text: str) -> None:
        """Coloca uma mensagem na fila. Descarta silenciosamente se cheia."""
        if not self._enabled:
            return
        if self._queue is None:
            self._send_sync(text)
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            logger.warning("Fila Telegram cheia — mensagem descartada: %s", text[:80])

    def _send_sync(self, text: str) -> None:
        """Fallback síncrono para contextos fora de asyncio (ex: testes)."""
        if not self._enabled:
            return
        url  = TELEGRAM_API.format(token=self._token)
        text = text.replace("\\n", "\n")
        try:
            resp = httpx.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10.0,
            )
            if not resp.is_success:
                logger.error("Telegram error %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.error("Failed to send Telegram (sync): %s", exc)


    async def notify_offline_async(
        self, name: str, host: str, timestamp: str, detail: str, location: str
    ) -> None:
        try:
            text = self._fmt_offline.format(
                name=name, host=host, timestamp=timestamp,
                detail=detail, location=location,
            )
            self._enqueue(text)
        except Exception as exc:
            logger.error("Error formatting Telegram offline message: %s", exc)

    async def notify_online_async(
        self, name: str, host: str, timestamp: str, duration_str: str
    ) -> None:
        try:
            text = self._fmt_online.format(
                name=name, host=host, timestamp=timestamp,
                duration_str=duration_str,
            )
            self._enqueue(text)
        except Exception as exc:
            logger.error("Error formatting Telegram online message: %s", exc)


    def notify_offline(
        self, name: str, host: str, timestamp: str, detail: str, location: str
    ) -> None:
        try:
            text = self._fmt_offline.format(
                name=name, host=host, timestamp=timestamp,
                detail=detail, location=location,
            )
            self._enqueue(text)
        except Exception as exc:
            logger.error("Error formatting Telegram offline message: %s", exc)

    def notify_online(
        self, name: str, host: str, timestamp: str, duration_str: str
    ) -> None:
        try:
            text = self._fmt_online.format(
                name=name, host=host, timestamp=timestamp,
                duration_str=duration_str,
            )
            self._enqueue(text)
        except Exception as exc:
            logger.error("Error formatting Telegram online message: %s", exc)