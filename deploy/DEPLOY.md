# vCCA 网页前端 —— Linux 服务器部署

目标环境：一台 Debian / Ubuntu 服务器（Debian 12、Ubuntu 24.04 自带 Python ≥ 3.11；Ubuntu 22.04 见下），
通过域名 + HTTPS（nginx + certbot）访问。全新安装只需要三样东西：**域名**、**FR24 账号**、**网页登录密码**。

- 独立域名：第 1–6 节。
- 挂在 DMIT 测试站 `https://genshin.fastcdn.world:8443/plan/` 下：另见第 7 节。
- 服务器上已经跑着旧版：见第 8 节「如何从旧版更新」。

## 架构

```
浏览器 ──HTTPS──> nginx (443, certbot 证书) ──> gunicorn 127.0.0.1:8770 (1 worker × 8 threads) ──> vcca.webapp
                                                                   └─ 任务：子进程 python vcca_plan.py fetch/build/…
```

- **只能 1 个 worker**：任务队列（同时只跑一个 fetch/build/export）、进度和结果缓存都在这一个进程里。并发请求靠 `--threads 8`。
- gunicorn 只监听 `127.0.0.1`，不要把 8770 端口暴露到公网。

| 位置 | 内容 |
|---|---|
| `/opt/vcca/app` | 代码：本仓库的 git clone（含 `FlightRadarAPI/`，不要修改它） |
| `/opt/vcca/venv` | Python 虚拟环境 |
| `/srv/vcca` | **`VCCA_HOME`**（唯一的路径设置，属主 `vcca:vcca`，750）：`settings.toml`、`seasons/<航季>/`、`vamsys/workspaces.db`、`rules/`、`cache/fr24_cache.db`、`state/`（网页账号、会话、任务日志） |
| `/etc/vcca/vcca.env` | 只有 `FR24_USER` / `FR24_PASSWORD`（`root:vcca 0640`） |
| `/etc/systemd/system/vcca-web.service` | 服务单元（设定 `VCCA_HOME=/srv/vcca`、`VCCA_TRUST_PROXY=1`、`TZ=Asia/Shanghai`） |
| `/etc/nginx/sites-available/vcca` | nginx 站点（由 `deploy/nginx-vcca.conf.template` 生成，certbot 之后会修改它） |
| `/usr/local/bin/vcca` | 命令包装：`sudo vcca <命令>` |

## 1. 准备

- 域名的 A/AAAA 记录指向服务器；防火墙放行 80 和 443（例如 `sudo ufw allow 'Nginx Full'`）。
- Ubuntu 22.04 自带 Python 3.10，需要先装 3.11（安装脚本检测到时也会提示）：
  ```bash
  sudo apt-get install -y software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv
  ```
  之后运行安装脚本时加 `PYTHON=/usr/bin/python3.11`。

## 2. 用 deploy key 拉取代码

仓库是私有的。在服务器上给 root 生成一把只读的 deploy key：

```bash
sudo ssh-keygen -t ed25519 -C "vcca-deploy@$(hostname)" -f /root/.ssh/id_ed25519 -N ""
sudo cat /root/.ssh/id_ed25519.pub
```

把输出的公钥添加到 GitHub：仓库 **Settings → Deploy keys → Add deploy key**，标题随意，
**不要**勾选 "Allow write access"。然后：

```bash
sudo apt-get install -y git
sudo git clone git@github.com:boeing777x9n779xy/Flight-Schedule-Manager.git /opt/vcca/app
```

（第一次连接 GitHub 会询问主机指纹，输入 `yes`。）

## 3. 安装

```bash
cd /opt/vcca/app
sudo bash deploy/install.sh --domain vcca.example.com --email you@example.com
```

- `--email` 是 Let's Encrypt 的通知邮箱；给出时证书非交互申请，不给则 certbot 会交互询问。
- `--no-certbot`：只配 80 端口，不申请证书（例如 DNS 还没生效；之后再运行一次不带它的命令即可）。
- Ubuntu 22.04：`sudo PYTHON=/usr/bin/python3.11 bash deploy/install.sh --domain ...`

脚本会：检查 Python ≥ 3.11；按需 `apt-get install` nginx、certbot、python3-certbot-nginx、python3-venv；
创建系统用户 `vcca`、venv、`/srv/vcca/{seasons,vamsys,rules,cache,state}`（`rules/` 仅在不存在时从仓库复制）；
安装 `/usr/local/bin/vcca` 和 systemd 服务并启动；生成 nginx 站点、`nginx -t`、重载；最后运行 certbot（自动跳转 HTTPS）。

**首次安装**会交互询问：

1. `FR24_USER` / `FR24_PASSWORD`（密码不回显）→ 写入 `/etc/vcca/vcca.env`；
2. 网页登录账号名（默认 `admin`）和密码（至少 8 位）。

再次运行 `install.sh` 是安全的：**不会**覆盖 `vcca.env`、`/srv/vcca` 里的数据和已存在的 nginx 站点
（certbot 修改过它；要重新生成就先删除 `/etc/nginx/sites-available/vcca`）。它会重启服务，正在跑的任务会被中断。

## 4. 登录、放数据

浏览器打开 `https://<域名>/`，用刚才的网页账号登录。所有数据都在网页的**数据页**上传：

- 新建航季，然后在配置页填好航季名称、起止日期、VAMSYS workspace 等；
- 上传 VAMSYS `workspaces.db`（上限 512 MB）；
- 上传 CAAC 计划 `.xls`；
- 可选：导入本机已有的 `fr24_cache.db`，免去首次 fetch 的 30–40 分钟。

然后在**运行页**先跑 `status` 检查配置和数据，再跑 `run`。

## 5. 命令行

`/usr/local/bin/vcca` 以 `vcca` 用户、带上 `vcca.env` 和 `VCCA_HOME=/srv/vcca` 运行 `vcca_plan.py`：

```bash
sudo vcca status
sudo vcca run
sudo vcca web-user set <名字>      # 添加账号或改密码（交互输入）
sudo vcca web-user list
sudo vcca web-user delete <名字>
sudo vcca logs                     # = journalctl -u vcca-web -f
```

修改 FR24 账号：`sudo nano /etc/vcca/vcca.env`（格式 `FR24_PASSWORD='...'`，值里不能有单引号），
然后 `sudo systemctl restart vcca-web`。

## 6. 更新

```bash
sudo vcca update
```

依次：检查是否有任务在运行（有则询问，非交互时拒绝；`--force` 跳过）→ `git pull --ff-only` →
`pip install -r requirements.txt` → 刷新 systemd 单元和 `/usr/local/bin/vcca` → 重启 `vcca-web`。
重启会中断正在运行的任务（下次启动标记为"中断"；fetch 已抓的数据不会丢，重新运行即可续抓）。
nginx 站点不会被更新改动。

## 7. 在 DMIT 测试站的 `/plan/` 下运行

DMIT 测试服务器上，本工具不使用独立域名，而是挂在已有站点 `https://genshin.fastcdn.world:8443/plan/` 下：
8443 的 server 块属于 AirChina Ops（`/company`、`/pilot`、`/api/`），带 Basic Auth，证书由该站点管理。
做法是：照常安装服务，但**不用**安装脚本生成的独立站点，改为在 8443 的 server 块里 include 一段转发配置
（`deploy/dmit-portal/routes.conf`，详见 [dmit-portal/README.md](dmit-portal/README.md)）。

1. 按第 2 节拉取代码，然后安装（不申请证书）：
   ```bash
   cd /opt/vcca/app
   sudo bash deploy/install.sh --domain genshin.fastcdn.world --no-certbot
   ```
2. 停用脚本生成的独立站点（它监听 80 端口，这里用不到）：
   ```bash
   sudo rm /etc/nginx/sites-enabled/vcca
   ```
   **注意**：以后每次重新运行 `install.sh` 都会重新启用它，运行后要再删一次。日常更新用 `sudo vcca update`，它不动 nginx。
3. 安装转发配置和测试中心首页：
   ```bash
   sudo install -D -m 644 deploy/dmit-portal/routes.conf /etc/nginx/snippets/vcca-test-portal.conf
   sudo install -D -m 644 deploy/dmit-portal/index.html /var/www/vcca-test-portal/index.html
   ```
4. 确认 AirChina Ops 站点（`/etc/nginx/sites-available/` 下的 airchina-ops-test 配置）**8443 的 server 块**里有这一行，没有就加上：
   ```nginx
   include /etc/nginx/snippets/vcca-test-portal.conf;
   ```
5. 检查通过后再重载：
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```
6. 打开 `https://genshin.fastcdn.world:8443/plan/`（先过站点的 Basic Auth，再用网页账号登录）。

说明：

- `routes.conf` 把 `/plan/` 转发到 `127.0.0.1:8770/`（去掉前缀），把会话 Cookie 的路径改成 `/plan/`
  （`proxy_cookie_path`），清掉 Basic Auth 的 `Authorization` 头，上传上限 600m；`/plan` 会跳转到 `/plan/`。
  前端所有请求和下载链接都是相对路径，应用本身不需要知道前缀。
- `location = /` 把站点根目录换成测试中心首页（`index.html`，链接到 `/company` 和 `/plan/`）。
- 改 `routes.conf` 后重复第 3、5 步。要撤掉：删除 include 那一行，`nginx -t` 后重载。

## 8. 如何从旧版更新

适用于用旧版部署文件安装的服务器（代码在 `/opt/vcca/fr24_work`，是 rsync 拷过去的，不是 git；
航季文件夹直接放在 `/srv/vcca/<航季>/`；`/srv/vcca/Data/workspaces.db`；`vcca.env` 里有 `VCCA_PLAN_ROOT` 等路径变量）。
新版的变化：代码改为 git clone 到 `/opt/vcca/app`；航季移到 `/srv/vcca/seasons/`；全站设置拆到 `/srv/vcca/settings.toml`；
数据库放在 `/srv/vcca/vamsys/`；`vcca.env` 只剩 FR24 账号。`rules/`、`cache/`、`state/`（含网页账号 `users.toml`）位置不变。

**0. 先看清现状**（实际服务器可能和上面不完全一样，以看到的为准）：

```bash
systemctl cat vcca-web | grep -E 'WorkingDirectory|EnvironmentFile'
sudo grep -o '^[A-Z_0-9]*=' /etc/vcca/vcca.env      # 只看键名，不显示密码
sudo ls -la /srv/vcca /srv/vcca/Data
```

如果 `VCCA_RULES_DIR` / `VCCA_CACHE_DIR` / `VCCA_STATE_DIR` 不是 `/srv/vcca/rules|cache|state`，
下面第 4 步要把对应目录也移到这些位置。先在网页运行页确认没有任务在跑。

**1. 停服务并备份**

```bash
sudo systemctl stop vcca-web
sudo tar czf /var/backups/vcca-pre-update-$(date +%F).tgz -C / srv/vcca etc/vcca etc/systemd/system/vcca-web.service
```

**2. 拉取新代码**：按第 2 节设置 deploy key，`git clone` 到 `/opt/vcca/app`。旧的 `/opt/vcca/fr24_work` 先保留（回滚用）。

**3. 移动航季文件夹**（每个航季一次，`<航季>` 例如 `2026-27-1`）：

```bash
sudo install -d -o vcca -g vcca -m 750 /srv/vcca/seasons /srv/vcca/vamsys
sudo mv /srv/vcca/<航季> /srv/vcca/seasons/
```

**4. 转换配置**：旧的 `season.toml` 一个文件包含全部设置，新版拆成全站 `settings.toml` + 精简的航季 `season.toml`。

```bash
cd /srv/vcca/seasons/<航季>
sudo mv season.toml season.toml.old
# 先预览（不写文件）
sudo -u vcca env VCCA_HOME=/srv/vcca /opt/vcca/venv/bin/python /opt/vcca/app/vcca_plan.py \
    migrate-config --from /srv/vcca/seasons/<航季>/season.toml.old --name <航季>
# 确认后写入 /srv/vcca/settings.toml 和 seasons/<航季>/season.toml
sudo -u vcca env VCCA_HOME=/srv/vcca /opt/vcca/venv/bin/python /opt/vcca/app/vcca_plan.py \
    migrate-config --from /srv/vcca/seasons/<航季>/season.toml.old --name <航季> --write --force
```

有多个航季时按从旧到新的顺序逐个执行：`--force` 会覆盖 `settings.toml`，所以最后执行的（最新的）航季的全站设置生效。
`[vamsys] db_path` 不再是配置项，会被自动去掉。

**5. `workspaces.db`**：不要直接拷贝。服务启动后在网页**数据页**重新上传 `/srv/vcca/Data/workspaces.db`
（可先 `scp` 回本机），上传时会精简（去掉密钥等无关表）并检查工作区，然后选择工作区保存。
确认无误后，`/srv/vcca/Data/` 可以手动删除。

**6. 精简 `vcca.env`**：

```bash
sudo cp -p /etc/vcca/vcca.env /etc/vcca/vcca.env.old
sudo nano /etc/vcca/vcca.env
```

只保留两行（加单引号，值里不能有单引号），删除所有 `VCCA_*` 行：

```
FR24_USER='你的账号'
FR24_PASSWORD='你的密码'
```

**7. 安装新版**：

```bash
sudo chown -R vcca:vcca /srv/vcca
cd /opt/vcca/app
sudo bash deploy/install.sh --domain <域名> [--email ...]          # 独立域名
sudo bash deploy/install.sh --domain genshin.fastcdn.world --no-certbot   # DMIT：之后按第 7 节第 2 步删除独立站点
```

`vcca.env` 已存在，所以不会再询问 FR24 账号和网页账号；旧的网页账号（`state/users.toml`）照常可用。
脚本会重建 venv 依赖、安装新的服务单元（`WorkingDirectory=/opt/vcca/app`、`VCCA_HOME=/srv/vcca`）和 `vcca` 命令，并启动服务。

**8. 检查**：`sudo vcca status`；网页登录 → 数据页上传 `workspaces.db`（第 5 步）→ 运行页跑 `status`，再跑 `run`。
之后的更新都用 `sudo vcca update`。全部确认后，旧代码 `/opt/vcca/fr24_work` 和 `/etc/vcca/vcca.env.old` 可以手动删除。

**回滚**（恢复到更新前的旧版）：

```bash
sudo systemctl stop vcca-web
sudo mv /srv/vcca /srv/vcca.failed                     # 保留以便排查
sudo tar xzf /var/backups/vcca-pre-update-<日期>.tgz -C /     # 恢复数据、vcca.env 和旧服务单元
sudo /opt/vcca/venv/bin/pip install -r /opt/vcca/fr24_work/deploy/requirements.txt
sudo systemctl daemon-reload && sudo systemctl start vcca-web
```

旧代码 `/opt/vcca/fr24_work` 没有被改动，所以恢复后就是更新前的状态。`/usr/local/bin/vcca` 可删除。

## 9. 备份

需要备份的只有 `/srv/vcca`（设置、航季、上传的数据、规则、缓存、账号）和 `/etc/vcca`。手动：

```bash
sudo tar czf /var/backups/vcca-$(date +%F).tgz -C / srv/vcca etc/vcca
```

每日自动备份的例子（**不会**自动安装；需要时写入 `/etc/cron.d/vcca-backup`）：

```cron
# 每天 04:30 备份，保留 14 天
30 4 * * * root tar czf /var/backups/vcca-$(date +\%F).tgz -C / srv/vcca etc/vcca && find /var/backups -name 'vcca-*.tgz' -mtime +14 -delete
```

备份里有 FR24 密码和网页账号，注意保管。恢复：停服务 → 解压回 `/` → `chown -R vcca:vcca /srv/vcca` → 启动服务。

## 10. 日志与排错

- `sudo vcca logs`：服务日志（gunicorn 访问日志 + 应用错误）。
- 任务日志：网页运行页，或 `/srv/vcca/state/jobs/<任务id>.log`。
- 服务状态：`systemctl status vcca-web`；nginx：`sudo nginx -t`、`/var/log/nginx/error.log`。
- 证书：certbot 自带续期定时器，`sudo certbot renew --dry-run` 可测试。
- 时区：服务设了 `TZ=Asia/Shanghai`；运行文件夹名和报告时刻固定为 UTC，网页上的文件时间按浏览器时区显示。

## 风险与注意

- **FR24 从数据中心 IP 访问可能被限流或拦截**（常被 Cloudflare 额外校验）。部署后先在运行页跑一次
  **真实的小范围 fetch**（"只抓这些班号"填 1–2 个班号；"只预览"不发请求，不算数），确认能登录、能取数据，
  再跑完整的 `run`。
- 登录会话 cookie 只在 HTTPS 下带 `Secure`，请始终通过 HTTPS 访问。
- 目前只有"账号"没有角色：任何登录用户都能运行任务、改配置、上传数据。
- `FlightRadarAPI/` 是借用的第三方库，随代码一起部署，不要修改。
