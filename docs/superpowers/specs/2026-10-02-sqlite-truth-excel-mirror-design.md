# SQLite 真相源 + Excel 镜像（设计，2026-10-02）

- 状态：设计已与用户确认第 1 节；第 2–4 节由实现方按用户授权自行决策，均可否决。
- 用户（需求方，非软件工程师）原话：
  > 我当然希望做成 SQLite 库，但是要支持操作后自动更新生成 Excel，因为一些用户已经习惯了用 Excel 管理。
  > 操作过程中不要一直 Excel 写入，而是数据库管理，但结束或者保存可以自动写入 Excel，或者搞个按键支持自动写入 Excel 或导出 Excel。
  > 识别到旧的用旧的，识别到新的用新的。识别到旧版本自动改为 SQLite，不需要问。
  > 向下兼容、稳健、不要数据丢失。

## 0. 一句话

软件运行时只读写 `数据/标本数据.sqlite`；Excel 文件变成"按时机生成的镜像"（文件名、表头、行序和今天完全一样）。
老工作区第一次用新版打开时自动转换；没转过的工作区走今天的 xlsx 逻辑，一字不改。

## 1. 数据模型与两种模式（用户已确认）

- 库文件 `数据/标本数据.sqlite`，与现有 `操作记录.sqlite`、`summary_cache.sqlite` 并排。操作记录不动。
- 11 张表 = 9 本 xlsx 原样映射：标本信息、分类信息、照片信息、编号索引、修改明细、修改汇总（修改记录.xlsx 两个 sheet）、
  编号分发记录、数据版本记录；入库人员（persons_store）第 2 段并入，先留 xlsx。
- 列名 = 现在的中文表头，一字不改；值一律 TEXT（store 今天就把一切转成字符串写 xlsx），SQLite ↔ Excel 往返逐格相等，可自动比对。
- 每表隐藏列 `_seq INTEGER`（行序）；生成 Excel 按 `_seq`，行序与今天一致（撤销/显示依赖行序）。
- `meta` 表：schema 版本、每表变更计数、上次生成 Excel 的计数/时间。
- 模式识别：`数据/标本数据.sqlite` 存在 → 新模式；否则旧模式。新模式工作区 `data_schema_version` 1.1.3 → **1.2.0**
  （现有硬门 `_assert_supported_data_schema` 让旧版程序拒开并提示升级）；旧模式工作区版本号不动。
- 后端接口 6 个动作：读整表、整表替换、按键更新、追加、删除、取变更计数。`XlsxBackend` = 现有原语搬入；
  `SqliteBackend` = 每动作一个事务。store 的内存行缓存保留，失效改看变更计数。
- 线程：沿用 `_rw_lock` + `StoreWorkerThread`；SQLite 连接 `check_same_thread=False`，写入仍串行。

**实现要点（"换底不换壳"的具体含义）**：`ExcelStore.read_rows / _write_rows / _read_plain_rows / _write_plain_rows`
这四个方法名和签名不变，只是方法体改为调用 `self._backend`；全文 ~80 处调用点一行不改。
`_write_changes_and_summary`（两 sheet）、`_append_row_incremental`、`_update_index_fingerprint` 改为走后端动作。
`_record_action` 的 xlsx 兜底分支保留不动。

## 2. 自动转换、Excel 生成、界面

### 2.1 自动转换（旧 → 新）

触发：以**可写**方式打开一个只有 xlsx 的工作区时。只读副本、被别的进程锁住、磁盘只读 → 不转，照旧模式打开。

步骤（任何一步失败 → 删临时文件、留在旧模式、状态栏提示 + stderr 日志；**源 xlsx 从头到尾不被修改**）：
1. `create_data_snapshot("自动转换为 SQLite")`——现有快照机制，先留一份。
2. 用 `XlsxBackend` 读 9 本 xlsx 全部表。
3. 写到临时库 `数据/标本数据.sqlite.converting`。
4. 校验：从临时库逐表读回，与第 2 步的行逐格比对（表头、值、行序、行数）；任一不等 → 失败。
5. 临时库改名为 `标本数据.sqlite`（原子）；配置 JSON 原子写入 `storage_backend: "sqlite"`、`data_schema_version: "1.2.0"`。
6. `meta` 标记"全部表已导出"（此刻 Excel == 数据库）。

### 2.2 Excel 生成（`excel_mirror.py`，独立模块）

- `export_dirty(store)`：只重写"变更计数 > 上次导出计数"的表对应的 xlsx；每个文件沿用现有 tmp→replace + 写后 ZIP 校验；
  在 `StoreWorkerThread` 里执行。`export_all(store)` 用于降级/手动全量导出。
- 时机：
  1. 关闭软件（`closeEvent`：排空写队列 → 生成 → 关库；状态栏"正在生成 Excel…"）；
  2. 切换工作区（`_load_workspace_into_window`，同上）；
  3. 点「保存」/「全部保存」按钮（排空队列后生成）；「工具 → 导出 Excel」立即全量生成；
  4. 工具栏可勾选项「自动写入 Excel」（`settings.json: auto_excel_mirror`，默认**关**）：开着时，最后一次改动后空闲 5 秒自动生成；
  5. 新模式启动时若 `meta` 显示有未导出改动（上次崩溃）→ 后台补生成，Excel 自动追平。
- 状态栏常驻小字："Excel 已同步" / "Excel 落后 N 次改动"。
- Excel 是输出不是输入：用户在 Excel 里改了不回流，下次生成覆盖。「工具 → 用 Excel 打开数据文件…」的警告文案改为说明这一点。

### 2.3 回退（新 → 旧）

「工具 → 导出 Excel 并降级工作区…」（管理员密码，同现有 ADMIN_PASSWORD）：全量生成 Excel → 校验 → `标本数据.sqlite`
改名为 `标本数据.sqlite.bak-<时间戳>`（不删）→ 配置 JSON 版本号回 1.1.3、去掉 `storage_backend` → 重新以旧模式打开。

### 2.4 旧版程序

遇到 1.2.0 工作区：现有硬门弹"请升级到 0.11+；如需回退请在新版本里使用「导出 Excel 并降级工作区」"。未转换的工作区旧版照开。

## 3. 稳健性

- 单写者：现有 `.workspace.lock` + 心跳不变。
- 日志模式：本地盘 `journal_mode=WAL`；工作区在网络/挂载盘（复用 `startup_diag.detect_workspace_on_windows_mounted_filesystem`
  + UNC 路径判断）时用 `journal_mode=DELETE` + `synchronous=FULL`（SQLite 官方：WAL 在网络文件系统上不安全）。`busy_timeout` 10 s。
- 事务：一次 store 操作 = 一个 SQLite 事务。今天 `set_fields` 要分别写 3 本 xlsx（中途断电 = 三本不一致，靠 transaction.jsonl 补救）；
  新模式三张表一个事务，要么全成要么全不成。旧模式的 transaction.jsonl 机制保留。
- 快照：`create_data_snapshot` 增加拷贝 `.sqlite`（拷前 `PRAGMA wal_checkpoint(TRUNCATE)`）；快照恢复同样恢复 sqlite。
- 完整性：打开时 `PRAGMA quick_check`；坏了 → 拒绝打开并指向最近快照和最近一次 Excel 镜像（Excel 本身就是一份人类可读的完整备份，
  这是镜像设计附带的好处）。
- 撤销/重做、指纹、管内编号派生、导入合并：逻辑不变，底下换后端。
- 跨工作区读取（服务器同步预览、导入合并、任务包）：统一读取器"有 sqlite 读 sqlite，否则读 xlsx"。
- 已知残余风险（明说）：用户在 Excel 里改了数据以为软件会认——不会。界面与文档都写明。

## 4. 分段、测试与验收

| 段 | 内容 | 用户可见 | 发版 |
|---|---|---|---|
| 1 | 抽后端接口 + `XlsxBackend`，store 四个原语改为委托；`_write_changes_and_summary` 等改走后端 | 无 | v0.10.34 |
| 2 | `SqliteBackend`、自动转换、Excel 镜像 + 界面、版本号门、降级工具、跨工作区读取器 | 有（转换 + 新按钮） | v0.11.0 |
| 3 | 汇总缓存按变更计数失效、快照含 sqlite、网络盘日志模式、入库人员表并入 | 无 | v0.11.1 |

验收门（每段）：
- 第 1 段：全量测试绿；新增 `tests/test_table_backend.py`——`XlsxBackend` 写出的 xlsx 读回内容与旧写法逐格相等。
- 第 2 段：在自带「测试数据集」副本（74 条标本 / 169 张照片）上：转换 → 11 表逐格比对 → 全量导出 → 与原 xlsx 逐格比对；
  转换中途失败（模拟）不留半截且源 xlsx 未变；降级往返；旧版硬门（模拟 CURRENT=1.1.3 打开 1.2.0）；
  镜像四个时机（offscreen 真实窗口，同 P0-1 测法）；性能：1500 条标本 `set_fields` < 50 ms（旧模式 0.65–0.8 s）。
- 发版说明必须写：升级后首次打开会自动转换；所有机器先升级再打开共享工作区。
