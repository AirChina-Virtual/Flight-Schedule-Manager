#!/usr/bin/env bash
# vCCA web front end — install / re-install on a Debian/Ubuntu server (run as root from /opt/vcca/app).
#
#   sudo bash deploy/install.sh --domain vcca.example.com [--email you@example.com] [--no-certbot]
#
# Layout (fixed):
#   /opt/vcca/app      git clone of the repository (code)       /opt/vcca/venv   Python >= 3.11 venv
#   /srv/vcca          VCCA_HOME (owner vcca:vcca, 750): settings.toml seasons/ vamsys/ rules/ cache/ state/
#   /etc/vcca/vcca.env FR24_USER / FR24_PASSWORD only (root:vcca 0640)
#   /etc/systemd/system/vcca-web.service, /etc/nginx/sites-available/vcca, /usr/local/bin/vcca
#
# First run asks for the FR24 account and a web account. Re-running is safe: it never overwrites
# /etc/vcca/vcca.env, the data in /srv/vcca or an existing nginx site; it refreshes venv, unit and wrapper
# and restarts the service (which interrupts a running job — for code updates use `sudo vcca update`).
set -euo pipefail

APP=/opt/vcca/app
VENV=/opt/vcca/venv
HOME_DIR=/srv/vcca
ETC=/etc/vcca
ENV_FILE=$ETC/vcca.env
SERVICE=vcca-web
SITE=/etc/nginx/sites-available/vcca
REPO_SSH=git@github.com:boeing777x9n779xy/Flight-Schedule-Manager.git

DOMAIN=""
EMAIL=""
CERTBOT=1

usage() {
    cat <<EOF
用法: sudo bash deploy/install.sh --domain <域名> [--email <证书通知邮箱>] [--no-certbot]
  --domain      访问网页用的域名（DNS 需已指向本机，certbot 才能签发证书）
  --email       certbot 注册邮箱；给出时非交互申请证书，否则 certbot 会交互询问
  --no-certbot  不申请 HTTPS 证书（只配置 nginx 的 80 端口）
环境变量 PYTHON=/usr/bin/python3.12 可指定 Python（需 >= 3.11）。
EOF
}

die()  { echo "错误: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
warn() { echo "警告: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain)     [[ $# -ge 2 ]] || die "--domain 需要参数"; DOMAIN=$2; shift 2 ;;
        --domain=*)   DOMAIN=${1#*=}; shift ;;
        --email)      [[ $# -ge 2 ]] || die "--email 需要参数"; EMAIL=$2; shift 2 ;;
        --email=*)    EMAIL=${1#*=}; shift ;;
        --no-certbot) CERTBOT=0; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            usage >&2; die "未知参数: $1" ;;
    esac
done

[[ -n $DOMAIN ]] || { usage >&2; die "缺少 --domain"; }
[[ $DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] \
    || die "域名格式不对: $DOMAIN"
[[ $EUID -eq 0 ]] || die "需要 root：sudo bash deploy/install.sh ..."
command -v apt-get >/dev/null || die "只支持 Debian/Ubuntu（找不到 apt-get）"

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ $SRC != "$APP" ]]; then
    cat >&2 <<EOF
错误: install.sh 必须在 $APP 的仓库里运行（当前: $SRC）。
  sudo git clone $REPO_SSH $APP
  cd $APP && sudo bash deploy/install.sh --domain $DOMAIN
（deploy key 的设置见 deploy/DEPLOY.md）
EOF
    exit 1
fi

# ---------------------------------------------------------------- python
step "Python"
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null || die "找不到 $PY"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "需要 Python >= 3.11（当前: $("$PY" -V 2>&1)）。" >&2
    # shellcheck disable=SC1091
    if [[ -r /etc/os-release ]] && (. /etc/os-release; [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 22.04 ]]); then
        cat >&2 <<'EOF'
Ubuntu 22.04 自带 3.10，可这样装 3.11：
  sudo apt-get install -y software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv
  sudo PYTHON=/usr/bin/python3.11 bash deploy/install.sh --domain ...
（或升级到 Ubuntu 24.04 / Debian 12，自带 3.12 / 3.11）
EOF
    else
        echo "安装 3.11+ 后用 PYTHON=/usr/bin/python3.X 再运行。" >&2
    fi
    exit 1
fi
PYVER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "$PY -> Python $PYVER"

# ---------------------------------------------------------------- packages
step "系统软件包"
# ${Status} below is a dpkg-query format field, not a shell expansion.
# shellcheck disable=SC2016
pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
need=()
if ! "$PY" -c 'import ensurepip, venv' 2>/dev/null; then
    if [[ $PY == python3 || $(command -v "$PY") == /usr/bin/python3 ]]; then need+=(python3-venv); else need+=("python$PYVER-venv"); fi
fi
pkgs=(nginx git)
[[ $CERTBOT -eq 1 ]] && pkgs+=(certbot python3-certbot-nginx)
for p in "${pkgs[@]}"; do pkg_installed "$p" || need+=("$p"); done
if [[ ${#need[@]} -gt 0 ]]; then
    echo "安装: ${need[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q "${need[@]}"
else
    echo "已齐全"
fi

# ---------------------------------------------------------------- user, venv, data
step "用户 vcca"
if id vcca >/dev/null 2>&1; then
    echo "已存在"
else
    useradd --system --user-group --home-dir "$HOME_DIR" --no-create-home --shell /usr/sbin/nologin vcca
    echo "已创建"
fi

step "venv -> $VENV"
[[ -x $VENV/bin/python ]] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --disable-pip-version-check -q --upgrade pip
"$VENV/bin/pip" install --disable-pip-version-check -q -r "$APP/requirements.txt"
"$VENV/bin/python" -c 'import sys; print("venv Python", sys.version.split()[0])'

step "数据目录 VCCA_HOME=$HOME_DIR"
install -d -m 750 -o vcca -g vcca "$HOME_DIR"
for d in seasons vamsys cache state; do
    install -d -m 750 -o vcca -g vcca "$HOME_DIR/$d"
done
if [[ ! -e $HOME_DIR/rules ]]; then
    cp -a "$APP/rules" "$HOME_DIR/rules"
    chown -R vcca:vcca "$HOME_DIR/rules"
    chmod -R u+rwX,g+rX,g-w,o-rwx "$HOME_DIR/rules"
    echo "rules/ 已从仓库复制（之后在网页配置页修改）"
else
    echo "rules/ 已存在，未改动"
fi

# ---------------------------------------------------------------- wrapper (needed for web-user below)
step "/usr/local/bin/vcca"
install -m 755 "$APP/deploy/vcca" /usr/local/bin/vcca

# ---------------------------------------------------------------- secrets (first install only)
# Values are written single-quoted (systemd: no escape processing); the wrapper strips the quotes.
ask_value() {   # ask_value <prompt> <silent 0|1> -> stdout
    local v
    while :; do
        if [[ $2 -eq 1 ]]; then read -rs -p "$1" v </dev/tty; echo >/dev/tty; else read -r -p "$1" v </dev/tty; fi
        if [[ $v == *"'"* ]]; then echo "不能包含单引号 ' ，请重新输入" >/dev/tty; continue; fi
        printf '%s' "$v"
        return 0
    done
}

FIRST_INSTALL=0
step "$ENV_FILE"
install -d -m 755 "$ETC"
if [[ -e $ENV_FILE ]]; then
    echo "已存在，未改动（修改后 systemctl restart $SERVICE）"
else
    [[ -t 0 && -r /dev/tty ]] || die "首次安装需要在终端里交互运行（要输入 FR24 账号和网页密码）"
    FIRST_INSTALL=1
    echo "输入 FlightRadar24 账号（fetch 使用；留空则稍后编辑 $ENV_FILE）"
    fr24_user=$(ask_value "FR24_USER: " 0)
    fr24_pass=""
    [[ -n $fr24_user ]] && fr24_pass=$(ask_value "FR24_PASSWORD（不回显）: " 1)
    tmp=$(mktemp "$ETC/.vcca.env.XXXXXX")
    {
        echo "# vCCA secrets — read by systemd (vcca-web.service) and by /usr/local/bin/vcca."
        echo "# Format: KEY='value' (no single quote inside the value). Paths are NOT configured here."
        printf "FR24_USER='%s'\n" "$fr24_user"
        printf "FR24_PASSWORD='%s'\n" "$fr24_pass"
    } > "$tmp"
    unset fr24_pass
    chown root:vcca "$tmp"
    chmod 640 "$tmp"
    mv "$tmp" "$ENV_FILE"
    [[ -n $fr24_user ]] || warn "FR24 账号为空：fetch 前请编辑 $ENV_FILE 并 systemctl restart $SERVICE"
fi
chown root:vcca "$ENV_FILE"
chmod 640 "$ENV_FILE"

if [[ $FIRST_INSTALL -eq 1 ]]; then
    step "网页账号"
    read -r -p "网页登录账号名 [admin]: " web_user </dev/tty
    web_user=${web_user:-admin}
    if ! /usr/local/bin/vcca web-user set "$web_user" </dev/tty; then
        warn "创建网页账号失败；稍后运行：sudo vcca web-user set $web_user"
    fi
fi

# ---------------------------------------------------------------- systemd
step "systemd: $SERVICE"
install -m 644 "$APP/deploy/vcca-web.service" /etc/systemd/system/vcca-web.service
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    echo "运行中"
else
    systemctl --no-pager --lines=20 status "$SERVICE" || true
    die "$SERVICE 未能启动（journalctl -u $SERVICE 查看原因）"
fi

# ---------------------------------------------------------------- nginx
step "nginx: $SITE"
NEW_SITE=0
if [[ -e $SITE ]]; then
    echo "已存在，未改动（certbot 会修改它；要重新生成请先删除）"
    dom_re=${DOMAIN//./[.]}                  # dots literal in the regex
    grep -Eq "server_name[[:space:]]+([^;]*[[:space:]])?${dom_re}[[:space:];]" "$SITE" \
        || warn "$SITE 里的 server_name 不是 $DOMAIN"
else
    sed "s/@DOMAIN@/$DOMAIN/g" "$APP/deploy/nginx-vcca.conf.template" > "$SITE"
    chmod 644 "$SITE"
    NEW_SITE=1
    echo "已生成"
fi
install -d -m 755 /var/www/html
ln -sfn "$SITE" /etc/nginx/sites-enabled/vcca
nginx -t
systemctl enable nginx >/dev/null 2>&1
systemctl reload nginx 2>/dev/null || systemctl restart nginx

# ---------------------------------------------------------------- certbot
HTTPS=0
if [[ $CERTBOT -eq 1 ]]; then
    step "certbot: $DOMAIN"
    if [[ -d /etc/letsencrypt/live/$DOMAIN && $NEW_SITE -eq 0 ]]; then
        echo "证书已存在（certbot 自动续期）"
        HTTPS=1
    else
        cb=(certbot --nginx -d "$DOMAIN" --redirect)
        if [[ -n $EMAIL ]]; then
            cb+=(-m "$EMAIL" --agree-tos --non-interactive)
        fi
        if "${cb[@]}"; then
            HTTPS=1
        else
            warn "证书申请失败：确认 $DOMAIN 的 DNS 指向本机、防火墙放行 80/443，然后运行：sudo certbot --nginx -d $DOMAIN --redirect"
        fi
    fi
fi

# ---------------------------------------------------------------- summary
if [[ $HTTPS -eq 1 ]]; then URL="https://$DOMAIN/"; else URL="http://$DOMAIN/"; fi
cat <<EOF

================================================================
 vCCA 已安装
   网址:     $URL
   数据目录: $HOME_DIR      密钥: $ENV_FILE
 下一步:
   1. 浏览器打开上面的网址，用刚才的网页账号登录
   2. 数据页：新建航季，上传 workspaces.db、CAAC 计划 .xls（可选导入 fr24_cache.db）
   3. 运行页：先跑 status 检查，再用 1–2 个班号试一次真实 fetch，然后 run
 常用:
   sudo vcca status            命令行（以 vcca 用户运行 vcca_plan.py）
   sudo vcca web-user set 名字  添加/改密码网页账号
   sudo vcca update            拉取新代码并重启
   sudo vcca logs              查看服务日志
EOF
[[ $HTTPS -eq 1 || $CERTBOT -eq 0 ]] || echo " 注意: HTTPS 未配置成功，登录前请先完成 certbot。"
[[ $CERTBOT -eq 1 ]] || echo " 注意: 未申请证书（--no-certbot）；不要在公网上用 HTTP 登录。"
echo "================================================================"
