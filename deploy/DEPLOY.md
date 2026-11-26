# vCCA 网页前端 —— Linux 服务器部署

> 本机没有 Linux 环境，这套脚本只做过**代码层面**的检查（路径/编码/中断信号都按 Linux 写好并有单元测试），
> 首次真实部署请按下面步骤执行，遇到问题把报错带回来修。

## 架构

```
浏览器 ──HTTPS──> nginx (443, 证书 certbot) ──> gunicorn 127.0.0.1:8770 (1 worker × 8 threads) ──> vcca.webapp
                                                                    └─ 任务：子进程 python vcca_plan.py fetch/build/…
```

- **只能 1 个 worker**：任务队列（同时只跑一个 fetch/build/export）、任务进度和结果缓存都在这一个进程里。
  多开 worker 会出现"两个 fetch 同时跑"、看不到别的 worker 启动的任务。并发请求靠 `--threads 8`。
- 代码与数据分离：

| 位置 | 内容 |
|---|---|
| `/opt/vcca/fr24_work` | 代码（本文件夹，含 `FlightRadarAPI/`；不要修改它） |
| `/opt/vcca/venv` | Python 虚拟环境（Python ≥ 3.11） |
| `/srv/vcca/<航季>/` | 航季文件夹：`season.toml`、CAAC `.xls`、`output/<时间>/` |
| `/srv/vcca/Data/workspaces.db` | VAMSYS 数据库（网页上传） |
| `/srv/vcca/rules/` | 机场等级 / 机队规则（首次安装从代码复制，之后在网页配置页修改） |
| `/srv/vcca/cache/` | FR24 缓存 `fr24_cache.db` |
| `/srv/vcca/state/` | 网页账号 `users.toml`、会话、任务日志、上传临时文件 |
| `/etc/vcca/vcca.env` | 环境变量（路径、FR24 账号），`root:vcca 0640` |

环境变量（`vcca.env`）：`VCCA_PLAN_ROOT`、`VCCA_RULES_DIR`、`VCCA_CACHE_DIR`、`VCCA_STATE_DIR`、
`VCCA_TRUST_PROXY=1`（信任 nginx 传来的 `X-Forwarded-For/Proto`，用于登录限速与 `Secure` cookie）、
`VCCA_MAX_UPLOAD_MB`（默认 512）、`FR24_USER` / `FR24_PASSWORD`。

## 首次安装

1. 服务器准备：`sudo apt install python3 python3-venv nginx certbot python3-certbot-nginx rsync`
   （`python3 --version` 需 ≥ 3.11；否则装 3.12 并用 `PYTHON=/usr/bin/python3.12`）。
2. 把整个 `fr24_work` 文件夹拷到服务器（不需要 `fr_venv/`、`fr24_cache/`、`webstate/`、`legacy/`），例如
   `rsync -a --exclude fr_venv --exclude fr24_cache --exclude webstate --exclude legacy fr24_work/ server:~/fr24_work/`
3. `cd ~/fr24_work && sudo bash deploy/install.sh`
4. `sudo nano /etc/vcca/vcca.env` 填 `FR24_USER` / `FR24_PASSWORD`，`sudo systemctl restart vcca-web`
5. 创建网页账号（交互输入密码，至少 8 位）：
   ```bash
   sudo -u vcca env $(grep -v '^#' /etc/vcca/vcca.env | xargs) \
       /opt/vcca/venv/bin/python /opt/vcca/fr24_work/vcca_plan.py web-user set <名字>
   ```
   `web-user list` 列出账号，`web-user delete <名字>` 删除。
6. nginx：
   ```bash
   sudo cp /opt/vcca/fr24_work/deploy/nginx-vcca.conf /etc/nginx/sites-available/vcca
   sudo nano /etc/nginx/sites-available/vcca          # 把 vcca.example.com 换成你的域名
   sudo ln -s /etc/nginx/sites-available/vcca /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d <你的域名>
   ```
7. 浏览器打开 `https://<你的域名>/` 登录。

## 首次放数据

二选一：

- **网页**：数据页 → "新建航季"（没有航季时用模板），然后配置页填 `[vamsys] workspace_id`、
  `[season] name / start_date / end_date`；数据页上传 `workspaces.db` 和三份 CAAC `（三字码）.xls`。
- **直接拷贝**：把本机的航季文件夹（`season.toml` + `.xls`）拷到 `/srv/vcca/<航季>/`，
  `workspaces.db` 拷到 `/srv/vcca/Data/`，`chown -R vcca:vcca /srv/vcca`。
  **注意** `season.toml` 里 `[vamsys] db_path` 是相对 `VCCA_PLAN_ROOT` 的：本机是 `"../Data/workspaces.db"`
  （= `F:\vCCA\Data`），服务器上要改成 `"Data/workspaces.db"`（= `/srv/vcca/Data`）。
- 想沿用本机的 FR24 缓存，把 `fr24_work/fr24_cache/fr24_cache.db` 拷到 `/srv/vcca/cache/`（否则首次 fetch 约 30–40 分钟）。

然后在运行页跑 `status` 检查，再 `run`。

## 更新代码

```bash
rsync -a --exclude fr_venv --exclude fr24_cache --exclude webstate --exclude legacy fr24_work/ server:~/fr24_work/
ssh server 'cd ~/fr24_work && sudo bash deploy/install.sh'
```
`install.sh` 可重复运行：只刷新代码与依赖，不动 `/srv/vcca` 和 `vcca.env`。重启服务会中断正在运行的任务
（下次启动时该任务标记为"中断"；fetch 已抓的数据不会丢，重新运行即可续抓）。

## 备份

需要备份的只有 `/srv/vcca`（航季、上传的数据、规则、缓存、账号）和 `/etc/vcca/vcca.env`。例如每日：
```bash
tar czf /var/backups/vcca-$(date +%F).tgz -C / srv/vcca etc/vcca
```
配置每次保存前自动备份到 `<航季>/.config_history/`、`/srv/vcca/rules/.history/`；
上传 `workspaces.db` 时旧文件改名为 `workspaces.<时间>.db`（保留 5 份）；同名计划 `.xls` 备份到 `<航季>/.plan_history/`。

## 日志与排错

- `journalctl -u vcca-web -f`：服务日志（gunicorn 访问日志 + 应用错误）。
- 任务日志：网页运行页，或 `/srv/vcca/state/jobs/<任务id>.log`。
- 手动执行命令（与网页任务相同的环境）：
  ```bash
  sudo -u vcca env $(grep -v '^#' /etc/vcca/vcca.env | xargs) /opt/vcca/venv/bin/python /opt/vcca/fr24_work/vcca_plan.py status
  ```
- 时区：服务单元设了 `TZ=Asia/Shanghai`；运行文件夹名和报告时刻固定为 UTC，网页上的文件时间按浏览器时区显示。

## 风险与注意

- **FR24 从服务器 IP 访问可能被限流或拦截**（数据中心 IP 常被 Cloudflare 额外校验）。部署后先在运行页跑
  `fetch`（勾"只预览"不会发请求，所以要跑一次真实的小范围抓取，例如"只抓这些班号"填 1–2 个班号）确认能登录、能取数据；
  不行的话可以继续在本机抓取，再把 `fr24_cache.db` 同步到服务器（或反过来只在服务器上 build/export）。
- 登录会话 cookie 只在 HTTPS 下带 `Secure`；请始终通过 nginx 的 HTTPS 访问，不要把 8770 端口暴露到公网
  （gunicorn 只监听 127.0.0.1）。
- 目前只有"账号"没有角色：任何登录用户都能运行任务、改配置、上传数据。
- `FlightRadarAPI/` 是借用的第三方库，随代码一起部署，不要修改。
