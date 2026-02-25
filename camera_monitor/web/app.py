from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Form, HTTPException, Query, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..config import AppConfig
from ..database import Database
from ..utils import Notifier
from ..monitor import get_db_detailed_stats, get_disk_usage, get_memory_usage, get_process_memory_mb

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()

def _fmt_timestamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return iso

def _fmt_duration(seconds: float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def create_app(db: Database, cfg: AppConfig, notifier: Notifier, config_path: str = "") -> FastAPI:
    app = FastAPI(
        title="Camera Monitor",
        description="Camera IP Monitoring Dashboard – São Luís",
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        public_paths = ["/login", "/health", "/settings/test-zabbix", "/settings/test-telegram"]
        if request.url.path in public_paths or any(
            request.url.path.startswith(p) for p in ("/static", "/api/zabbix")
        ):
            return await call_next(request)

        if not request.session.get("authenticated"):
            return RedirectResponse(url="/login", status_code=303)

        return await call_next(request)

    app.add_middleware(SessionMiddleware, secret_key="camera-monitor-super-secret-key")
    failed_attempts = {}

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def fmt_ts(iso: Optional[str]) -> str:
        if not iso:
            return "—"
        return _fmt_timestamp(iso)

    def fmt_duration(opened: Optional[str], closed: Optional[str] = None) -> str:
        if not opened:
            return "—"
        try:
            start = datetime.fromisoformat(opened)
            end = (
                datetime.fromisoformat(closed)
                if closed
                else datetime.now(tz=timezone.utc)
            )
            secs = int((end - start).total_seconds())
            return _fmt_duration(secs)
        except Exception:
            return "—"

    def status_badge(status: Optional[str]) -> str:
        mapping = {
            "ONLINE":   "badge-online",
            "OFFLINE":  "badge-offline",
            "UNKNOWN":  "badge-unknown",
            "OPEN":     "badge-offline",
            "RESOLVED": "badge-online",
        }
        return mapping.get(status or "", "badge-unknown")

    def parse_ports(ports_json: Optional[str]) -> str:
        if not ports_json:
            return "—"
        try:
            return ", ".join(str(p) for p in json.loads(ports_json))
        except Exception:
            return ports_json or "—"

    def ticket_code(ticket_id: int, opened_at: Optional[str]) -> str:
        try:
            dt = datetime.fromisoformat(opened_at).astimezone()
            return dt.strftime("%d%m%Y") + f"-{ticket_id}"
        except Exception:
            return str(ticket_id)

    templates.env.filters["fmt_ts"]       = fmt_ts
    templates.env.filters["fmt_duration"] = fmt_duration
    templates.env.filters["status_badge"] = status_badge
    templates.env.filters["parse_ports"]  = parse_ports
    templates.env.filters["ticket_code"]  = ticket_code
    templates.env.globals["now"] = lambda: datetime.now(tz=timezone.utc).astimezone().strftime(
        "%d/%m/%Y %H:%M:%S"
    )

    def _build_map_markers(cameras_with_status: list) -> str:
        markers = []
        cfg_map = {c.id: c for c in cfg.cameras}
        for cam in cameras_with_status:
            cc = cfg_map.get(cam["id"])
            if not cc or cc.lat is None or cc.lng is None:
                continue
            markers.append({
                "id":       cam["id"],
                "name":     cam["name"],
                "host":     cam["host"],
                "location": cam["location"] or "",
                "status":   cam["last_status"] or "UNKNOWN",
                "lat":      cc.lat,
                "lng":      cc.lng,
                "rtt":      cam["last_rtt_ms"],
            })
        return json.dumps(markers)

    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request):
        if request.session.get("authenticated"):
            return RedirectResponse(url="/", status_code=303)
        return templates.TemplateResponse("login.html", {"request": request, "error": None})

    @app.post("/login", response_class=HTMLResponse)
    async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
        ip = request.client.host if request.client else "unknown"

        if failed_attempts.get(ip, 0) >= 10:
            return templates.TemplateResponse("login.html", {
                "request": request, 
                "error": "Muitas tentativas falhas. Bloqueado temporariamente (reinicie o servidor se necessário)."
            })

        if username == "admin" and password == "admin":
            request.session["authenticated"] = True
            failed_attempts[ip] = 0
            return RedirectResponse(url="/", status_code=303)

        failed_attempts[ip] = failed_attempts.get(ip, 0) + 1
        return templates.TemplateResponse("login.html", {
            "request": request, 
            "error": f"Usuário ou senha incorretos. Tentativa {failed_attempts[ip]} de 10."
        })

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    def _disk_path() -> str:
        if sys.platform.startswith("win"):
            db_drive = Path(db._path).resolve().anchor
            return db_drive if db_drive else "C:\\\\"
        return "/"

    def _sync_config_yaml():
        if not config_path or not Path(config_path).exists():
            return
        import yaml as _yaml
        path = Path(config_path)
        with open(path, "r", encoding="utf-8") as f:
            raw = _yaml.safe_load(f) or {}

        cam_list = []
        for c in cfg.cameras:
            entry = {
                "id":       c.id,
                "name":     c.name,
                "host":     c.host,
                "location": c.location,
                "ports":    c.ports,
                "enabled":  c.enabled,
                "icmp_only": c.icmp_only,
            }
            if c.lat is not None: entry["lat"] = c.lat
            if c.lng is not None: entry["lng"] = c.lng
            cam_list.append(entry)

        raw["cameras"] = cam_list

        if "telegram" not in raw:
            raw["telegram"] = {}
        raw["telegram"]["bot_token"] = cfg.telegram_bot_token
        raw["telegram"]["chat_id"] = cfg.telegram_chat_id
        raw["telegram"]["msg_format_offline"] = cfg.telegram_msg_format_offline
        raw["telegram"]["msg_format_online"] = cfg.telegram_msg_format_online

        tmp = path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _yaml.dump(raw, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        tmp.replace(path)
        logger.info("config.yaml sincronizado com %d câmeras e novas configs", len(cam_list))

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        stats   = db.get_overview_stats()
        cameras = db.get_cameras_with_status()
        markers_json = _build_map_markers(cameras)
        return templates.TemplateResponse(
            "overview.html",
            {
                "request":      request,
                "stats":        stats,
                "cameras":      cameras,
                "markers_json": markers_json,
                "active_page":  "overview",
            },
        )

    @app.get("/tickets", response_class=HTMLResponse)
    async def tickets_page(
        request: Request,
        status: Optional[str] = Query(None),
    ):
        if status not in {None, "OPEN", "RESOLVED"}:
            raise HTTPException(400, "Invalid status filter")
        tickets = db.get_all_tickets(status_filter=status, limit=200)
        return templates.TemplateResponse(
            "tickets.html",
            {
                "request":       request,
                "tickets":       tickets,
                "status_filter": status,
                "active_page":   "tickets",
            },
        )

    @app.get("/camera/{camera_id}", response_class=HTMLResponse)
    async def camera_detail(request: Request, camera_id: str):
        cam = db.get_camera(camera_id)
        if not cam:
            raise HTTPException(404, "Camera not found")

        checks      = db.get_recent_checks(camera_id, limit=30)
        state       = db.get_state(camera_id)
        open_ticket = db.get_open_ticket(camera_id)
        all_tickets = db.get_all_tickets(limit=200)
        cam_tickets = [t for t in all_tickets if t["camera_id"] == camera_id]

        raw_losses = db.get_recent_losses(camera_id, limit=50)
        recent_losses = []
        last_loss_dt = None
        for loss in raw_losses:
            try:
                dt = datetime.fromisoformat(loss["ts"])
                if last_loss_dt is None or (last_loss_dt - dt).total_seconds() > 300:
                    recent_losses.append(loss)
                    last_loss_dt = dt
                if len(recent_losses) >= 10:
                    break
            except Exception:
                continue

        cam_cfg     = next((c for c in cfg.cameras if c.id == camera_id), None)

        return templates.TemplateResponse(
            "camera_detail.html",
            {
                "request":        request,
                "cam":            cam,
                "cam_cfg":        cam_cfg,
                "checks":         checks,
                "recent_losses":  recent_losses,
                "state":          state,
                "open_ticket":    open_ticket,
                "cam_tickets":    cam_tickets,
                "active_page":    "none",
            },
        )

    @app.post("/camera/{camera_id}/simulate-failure")
    async def simulate_failure(camera_id: str):
        cam_db = db.get_camera(camera_id)
        if not cam_db:
            raise HTTPException(404, "Camera not found")

        existing = db.get_open_ticket(camera_id)
        if existing:
            return JSONResponse(
                status_code=409,
                content={
                    "status":  "already_offline",
                    "message": f"Câmera já possui ticket aberto #{ existing['id'] }",
                },
            )

        cam_cfg = next((c for c in cfg.cameras if c.id == camera_id), None)
        ts = _now_iso()

        for _ in range(cfg.failure_threshold):
            db.insert_check(
                camera_id=camera_id,
                ts=ts,
                ok=False,
                rtt_ms=None,
                method="simulated",
                detail="[SIMULAÇÃO] Falha ICMP forçada para teste",
                port=None,
            )

        db.update_state(
            camera_id=camera_id,
            consecutive_failures=cfg.failure_threshold,
            consecutive_successes=0,
            last_status="OFFLINE",
            last_change_ts=ts,
        )

        ticket_id = db.open_ticket(
            camera_id, "[SIMULAÇÃO] Falha ICMP forçada para teste", ts
        )

        notifier.notify_offline(
            name=cam_db["name"],
            host=cam_db["host"],
            timestamp=_fmt_timestamp(ts),
            detail="[SIMULAÇÃO] Falha ICMP forçada para teste",
            location=cam_db.get("location", ""),
        )

        logger.warning(
            "SIMULATE_FAIL  camera=%s  ticket=#%s", cam_db["name"], ticket_id
        )

        return JSONResponse(
            content={
                "status":    "ok",
                "ticket_id": ticket_id,
                "message":   f"Falha simulada! Ticket #{ticket_id} aberto e notificação Telegram enviada.",
            }
        )

    @app.get("/cameras/add", response_class=HTMLResponse)
    async def add_camera_form(request: Request, edit: Optional[str] = Query(None)):
        existing = db.get_all_cameras()

        max_num = 0
        for c in existing:
            match = re.search(r'cam(\d+)', c["id"])
            if match:
                max_num = max(max_num, int(match.group(1)))
        next_num = max_num + 1

        form_data = {}
        if edit:
            cam = db.get_camera(edit)
            if cam:
                cc = next((c for c in cfg.cameras if c.id == edit), None)
                form_data = dict(cam)
                form_data["ports"] = ", ".join(str(p) for p in json.loads(cam["ports_json"]))
                form_data["icmp_only"] = bool(cam.get("icmp_only"))
                if cc:
                    form_data["lat"] = cc.lat
                    form_data["lng"] = cc.lng
                form_data["is_edit"] = True

        return templates.TemplateResponse(
            "add_camera.html",
            {
                "request":          request,
                "active_page":      "add_camera",
                "existing_cameras": existing,
                "next_num":         next_num,
                "form":             form_data,
                "error":            None,
                "success":          None,
            },
        )

    @app.post("/cameras/add", response_class=HTMLResponse)
    async def add_camera_submit(
        request:  Request,
        cam_id:   str  = Form(...),
        name:     str  = Form(...),
        host:     str  = Form(...),
        location: str  = Form(""),
        ports:    str  = Form("554"),
        icmp_only: str = Form("off"),
        lat:      str  = Form(""),
        lng:      str  = Form(""),
        is_edit:  bool = Form(False),
        old_id:   str  = Form(""),
    ):
        form_data = {
            "cam_id": cam_id, "name": name, "host": host,
            "location": location, "ports": ports,
            "icmp_only": icmp_only == "on", "lat": lat, "lng": lng,
            "is_edit": is_edit,
        }
        existing = db.get_all_cameras()

        cam_id = cam_id.strip().lower()
        if not re.match(r'^[a-z0-9\-]+$', cam_id):
            return templates.TemplateResponse("add_camera.html", {
                "request": request, "active_page": "add_camera",
                "existing_cameras": existing, "form": form_data,
                "error": "ID inválido. Use apenas letras minúsculas, números e hífens.",
                "success": None,
            })

        if not is_edit and db.get_camera(cam_id):
            return templates.TemplateResponse("add_camera.html", {
                "request": request, "active_page": "add_camera",
                "existing_cameras": existing, "form": form_data,
                "error": f"ID '{ cam_id }' já existe. Escolha outro ID.",
                "success": None,
            })

        if not host.strip():
            return templates.TemplateResponse("add_camera.html", {
                "request": request, "active_page": "add_camera",
                "existing_cameras": existing, "form": form_data,
                "error": "Host / IP é obrigatório.",
                "success": None,
            })

        try:
            port_list = [int(p.strip()) for p in ports.split(",") if p.strip()]
            if not port_list:
                port_list = [554]
        except ValueError:
            port_list = [554]

        lat_f = float(lat) if lat.strip() else None
        lng_f = float(lng) if lng.strip() else None
        is_icmp_only = icmp_only == "on"

        if is_edit and old_id and old_id != cam_id:
            db.rename_camera(old_id, cam_id)
            cfg.cameras = [c for c in cfg.cameras if c.id != old_id]

        db.upsert_camera(
            cam_id=cam_id, name=name.strip(), host=host.strip(),
            location=location.strip(), ports=port_list, 
            enabled=True, icmp_only=is_icmp_only,
            lat=lat_f, lng=lng_f,
        )
        db.ensure_state(cam_id)

        from ..config import CameraConfig as CC
        new_cam = CC(
            id=cam_id, name=name.strip(), host=host.strip(),
            location=location.strip(), ports=port_list, 
            enabled=True, icmp_only=is_icmp_only,
            lat=lat_f, lng=lng_f,
        )

        updated = False
        for i, c in enumerate(cfg.cameras):
            if c.id == cam_id:
                cfg.cameras[i] = new_cam
                updated = True
                break
        if not updated:
            cfg.cameras.append(new_cam)

        try:
            _sync_config_yaml()
        except Exception as exc:
            logger.warning("Nao foi possivel atualizar config.yaml: %s", exc)

        logger.info("CAMERA_UPSERT id=%s name='%s' edit=%s", cam_id, name.strip(), is_edit)

        return templates.TemplateResponse(
            "add_camera.html",
            {
                "request":          request,
                "active_page":      "add_camera",
                "existing_cameras": db.get_all_cameras(),
                "form":             {},
                "error":            None,
                "success":          {"id": cam_id, "name": name.strip(), "host": host.strip(), "action": "editada" if is_edit else "adicionada"},
            },
        )

    @app.post("/cameras/delete")
    async def delete_camera_submit(cam_id: str = Form(...)):
        db.delete_camera(cam_id)
        cfg.cameras = [c for c in cfg.cameras if c.id != cam_id]
        try:
            _sync_config_yaml()
        except Exception as exc:
            logger.warning("Nao foi possivel atualizar config.yaml na delecao: %s", exc)

        logger.info("CAMERA_DELETED id=%s", cam_id)
        return RedirectResponse(url="/cameras/add", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_get(request: Request):
        display_cfg = AppConfig(**cfg.__dict__)
        display_cfg.telegram_msg_format_offline = cfg.telegram_msg_format_offline.replace("\\n", "\n")
        display_cfg.telegram_msg_format_online = cfg.telegram_msg_format_online.replace("\\n", "\n")

        return templates.TemplateResponse(
            "settings.html",
            {
                "request":     request,
                "active_page": "settings",
                "cfg":         display_cfg,
                "error":       None,
                "success":     None,
            }
        )

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_post(
        request: Request,
        bot_token: str = Form(...),
        chat_id:   str = Form(...),
        fmt_offline: str = Form(...),
        fmt_online:  str = Form(...),
    ):
        cfg.telegram_bot_token = bot_token.strip()
        cfg.telegram_chat_id = chat_id.strip()
        cfg.telegram_msg_format_offline = fmt_offline.strip().replace("\n", "\\n").replace("\r", "")
        cfg.telegram_msg_format_online = fmt_online.strip().replace("\n", "\\n").replace("\r", "")

        notifier._token = cfg.telegram_bot_token
        notifier._chat_id = cfg.telegram_chat_id
        notifier._enabled = bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
        notifier._fmt_offline = cfg.telegram_msg_format_offline
        notifier._fmt_online = cfg.telegram_msg_format_online

        try:
            _sync_config_yaml()
            success = "Configurações do Telegram salvas e aplicadas!"
        except Exception as e:
            return templates.TemplateResponse("settings.html", {
                "request": request, "active_page": "settings", "cfg": cfg, "error": str(e), "success": None
            })

        display_cfg = AppConfig(**cfg.__dict__)
        display_cfg.telegram_msg_format_offline = cfg.telegram_msg_format_offline.replace("\\n", "\n")
        display_cfg.telegram_msg_format_online = cfg.telegram_msg_format_online.replace("\\n", "\n")

        return templates.TemplateResponse(
            "settings.html",
            {
                "request":     request,
                "active_page": "settings",
                "cfg":         display_cfg,
                "error":       None,
                "success":     success,
            }
        )

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        s    = get_db_detailed_stats(
            db_path=db._path,
            retention_days=cfg.retention_days,
            num_cameras=len(cfg.enabled_cameras),
            interval_s=cfg.check_interval,
        )
        disk = get_disk_usage(_disk_path())
        mem  = get_memory_usage()
        proc = get_process_memory_mb()
        return templates.TemplateResponse(
            "status.html",
            {
                "request":     request,
                "s":           s,
                "disk":        disk,
                "mem":         mem,
                "proc_mem_mb": proc,
                "vacuum_days": cfg.vacuum_interval_days,
                "timeout_s":   cfg.timeout,
                "fail_thr":    cfg.failure_threshold,
                "rec_thr":     cfg.recovery_threshold,
                "web_host":    cfg.web_host,
                "web_port":    cfg.web_port,
                "active_page": "status",
            },
        )

    @app.post("/settings/test-zabbix")
    async def test_zabbix(ip: str = Form(...)):
        import socket
        try:
            ports = [10051, 8080, 80]
            reachable = []
            for port in ports:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    if s.connect_ex((ip, port)) == 0:
                        reachable.append(str(port))
            if reachable:
                return {"ok": True, "message": f"Conexão OK nas portas: {', '.join(reachable)}"}
            return {"ok": False, "message": "Host inacessível (portas 10051, 8080, 80 testadas)"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    @app.post("/settings/test-telegram")
    async def test_telegram(
        bot_token: Optional[str] = Form(None),
        chat_id:   Optional[str] = Form(None),
    ):
        token = (bot_token or cfg.telegram_bot_token or "").strip()
        chid  = (chat_id or cfg.telegram_chat_id or "").strip()

        # Limpeza do token (caso o usuário cole a URL ou inclua "bot" no início)
        if "api.telegram.org/bot" in token:
            token = token.split("/bot")[-1].split("/")[0]
        elif token.lower().startswith("bot"):
            token = token[3:]

        logger.info("Testando Telegram: token=%s... chat_id=%s", token[:10], chid)

        if not token or not chid:
            return {"ok": False, "message": "Token ou Chat ID ausentes."}
        
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            msg = "🔔 <b>Mensagem de Teste</b>\nSua integração com o Camera Monitor está funcionando corretamente!"
            
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={
                    "chat_id": chid,
                    "text": msg,
                    "parse_mode": "HTML"
                })
                
                logger.info("Telegram response: %d - %s", resp.status_code, resp.text[:100])
                
                if resp.is_success:
                    return {"ok": True, "message": "Mensagem enviada com sucesso!"}
                else:
                    try:
                        error_data = resp.json()
                        desc = error_data.get("description", resp.text)
                    except:
                        desc = resp.text
                    return {"ok": False, "message": f"Erro do Telegram ({resp.status_code}): {desc}"}
                    
        except Exception as e:
            logger.error("Erro no teste do Telegram: %s", e)
            return {"ok": False, "message": f"Erro de conexão: {str(e)}"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # =========================================================================
    # Zabbix API  —  endpoints públicos com API key opcional
    # =========================================================================
    #
    # Configuração no config.yaml:
    #   zabbix_api_key: "sua-chave-secreta"   # vazio = acesso apenas localhost
    #
    # Endpoints:
    #   GET /api/zabbix/ping                    → health check do serviço
    #   GET /api/zabbix/summary?key=K           → totais (online/offline)
    #   GET /api/zabbix/discovery?key=K         → LLD: lista de câmeras
    #   GET /api/zabbix/camera/{id}?key=K       → métricas de uma câmera
    # =========================================================================

    async def _zabbix_auth(
        request: Request,
        key: str = Query("", alias="key"),
    ) -> None:
        """
        Valida API key para endpoints Zabbix.
        - Se zabbix_api_key estiver configurado: exige a chave correta.
        - Se vazio: permite acesso apenas de localhost (127.0.0.1 / ::1).
        """
        configured = cfg.zabbix_api_key
        if not configured:
            host = request.client.host if request.client else ""
            if host not in ("127.0.0.1", "::1", "localhost"):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "zabbix_api_key não configurada no config.yaml; "
                        "acesso restrito ao localhost."
                    ),
                )
            return
        if key != configured:
            raise HTTPException(status_code=403, detail="API key inválida.")

    @app.get("/api/zabbix/ping")
    async def zabbix_ping():
        """
        Verifica se o serviço Camera Monitor está respondendo.
        Não requer autenticação — usado pelo Zabbix para checar disponibilidade.
        """
        return {"status": "ok", "timestamp": _now_iso()}

    @app.get("/api/zabbix/summary")
    async def zabbix_summary(_: None = Depends(_zabbix_auth)):
        """
        Retorna totais agregados de câmeras.
        Usado pelos itens de resumo do template Zabbix.
        """
        stats = db.get_overview_stats()
        return {
            "total_cameras":   stats["total_cameras"],
            "online_cameras":  stats["online_cameras"],
            "offline_cameras": stats["offline_cameras"],
            "timestamp":       _now_iso(),
        }

    @app.get("/api/zabbix/discovery")
    async def zabbix_discovery(_: None = Depends(_zabbix_auth)):
        """
        Retorna lista de câmeras no formato LLD nativo do Zabbix.
        O Zabbix usa esta lista para criar itens e triggers automaticamente
        por câmera (Low-Level Discovery).
        """
        cameras = db.get_cameras_with_status()
        return [
            {
                "{#CAM_ID}":       cam["id"],
                "{#CAM_NAME}":     cam["name"],
                "{#CAM_HOST}":     cam["host"],
                "{#CAM_LOCATION}": cam.get("location") or "",
                "{#CAM_STATUS}":   cam.get("last_status") or "UNKNOWN",
            }
            for cam in cameras
        ]

    @app.get("/api/zabbix/camera/{camera_id}")
    async def zabbix_camera(
        camera_id: str,
        _: None = Depends(_zabbix_auth),
    ):
        """
        Retorna métricas detalhadas de uma câmera específica.
        O status_code é um inteiro para facilitar triggers no Zabbix:
          1 = ONLINE, 0 = OFFLINE, -1 = UNKNOWN
        """
        cameras = db.get_cameras_with_status()
        cam_data = next((c for c in cameras if c["id"] == camera_id), None)
        if cam_data is None:
            raise HTTPException(status_code=404, detail="Camera not found")

        status = cam_data.get("last_status") or "UNKNOWN"
        status_code_map = {"ONLINE": 1, "OFFLINE": 0, "UNKNOWN": -1}

        return {
            "status":                status,
            "status_code":           status_code_map.get(status, -1),
            "rtt_ms":                cam_data.get("last_rtt_ms"),
            "offline_since":         cam_data.get("offline_since"),
            "last_check_ts":         cam_data.get("last_check_ts"),
            "last_detail":           cam_data.get("last_detail") or "",
            "timestamp":             _now_iso(),
        }

    return app