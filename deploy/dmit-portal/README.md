# DMIT 测试导航

`https://genshin.fastcdn.world:8443/` 是两个测试系统的共同入口。AirChina Ops 继续使用 `/company`、`/pilot` 和 `/api/`；`/plan/` 转发到航季工具的 `127.0.0.1:8770`。

部署时，`index.html` 对应服务器的 `/var/www/vcca-test-portal/index.html`，`routes.conf` 对应 `/etc/nginx/snippets/vcca-test-portal.conf`。现有 `airchina-ops-test` 站点在 **8443 的 server 块**内引用：

```nginx
include /etc/nginx/snippets/vcca-test-portal.conf;
```

两个入口都继承该块的 Basic Auth；航季工具的会话另由应用管理，Cookie 限于 `/plan/`。如果重新生成 AirChina Ops 的 nginx 站点配置，保留这条 include。变更后运行 `nginx -t`，通过后再重载 nginx。要恢复原站首页，移除这条 include 并按相同步骤重载。
