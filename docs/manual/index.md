# 标本入库管理 — 用户手册

本手册对应软件版本 **v{{version}}**。

> 应用内入口：**菜单栏 → 帮助 → 使用说明**
> 也可用文本编辑器或 Markdown 阅读器直接打开 `docs/manual/` 内的 `.md` 文件。

---

## 章节导航

### 开始
- [开始使用](getting-started.md) — 首次启动 / 选择工作区 / 基本概念
- [工作区与数据](workspace.md) — 工作区结构 / 数据文件 / 文件锁

### 录入流程
- [入库编号](voucher-numbers.md) — YZZ/BMNH/AMNH 格式 / 自动派生规则
- [编号系列管理](accession-series.md) — 多机构系列 / 批量预留
- [数据录入](data-entry.md) — 字段填写 / 沿用上条 / 字段帮助
- [照片管理](photos.md) — 关联 / 检索 / 批量 / 取消关联
- [分类信息与 WoRMS](classification.md) — 物种 / 科匹配 / WoRMS 集成

### 汇总与导出
- [入库汇总](ingest-summary.md) — 宽表 / 筛选 / 跳转
- [批量导出](batch-export.md) — Batch Entrez 风格
- [Darwin Core 导出](dwc-export.md) — DwC-A zip 包
- [EXIF 回填](exif-backfill.md) — 从照片元数据回填采集日期

### 高阶
- [WoRMS 物种分类](worms.md) — 离线缓存 / 后台抓取 / 数据库管理
- [数据版本快照](version-snapshots.md) — 创建 / 还原 / 兼容降级
- [多人协作](multi-user.md) — 任务包 / 收件箱聚合
- [合并 / 导入示例](import-merge.md) — 4 种合并场景
- [软件更新](update.md) — GitHub 增量更新

### 参考
- [故障排查](troubleshooting.md) — 崩溃 / 关闭挂起
- [Linux 安装](install-linux.md)
- [快捷键速查](shortcuts.md)
- [关于](about.md)

---

## 关于本手册

- 本手册随软件分发，应用内 **帮助 → 使用说明** 即可阅读。
- 各章节正在迭代中。如发现错漏，请通过 **帮助 → 关于 → 复制系统信息** 收集环境后反馈。
- 截图大多为 `TODO_screenshots/` 占位，正在分批补齐。
