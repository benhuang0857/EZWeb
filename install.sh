#!/usr/bin/env bash
#
# EZWeb installer
#
# Checks every host dependency EZWeb needs, installs whatever is missing,
# then sets up the web-ui (Python venv, packages, .env) and optionally a
# systemd service. Safe to re-run: steps that are already done are skipped.
#
#   ./install.sh                 check + install interactively
#   ./install.sh --check         only report what is missing, change nothing
#   ./install.sh --yes --service non-interactive install + start as a service
#
# Supported: Ubuntu / Debian (apt), Fedora / RHEL / Rocky / Alma (dnf).

set -euo pipefail

EZWEB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBUI_DIR="$EZWEB_ROOT/web-ui"
VENV_DIR="$WEBUI_DIR/.venv"
ENV_FILE="$WEBUI_DIR/.env"
SERVICE_NAME="ezweb"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PY_MIN_MAJOR=3
PY_MIN_MINOR=10   # app uses `str | None` in pydantic models -> 3.10+

ASSUME_YES=0
CHECK_ONLY=0
WITH_NGINX=0
INSTALL_SERVICE=0
PORT=3000

# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------
if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLU=$'\033[34m'; C_BLD=$'\033[1m';  C_RST=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_BLD=""; C_RST=""
fi

step() { printf '\n%s==> %s%s\n' "$C_BLD$C_BLU" "$*" "$C_RST"; }
ok()   { printf '  %s✔%s %s\n' "$C_GRN" "$C_RST" "$*"; }
miss() { printf '  %s✘%s %s\n' "$C_RED" "$C_RST" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_YEL" "$C_RST" "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n%s錯誤：%s%s\n' "$C_RED" "$*" "$C_RST" >&2; exit 1; }

usage() {
    cat <<EOF
用法：./install.sh [選項]

  --check          只檢查依賴，不安裝、不修改任何東西
  -y, --yes        不詢問，直接安裝缺少的項目
  --service        安裝並啟動 systemd 服務（開機自動啟動 EZWeb）
  --with-nginx     一併安裝 nginx 與 certbot（綁定網域 / HTTPS 用）
  --port PORT      web-ui 監聽的 port（預設 3000）
  -h, --help       顯示這個說明
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check)      CHECK_ONLY=1 ;;
        -y|--yes)     ASSUME_YES=1 ;;
        --service)    INSTALL_SERVICE=1 ;;
        --with-nginx) WITH_NGINX=1 ;;
        --port)       shift; PORT="${1:-}" ;;
        --port=*)     PORT="${1#*=}" ;;
        -h|--help)    usage; exit 0 ;;
        *)            usage; die "未知的選項：$1" ;;
    esac
    shift
done

case "$PORT" in
    ''|*[!0-9]*) die "--port 必須是數字" ;;
esac

# --------------------------------------------------------------------------
# Privileges: support both `./install.sh` (uses sudo) and `sudo ./install.sh`
# (the real user comes from SUDO_USER). App files are always owned by the
# real user, never root.
# --------------------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
    RUN_USER="${SUDO_USER:-root}"
else
    RUN_USER="$(id -un)"
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        SUDO=""
        [ "$CHECK_ONLY" -eq 1 ] || die "需要 root 權限安裝套件，但找不到 sudo。請改用 root 執行。"
    fi
fi

as_user() {
    if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
        sudo -u "$RUN_USER" -H "$@"
    else
        "$@"
    fi
}

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    if [ ! -t 0 ]; then
        warn "非互動模式下無法詢問，請加上 --yes"
        return 1
    fi
    local ans
    read -r -p "  $1 [Y/n] " ans
    case "$ans" in ''|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# --------------------------------------------------------------------------
# OS / package manager
# --------------------------------------------------------------------------
OS_ID="unknown"; OS_LIKE=""; OS_NAME="unknown"; PKG=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"; OS_LIKE="${ID_LIKE:-}"; OS_NAME="${PRETTY_NAME:-$OS_ID}"
fi
if command -v apt-get >/dev/null 2>&1; then
    PKG="apt"
elif command -v dnf >/dev/null 2>&1; then
    PKG="dnf"
fi

# Package-manager output is long and unhelpful when things work, so it goes
# to a log file; on failure the tail of the log is shown.
LOG_FILE="/tmp/ezweb-install.log"
: > "$LOG_FILE" 2>/dev/null || LOG_FILE="/dev/null"

run_logged() {
    if ! "$@" >>"$LOG_FILE" 2>&1; then
        printf '\n%s指令失敗：%s%s\n' "$C_RED" "$*" "$C_RST" >&2
        [ "$LOG_FILE" != "/dev/null" ] && tail -n 20 "$LOG_FILE" >&2
        die "安裝失敗，完整紀錄：$LOG_FILE"
    fi
}

APT_UPDATED=0
pkg_install() {
    case "$PKG" in
        apt)
            if [ "$APT_UPDATED" -eq 0 ]; then
                run_logged $SUDO apt-get update
                APT_UPDATED=1
            fi
            run_logged $SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y "$@"
            ;;
        dnf)
            run_logged $SUDO dnf install -y "$@"
            ;;
        *)
            die "不支援的系統（$OS_NAME），請手動安裝：$*"
            ;;
    esac
}

pkg_available() {
    case "$PKG" in
        apt) apt-cache show "$1" >/dev/null 2>&1 ;;
        dnf) dnf -q list --available "$1" >/dev/null 2>&1 || dnf -q list --installed "$1" >/dev/null 2>&1 ;;
        *)   return 1 ;;
    esac
}

# --------------------------------------------------------------------------
# Checks. Each check_* returns 0 when satisfied and records details in globals.
# --------------------------------------------------------------------------
PY=""
PY_VER=""
find_python() {
    PY=""; PY_VER=""
    local c path
    for c in python3.13 python3.12 python3.11 python3.10 python3; do
        path="$(command -v "$c" 2>/dev/null)" || continue
        if "$path" -c "import sys; sys.exit(0 if sys.version_info >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" 2>/dev/null; then
            PY="$path"
            PY_VER="$("$path" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
            return 0
        fi
    done
    return 1
}

check_curl()   { command -v curl >/dev/null 2>&1; }
check_python() { find_python; }
check_venv()   { [ -n "$PY" ] && "$PY" -c "import venv, ensurepip" >/dev/null 2>&1; }
check_docker() { command -v docker >/dev/null 2>&1; }

# Run the docker CLI directly when the user can already reach the daemon
# (docker group), otherwise via sudo. In --check mode never prompt for a
# password: use `sudo -n`, which just fails if a password would be needed.
docker_cli() {
    if docker info >/dev/null 2>&1 || [ -z "$SUDO" ]; then
        docker "$@"
    elif [ "$CHECK_ONLY" -eq 1 ]; then
        sudo -n docker "$@"
    else
        sudo docker "$@"
    fi
}

check_docker_running() {
    check_docker || return 1
    docker_cli info >/dev/null 2>&1 && return 0
    # Can't talk to it without a password, but systemd knows whether it's up.
    command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet docker
}

COMPOSE_CMD=""
check_compose() {
    COMPOSE_CMD=""
    check_docker || return 1
    # `version` is client-only, so no daemon access (and no sudo) is needed.
    if docker compose version >/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
        COMPOSE_CMD="docker-compose"
    else
        return 1
    fi
}

check_docker_group() {
    [ "$RUN_USER" = "root" ] && return 0
    id -nG "$RUN_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker
}

check_nginx()   { command -v nginx >/dev/null 2>&1; }
check_certbot() { command -v certbot >/dev/null 2>&1; }

# --------------------------------------------------------------------------
# Installers
# --------------------------------------------------------------------------
install_curl() { pkg_install curl ca-certificates; }

install_python() {
    case "$PKG" in
        apt) pkg_install python3 python3-venv python3-pip ;;
        dnf) pkg_install python3 python3-pip ;;
    esac
    find_python && return 0

    # Distro python3 is too old: try a newer side-by-side package.
    local v
    for v in 3.12 3.11 3.10; do
        if pkg_available "python$v"; then
            if [ "$PKG" = "apt" ]; then
                pkg_install "python$v" "python$v-venv"
            else
                pkg_install "python$v"
            fi
            find_python && return 0
        fi
    done

    # Old Ubuntu (e.g. 20.04): use the deadsnakes PPA.
    if [ "$OS_ID" = "ubuntu" ]; then
        info "系統 Python 太舊，改用 deadsnakes PPA 安裝 Python 3.11"
        pkg_install software-properties-common
        run_logged $SUDO add-apt-repository -y ppa:deadsnakes/ppa
        APT_UPDATED=0
        pkg_install python3.11 python3.11-venv
        find_python && return 0
    fi
    die "無法自動安裝 Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+，請手動安裝後重跑此腳本。"
}

install_venv() {
    [ "$PKG" = "apt" ] || die "Python 缺少 venv/ensurepip 模組，請手動安裝。"
    if pkg_available "python${PY_VER}-venv"; then
        pkg_install "python${PY_VER}-venv"
    else
        pkg_install python3-venv
    fi
}

install_docker() {
    info "使用 Docker 官方安裝腳本（https://get.docker.com）"
    local tmp
    tmp="$(mktemp)"
    curl -fsSL https://get.docker.com -o "$tmp"
    run_logged $SUDO sh "$tmp"
    rm -f "$tmp"
}

start_docker() {
    if command -v systemctl >/dev/null 2>&1; then
        $SUDO systemctl enable --now docker
    else
        $SUDO service docker start
    fi
}

install_compose() {
    local p
    for p in docker-compose-plugin docker-compose-v2; do
        if pkg_available "$p"; then
            pkg_install "$p"
            return 0
        fi
    done
    die "找不到 docker compose 套件，請參考 https://docs.docker.com/compose/install/ 手動安裝。"
}

add_docker_group() {
    $SUDO groupadd -f docker
    $SUDO usermod -aG docker "$RUN_USER"
    DOCKER_GROUP_ADDED=1
}

install_nginx() {
    pkg_install nginx
    if command -v systemctl >/dev/null 2>&1; then
        $SUDO systemctl enable --now nginx
    fi
}

install_certbot() { pkg_install certbot python3-certbot-nginx; }

# --------------------------------------------------------------------------
# Phase 1: check everything
# --------------------------------------------------------------------------
printf '%sEZWeb 安裝程式%s\n' "$C_BLD" "$C_RST"
info "目錄：$EZWEB_ROOT"
info "系統：$OS_NAME"
info "使用者：$RUN_USER"

[ -d "$WEBUI_DIR" ] || die "找不到 $WEBUI_DIR，請在 EZWeb 專案根目錄執行此腳本。"
[ "$(uname -s)" = "Linux" ] || die "此腳本只支援 Linux。"
if [ -z "$PKG" ]; then
    warn "無法辨識套件管理工具（只支援 apt / dnf），缺少的項目需要手動安裝。"
fi

step "檢查依賴"
TODO=()
DOCKER_GROUP_ADDED=0

report() {  # report <id> <check-fn> <label-ok> <label-missing>
    if "$2"; then ok "$3"; else miss "$4"; TODO+=("$1"); fi
}

report curl check_curl "curl" "curl 未安裝"
if check_python; then
    ok "Python $PY_VER ($PY)"
else
    miss "Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+ 未安裝"; TODO+=("python")
fi
if [ -n "$PY" ]; then
    report venv check_venv "Python venv 模組"                       "Python venv 模組未安裝"
else
    TODO+=("venv")
fi
report docker check_docker "Docker $(docker --version 2>/dev/null | sed 's/^Docker version //; s/,.*//')" "Docker 未安裝"
if check_docker; then
    report docker_running check_docker_running "Docker daemon 執行中"  "Docker daemon 沒有在執行"
    if check_compose; then
        ok "Docker Compose（$COMPOSE_CMD）"
    else
        miss "Docker Compose 未安裝"; TODO+=("compose")
    fi
else
    TODO+=("docker_running" "compose")
fi
report docker_group check_docker_group "使用者 $RUN_USER 在 docker 群組" "使用者 $RUN_USER 不在 docker 群組（EZWeb 需要呼叫 docker）"
if [ "$WITH_NGINX" -eq 1 ]; then
    report nginx   check_nginx   "nginx"   "nginx 未安裝"
    report certbot check_certbot "certbot" "certbot 未安裝"
fi

# Disk space: each site pulls WordPress/MariaDB/phpMyAdmin images (~1.5 GB)
# plus its database, so check both the project dir and Docker's data dir.
DISK_MIN_GB=2
DISK_WARN_GB=5
check_disk() {  # check_disk <path> <label>
    local p="$1" avail_kb avail_gb
    while [ ! -e "$p" ]; do p="$(dirname "$p")"; done
    avail_kb="$(df -Pk "$p" | awk 'NR==2 {print $4}')"
    avail_gb=$((avail_kb / 1024 / 1024))
    if [ "$avail_gb" -lt "$DISK_MIN_GB" ]; then
        miss "$2 可用空間只剩 $((avail_kb / 1024)) MB（至少需要 ${DISK_MIN_GB} GB）"
        TODO+=("disk")
    elif [ "$avail_gb" -lt "$DISK_WARN_GB" ]; then
        warn "$2 可用空間 ${avail_gb} GB，建站多了可能不夠（建議 ${DISK_WARN_GB} GB 以上）"
    else
        ok "$2 可用空間 ${avail_gb} GB"
    fi
}
check_disk "$EZWEB_ROOT" "專案目錄"
DOCKER_ROOT="$(docker_cli info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
DOCKER_ROOT="${DOCKER_ROOT:-/var/lib/docker}"
if [ "$(df -P "$EZWEB_ROOT" | awk 'NR==2 {print $1}')" != "$(df -P "$(dirname "$DOCKER_ROOT")" | awk 'NR==2 {print $1}')" ]; then
    check_disk "$DOCKER_ROOT" "Docker 資料目錄"
fi

# App-level state (informational in this phase)
pending() { printf '  %s○%s %s\n' "$C_BLU" "$C_RST" "$*"; }
if [ -x "$VENV_DIR/bin/python" ]; then ok "web-ui 虛擬環境已建立"; else pending "web-ui 虛擬環境（安裝時會建立）"; fi
if [ -f "$ENV_FILE" ]; then ok "web-ui/.env 已存在"; else pending "web-ui/.env（安裝時會建立）"; fi
if ls "$EZWEB_ROOT"/theme/*.wpress >/dev/null 2>&1; then
    ok "站台範本：$(ls "$EZWEB_ROOT"/theme/*.wpress | wc -l) 個 .wpress"
else
    warn "theme/ 裡沒有 .wpress 範本 — 建站時需要至少一個（此目錄不在 git 裡，請自行複製）"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo
    if [ ${#TODO[@]} -eq 0 ]; then
        printf '%s所有依賴都已就緒。%s\n' "$C_GRN" "$C_RST"
        exit 0
    fi
    printf '%s缺少 %d 個項目。執行 ./install.sh 即可自動安裝。%s\n' "$C_YEL" "${#TODO[@]}" "$C_RST"
    exit 1
fi

# --------------------------------------------------------------------------
# Phase 2: install what is missing
# --------------------------------------------------------------------------
for t in ${TODO[@]+"${TODO[@]}"}; do
    if [ "$t" = "disk" ]; then
        die "磁碟空間不足，請先清出空間（例如 docker system prune、擴充磁碟）再重跑。"
    fi
done

if [ ${#TODO[@]} -gt 0 ]; then
    step "安裝缺少的依賴"
    info "將處理：${TODO[*]}"
    confirm "要繼續安裝嗎？" || die "已取消。"

    has_todo() { local t; for t in "${TODO[@]}"; do [ "$t" = "$1" ] && return 0; done; return 1; }

    if has_todo curl;   then info "安裝 curl...";   install_curl; fi
    if has_todo python; then info "安裝 Python..."; install_python; fi
    if ! check_venv;    then info "安裝 Python venv..."; install_venv; fi
    check_venv || die "Python venv 模組安裝失敗。"
    ok "Python $PY_VER"

    if ! check_docker; then
        info "安裝 Docker..."
        install_docker
        check_docker || die "Docker 安裝失敗。"
    fi
    if ! check_docker_running; then
        info "啟動 Docker..."
        start_docker
        check_docker_running || die "Docker daemon 無法啟動，請查看：sudo journalctl -u docker"
    fi
    ok "Docker 執行中"
    if ! check_compose; then
        info "安裝 Docker Compose..."
        install_compose
        check_compose || die "Docker Compose 安裝失敗。"
    fi
    ok "Docker Compose（$COMPOSE_CMD）"
    if ! check_docker_group; then
        info "把 $RUN_USER 加入 docker 群組..."
        add_docker_group
        ok "已加入 docker 群組"
    fi
    if [ "$WITH_NGINX" -eq 1 ]; then
        check_nginx   || { info "安裝 nginx...";   install_nginx; }
        check_certbot || { info "安裝 certbot..."; install_certbot; }
        ok "nginx / certbot"
    fi
else
    check_compose || true   # make sure COMPOSE_CMD is set
fi

# --------------------------------------------------------------------------
# Phase 3: set up the application
# --------------------------------------------------------------------------
step "設定 EZWeb web-ui"

# A venv copied from another machine (or built with a Python that was since
# removed) has a dead interpreter — rebuild it in that case.
if [ -x "$VENV_DIR/bin/python" ] && ! "$VENV_DIR/bin/python" -c "import sys" >/dev/null 2>&1; then
    warn "現有虛擬環境已損壞，重新建立"
    rm -rf "$VENV_DIR"
fi
if [ ! -x "$VENV_DIR/bin/python" ]; then
    as_user "$PY" -m venv "$VENV_DIR"
    ok "建立虛擬環境 web-ui/.venv"
fi

info "安裝 Python 套件（requirements.txt）..."
run_logged as_user "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip
run_logged as_user "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$WEBUI_DIR/requirements.txt"
ok "Python 套件已安裝"

gen_secret() { "$PY" -c 'import secrets; print(secrets.token_urlsafe(48))'; }

# The address browsers use to reach this server. Sites created without a
# domain get http://PUBLIC_HOST:<port> as their WordPress URL.
DETECTED_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
DETECTED_HOST="${DETECTED_HOST:-localhost}"

if [ ! -f "$ENV_FILE" ]; then
    as_user sh -c "umask 077 && cat > '$ENV_FILE'" <<EOF
SECRET_KEY=$(gen_secret)
SESSION_MAX_AGE=3600
PORT_RANGE_START=8100
PORT_RANGE_END=8999
DOCKER_COMPOSE_CMD=$COMPOSE_CMD
NGINX_CONFIG_DIR=data/nginx-configs
API_KEY=$(gen_secret)
PUBLIC_HOST=$DETECTED_HOST
EOF
    ok "建立 web-ui/.env（已產生隨機 SECRET_KEY 與 API_KEY，PUBLIC_HOST=$DETECTED_HOST）"
else
    if ! grep -q '^PUBLIC_HOST=' "$ENV_FILE"; then
        printf 'PUBLIC_HOST=%s\n' "$DETECTED_HOST" | as_user tee -a "$ENV_FILE" >/dev/null
        ok "web-ui/.env 補上 PUBLIC_HOST=$DETECTED_HOST（站台網址會用這個位址）"
    fi
    if grep -qE '^SECRET_KEY=(change-me)?$' "$ENV_FILE" || ! grep -q '^SECRET_KEY=' "$ENV_FILE"; then
        warn "web-ui/.env 的 SECRET_KEY 是空的或預設值，請改成隨機字串"
    fi
    current_cmd="$(sed -n 's/^DOCKER_COMPOSE_CMD=//p' "$ENV_FILE" | tail -1)"
    if [ -z "$current_cmd" ]; then
        printf 'DOCKER_COMPOSE_CMD=%s\n' "$COMPOSE_CMD" | as_user tee -a "$ENV_FILE" >/dev/null
        ok "web-ui/.env 補上 DOCKER_COMPOSE_CMD=$COMPOSE_CMD"
    elif [ "$current_cmd" != "$COMPOSE_CMD" ]; then
        warn "web-ui/.env 的 DOCKER_COMPOSE_CMD=\"$current_cmd\"，但這台主機偵測到的是 \"$COMPOSE_CMD\""
    else
        ok "web-ui/.env 已存在（保留原設定）"
    fi
fi

as_user mkdir -p "$EZWEB_ROOT/sites" "$EZWEB_ROOT/theme" "$WEBUI_DIR/data"

# --------------------------------------------------------------------------
# Phase 4: optional systemd service
# --------------------------------------------------------------------------
SERVICE_OK=0
if [ "$INSTALL_SERVICE" -eq 1 ]; then
    step "安裝 systemd 服務（$SERVICE_NAME）"
    command -v systemctl >/dev/null 2>&1 || die "這台主機沒有 systemd，無法使用 --service。"
    $SUDO tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=EZWeb - WordPress site manager
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$WEBUI_DIR
ExecStart=$VENV_DIR/bin/python -m uvicorn main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
    $SUDO systemctl restart "$SERVICE_NAME"
    ok "已寫入 $SERVICE_FILE 並設為開機啟動"

    info "等待服務啟動..."
    for _ in $(seq 1 20); do
        if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login" 2>/dev/null; then
            SERVICE_OK=1
            break
        fi
        sleep 1
    done
    if [ "$SERVICE_OK" -eq 1 ]; then
        ok "EZWeb 正在 port $PORT 執行"
    else
        warn "服務沒有在 20 秒內回應，請查看：sudo journalctl -u $SERVICE_NAME -n 50"
    fi
fi

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-localhost}"

step "完成"
if [ "$INSTALL_SERVICE" -eq 1 ] || [ -f "$SERVICE_FILE" ]; then
    if [ "$INSTALL_SERVICE" -eq 0 ]; then
        info "（偵測到已安裝的 $SERVICE_NAME 服務；更新程式後請執行 sudo systemctl restart $SERVICE_NAME）"
    fi
    info "網址：      http://$HOST_IP:$PORT"
    info "查看狀態：  sudo systemctl status $SERVICE_NAME"
    info "查看日誌：  sudo journalctl -u $SERVICE_NAME -f"
else
    info "啟動方式：  ./web-ui/start.sh"
    info "            （或重跑 ./install.sh --service 設成開機自動啟動）"
    info "網址：      http://$HOST_IP:$PORT"
fi
info "首次啟動的預設帳密是 admin / admin，請登入後立刻修改。"
if [ "$DOCKER_GROUP_ADDED" -eq 1 ] && [ "$INSTALL_SERVICE" -eq 0 ]; then
    warn "$RUN_USER 剛被加入 docker 群組，請登出再登入（或執行 newgrp docker）後再啟動 EZWeb。"
fi
