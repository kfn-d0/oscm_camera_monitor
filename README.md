# Camera Monitor 

Monitor de disponibilidade de câmeras IP com dashboard moderno, sistema de chamados, notificações (Telegram) e gerenciamento web.

<img width="1914" height="910" alt="image" src="https://github.com/user-attachments/assets/5222ec5e-b912-4615-9ca8-02a71d43e9a6" />


## Principais Funcionalidades

- **Monitoramento Inteligente**: Sonda dupla (TCP + ICMP) para diagnóstico preciso. Identifica se o problema é na rede ou no serviço de vídeo.
- **Modo ICMP-Only**: Opção de monitorar apenas via Ping para dispositivos que não expõem RTSP.
- **ID Único Automático**: Padrão organizacional `camXXX-icmp-host` gerado automaticamente no cadastro.
- **Dashboard Moderno**: Interface web rápida e intuitiva para visualização de status graficos e histórico, com tema claro/escuro.
- **Gerenciamento de Hosts**: Adicione, edite ou exclua câmeras diretamente pela interface web.
- **Ferramentas de Teste**: Botões integrados para validar conectividade Zabbix e notificações Telegram instantaneamente.
- **Alertas via Telegram**: Notificações automáticas com formato editável e suporte a tags HTML.
- **Filtro Anti-Spam**: Log de perdas de pacotes com agrupamento inteligente para evitar poluição visual durante quedas.
- **Manutenção Automática**: Rotinas de limpeza de dados antigos e otimização do banco de dados (VACUUM).
- **Mapa Interativo**: Localização geográfica integrada com Leaflet.js (Centralizado em São Luís - MA).
- **Diagnóstico do Sistema**: Monitoramento de hardware (disco/memória) e projeção de banco de dados.

## Tecnologias Utilizadas

- **Backend**: Python 3.11+, FastAPI, Asyncio, SQLite.
- **Frontend**: HTML5, Vanilla CSS, Jinja2, Leaflet.js (Mapas).
- **Infraestrutura**: Logrotate (logs), Systemd (serviço no Linux).

## Instalação (Ubuntu/CentOS)

Para instalar automaticamente todas as dependências, configurar o serviço e os logs, execute o script de instalação como root:

```bash
git clone https://github.com/kfn-d0/camera_monitor.git
cd camera_monitor
chmod +x install.sh
sudo ./install.sh
```

O script irá:
1. Instalar `python3`, `pip3` e `chrony`.
2. Instalar as bibliotecas via `pip` (usando `--break-system-packages` quando necessário).
3. Configurar os diretórios de `data/` e logs com as permissões corretas.
4. Configurar a rotação automática de logs (Logrotate).
5. Instalar e iniciar o serviço Systemd automaticamente.
6. Abrir a porta 9001 no firewall (`ufw` ou `firewalld`).


## Acesso e Credenciais

O dashboard estará disponível em: `http://<IP-DO-SERVIDOR>:9001`

- **Usuário padrão**: `admin`
- **Senha padrão**: `admin`

> **Importante**: Altere as credenciais antes de expor na rede. As credenciais são configuradas em `web/app.py`.

## Capacidade e Retenção

O sistema foi projetado para rodar por **longos períodos** de forma estável:
- **Retenção de dados**: 90 dias (configurável no `config.yaml`).
- **Manutenção**: SQLite VACUUM semanal automático.
- **Logs**: Rotação diária gerenciada pelo sistema.
- **Localização**: Focado na região de São Luís - MA.

## Integração com Zabbix (LLD)

Ao invés de cadastrar cada câmera individualmente no Zabbix, o sistema expoe uma **API REST dedicada** que permite ao Zabbix:

- **Monitorar** o próprio serviço Camera Monitor (1 host, 1 template)
- **Descobrir automaticamente** todas as câmeras via LLD (Low-Level Discovery)
- **Criar itens e triggers por câmera** sem configurar nada manualmente
- **Receber alertas consolidados** diretamente no Zabbix (OFFLINE, alta latência)

### Endpoints da API Zabbix

| Endpoint | Auth | Descrição |
|---|---|---|
| `GET /api/zabbix/ping` | Não requer | Health check do serviço |
| `GET /api/zabbix/summary?key=K` | API key | Total / online / offline |
| `GET /api/zabbix/discovery?key=K` | API key | Lista de câmeras (formato LLD) |
| `GET /api/zabbix/camera/{id}?key=K` | API key | Métricas de uma câmera |

**Exemplo de resposta** de `/api/zabbix/camera/cam001-rtsp-entrada`:
```json
{
  "status": "ONLINE",
  "status_code": 1,
  "rtt_ms": 12.5,
  "offline_since": null,
  "last_check_ts": "2026-02-25T12:00:00+00:00",
  "last_detail": "TCP:554 OK"
}
```

> `status_code`: `1` = ONLINE, `0` = OFFLINE, `-1` = UNKNOWN

### Como configurar

**1. Ative a API key** no `config.yaml`:
```yaml
# Gere uma chave segura: python3 -c "import secrets; print(secrets.token_hex(24))"
zabbix_api_key: "sua-chave-aqui"  # ou vazio para permitir apenas localhost
```

**2. Importe o template** no Zabbix:
```
Zabbix UI → Data collection → Templates → Import
→ Selecione: data/deploy/zabbix_template.yaml
```

**3. Crie um host** no Zabbix:
- **IP/DNS**: endereço do servidor Camera Monitor
- **Template**: `Camera Monitor`
- **Macros do host**:
  - `{$CM_PORT}` = porta do serviço (padrão: `9001`)
  - `{$CM_API_KEY}` = valor de `zabbix_api_key` do config.yaml

**4. Aguarde** o primeiro ciclo de coleta (até 5 minutos). O Zabbix vai:
- Criar itens de resumo (total / online / offline)
- Descobrir todas as câmeras automaticamente
- Criar itens e triggers por câmera

### Triggers criadas automaticamente

| Trigger | Severidade | Condição |
|---|---|---|
| `Camera Monitor: Serviço indisponível` | HIGH | Ping sem resposta por 3 min |
| `Camera Monitor: N cãmera(s) offline` | HIGH | `offline_cameras > 0` |
| `Câmera {NOME}: OFFLINE` | HIGH | `status_code = 0` |
| `Câmera {NOME}: Latência elevada (> {$CM_RTT_WARN}ms)` | WARNING | RTT acima do threshold |


## Arquitetura Interna

```
camera_monitor/
├── __main__.py      # Ponto de entrada: inicializa DB, Notifier e loop de monitoramento
├── config.py        # Parsing do config.yaml via dataclasses
├── database.py      # Camada de persistência SQLite com connection pool por thread
├── healthcheck.py   # Sonda TCP + ICMP por câmera
├── monitor.py       # Loop assíncrono de verificação com lock por câmera
├── utils.py         # Logging e Notifier Telegram com rate limiting
└── web/
    ├── app.py       # Roteamento FastAPI + templates Jinja2
    └── templates/   # HTML (overview, status, tickets, camera_detail, etc.)
```

### Decisões de Design Notáveis

| Componente | Decisão | Motivo |
|---|---|---|
| **DB Connection Pool** | `threading.local` — uma conexão por thread | Evita overhead de abrir/fechar conexão por operação sem violar thread-safety do SQLite |
| **Race Condition** | `update_state_atomic()` + lock asyncio por câmera | O read-modify-write do estado é feito em transação SQLite única, eliminando janela de concorrência |
| **Telegram Rate Limit** | Fila `asyncio.Queue` + worker background + `≥ 1,1 s` entre envios | Respeita o limite da API do Telegram (1 msg/s por chat) sem bloquear o loop de monitoramento |
| **Sonda dupla** | TCP 554 + ICMP em sequência | Distingue "serviço RTSP caiu" de "host inacessível na rede" — diagnóstico mais preciso |
| **WAL mode** | `PRAGMA journal_mode=WAL` | Permite leituras e escritas simultâneas (web lê enquanto monitor escreve) sem lock total |

---




