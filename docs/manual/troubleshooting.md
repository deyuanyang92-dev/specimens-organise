# 故障诊断 + 跨平台关闭 + 后台下载

> v0.5.0+ E1/E3/C1/C2/W1/W2 新增功能的用户指南。

## 1. 异常退出诊断（E1）

### 自动诊断

应用启动时检查上次是否正常退出。若上次是异常退出（崩溃 / kill / 断电），启动 ~2 秒后会弹窗提示，列出最近的崩溃日志路径。

### 崩溃日志位置

`~/.specimen_inventory/crash_<时间戳>.log`（Linux/Mac）或 `%APPDATA%\标本入库管理\crash_<时间戳>.log`（Windows）。

每个文件含：
- 时间戳
- 应用版本
- 平台 + Python 版本
- PID
- 完整异常堆栈

最多保留 20 个 crash log，更老的自动清理。

### 反馈崩溃

把对应 `crash_*.log` 文件内容（可直接复制）发给开发者。日志含足够诊断信息。

### 主进程一定能退（E3）

`closeEvent` 中后台 QThread `wait(timeout)` 超时后会自动 `terminate()` 强杀。不会"关不掉应用"的情况。

---

## 2. 跨平台关闭（C1/C2）

### Linux / WSL

- Ctrl+C（终端运行时）→ 应用收到 SIGINT → 优雅关闭
- `kill <pid>` → SIGTERM → 优雅关闭
- 点窗口 X 按钮 → `closeEvent`

### Windows

- 点窗口 X 按钮 → `closeEvent`
- 任务管理器结束任务 → 进程立即终止（atexit 钩子可能不跑，但 workspace lock 由 10 分钟 stale 机制自愈）

### 主窗口接管所有 dialog（C1）

主窗口关闭时会接管所有打开的对话框（如 WoRMS 数据库管理）的后台 worker：
- 优雅请求停止 → 等待 3 秒 → 仍跑则强杀

确保**任何子窗口的后台任务都不会阻止主窗口关闭**。

### Lock 文件死锁兜底（C2）

如果 workspace lock 文件在 SMB / NAS / OneDrive 等网络盘上，断网时 `unlink()` 可能挂死。`atexit` 钩子用守护线程 + 3 秒超时执行 release_lock：
- 3 秒内释放 → 干净退出
- 超时 → 主进程仍能退；残留 lock 文件由现有 **10 分钟 stale 检测**自动清理

下次启动时若 lock 文件存在但年龄 >10 分钟 → 自动判定 stale → 释放。

---

## 3. WoRMS 后台下载（W1/W2）

### 前台模式（默认，关应用就停）

工具菜单 → WoRMS → 管理本地数据库 → 「通过 WoRMS REST 全量抓取」

- 走 QThread 后台线程
- 关闭对话框 / 关闭应用 → worker 优雅停（W2）
- state 文件自动保存（每 5000 条 + 完成时清除）
- 下次启动点同一按钮 → 自动续传

### 后台模式（W1，关应用也继续）

WoRMS 数据库管理对话框中勾选「**在独立后台进程下载（即使关闭应用也继续，断点续传）**」，再点抓取按钮：

- 应用启动 `subprocess.Popen` 跑 `python -m specimen_app.worms_crawler_daemon` 独立进程
- 跨平台 detach：
  - Linux/WSL/macOS：`start_new_session=True`
  - Windows：`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`
- 关闭应用 / 关闭对话框 → daemon 进程继续跑
- daemon 写 PID 到 `~/.specimen_inventory/worms_crawler.pid`，结束时自动清理
- daemon 共享同一 state 文件，断点续传机制无缝

### 检查后台进度

WoRMS 数据库管理对话框 → 「**检查后台下载状态**」按钮：
- 若 daemon 在跑 → 显示 PID + 已导入数 + 已访问节点 + 剩余队列
- 若 daemon 未跑但 state 文件存在 → 显示"上次抓取的进度"（可续传）

### 停止后台下载

WoRMS 数据库管理对话框 → 「**停止后台下载**」按钮：
- POSIX：发 SIGTERM
- Windows：调 `taskkill /PID <pid> /T`
- daemon 收到信号后优雅停（state 已自动保存）
- 下次启动勾选「在独立后台进程下载」+ 点抓取按钮 → 自动续传

### 注意事项

- **只允许一个 daemon 同时跑**（PID 文件检测）— 防止两个进程同时写同一 SQLite 缓存
- daemon 进程的 stdout/stderr 被重定向到 DEVNULL（不污染父进程）
- daemon 异常退出时也写 crash log 到 `~/.specimen_inventory/crash_*.log`（与主应用共享日志机制）
- 关闭笔记本盖子 / 系统休眠会暂停 daemon；唤醒后会继续（依赖 OS 调度）
- 强制重启 / 断电后 PID 文件会留下（pid 死了）→ 下次启动应用自动清理

---

## 4. 检查日志位置（一键打开）

应用配置目录：
- Linux/WSL/macOS：`~/.specimen_inventory/`
- Windows：`%APPDATA%\标本入库管理\`

包含：
- `crash_<时间戳>.log` — 崩溃日志
- `startup_diagnostics.log` — 启动诊断
- `last_exit_clean` — clean exit marker
- `worms_crawl_state.json` — WoRMS 抓取断点续传 state
- `worms_crawler.pid` — daemon PID lock
- `worms_cache.sqlite` — WoRMS 缓存
- `settings.json` — 应用设置

---

## 5. 常见故障对照

| 现象 | 原因 | 处理 |
|------|------|------|
| 主窗口关不掉，转圈 | 后台 worker 卡住 | 等 3-5 秒，会自动 `terminate()` 强杀 |
| 启动后弹"上次异常退出" | 上次 crash / kill / 断电 | 查看 `crash_*.log`；反馈给开发者 |
| 启动报"工作区已被占用" | lock 文件残留 | 等 10 分钟（stale 自愈）；或手动删 `数据/.workspace.lock` |
| WoRMS 抓取关应用就停 | 没勾「在独立后台进程下载」| 勾选后再次抓取 → 后台 daemon 不依赖应用 |
| 后台 daemon 没在跑但 state 有 | daemon 已结束但 state 未清 | 状态对话框显示"上次抓取的进度"；勾后台 + 抓取按钮自动续传 |
| 同时启动两个 daemon | PID 文件防护 | 第二个会立刻退出（EXIT_ALREADY_RUNNING）|
| Windows 任务管理器结束应用，lock 残留 | atexit 钩子未跑 | 10 分钟后 stale 自动释放 |
