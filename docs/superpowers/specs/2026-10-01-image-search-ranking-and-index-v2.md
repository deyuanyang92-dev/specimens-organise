# 图片检索：相关度排序 + 精准/模糊模式 + 索引 v2（2026-10-01）

## 用户报障（原话）

> 图片检索部分，输入编号并未按最相关的进行排序，核心编号过短，应为框内输入的字符，
> 按最相关进行排序，可设置精准匹配和模糊匹配模式；另外，新图片增加，建立索引太慢。
> 请在不破坏功能的前提下优化、修复。如果可能最好把这些软件模块化，比如构建索引单独一个模块，
> 目前我发现效率远远低于 Everything 这个软件。

截图：输入 `GDLZ-LZC-OWC`，结果里 `GDLZ-LZC-ONFC001-*` 排在 `GDLZ-LZC-OWC001-*` 前面，
每张卡片都显示「核心编号：gdlz-lzc」。

## 根因（已核实 file:line，旧代码）

1. **排序 / 核心编号过短**：`image_search.py` 旧 `ImageIndexStore._verify_entry`（及内存版
   `ImageSearchIndex._verify_positions`）规定：stem token 在 query token 之后"剩余全是数字 ⇒ 不匹配"。
   本意是 `WenSC004` 不能命中 `WenSC0042`，但用户敲站位码 `OWC` 时 `OWC001` 也被拒 ⇒ 三段全部失配
   ⇒ `search_entries` 逐段 pop 退化到 `gdlz-lzc` ⇒ 整个断面命中 ⇒ 按文件名自然排序（ONFC < OWC）
   ⇒ `matched_keywords=("gdlz-lzc",)`，分数却仍是 100。
2. **建索引慢**：每个文件除 `entries` 一行外，还把 stem 每个 token 的**所有前缀**写进 `tokens` 表，
   每行带完整路径；外加一个与主键重复的索引。本机实测 20 000 张：68.7 万行、541 MB、全量 43.7 s。
   另外 `os.walk` 之后对每个文件再单独 `stat()` 一次（Windows 上 `DirEntry.stat()` 本可免费拿到）。

## 设计

### 模块拆分（对外 API 不变）

| 模块 | 职责 | 依赖 |
|---|---|---|
| `specimen_app/image_index.py` | 目录扫描（`scan_image_files`，os.scandir）+ SQLite 索引 v2（`ImageIndexStore`）+ 作用域工具 | 无 Qt |
| `specimen_app/image_match.py` | `tokenize` / `token_match_level` / 分层打分 / `rank_entries` / 匹配模式 | 纯函数 |
| `specimen_app/image_search.py` | 门面：作用域解析、内存缓存、结果装配（关联、归档别名折叠）、全部旧 import 名 | 上两者 |

`ui.py` 与 `tests/test_core.py` 原有的 `from .image_search import …` 全部保留可用。

### token 规则（`token_match_level`）

| query token | stem token | 等级 | 说明 |
|---|---|---|---|
| `owc` | `owc` | 3 完全相等 | |
| `owc` | `owc001` | 2 编号延续 | 字母结尾 + 数字余量 —— 修复点 |
| `ow` | `owc001` | 1 普通前缀 | |
| `sc004` | `sc0042` | 0 | 旧防线保留：数字结尾的 query 不许再延长数字 |
| `sc004` | `sc004b` | 1 | |

### 相关度分层（分数越高越前，同分按文件名自然排序）

| 分数 | kind | 条件 | 模糊 | 精准 |
|---|---|---|---|---|
| 100 | exact | 从文件名开头起逐段完全相等 | ✓ | ✓ |
| 95 | prefix | 从开头起逐段命中，末段为编号延续（OWC→OWC001） | ✓ | ✓ |
| 93 | prefix | 从开头起逐段命中，含普通前缀 | ✓ | |
| 90 | inner | 逐段连续命中，但不在开头（图200-A-…） | ✓ | |
| 85 | any_order | 每个 query token 都能前缀命中某个 stem token | ✓ | |
| 60 | contains | 整个查询串作为子串出现，数字不得被"延长" | ✓ | |
| 30–55 | partial | 以上全无时逐段去掉末尾 token，取最长可命中前缀 | ✓ | |

- 真命中（≥60）时 `matched_keywords = (用户输入原文,)`，卡片显示「核心编号：GDLZ-LZC-OWC」。
- 只有 partial 才回报退化后的前缀；卡片显示「核心编号：GDLZ-LZC-OWC-99（仅匹配 GDLZ-LZC-OWC）」。
- `ImageSearchResult` 新增 `match_kind` 字段（默认 `""`，旧调用方不受影响）。
- 模式持久化：`settings.json` 的 `image_search_match_mode`（`fuzzy` 默认 / `exact`）；
  对话框「类型」右侧新增「匹配」下拉。

### 索引 v2

- 表：`meta`（schema_version=2, needs_vacuum）、`scopes`、`entries`（一行/文件）。**无 `tokens` 表**。
- 候选：SQL `instr(py_lower(stem), 查询第一段) > 0 AND suffix IN (…)` 流式取回，再 `rank_entries` 打分。
  后缀在 SQL 里过滤 —— 旧版先取 limit×3 条再过滤后缀，大量 JPG 会把 TIF 挤出候选（已加测试）。
- 旧库兼容：`_connect` 发现 `tokens` 表 ⇒ `DROP`（entries 原样保留，**不重扫**），标记 `needs_vacuum`，
  下次 `reconcile_scope`（工作线程）里 `VACUUM` 回收空间。
- `last_scan` 改记"扫描开始时刻 − 2 s"：修掉扫描期间新增文件被永久漏掉、以及目录 mtime 粗粒度时钟
  落后 `time.time()` 导致紧随其后的新增被增量扫跳过（v2 变快后测试实测复现）。
- 扫描器语义与旧 `iter_images` 一致（不跟目录软链、`max_depth`、排除目录、增量 mtime 门控、自然排序）。

## 实测（本机 WSL2，/tmp ext4，20 000 个合成文件）

| 指标 | 旧 v1 | 新 v2 |
|---|---|---|
| 全量建索引 | 43.65 s | 1.63 s |
| SQLite 大小 | 541 MB | 15 MB |
| 增量 reconcile（+50 张） | 0.89 s | 0.51 s |
| 无变化全量 reconcile | 1.39 s | 0.85 s |
| 检索 `GDLZ-LZC-OWC` | 0.14 s，首条 CK021，kw=gdlz-lzc | 0.17 s，首条 OWC001，kw=GDLZ-LZC-OWC |
| 检索 `OWC` | 0.21 s，score 60（子串兜底） | 0.08 s，score 90 |

与 Everything 的差距说明：Everything 直接读 NTFS MFT / USN 日志，用户态程序做不到；
v2 把用户态能做的（一次扫描只走一遍 scandir、一行/文件、增量目录门控）做到位，
首次建索引从"分钟级"降到"秒级"，之后新增照片走增量/直接 upsert。

## 测试

- 新增 `tests/test_image_search_ranking.py`（20 条）：token 规则、分层与模式、partial 回报、
  后缀预筛、索引 v2 结构、v1→v2 迁移不重扫、扫描器 stat/深度、增量 diff、设置往返。
- `tests/test_core.py` 原 17 条图片检索用例全部保持通过（语义未变的锚点：
  `WenSC004 ≠ WenSC0042`、`A-` 单字母、子串兜底 score=60、别名折叠、增量 reconcile）。
- 全量 `python -m unittest discover -s tests`：381 通过。
