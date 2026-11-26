#!/usr/bin/env bash
# vCCA web front end — first install / update on a Linux server (Debian/Ubuntu style; run as root).
#
#   sudo bash deploy/install.sh            # from the unpacked fr24_work folder (code is copied to /opt/vcca)
#
# Layout:  /opt/vcca/fr24_work   code (this folder, incl. FlightRadarAPI/ and rules/ defaults)
#          /opt/vcca/venv        Python virtualenv (Python >= 3.11)
#          /srv/vcca             data: season folders (VCCA_PLAN_ROOT), Data/workspaces.db, rules/, cache/, state/
#          /etc/vcca/vcca.env    environment (paths, FR24 account) — root:vcca 0640
# Re-running is safe: code and venv are refreshed, data and vcca.env are never overwritten.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"     # fr24_work
APP=/opt/vcca
DATA=/srv/vcca
ETC=/etc/vcca
PY="${PYTHON:-python3}"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
    || { echo "需要 Python >= 3.11（tomllib）。可设置 PYTHON=/usr/bin/python3.12 再运行"; exit 1; }

echo "== user vcca"
id vcca >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin vcca

echo "== code -> $APP/fr24_work"
mkdir -p "$APP"
if command -v rsync >/dev/null; then
    rsync -a --delete --exclude fr_venv --exclude __pycache__ --exclude webstate --exclude fr24_cache \
          --exclude legacy --exclude '.env' "$SRC/" "$APP/fr24_work/"
else
    rm -rf "$APP/fr24_work.new" && cp -a "$SRC" "$APP/fr24_work.new"
    rm -rf "$APP/fr24_work.new/fr_venv" "$APP/fr24_work.new/webstate" "$APP/fr24_work.new/fr24_cache" \
           "$APP/fr24_work.new/legacy" "$APP/fr24_work.new/.env"
    rm -rf "$APP/fr24_work" && mv "$APP/fr24_work.new" "$APP/fr24_work"
fi
chown -R root:vcca "$APP/fr24_work" && chmod -R g+rX,o-rwx "$APP/fr24_work"

echo "== venv"
[[ -x "$APP/venv/bin/python" ]] || "$PY" -m venv "$APP/venv"
"$APP/venv/bin/pip" install --upgrade pip >/dev/null
"$APP/venv/bin/pip" install -r "$APP/fr24_work/deploy/requirements.txt"

echo "== data dirs -> $DATA"
mkdir -p "$DATA/Data" "$DATA/cache" "$DATA/state"
if [[ ! -d "$DATA/rules" ]]; then cp -a "$APP/fr24_work/rules" "$DATA/rules"; fi
chown -R vcca:vcca "$DATA" && chmod 750 "$DATA"

echo "== $ETC/vcca.env"
mkdir -p "$ETC"
if [[ ! -f "$ETC/vcca.env" ]]; then
    cat > "$ETC/vcca.env" <<EOF
# vCCA environment (read by systemd; also export it before running vcca_plan.py by hand)
VCCA_PLAN_ROOT=$DATA
VCCA_RULES_DIR=$DATA/rules
VCCA_CACHE_DIR=$DATA/cache
VCCA_STATE_DIR=$DATA/state
VCCA_TRUST_PROXY=1
VCCA_MAX_UPLOAD_MB=512
# FlightRadar24 account used by fetch (never printed by the tool)
FR24_USER=
FR24_PASSWORD=
EOF
fi
chown root:vcca "$ETC/vcca.env" && chmod 640 "$ETC/vcca.env"

echo "== systemd"
install -m 644 "$APP/fr24_work/deploy/vcca-web.service" /etc/systemd/system/vcca-web.service
systemctl daemon-reload
systemctl enable vcca-web.service >/dev/null
systemctl restart vcca-web.service
sleep 2
systemctl --no-pager --lines=5 status vcca-web.service || true

cat <<EOF

完成。接下来：
  1. 编辑 $ETC/vcca.env 填 FR24_USER / FR24_PASSWORD，然后 systemctl restart vcca-web
  2. 创建网页账号：
       sudo -u vcca env \$(grep -v '^#' $ETC/vcca.env | xargs) $APP/venv/bin/python $APP/fr24_work/vcca_plan.py web-user set <名字>
  3. nginx：cp $APP/fr24_work/deploy/nginx-vcca.conf /etc/nginx/sites-available/vcca（改域名）并 certbot --nginx
  4. 浏览器登录 → 数据页新建航季 / 上传 workspaces.db 与 CAAC 计划（详见 deploy/DEPLOY.md）
EOF
