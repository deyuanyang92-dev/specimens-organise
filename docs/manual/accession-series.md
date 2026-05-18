# 标本入库管理 — 功能更新记录（v0.5.x 开发会话）

本文档记录该开发会话中对 `specimen_app/` 所做的全部新功能、优化与修复，供维护者存档查阅。

---

## 1. 多系列入库编号支持

### 功能描述
原系统仅支持机构自有的 `YZZ` 格式（`YZZ000001`，无分隔符，6 位流水号）。
本次新增 `AccessionSeries` 数据模型，支持在同一工作区内并存多种机构编号格式，
兼容大英自然历史博物馆（BMNH）、美国自然历史博物馆（AMNH）、中科院（IZCAS）、
史密森学会（USNM）等国际规范，同时保持 YZZ 原有做法不变。

### 核心设计决策
- **YZZ 不变**：`parsing.py` 中的 `VOUCHER_RE` 和 `format_voucher()` 完全不动，
  YZZ 编号继续由 `parse_voucher_serial()` 管理。
- **非 YZZ 系列**：通过 `AccessionSeries` 数据类描述（前缀、位数、分隔符、年份位置、
  流水号起点、步长），配置持久化到 `工作区配置.json` 的 `accession_series` 数组。
- **向后兼容**：老工作区打开时，`DEFAULT_CONFIG` 的 `active_series_name`（默认 `"YZZ"`）
  和 `accession_series`（默认 `[]`）自动补入，无需任何迁移。
- **混合排序**：`_voucher_sort_key()` 让 YZZ 编号按流水号升序排在最前，
  其他系列按字母序追加。
- **Darwin Core 兼容**：非 YZZ 系列接受任意字符串格式，兼容真实馆藏编号可能不
  符合规范的历史情况。

### 关键文件
| 文件 | 改动 |
|------|------|
| `specimen_app/accession_series.py` | 新建：`AccessionSeries` 数据类、`format_series_number()`、`series_prefix_of()`、`BUILTIN_PRESETS` |
| `specimen_app/excel_store.py` | 新增 `active_series_name`/`accession_series` 配置键；`next_voucher()` 分支；8 个系列管理方法；`_voucher_sort_key()` |
| `specimen_app/ui.py` | 左侧面板新增系列选择器下拉 + 管理按钮；`AccessionSeriesDialog`；`_SeriesEditDialog` |

---

## 2. 菜单栏「编号」入口 + 分发统计

### 功能描述
- 菜单栏新增顶层「**编号**」菜单（位于「工具」与「WoRMS」之间）：
  - **系列管理…**：打开 `AccessionSeriesDialog`
  - **切换活动系列**：动态子菜单，每次展开时重建，当前活动系列打勾
- 「工具」菜单顶部也保留「入库编号系列管理…」快捷入口
- `AccessionSeriesDialog` 升级为 5 列表格（名称 | 示例编号 | **已分发** | 下一号 | 步长）
- YZZ 作为第 0 行固定显示（灰色背景，不可删除/编辑）
- 选中 YZZ 行时删除/编辑按钮自动禁用

### 关键方法
- `ExcelStore.count_vouchers_by_series(series_name)` — 从标本数据直接计数，
  YZZ 用 `parse_voucher_serial()` 匹配，其他系列用 `series_prefix_of()` 匹配
- `SpecimenWindow._populate_series_switch_menu()` — 菜单 `aboutToShow` 信号触发动态重建
- `SpecimenWindow._switch_active_series(name)` — 切换并同步左侧下拉框

---

## 3. 崩溃安全写入（断电/异常退出不丢数据）

### 问题
原 `_save_config()` 直接写 JSON，`_ensure_workbook()` / `_ensure_change_log()` 直接
写新建 xlsx，均无原子保护。崩溃/断电时可能留下截断文件，导致下次打开工作区失败。

### 修复方案
所有文件写入统一采用「**临时文件 + `os.replace()` 原子替换**」模式：
```
写到 file.{pid}.tmp  →  os.replace(tmp, target)
```

| 修复点 | 文件:行 | 改动 |
|--------|---------|------|
| `_save_config()` | `excel_store.py` | 写 `.{pid}.tmp` 再替换；异常时清理 tmp |
| `_ensure_workbook()` 新建分支 | `excel_store.py` | 同上 |
| `_ensure_change_log()` 新建分支 | `excel_store.py` | 同上 |
| `ensure_files()` 启动清理 | `excel_store.py` | glob `*.tmp` 删除上次崩溃遗留文件 |
| `closeEvent` 刷写防抖 | `ui.py` | 关窗前调 `_flush_pending_saves()`，确保 500ms 防抖未触发的最后一次编辑落盘 |

已有的 `_write_plain_rows()`（主数据写入）和照片归档写入原本已使用原子替换，本次改动补齐了之前遗漏的几处。

---

## 4. 内存与关闭优化

### `_photo_view_states` 无上限增长
长时间浏览后字典持续累积（每条记录一个 key）。修复：超 500 条时淘汰前 250 条旧条目。

### `_list_refresh_timer` 关闭时序
`closeEvent` 未在关闭 store 前停止定时器，可能触发回调访问已释放对象。修复：
`closeEvent` 第一步先 `timer.stop()`，再处理线程清理和 store 关闭。

### WSL 退出兼容（前序会话修复）
- `ThumbnailWorker.stop()`：线程池 `shutdown(wait=False)` 避免阻塞
- SIGINT/SIGTERM 信号处理 + 500ms keepalive QTimer（Qt 需要周期性回到 Python 解释器才能响应信号）
- `UpdateCheckWorker` / `ImportThread` 在 `closeEvent` 中 `requestInterruption()` + `wait(2000)`

---

## 5. 入库汇总全列检索

### 功能描述
`IngestSummaryDialog` 搜索栏的范围下拉从 4 个硬编码选项（全部/入库编号/管内编号/照片名）
扩展为「全部」+ 全部 `SUMMARY_COLUMNS` 列名（含入库日期、存放位置、核对人员、种名*、科* 等）。

选「保存方式」→ 输入「9E」→ 仅显示该字段含 `9E` 的行；选「全部」→ 对所有列做子串匹配（OR）。

### 实现
```python
# _apply_filters() 搜索分支（简化）
if scope == "全部":
    ok = any(query in self._summary_cell_text(record.get(col, "")).lower()
             for col in SUMMARY_COLUMNS)
else:
    ok = query in self._summary_cell_text(record.get(scope, "")).lower()
```

`_summary_cell_text()` 统一处理标量值和列表值（照片聚合列），无需按列类型特判。

---

## 设计原则（跨功能）

1. **数据向后兼容强制**：任何改动不得破坏现有工作区数据（xlsx / JSON / 照片路径 / 指纹）
2. **原子写入**：所有写文件操作走 temp+`os.replace()` 路径
3. **YZZ 冻结**：`parsing.py` 的 `VOUCHER_RE` / `format_voucher` / `parse_voucher_serial` 永不改动
4. **旧逻辑注释**：改动时保留 `# 旧：...` 注释，记录原行为和兼容决策

---

## 6. 编号分发管理（批量预留 + 录入任务门控 + 工作量统计）

### 背景与目标

入库编号具有唯一性和机构权威性，必须追踪每次分发与使用。
以往系统允许任何人随意点击「新增入库编号」，无操作记录、无人员追踪。

新增三项能力：
1. **批量预留编号**：一次性预留 N 个连续编号用于打印外贴标签，写入审计日志，导出 xlsx/csv
2. **录入任务门控**：必须「开始录入任务」才能新增标本，强制记录录入人、时间、用途
3. **工作量统计**：记录每位录入人员的工作时长与录入量，用于致谢与薪酬核算

### 术语定界

| 术语 | 定义 |
|------|------|
| **领取人** | 领走编号段去打标签的责任人（研究员/采集者），与录入人无关 |
| **录入人 / 信息录入人员** | 坐在电脑前录数据的人，在「开始录入任务」时填写 |
| **批量领取** | 仅预留编号段用于打印标签；不创建标本记录；可多次领取 |
| **录入任务** | 一次有头有尾的数据录入会话；记录录入人、开始/结束时间、录入量 |
| **分发日志** | `编号分发记录.xlsx`，记录批量领取和录入任务的完整审计流水 |

**两个功能完全独立**：批量领取 ≠ 开始录入任务；领取人 ≠ 录入人；两者的记录不关联。

### 数据结构

#### `编号分发记录.xlsx`（`ALLOC_LOG_FILE`）

| 列名 | 说明 |
|------|------|
| 记录ID | 12 位 hex UUID 片段 |
| 时间 | ISO 8601，精确到秒 |
| 类型 | `批量领取` / `任务开始` / `任务结束` |
| 人员 | 领取人（批量领取）或录入人（任务） |
| 用途 | 标签打印 / 入库 / 整理 / 核查 / 其他 |
| 备注 | 可选 |
| 编号系列 | YZZ 等 |
| 编号起始 | 批量领取时填写 |
| 编号结束 | 批量领取时填写 |
| 数量 | 批量领取数量 / 任务结束时的录入量 |
| 关联任务ID | 任务结束时指向对应任务开始的记录ID |

老工作区不存在该文件时，`_ensure_alloc_log()` 自动创建（无感升级）。

#### `工作区配置.json` 新增键

| 键 | 类型 | 说明 |
|----|------|------|
| `reserved_through_serial` | int | YZZ 系列已预留到的流水号（默认 0 = 无预留） |

`next_voucher()` for YZZ 计算：`max(_max_existing_serial() + 1, reserved_through_serial + 1)`，
确保批量预留不会与真实录入编号冲突。

### 实现文件

| 文件 | 改动 |
|------|------|
| `models.py` | 新增 `ALLOC_LOG_FILE`、`ALLOC_LOG_HEADERS` |
| `excel_store.py` | `_ensure_alloc_log()`、`batch_reserve_vouchers()`、`log_alloc_event()`、`read_alloc_log()` |
| `ui.py` | 任务指示器 UI、`_start_task()`/`_end_task()`/`_update_task_indicator()`、`_StartTaskDialog`、`BatchGenerateDialog`、`WorkloadReportDialog` |

### UI 交互

**录入任务门控（左侧面板顶部）**

```
无活动任务：
  [ ▶ 开始录入任务 ]
  [ ＋新增入库编号 ]  ← disabled，tooltip="请先开始录入任务"

任务进行中（绿色背景）：
  ● 张三 · 入库 · 3条      [ 结束任务 ]
  [ ＋新增入库编号 ]         ← enabled
```

每次点击「新增入库编号」后，任务的「新增数量」自动 +1；结束任务时写入日志。

**批量生成编号**：菜单「编号 → 批量生成编号…」→ `BatchGenerateDialog`
- 填写数量（1–9999）、领取人（必填）、用途、备注
- 调用 `store.batch_reserve_vouchers(n)` 预留编号段，写入分发日志
- 弹出保存对话框导出 xlsx（同时自动导出同名 csv）

**录入工作量报告**：菜单「工具 → 录入工作量报告…」→ `WorkloadReportDialog`
- 汇总视图：按录入人聚合（任务次数、录入标本数、累计时长）
- 明细视图：每条任务一行（开始时间、录入人、用途、录入量、时长）
- 支持按人员筛选、按日期范围筛选
- 导出 Excel（两个 Sheet：汇总 + 明细）

