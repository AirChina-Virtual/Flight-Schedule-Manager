# vCCA 网页前端 —— Linux 服务器部署

目标环境：一台 Debian / Ubuntu 服务器（Debian 12、Ubuntu 24.04 自带 Python ≥ 3.11；Ubuntu 22.04 见下），
通过域名 + HTTPS（nginx + certbot）访问。全新安装只需要三样东西：**域名**、**FR24 账号**、**网页登录密码**。

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

## 7. 备份

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

## 8. 日志与排错

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
