# vCCA 航季航班计划工具 —— 使用说明

把 **CAAC 预先计划** + **FR24 实飞数据** 与 **VAMSYS 现行航线**（`F:\vCCA\Data\workspaces.db`，只读）对账，输出
FlightOpsDesk 导入 CSV、核查 xlsx 和 HTML 报告。设计与判定规则见 [重构计划.md](重构计划.md)。

## 1. 运行方式

在 `F:\vCCA\plan\fr24_work` 下用项目自带的 Python：

```powershell
.\fr_venv\Scripts\python.exe vcca_plan.py            # 文字菜单
.\fr_venv\Scripts\python.exe vcca_plan.py status     # 配置、计划文件、VAMSYS、账号、缓存新鲜度
.\fr_venv\Scripts\python.exe vcca_plan.py fetch      # 增量抓取 FR24（按 P0/P1/P2 有效期）
.\fr_venv\Scripts\python.exe vcca_plan.py build      # 计算 → output\<时间>\decisions.json
.\fr_venv\Scripts\python.exe vcca_plan.py export     # 生成 xlsx / CSV / report.html / summary.json
.\fr_venv\Scripts\python.exe vcca_plan.py run        # fetch + build + export
.\fr_venv\Scripts\python.exe vcca_plan.py test       # 规则与格式测试
.\fr_venv\Scripts\python.exe vcca_plan.py serve      # 网页前端 http://127.0.0.1:8770/（见第 7 节；也可双击 启动前端.cmd）
```

常用参数：
- `--season 2026-27-1`：指定航季文件夹（默认取 `F:\vCCA\plan` 下最新含 `season.toml` 的文件夹）
- `fetch --dry-run`：只列出将抓的目标、请求数和预计耗时，不发请求
- `fetch --deep`：全机队注册号扫描（约 550 个请求；默认每 7 天自动做一次）
- `fetch --flights CA1234,CA5678` / `--regs B-2091`：只抓指定班号 / 注册号
- `fetch --boards ZBAA,ZSPD` / `--search`：可选的机场看板 / 搜索索引（默认不抓）
- `export --run 2026-11-26_0613`：对某次计算重新导出（文件夹名是计算时的 UTC 时间）

## 2. 日常流程

1. `run`（或依次 `fetch` → `build` → `export`）。日常增量抓取通常几十到几百个请求。
2. 打开 `output\<时间>\report.html` 浏览：概览、各分类表格、单条航线"计划 / VAMSYS / FR24"对照、与上次运行的差异。
   星期变更在表格里显示为 新增<ins>绿</ins> / 删除<del>红</del>；点开详情的"FR24 逐日记录"是 周 × 星期 的日历格：
   绿 = 推荐时段实飞，黄 = 其他时段实飞，红 = 有计划未见实飞，虚线 = 尚未到期的计划，底行是各星期在推荐时段的实飞周数。
   一条航线有多个排期（如 CAO1008）时，详情先列"同航线全部排期" S1…Sn（点行切换），日历里每班后标 Sn 表示归入哪个排期，
   蓝 = 其他排期实飞，黄 = 不属于任何排期。
3. 看 `重算清单.xlsx` 的 **9 人工复核** 表，处理 `_待复核` CSV 里的条目（确认无误的行可合并进对应主 CSV）。
4. 按顺序在 FlightOpsDesk 导入：
   1. `01_airports_add_*.csv`（如有）
   2. `02_routes_retire_*.csv`（停用，写 End Date）
   3. `02b_routes_retire_deferred_*.csv`（暂缓停用，确认后再导）
   4. `03_routes_update_*.csv`
   5. `04_routes_create_*.csv`
5. FlightOpsDesk 同步 VAMSYS 后再跑一次 `build`：结果应只剩新的变化（已导入的不会重复出现）。

`CHARTER`（临时加班/包机）、`POSITIONING`（调机/测试）、`WATCH_PLAN`（计划有但近期未飞）、`HOLD` 只出现在报告里，不进 CSV。

## 3. 输出文件（`<航季>\output\<YYYY-MM-DD_HHMM>\`）

| 文件 | 内容 |
|---|---|
| `重算清单.xlsx` | 0 总览 / 1 新增 / 2 调整 / 3 停用 / 4 计划外临时航班 / 5 待观察 / 6 一致 / 7 去回程缺失段 / 8 机队与机场审计 / 9 人工复核 |
| `0x_*.csv` | FlightOpsDesk 导入文件，列与格式与历史文件一致（CRLF、无 BOM）；文件名末尾是行数 |
| `*_待复核_*.csv` | 需要人工确认的同格式文件 |
| `report.html` | 单文件离线报告（浅色/深色） |
| `decisions.json` | 完整决策（`export` 只读它，可重复导出） |
| `summary.json` / `run.log` | 数量汇总、告警 |
| `target_states.json` | 回写给抓取层的分级（同时写入缓存库） |

生效时间（停用 End Date / 新建 Start Date）默认是"运行当天的次日 00:00 北京时间"（UTC 16:00）。

## 4. 换季操作清单

1. 在 `F:\vCCA\plan` 下新建航季文件夹，如 `2027-1`。
2. 放入 CAAC 的三份 `（三字码）.xls`。
3. 复制上一季的 `season.toml`，修改：
   - `[season]`：`name`、`start_date` / `end_date`（可删掉，自动取计划 FD23/FD24 众数）、`plan_files`
   - 如 VAMSYS 工作区变了：`[vamsys]` 的 `workspace_id`
   - 需要时调整 `[export]` 生效时间、默认 Cost Index，和 `[thresholds]`
4. 机场等级或机队变化时改 `fr24_work\rules\airport_grades.toml` / `fleets.toml`（未定级机场会在报告中标记复核）。
5. `vcca_plan.py --season 2027-1 status` 检查配置无报错，再 `run`。新航季首次抓取会把所有计划班号当作新目标，约 30–40 分钟。

## 5. 配置与数据位置

| 位置 | 说明 |
|---|---|
| `<航季>\season.toml` | 本季配置（日期、文件、工作区、航司、生效时间、Cost Index、抓取与判定阈值） |
| `fr24_work\rules\*.toml` | 机场飞行区等级、高原/机位排除、机队分组与机型映射 |
| `fr24_work\fr24_cache\fr24_cache.db` | FR24 缓存（SQLite，按航班 id 累积；可随时中断续抓） |
| `C:\Users\<你>\.env` | `FR24_USER` / `FR24_PASSWORD`（工具只检查是否存在，不打印） |
| `~\.fr24_session.json` | 登录会话缓存（6 小时） |
| `fr24_work\webstate\` | 网页前端：账号 `users.toml`、会话、任务日志（`VCCA_STATE_DIR`） |
| `fr24_work\legacy\` | 旧脚本备份，仅供对照 |
| `fr24_work\FlightRadarAPI\` | 借用的第三方库，不要修改 |

## 6. 常见问题

- **`status` 报配置错误**：按提示逐条修改 `season.toml`，所有问题会一次列出。
- **抓取中断**：直接重新运行 `fetch`，已抓的不会重复。连续失败 8 次会自动停止。
- **429 限流**：工具会自动降速退避；频繁出现可把 `[fetch] rate_per_sec` 调低。
- **登录失败**：检查 `.env`；删除 `~\.fr24_session.json` 可强制重新登录。
- **"去向核账"失败**：`build` 会报错并停止导出，说明有 VAMSYS 航线没有去向或重复去向，属于程序问题，需要修代码。
- **CSV 自检失败**：`export` 不写 CSV、退出码 1，并列出问题（如机队 ID 不存在、缺机场）。

## 7. 网页前端

同一套功能的浏览器界面：看状态、运行 fetch / build / export / run / test 并看实时日志、在页面里浏览结果、改配置、上传数据。
只用 Python 标准库（本机不需要装新依赖）；结果页与 `report.html` 共用同一个渲染器（`vcca/web/report.js`）。

**本机启动**：双击 `fr24_work\启动前端.cmd`，或

```powershell
.\fr_venv\Scripts\python.exe vcca_plan.py web-user set 你的名字     # 第一次：创建账号（输入两次密码，至少 8 位）
.\fr_venv\Scripts\python.exe vcca_plan.py serve                     # 打开 http://127.0.0.1:8770/
```

`serve` 参数：`--port 8770`、`--no-browser`（不自动打开浏览器）、`--host 0.0.0.0`（让局域网访问；只有 HTTP，慎用）。
账号存在 `fr24_work\webstate\users.toml`（PBKDF2 哈希，不存明文）；`web-user list` / `web-user delete 名字`。

| 页面 | 内容 |
|---|---|
| 概览 | 与 `status` 相同的检查（航季、计划文件、VAMSYS revision、未定级机场、FR24 账号与缓存、配置警告）、最新结果与 CSV、快捷按钮 |
| 运行 | 各命令的表单（fetch 选项、export 指定运行）、实时日志、进度条与剩余时间、**中断**（fetch 会保存进度）、任务历史 |
| 结果 | 运行列表 → 单次运行：完整结果浏览（与 report.html 相同：分类、搜索筛选、详情抽屉、FR24 逐日日历、差异——可选对比任意更早的运行）；左栏按 FlightOpsDesk 导入顺序列出 CSV，单个下载或整个文件夹 zip；重新导出 |
| 数据 | 上传 VAMSYS `workspaces.db`（校验后替换，旧文件备份保留 5 份）、上传 CAAC 计划 `.xls`（可同时加入 `plan_files`）、新建航季 |
| 配置 | 编辑 `season.toml` / `rules/airport_grades.toml` / `rules/fleets.toml`：仅校验、保存（先完整校验，失败不写入；保存前自动备份）、历史版本预览与恢复；Ctrl+S 保存 |
| 说明 | 日常流程与 FlightOpsDesk 导入顺序 |

- 同一时间只能运行一个任务；任务运行中不能保存配置或上传数据。任务日志在 `webstate\jobs\`，服务重启后仍可回看。
- 本机上传 `workspaces.db` 会替换 `F:\vCCA\Data\workspaces.db`（VAMSYS 客户端正在使用时会拒绝）；平时本机不需要上传，主要用于服务器。
- 部署到 Linux 服务器（systemd + gunicorn + nginx HTTPS）：见 [deploy/DEPLOY.md](deploy/DEPLOY.md)。
  路径可用环境变量覆盖：`VCCA_PLAN_ROOT`（航季文件夹所在目录）、`VCCA_RULES_DIR`、`VCCA_CACHE_DIR`、`VCCA_STATE_DIR`。
