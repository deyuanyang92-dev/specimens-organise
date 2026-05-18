# Release 验收清单

每次发版前对照此清单逐项核验。来源：`/root/.claude/plans/worms-help-swift-jellyfish.md` H 节。

## 启动与菜单

- [ ] 启动应用 → 菜单栏 = `工具 / 编号 / 视图 / 帮助`（无顶层 WoRMS）
- [ ] 工具菜单底部有 `WoRMS 分类匹配… / WoRMS 分类浏览… / WoRMS 本地数据库管理…`，且仅此三处
- [ ] 工具栏最右段 = `... 设置 / WoRMS / 自动保存`

## Help 菜单

- [ ] Help → 使用说明 → 弹窗加载 index.md，左侧树点 5 个不同章节，右侧切章正常
- [ ] Help → 使用说明 → 搜索框输 "WoRMS" 过滤命中
- [ ] Help → 使用说明 → 章节内图片路径（相对路径）能正确加载（TODO 占位显示 broken image 也算 OK）
- [ ] Help → 快捷键速查 → 显示完整快捷键表
- [ ] Help → 字段填写说明速查 → 表格展示全部字段（数据来自 xlsx）
- [ ] Help → 检查更新 → 触发 GitHub 请求
- [ ] Help → 打开崩溃日志目录 → 文件管理器打开崩溃目录
- [ ] Help → 关于 → 版本号 = `__version__`，作者/许可证占位可见，[复制系统信息] 可用

## 共享单实例

- [ ] 工具栏 WoRMS 按钮 + 右键菜单 `_open_worms_match_for_vouchers` 共享同一 `WormsMatchWindow` 单实例

## 打包

- [ ] 打包产物 `releases/v.../_internal/docs/manual/index.md` 存在
- [ ] WSL2 + Windows 10 物理机各跑 frozen exe，Help 各项不崩

## 回归

- [ ] 字段帮助 `?` 按钮仍弹原 QMessageBox（数据来自 xlsx）
- [ ] 原「打开合并示例」指向新路径 `docs/manual/import-merge.md`
- [ ] 增量更新：旧版升新版，`docs/manual/` 出现在应用分区，runtime_hash 未变
