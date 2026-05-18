# WoRMS 数据本地化优化建议（2026-05-16）

> **当前状态（2026-05-16 会话结束）**：
> - P0a / P0b 已实施（错误诊断 + bootstrap 自动注入 741 starter）
> - **2026-05-16 后续更新已实施**：REST 全树递归抓取（取代失效的 DwC-A 直链 403）
> - **2026-05-16 Hotfix-b 已实施**：SSL EOF 重试 + visited 时机修复 + skip-on-fail 韧性
> - **130 unittest 通过**，待用户在 WSL 真机跑完一次 ~83 分钟全量抓取验证
> - 历史路线图在 § 1-7 节；最新实施分两段附在文末（§ 后续更新 + § Hotfix-b）

> **触发场景**：用户在离线/受限网络环境下使用 WoRMS 分类匹配功能，下载全部失败。

---

## TL;DR — 下次会话起点

如需继续工作或排查，按此顺序：

1. **代码**
   - `specimen_app/worms_client.py:crawl_full_rest()` — REST 全树 BFS 抓取（rate=2.0 默认，~83 min）
   - `specimen_app/worms_client.py:_http_get_json()` — 含 `_TRANSIENT_NET_ERRORS` 白名单 + 7 次退避 + jitter
   - `specimen_app/worms_match.py:_WormsRestCrawlWorker` + `_CopyableErrorDialog` + `_auto_export_to_desktop()`
   - `tools/build_worms_cache.py` — `--rest` / `--export` / `--download`(alias)
   - `tests/test_worms_crawler.py` — 14 测试
2. **运行时文件**
   - `~/.specimen_inventory/worms_cache.sqlite` — 缓存主库
   - `~/.specimen_inventory/worms_crawl_state.json` — 续传 state（visited / failed / queue / imported）
   - `~/.specimen_inventory/worms_crawl_state_failed.json` — 抓取完成时跳过的 AphiaID 列表（如有）
   - `specimen_app/assets/worms_cache_bootstrap.sqlite.gz` — 741 条 starter（首启自动注入）
3. **WoRMS 端点现状**
   - `WoRMS_DwCA.zip`（旧 URL `/export/exports/`） → **403 弃用**
   - `WoRMS_DwC-A.zip`（真 URL `/export/gbif/`） → **403 GBIF IP 白名单**
   - `usersrequest.php` → 人工审批（机构邮箱，不可再分发）
   - **REST `AphiaChildrenByAphiaID/{id}?offset=N`** → **公开可用**，分页 50/call
4. **用户待操作**
   - WSL 重启 app → 「通过 WoRMS REST 全量抓取」→ 自动从断点续传
   - 完成后桌面 `worms_cache_YYYY-QN.sqlite.gz` 可上传 GitHub Release
5. **测试**：`cd specimens-organise && python -m unittest discover -s tests` → 期望 130 OK

---

## 本次会话两次决策记录

### 决策 1（用户原始诉求 → REST 递归全树）
- 用户："全部 WoRMS 数据，不只 700 多个，下载不能用"
- 调研：DwC-A 直链 403（WebFetch 实测），GBIF 镜像无 WoRMS，usersrequest 需机构邮箱
- 决策：REST `AphiaChildrenByAphiaID` BFS 全树（公开可用），双入口（UI + CLI），完成自动导出到桌面
- 用户已 approve

### 决策 2（用户 WSL 实跑遇 SSL EOF → Hotfix-b 韧性）
- 用户："SSL UNEXPECTED_EOF_WHILE_READING URL: AphiaChildrenByAphiaID/853056"
- 诊断三 bug：SSL 不在 retry 白名单 / visited 提前标记丢节点 / 单节点失败 abort 整库
- 决策：扩 `_TRANSIENT_NET_ERRORS` 白名单 + max_retries 7 + jitter + visited 推迟 + skip-on-fail + rate 3→2
- 用户已 approve「跳过节点继续 + 降到 2 qps」

---

## 1. Context

### 用户场景
项目部署在野外考察、机构内网或其他无公网访问的环境。用户打开「WoRMS → 管理本地数据库」点击下载按钮，全部返回「网络问题」错误。用户没有上下文判断这是临时故障还是部署问题，也不知道有「拷贝文件离线导入」的替代路径。

### 目标
1. 让 WoRMS 功能在**完全离线**的环境下也能使用（开箱有数据）
2. 错误信息**指导用户走离线路径**，而不是反复尝试下载
3. 提供**跨机器迁移**的明确流程（联网电脑下载 → U盘传输 → 离线导入）
4. **不引入新依赖**，保持 stdlib-only 现状

### 范围
本文档**只给建议**，不动代码。后续按 P0/P1/P2/P3 优先级分批实施。

---

## 2. 现状审计

### 模块清单

| 文件 | 行数 | 职责 |
|------|------|------|
| `specimen_app/worms_client.py` | 469 | urllib REST 客户端 + SQLite 本地缓存 + DwC-A 导入 + gz 压缩/解压 |
| `specimen_app/worms_match.py` | 1063 | UI 窗口、QThread workers、三种下载路径、批量匹配 UI |
| `tools/build_worms_cache.py` | 150 | 季度构建工具，生成 `worms_cache_YYYY-QN.sqlite.gz` 用于 GitHub Release |
| `tests/test_worms_client.py` | 368 | 全 mock 单元测试 |

### 已具备的离线能力

| 能力 | 实现 |
|------|------|
| SQLite 本地缓存 | `~/.specimen_inventory/worms_cache.sqlite`（Linux）/ `%APPDATA%/标本入库管理/`（Windows） |
| Cache-first 查询 | `query_worms_with_cache()` 先查 SQLite，未命中才走网络 |
| DwC-A 批量导入 | `import_dwca(zip_path)` 解析 `Taxon.tsv`，2000 行/批写入 |
| gz 压缩缓存分发 | `export_cache_gz()` / `install_cache_gz()`，~15MB 压缩 / ~50MB 解压 |
| Session 级内存缓存 | `WormsMatchWindow._session_cache` 防止同会话重复查询 |
| 原子写入 | temp 文件 + `os.replace()` 防崩溃损坏 |
| 主机白名单 | 仅允许 `marinespecies.org` / `github.com` / `githubusercontent.com` |
| 流式下载 | 256KB chunk + 进度回调 |

### 关键不足（用户痛点根因）

| # | 不足 | 用户感受 |
|---|------|----------|
| 1 | 开箱缓存为空，首次必须联网 | 「装好就不能用」 |
| 2 | 错误信息只显示「网络问题」 | 不知道下一步该做什么 |
| 3 | 没有「离线模式」UI 开关 | 下载按钮一直在那里诱导反复尝试 |
| 4 | 缺少「跨机器迁移」文档 | 不知道可以在另一台电脑下载后拷过来 |
| 5 | 无 retry / 退避 | 网络抖动一次就失败 |
| 6 | 无网络可达性自检 | 用户无法快速判断是哪个环节断了 |

---

## 3. 研究结论

### WoRMS 官方 API 关键事实

| 项 | 值 |
|----|-----|
| Base URL | `https://www.marinespecies.org/rest/` |
| 认证 | 无需 |
| 公开速率限制 | 未文档化（联系 `info@marinespecies.org`） |
| HTTPS 强制 | 是（2022-01-03 起 301 跳转） |
| 全库 dump | 需走 `https://www.marinespecies.org/usersrequest.php` 申请，**禁止再分发** |
| 公开 DwC-A | `https://www.marinespecies.org/export/exports/WoRMS_DwCA.zip`（~30-50MB） |
| 测试/staging 环境 | **无** |

### 同类开源项目（值得参考的设计模式）

| 项目 | 仓库 | 关键设计 |
|------|------|----------|
| **PyWorms** | [iobis/pyworms](https://github.com/iobis/pyworms) | 内存缓存 2000 条/端点；Python 库参考 API 命名 |
| **worrms** (R) | [ropensci/worrms](https://github.com/ropensci/worrms) | R 客户端；端点封装方式可参考 |
| **FathomNet worms-server** | [fathomnet/worms-server](https://github.com/fathomnet/worms-server) | **Docker 化的离线 WoRMS 镜像**——内存中保存全库；离线部署金标准 |
| **requests-cache** | [requests-cache/requests-cache](https://github.com/requests-cache/requests-cache) | SQLite/Redis/JSON backend；HTTP 缓存通用方案 |
| **backoff** / **tenacity** | [litl/backoff](https://github.com/litl/backoff) / [jd/tenacity](https://github.com/jd/tenacity) | 指数退避 + jitter；retry 标准库 |

### 通用最佳实践（来自上述参考项目）

- **超时**：connect 5-10s，read 15-30s
- **退避**：起始 1s，倍增，上限 10s，加 jitter
- **可重试错误**：5xx、超时、`URLError`；4xx 不重试
- **离线检测**：DNS 解析失败 → 直接走离线路径，跳过 retry

---

## 4. 优化建议（按优先级）

### P0 — 内置启动 SQLite 缓存（解决「开箱无数据」）

**问题**：首次安装后缓存为空，离线用户无法使用。

**建议**：
1. 项目仓库新增 `assets/worms/worms_cache_bootstrap.sqlite.gz`（季度更新，~15MB）
2. PyInstaller 打包时 `--add-data` 把该文件带入分发包
3. `worms_client.py` 在首次访问缓存时检测：若缓存文件不存在且 bootstrap 文件存在，调用现有 `install_cache_gz()` 解压到用户 cache 目录
4. UI「管理本地数据库」对话框显示：「内置缓存：YYYY-QN（约 N 条记录）」

**接口复用**：`install_cache_gz()` 已存在（`worms_client.py:437-468`），只需加自动触发逻辑。

**验收**：全新环境（无 `~/.specimen_inventory/`）启动后，「分类浏览」搜索 `Gadus morhua` 能直接返回结果，**不需任何网络**。

---

### P0 — 错误诊断改进（解决「不知道下一步」）

**问题**：当前网络错误统一显示「查询失败：URLError ...」，用户不知道是 DNS / 防火墙 / SSL / 服务器宕机。

**建议**：
1. `worms_client.py` 在 `URLError` 捕获处加分类：
   - `socket.gaierror` → 「DNS 解析失败：检查网络连接或使用离线模式」
   - `TimeoutError` / `socket.timeout` → 「请求超时：网络可能不稳定」
   - `ssl.SSLError` → 「SSL 证书错误：检查系统时间或代理配置」
   - 其他 → 通用「网络不可达」
2. 每种错误附带 **「使用本地缓存」/「打开离线导入向导」** 按钮链接
3. 错误对话框中增加「复制错误详情」按钮，便于报 issue

**验收**：在 `/etc/hosts` 把 `marinespecies.org` 解析到 `127.0.0.1` → 错误信息应明确显示「DNS 解析失败」并提示离线模式。

---

### P1 — 显式「离线模式」开关

**问题**：用户在离线环境下，下载按钮一直可点，反复尝试反复失败。

**建议**：
1. `app_settings.py` 新增 `worms_offline_mode: bool = False`
2. 「管理本地数据库」对话框顶部加 checkbox：「☑ 离线模式（不尝试网络请求）」
3. 离线模式开启时：
   - 三个下载按钮 disabled，tooltip 显示「离线模式已开启」
   - 「匹配更新」/「分类浏览」查询不未命中时不走网络，直接显示「本地缓存未命中」
   - UI 顶部加状态条：「🔌 离线模式」
4. 错误诊断（P0）检测到 DNS 失败时，弹出建议：「是否开启离线模式以避免重复尝试？」

**验收**：开启离线模式后，点击下载按钮无反应；查询未命中显示明确的「未找到」而不是「网络错误」。

---

### P1 — 跨机器迁移指南文档

**问题**：用户不知道可以在联网电脑下载缓存/DwC-A 后拷贝过来。

**建议**：新增 `docs/worms-offline-migration.md`，内容：
1. 路径 A：另一台电脑用相同版本 app 在线模式下载缓存 → 拷贝 `~/.specimen_inventory/worms_cache.sqlite` 到目标机器
2. 路径 B：浏览器从 GitHub Release 直接下载 `worms_cache_YYYY-QN.sqlite.gz` → 「管理本地数据库 → 从文件导入」
3. 路径 C：浏览器下载 WoRMS 官方 `WoRMS_DwCA.zip` → 「管理本地数据库 → 导入 DwC-A」
4. 每条路径附 4-5 步截图（待补）

**验收**：新用户跟着文档走 5 分钟内能让离线机器有数据。

---

### P2 — Retry + 指数退避（stdlib 实现）

**问题**：偶发网络抖动一次失败用户就放弃。

**建议**：
- `worms_client.py:query_worms()` 包装一层 retry：
  - 仅对 `URLError`（非 `gaierror`）和 `TimeoutError` 重试
  - 最多 3 次，1s / 2s / 4s + random jitter (0-1s)
  - 离线模式开启时跳过 retry
- 用 stdlib `time.sleep()` + `random.random()`，**不引入** `backoff` / `tenacity`

**验收**：临时拔网线 → 重新插回 5 秒内，查询应自动恢复成功。

---

### P2 — 网络可达性自检按钮

**问题**：用户没有简单方法判断是哪个环节断了。

**建议**：「管理本地数据库」对话框加「🔍 诊断连接」按钮，依次测试：
1. DNS 解析 `www.marinespecies.org`（`socket.gethostbyname`）
2. TCP 端口 443 可达性（`socket.create_connection` 超时 3s）
3. HTTPS GET `https://www.marinespecies.org/rest/AphiaIDByName/Gadus%20morhua`（超时 5s）
4. GitHub Release API 可达性

每步显示 ✓/✗ 和耗时。结果可一键复制。

**验收**：所有 4 步显示绿色 → 网络正常；任何一步红色 → 错误信息精准指向问题层。

---

### P3 — 评估 FathomNet worms-server 内网镜像

**问题**：机构有多台离线设备，每台都拷贝 50MB 数据不优雅。

**建议**：
- 评估在机构内网部署 [fathomnet/worms-server](https://github.com/fathomnet/worms-server) Docker 镜像
- 本项目 `worms_client.py` 加 config 项 `worms_api_base_url`（默认 marinespecies.org，可改为内网地址）
- 主机白名单同步允许配置项地址

**注意**：worms-server 的 API 路径与官方略有不同，可能需要适配层。仅 P3 候选，不必现在做。

**验收**：将 `worms_api_base_url` 指向内网 Docker → 查询正常返回数据。

---

## 5. 路线图（4 个迭代）

### Milestone 1（推荐先做）
- ✅ P0：内置启动 SQLite 缓存
- ✅ P0：错误诊断改进
- 验收：全新环境无网络可用

### Milestone 2
- ✅ P1：离线模式开关
- ✅ P1：跨机器迁移文档（含截图）
- 验收：用户主动选择离线流程不再被下载按钮误导

### Milestone 3
- ✅ P2：retry + 指数退避
- ✅ P2：网络诊断按钮
- 验收：偶发网络问题自愈，错误定位精准

### Milestone 4（可选）
- ✅ P3：worms-server 内网镜像支持
- 验收：机构内可部署本地 API 镜像

---

## 6. 参考链接

### 官方
1. WoRMS REST API：https://www.marinespecies.org/rest/
2. WoRMS Webservice：https://www.marinespecies.org/aphia.php?p=webservice
3. WoRMS Users Request（dump 申请）：https://www.marinespecies.org/usersrequest.php
4. WoRMS DwC-A 公开导出：https://www.marinespecies.org/export/exports/WoRMS_DwCA.zip

### 同类项目
5. PyWorms：https://github.com/iobis/pyworms
6. worrms (R)：https://github.com/ropensci/worrms
7. FathomNet worms-server：https://github.com/fathomnet/worms-server
8. mbari/worms-server Docker：https://hub.docker.com/r/mbari/worms-server

### 通用技术
9. requests-cache：https://github.com/requests-cache/requests-cache
10. backoff：https://github.com/litl/backoff
11. tenacity：https://github.com/jd/tenacity
12. Real Python: API Caching：https://realpython.com/caching-external-api-requests/

### 相关项目文档
13. `docs/marine_species_ml_review_2024_2026.md` — WoRMS + AI 集成的背景研究
14. `CLAUDE.md`（项目根） — 项目整体架构
15. `tools/build_worms_cache.py` — 缓存构建工具

---

## 7. 决策记录

| 日期 | 决策 | 原因 |
|------|------|------|
| 2026-05-16 | 仅写文档，不动代码 | 用户希望先有完整路线图，后续按优先级实施 |
| 2026-05-16 | 不引入 `requests-cache` / `backoff` | 保持 stdlib-only，与项目原则一致 |
| 2026-05-16 | 不立即实施 Docker 方案 | 用户当前为单机离线场景，先解决「开箱可用」 |
| 2026-05-16 | 优先级 P0 = 内置缓存 + 错误诊断 | 直接解决用户报告的痛点 |
| 2026-05-16 | 改为立即实施 P0 | 用户反馈「解决为导向」，决定一次性落地代码 |

---

## 8. 实施日志（2026-05-16 当天完成）

### P0a：错误诊断分类 ✅
- `specimen_app/worms_client.py`：`query_worms()` URLError 异常按 `socket.gaierror` / `socket.timeout` / `ssl.SSLError` / 通用 4 类分流，给出中文可操作提示
- 顶部增加 `import socket, ssl, sys`

### P0b：内置启动缓存自动注入 ✅
- `specimen_app/worms_client.py` 新增：
  - `_BOOTSTRAP_ASSET_NAME = "worms_cache_bootstrap.sqlite.gz"`
  - `_resolve_bootstrap_path()` — 跨 source / `_MEIPASS` 解析（与 `字段模版` 同模式）
  - `ensure_bootstrap_cache()` — 用户缓存空 + bootstrap 存在 → 调 `install_cache_gz()`；静默失败
- `specimen_app/ui.py:run_app()`：apply_app_icon 之后调用 `ensure_bootstrap_cache()`，成功时状态栏 3 秒提示
- `build_release.py`：仿照 `字段模版` 加 `--add-data` 把 `assets/worms_cache_bootstrap.sqlite.gz` 打入 `_internal/specimen_app/assets/`
- `specimen_app/assets/README.md`：构建/更新 bootstrap 文件的操作说明
- `.gitattributes`：`*.sqlite.gz binary` 防止 git 误判文本

### 测试 ✅
- `tests/test_worms_client.py` 新增 7 测试：
  - 4 个 `TestErrorClassification`：DNS / timeout / SSL / 通用
  - 3 个 `TestBootstrapCache`：空缓存安装 / 已有缓存跳过 / 无 bootstrap 跳过
- 全量：`python -m unittest discover -s tests` → **115 通过**（原 108 + 7 新增）

### 尚需手动操作
**唯一未完成的环节**：联网机器执行：
```bash
python tools/build_worms_cache.py --download
# 输出 worms_cache_YYYY-QN.sqlite.gz
mv worms_cache_*.sqlite.gz specimen_app/assets/worms_cache_bootstrap.sqlite.gz
```
将该文件放入 `specimen_app/assets/`，下次 `python build_release.py` 即可把 WoRMS 数据打入分发包。

文件缺失时代码静默跳过，**不影响 app 正常运行**。

### 用户体验变化
| 场景 | 改动前 | 改动后 |
|------|--------|--------|
| 离线首启动（有 bootstrap） | WoRMS 完全不可用 | 自动加载内置数据，立即可查全部分类 |
| 离线首启动（无 bootstrap） | 同左 | 行为不变，错误信息更友好 |
| 已有用户缓存 | 行为不变 | 行为不变（bootstrap 不覆盖用户数据） |
| DNS 失败 | `URLError: ...` | 「DNS 解析失败：建议检查网络或从其他电脑拷贝」 |
| 请求超时 | 同上 | 「请求超时：可稍后重试」 |
| SSL 错误 | 同上 | 「SSL 错误：检查系统时间或代理」 |

---

## 2026-05-16 修订（同日补丁）：DwC-A 端点被 GBIF IP 锁定 → 改 REST 全树递归

### 背景

P0b 完成后，用户在 WSL 实测发现「全量下载」按钮报 HTTP 403 Forbidden。

### 诊断

`WebFetch` 实测：

| URL | 状态 |
|-----|------|
| `https://www.marinespecies.org/export/exports/WoRMS_DwCA.zip` (代码原 URL) | **403 Forbidden** — 路径已弃用 |
| `https://www.marinespecies.org/dwca/WoRMS_DwC-A.zip` | 301 → `/export/gbif/WoRMS_DwC-A.zip` → **403** |
| `https://www.marinespecies.org/export/gbif/WoRMS_DwC-A.zip` (GBIF 元数据 doi:10.14284/170 中公布的真实 URL) | **403 Forbidden（Restricted to GBIF）** |
| `https://www.marinespecies.org/download/` | 401 → 必须机构邮箱注册 + 人工审批 |
| `https://hosted-datasets.gbif.org/datasets/…` | 不镜像 WoRMS |
| `https://www.marinespecies.org/rest/AphiaChildrenByAphiaID/2` | **200 OK** — 返回 48 phyla |

**结论**：WoRMS DwC-A zip 已限制为 GBIF IP 白名单，普通客户端无法绕过；REST API 仍完全公开。

### 修复

放弃 DwC-A 直链全量下载方案，新增 **REST 递归全树抓取**：

- `specimen_app/worms_client.py` 新增：
  - `crawl_full_rest(progress_cb, rate_limit_qps, resume_state_path, should_stop, kingdoms, timeout)` — BFS 从 8 个 kingdom 根 AphiaID 出发，递归调 `AphiaChildrenByAphiaID/{id}?marine_only=false&offset=N`（分页 50/call），子 JSON 已含完整 WoRMSRecord 字段，直接 batch INSERT。
  - `_http_get_json()` — 内部 HTTP 助手，含 429/503 指数退避（默认 5 次）+ 详细错误诊断（URL + 状态码 + 响应头 + 体首段）。
  - `_save_resume_state()` / `_load_resume_state()` — 断点续传 JSON 持久化（每 5000 条写一次，完成时删除）。
  - `_DEFAULT_KINGDOM_APHIA_IDS = (2, 4, 3, 383, 146, 147, 148, 149)` — 8 个 kingdom 根。
  - `WORMS_DWCA_URL` 保留但加注释标注已 IP 限制，仅供「导入本地 DwC-A zip」路径参考（用户在其他渠道获取 zip 后离线导入）。

- `specimen_app/worms_match.py`：
  - 删除 `_WormsDwcaDownloadWorker`，改为 `_WormsRestCrawlWorker`（QThread + `request_stop()` + 复用 `crawl_full_rest()`）。
  - 「从 WoRMS 官网下载全量 DwC-A」按钮改名「通过 WoRMS REST 全量抓取（约 30-60 分钟，约 50 万条）」。
  - 新增 `_CopyableErrorDialog`（QPlainTextEdit + 「复制全部」按钮）替代单行 QLabel 错误展示，HTTP 长报错可一键复制。
  - 完成后自动调 `_auto_export_to_desktop()`：导出 `worms_cache_YYYY-QN.sqlite.gz` 到 `~/Desktop`（WSL fallback `~/`），并弹消息框提示用户上传 GitHub Release。
  - 抓取期间按钮文字变「取消抓取」，第二次点击触发 `request_stop()`，已写入数据保留，断点 state 持久化供下次续传。

- `tools/build_worms_cache.py`：
  - `--download` 标记 DEPRECATED，自动转 `--rest`（打印警告）。
  - 新增 `--rest`（REST 递归全量）+ `--export`（仅导出当前缓存，不抓取）两个独立模式。
  - 移除 `_download_dwca()`，新增 `_do_rest_crawl()`。

- `tests/test_worms_crawler.py`：
  - 10 个新测试覆盖：BFS 递归 / 分页 / 断点续传 / 取消 / 429 重试 / 403 诊断 / 畸形记录 / 循环去重。

### 测试

`python -m unittest discover -s tests` → **126 通过**（前 116 + 10 新增）。

### 用户操作流程

| 场景 | 操作 |
|------|------|
| **首次使用 / 想要全库** | UI 点「通过 WoRMS REST 全量抓取」→ 等 30-60 分钟 → 自动导出 `worms_cache_*.sqlite.gz` 到桌面 |
| **想分发给团队** | 把桌面那份 `worms_cache_*.sqlite.gz` 作为 GitHub Release 资产上传，其他用户点「从 GitHub Release 下载」秒级安装 |
| **完全离线机器** | 在另一台联网机器跑完抓取 → 拷贝 `~/.specimen_inventory/worms_cache.sqlite` 整个文件 → 复制到离线机同路径 |
| **中途断网/退出** | 重新点「通过 WoRMS REST 全量抓取」自动从断点续传（`~/.specimen_inventory/worms_crawl_state.json`） |
| **手上已有 DwC-A zip**（其他渠道获取，如 GBIF 客户端 / WoRMS 用户审批） | 仍可点「导入本地 DwC-A zip 文件…」→ 调原 `import_dwca()` 路径 |

### 命令行

```bash
# REST 全量抓取并打包（约 30-60 分钟）
python tools/build_worms_cache.py --rest

# 仅导出当前缓存（不抓取，秒级）
python tools/build_worms_cache.py --export

# 旧调用方式（自动转 --rest，仅打印警告）
python tools/build_worms_cache.py --download
```

### 限制

- WoRMS REST 速率限制未公布，默认 3 req/s 经验值。如未来收紧 → 调小 `crawl_full_rest(rate_limit_qps=1.0)`。
- WoRMS 也可能未来限制 REST API（目前公开）。监控点：用户报告 429/403 持续出现。届时唯一兜底是 `usersrequest.php` 人工申请。

---

## 2026-05-16 Hotfix-b：SSL EOF 中断 + 抓取丢节点修复

### 触发

上一节 REST 全量抓取上线后，用户在 WSL 实测约 1-2 小时后报错：

```
SSL 错误：[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1032)
URL: https://www.marinespecies.org/rest/AphiaChildrenByAphiaID/853056?marine_only=false&offset=1
建议：检查系统时间 / 证书 / 代理。
```

### 诊断（三处 bug）

1. **`_http_get_json()` retry 白名单漏 SSL**：旧版仅含 `gaierror`/`timeout`/`TimeoutError`/`ConnectionError`，未含 `ssl.SSLError`。`SSL_ERROR_EOF` / `UNEXPECTED_EOF_WHILE_READING` 是远端 TLS 提前关闭，长会话 10k+ 请求必然偶发瞬时错误，应重试而非 abort。`http.client.IncompleteRead`、`RemoteDisconnected`、`ConnectionResetError`、裸 `OSError` 同理。
2. **`crawl_full_rest()` visited 时机错误**：`visited.add(current_id)` 在分页 loop **之前**执行，分页中途抛错 → state 已含 `current_id` 在 visited → resume 跳过该节点 → 剩余子节点永久丢失。
3. **单节点失败 abort 整库**：单节点重试 7 次仍失败 → 抛 `WormsError` → 终止整个抓取。50 万节点中只要 1 个坏节点就崩，应跳过 + 继续。

### 修复

| 文件 | 改动 |
|---|---|
| `specimen_app/worms_client.py` | 模块顶部加 `_TRANSIENT_NET_ERRORS` 元组（含 `ssl.SSLError` / `http.client.IncompleteRead` / `RemoteDisconnected` / `ConnectionError` / `OSError`）；`_http_get_json()` 双层 except（URLError 包装的 + 裸抛的）都用此白名单；`max_retries` 默认 5→7；退避加 10-30% jitter；上限 60s。`crawl_full_rest()` 默认 `rate_limit_qps=3.0→2.0`；`visited.add()` 推迟到分页 loop **成功后**；单节点 `WormsError` catch → `failed.add(current_id)` + 进度回调通知 + 继续；state schema 加 `failed: [aphia_ids]`；完成时把 failed 列表 dump 到 `~/.specimen_inventory/worms_crawl_state_failed.json`（独立于自动删除的 state 文件）。 |
| `tests/test_worms_crawler.py` | `test_403_*` 改名 + 改契约（403 现在 skip 而非 raise）；新增 `test_ssl_error_retried_then_succeeds` / `test_incomplete_read_retried_then_succeeds` / `test_persistent_failure_skips_and_continues` / `test_visited_not_set_until_pagination_complete`。|

### 测试

`python -m unittest discover -s tests` → **130 通过**（前 126 + 4 新增）。

### 用户操作

用户已有部分缓存 + state（含已 visited 节点）。修复后：

```
1. WSL 重启 app
2. 点「通过 WoRMS REST 全量抓取」
3. 自动从 ~/.specimen_inventory/worms_crawl_state.json 续传，跳过已 visited 节点
4. SSL EOF 现在会自动重试，长会话稳定性大幅提升
5. 单节点 7 次重试仍失败 → 进入 failed 集合，跳过继续，最终在
   ~/.specimen_inventory/worms_crawl_state_failed.json 记录跳过的 AphiaID 列表
6. UI 进度状态栏会显示「⚠ AphiaID X 跳过」实时通知
```

若想从头重抓：清 `~/.specimen_inventory/worms_crawl_state.json` + UI「清空缓存」。

### 总时长变化

| rate_limit_qps | 全库估算 |
|---|---|
| 3.0（旧默认） | ~55 min |
| **2.0（新默认）** | **~83 min** |
| 1.5（更保守） | ~110 min |

时长换稳定，长会话不易触发服务器侧节流。
