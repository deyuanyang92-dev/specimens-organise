# P0-1：字段保存后台化（不改文件格式、不丢数据）（2026-10-02）

## 用户要求（原话）

> 先做做 P0 吧，但由于我是数据库，长期管理，因此请支持向下兼容，然后确定稳健，不要数据丢失。

## 问题（实测，1500 条标本工作区）

一次字段保存 = 在 **GUI 线程**同步完成 4 步，合计 0.65–0.80 s，打字即卡：

| 步骤 | 文件 | 耗时 |
|---|---|---|
| `_write_rows` 整本重写 | 标本信息.xlsx | 0.11–0.16 s |
| `_write_changes_and_summary` load+append+save | 修改记录.xlsx（随历史无限增长） | 0.23–0.31 s |
| `_update_index_fingerprint` 整本重写 | 编号索引.xlsx | 0.07–0.12 s |
| `_record_action` | 操作记录.sqlite（Tier B 已是 SQLite） | ~1 ms |

同格式更快写法无效：openpyxl `write_only` / 有无 lxml 四种组合 0.126–0.145 s，差异 ≤ 15 %，放弃。

顺带发现的三处丢数据路径（与性能无关，但违反"不丢数据"）：

1. `StoreWorkerThread.run` 用 `while self._running` 循环，`request_stop()` 后处理完当前一个就退出，
   sentinel 之前已排队的操作**全部丢弃**（红测：排 5 个后立刻 stop → 跑了 0 个）。
2. `select_voucher` 不 flush 待保存字段；500 ms 防抖未到就点下一个编号 → `_save_pending_group`
   发现 voucher 已变直接 `return 0` → 那次编辑静默丢失。
3. `_load_workspace_into_window`（切换工作区）直接 `store.close()`，不 flush、不等后台线程。
   `closeEvent` 则是**先停** store 线程、**后** flush —— 字段保存一旦后台化就会排进死队列。

## 方案（P0-1，本次落地）

- **格式零改动**：xlsx / JSON / sqlite 文件、字段、原子 tmp→replace、写后 ZIP 校验全部照旧。
  老版本软件打开新版本写过的工作区无任何差别。
- `specimen` / `classification` 面板的字段保存改走已有的 `StoreWorkerThread`（Tier A）：
  - GUI 线程只读控件值 → `_queued_field_saves[voucher:category]`（加锁）→ 入队；
  - 同组再次编辑时合并到尚未执行的任务（1 次 `set_fields`，1 条 action-log，undo 一步还原）；
  - 任务执行时 `store` 在入队时已绑定，切换工作区不会写错库；
  - worker 不可用（已 stop / 未启动 / 拒收）→ 回退旧的同步 `_save_text_fields`，绝不排进死队列；
  - 完成回调回填派生字段（采集日期/采集地缩写/保存方式），用户写盘期间手改过的字段不覆盖；
    失败回调回滚控件 + 弹错。
- `StoreWorkerThread`：运行循环只认 sentinel（排队操作必然执行完）；新增 `wait_idle(timeout)` /
  `is_idle()` / `pending_count()` / `accepting()`；`enqueue()` 在 stop 之后返回 False。
- 排空点：`closeEvent`（flush → `_drain_store_worker(120 s)` → stop → close store）、
  `_load_workspace_into_window`（flush → drain，超时则中止切换并提示）、`select_voucher`（切换前 flush）。
- 「保存」按钮文案："已保存 (N 项)" → "已提交保存 (N 项，后台写入中)"。

未动：照片面板字段保存（`_save_photo_fields_batched`）与其它类目仍同步；频率低，下一轮再评估。

## 实测（1500 条标本，offscreen 真实 SpecimenWindow）

| 指标 | 旧 | 新 |
|---|---|---|
| 每次保存 GUI 线程阻塞 | 0.63–0.73 s | 0.1 ms |
| 后台落盘耗时 | — | 0.70–0.91 s |
| `request_stop` 后排队的 5 个操作 | 执行 0 个 | 执行 5 个 |

## 测试

`tests/test_store_worker_saves.py`（9 条）：
worker 排空/拒收/超时/异常不断链；真实窗口（offscreen）字段保存在非 GUI 线程执行且落盘、
worker 忙时两组编辑合并成一次 `set_fields`、关窗排空最后一笔、切换编号不丢待保存编辑、
worker 不可用时同步兜底。`tests/test_core.py` 原桩测试改为把异步入口转到同步记录（语义不变）。
全量 390 通过。

## 下一步（P0-2，待确认）

修改记录.xlsx 的 load+append+save 随历史线性增长（长期库最终会到秒级）。
可沿用本仓库已有的 Tier B 模式（`action_log_db.py`：SQLite 为真相 + xlsx 兜底/导出），
把修改记录也迁到 SQLite 并按需导出 xlsx。涉及文件布局，需单独确认兼容策略。
