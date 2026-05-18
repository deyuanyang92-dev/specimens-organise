# 入库编号管理设计文档

本文档记录入库编号（凭证号）的设计决策、业务规则和操作流程，供后续开发和维护参考。

---

## 1. 编号格式

### YZZ 系列（机构自有）

格式：`YZZ000001`（前缀 `YZZ` + 6 位零填充流水号，无分隔符）

- 定义位置：`specimen_app/parsing.py`，`VOUCHER_RE` / `format_voucher()` / `parse_voucher_serial()`
- **永久冻结**：这三个函数/正则绝不修改，以保证历史数据和导出文件的兼容性
- 流水号从 1 起，无上限；`_max_existing_serial()` 扫描现有数据取最大值

### 非 YZZ 系列（AccessionSeries）

由 `AccessionSeries` 数据类配置，支持：
- 自定义前缀（如 `BMNH`、`AMNH`）
- 位数（4–10）
- 分隔符（`.` / `-` / 无）
- 年份位置（前缀后 / 流水号前 / 无）
- 起始计数器、步长

多系列可共存，通过工作区配置 `active_series_name` 指定当前活动系列。

---

## 2. 编号生成规则

### 正常录入（new_specimen）

1. 调用 `store.next_voucher()` 获取下一个可用编号
2. YZZ：`max(_max_existing_serial() + 1, reserved_through_serial + 1)`
   - `reserved_through_serial`：批量预留操作设置的上界（默认 0）
   - 保证录入编号不与已预留编号冲突
3. 非 YZZ：从 `series.next_counter` 开始，+step；写入 config
4. 创建标本记录，写入编号索引（含 UUID、创建时间、指纹）

### 批量预留（batch_reserve_vouchers）

- 不创建标本记录，仅预留编号段
- YZZ：从 `max(existing+1, reserved+1)` 开始，预留 n 个，更新 `reserved_through_serial`
- 非 YZZ：从 `series.next_counter` 开始，步进 n 个，更新 `next_counter`
- 调用后必须写入 `编号分发记录.xlsx`（批量领取类型）

---

## 3. 分发审计日志

文件：`数据/编号分发记录.xlsx`

### 记录类型

| 类型 | 触发时机 | 必填字段 |
|------|----------|----------|
| 批量领取 | `BatchGenerateDialog` 确认 | 人员、用途、编号起始/结束/数量 |
| 任务开始 | `_start_task()` | 人员、用途 |
| 任务结束 | `_end_task()` / closeEvent | 关联任务ID、数量（录入量） |

### 完整列定义

```
记录ID      12 位 hex（uuid4 片段）
时间        ISO 8601，精确到秒
类型        批量领取 / 任务开始 / 任务结束
人员        领取人 or 录入人
用途        标签打印 / 入库 / 整理 / 核查 / 其他
备注        可选
编号系列    YZZ 等（批量领取时填写）
编号起始    批量领取时填写
编号结束    批量领取时填写
数量        批量领取数量 / 任务结束时的录入量
关联任务ID  任务结束时 = 对应任务开始的记录ID
```

---

## 4. 录入任务门控

### 业务规则

- 主窗口「新增入库编号」按钮默认 disabled
- 必须先「开始录入任务」才能启用该按钮（硬门控）
- 任务进行中：左侧面板顶部显示绿色状态栏（人员 · 用途 · N条）
- 每次创建新标本，`_active_task["新增数量"] += 1`
- 「结束任务」或窗口关闭时自动写入任务结束记录

### 设计决策

- **录入人 ≠ 领取人**：两者完全独立，不互相关联
- **不自动填充信息录入人员字段**：录入人由用户在标本表单里手动填写；任务人员仅用于工作量统计
- **closeEvent 自动结束任务**：防止未结束任务导致时长统计异常

---

## 5. 工作量统计

### 数据配对逻辑

```python
starts = {r["记录ID"]: r for r in rows if r["类型"] == "任务开始"}
ends   = [r for r in rows if r["类型"] == "任务结束"]
# 每条结束记录的 关联任务ID 指向对应开始记录的 记录ID
# 时长 = 结束时间 - 开始时间（秒）
# 录入量 = 结束记录的 数量 字段
```

### 汇总维度

| 维度 | 字段 |
|------|------|
| 录入人 | 任务次数、录入标本数、累计时长 |
| 明细 | 开始时间、录入人、用途、录入量、时长 |

---

## 6. 向后兼容

| 情况 | 处理 |
|------|------|
| 老工作区无 `编号分发记录.xlsx` | `_ensure_alloc_log()` 自动创建 |
| 老工作区配置无 `reserved_through_serial` | `config.get("reserved_through_serial", 0)` 默认 0 |
| 老工作区配置无 `active_series_name` | `config.get("active_series_name", "YZZ")` 默认 YZZ |

---

## 7. 文件位置速查

| 功能 | 文件 | 位置 |
|------|------|------|
| 编号格式/解析 | `parsing.py` | `VOUCHER_RE`, `format_voucher`, `parse_voucher_serial` |
| 非 YZZ 系列配置 | `accession_series.py` | `AccessionSeries` |
| 编号生成/预留 | `excel_store.py` | `next_voucher()`, `batch_reserve_vouchers()` |
| 分发日志读写 | `excel_store.py` | `log_alloc_event()`, `read_alloc_log()` |
| 任务门控 UI | `ui.py` | `_start_task()`, `_end_task()`, `_update_task_indicator()` |
| 批量生成对话框 | `ui.py` | `BatchGenerateDialog` |
| 工作量报告 | `ui.py` | `WorkloadReportDialog` |
| 常量定义 | `models.py` | `ALLOC_LOG_FILE`, `ALLOC_LOG_HEADERS` |
