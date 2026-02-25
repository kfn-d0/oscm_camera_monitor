#!/bin/bash

# Script de instalação automatizada para o Camera Monitor
# Compatível com Ubuntu/Debian e CentOS/RHEL

if [[ $EUID -ne 0 ]]; then
   echo "Este script deve ser executado como root (sudo ./install.sh)"
   exit 1
fi

BASE_DIR=$(pwd)
LOG_DIR="/var/log/camera-monitor"
SERVICE_NAME="camera-monitor.service"
INSTALL_USER=${SUDO_USER:-$(whoami)}

echo "=== Iniciando Instalação do Camera Monitor ==="

# Identificar a distribuição
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    OS=$(uname -s)
fi

# 1. Instalar dependências do sistema conforme a distro
echo "[1/5] Detectando SO: $OS"
if [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
    echo "Instalando dependências via apt..."
    apt update
    apt install -y python3 python3-pip chrony
elif [[ "$OS" == "centos" || "$OS" == "rhel" || "$OS" == "fedora" || "$OS" == "almalinux" || "$OS" == "rocky" ]]; then
    echo "Instalando dependências via dnf..."
    dnf install -y python3 python3-pip chrony
else
    echo "Distribuição não suportada automaticamente. Tente instalar python3-pip e chrony manualmente."
fi

# Garantir que o cronômetro (chrony) esteja rodando para manter o relógio certo
systemctl enable --now chronyd 2>/dev/null || systemctl enable --now chrony 2>/dev/null

# 2. Instalar requisitos do Python
echo "[2/5] Instalando requisitos do Python via pip..."
pip3 install --upgrade pip --break-system-packages
pip3 install -r requirements.txt --break-system-packages

# 3. Configurar Diretórios (Logs e Dados)
echo "[3/5] Configurando diretórios de logs e dados..."
mkdir -p "$LOG_DIR"
mkdir -p "$BASE_DIR/data"

# Definir permissões de diretório
chown -R "$INSTALL_USER:$INSTALL_USER" "$LOG_DIR"
chown -R "$INSTALL_USER:$INSTALL_USER" "$BASE_DIR/data"

# Configurar Logrotate
cp data/deploy/logrotate.d/camera-monitor /etc/logrotate.d/

# 4. Configurar Systemd
echo "[4/5] Configurando Systemd..."
cp data/deploy/camera-monitor.service /etc/systemd/system/

# Ajustar caminhos no arquivo de serviço dinamicamente
sed -i "s|WorkingDirectory=.*|WorkingDirectory=$BASE_DIR|g" /etc/systemd/system/$SERVICE_NAME
sed -i "s|User=.*|User=$INSTALL_USER|g" /etc/systemd/system/$SERVICE_NAME
sed -i "s|ExecStart=.*|ExecStart=/usr/bin/python3 -m camera_monitor --config data/config.yaml|g" /etc/systemd/system/$SERVICE_NAME

# Forçar o caminho correto do log no config.yaml para produção
sed -i "s|path:.*|path: /var/log/camera-monitor/camera-monitor.log|g" "$BASE_DIR/data/config.yaml"

# 5. Ativar serviço
echo "[5/5] Ativando serviço..."
systemctl daemon-reload
systemctl enable camera-monitor
systemctl restart camera-monitor

# 6. Configurar Firewall (Opcional)
echo "Configurando portas no firewall..."
if [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
    if command -v ufw >/dev/null; then
        ufw allow 9001/tcp
    fi
elif [[ "$OS" == "centos" || "$OS" == "rhel" || "$OS" == "almalinux" || "$OS" == "rocky" ]]; then
    if command -v firewall-cmd >/dev/null; then
        firewall-cmd --permanent --add-port=9001/tcp 2>/dev/null
        firewall-cmd --reload 2>/dev/null
    fi
fi

echo ""
echo "=== Instalação Concluída! ==="
echo "Dashboard disponível na porta 9001."
echo "Logs em: $LOG_DIR/camera-monitor.log"
echo "URL do Projeto: https://github.com/kfn-d0/camera_monitor"
echo ""
systemctl status camera-monitor --no-pager
