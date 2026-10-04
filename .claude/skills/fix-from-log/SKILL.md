---
name: fix-from-log
description: Fix specimen-organise bugs from a user-supplied error log (crash_*.log, gui_stall_*.log, boot_fault.log, startup_failure_*.log, or pasted traceback / 闪退 / 未响应 / 打不开 / 报错). Triage version first, reproduce with a failing test, fix the root cause, sweep the same pattern, and prove no other feature broke (full isolated suite + Windows/Linux CI) before release.
---

# 按错误日志修复

完整流程与理由见 `docs/fix-from-log.md`。必须按顺序执行，不跳步：

1. **分诊**：把日志存成文件，运行 `python scripts/triage_log.py <文件>`。
   - 日志版本比当前代码旧 → 先读报告列出的后续提交，判断是否已修复；已修复就告诉用户升级到哪个版本，不重复修。
   - 卡死要 `gui_stall_*.log`；证据不足就向用户要，标注"未确证"，不猜。
2. **复现**：写失败测试（同一调用链、同样的异常 / 条件），运行确认红。
3. **修根因**：在正确的层修；最小改动；不改界面外观；旧逻辑 `# 旧：…` 注释保留。
4. **同类排查**：grep 同样模式，一并修并补测试。
5. **不破坏其他功能**：
   - 列出被改函数的调用方逐个确认；数据格式向后兼容。
   - 绝不为了变绿修改 / 删除已有测试（除非用户明确要求改变该行为，并在提交说明写明）。
   - `python scripts/run_tests.py --isolate` 全绿。
   - 推分支，`gh run watch` 等 CI Windows + Linux 全绿后才合并到 main。
6. **发布**：改 `specimen_app/__init__.py` 版本号 → 合并 main → 打 `v*` 标签推送 → 等 Release 工作流（含测试门禁）完成。
7. **汇报**（中文、简短）：根因 + 证据（file:line）、修了什么、新增哪些测试、CI 结果、版本号；未确证的单列。
