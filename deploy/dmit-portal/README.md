# DMIT 测试导航

`https://genshin.fastcdn.world:8443/` 是两个测试系统的共同入口。AirChina Ops 继续使用 `/company`、`/pilot` 和 `/api/`；`/plan/` 转发到航季工具的 `127.0.0.1:8770`。

航季工具本身照常用 `deploy/install.sh --domain genshin.fastcdn.world --no-certbot` 安装，然后删除它生成的独立站点 `/etc/nginx/sites-enabled/vcca`（完整步骤见 [../DEPLOY.md](../DEPLOY.md) 第 7 节；之后每次重新运行 `install.sh` 都要再删一次，日常更新用 `sudo vcca update` 则不涉及 nginx）。

部署时，`index.html` 对应服务器的 `/var/www/vcca-test-portal/index.html`，`routes.conf` 对应 `/etc/nginx/snippets/vcca-test-portal.conf`：

```bash
cd /opt/vcca/app
sudo install -D -m 644 deploy/dmit-portal/routes.conf /etc/nginx/snippets/vcca-test-portal.conf
sudo install -D -m 644 deploy/dmit-portal/index.html /var/www/vcca-test-portal/index.html
```

现有 `airchina-ops-test` 站点在 **8443 的 server 块**内引用：

```nginx
include /etc/nginx/snippets/vcca-test-portal.conf;
```

两个入口都继承该块的 Basic Auth（`routes.conf` 转发给航季工具前会清掉 `Authorization` 头）；航季工具的会话另由应用管理，Cookie 由 `proxy_cookie_path` 限于 `/plan/`。航季工具前端只用相对路径，不需要知道 `/plan/` 前缀。如果重新生成 AirChina Ops 的 nginx 站点配置，保留这条 include。变更后运行 `sudo nginx -t`，通过后再 `sudo systemctl reload nginx`。要恢复原站首页，移除这条 include 并按相同步骤重载。
