# 截图采集规范

用户手册的截图随软件分发，对外可见。**不得**包含敏感数据。

## 不允许出现

- 真实人员姓名 / 工号（采集人 / 录入员 / 核对人字段）
- 真实采集地点详细坐标（缩写如 `LSD` 可保留 — 仅 3 字母码）
- 真实管内编号系列（凭证号请用 `YZZ000001` 这类示例段）
- 用户个人电脑的绝对路径（`/home/真名/...`、`C:\Users\真名\...`）
- 任何身份证 / 电话 / 邮箱

## 推荐做法

1. **专用演示工作区**：在 `~/Documents/specimen-demo-workspace/` 建一份纯演示数据。
2. 演示数据中的人员字段使用 `张三` / `李四` / `王五` 等通用占位。
3. 演示数据中的标本来源 / 物种使用公开发表的模式标本信息。
4. 截图前隐藏个人浏览器书签栏、桌面壁纸（窗口截图 vs 整屏截图差异巨大）。

## 拍摄工具

- **Linux**：`spectacle --region` / `gnome-screenshot --window`
- **Windows**：`Win + Shift + S` 区域截图
- **Win + Shift + S** 后用 mspaint / paint.net 标注红框

## 命名与放置

- 路径：`docs/manual/TODO_screenshots/<feature>-<step>.png`
- 命名：小写连字符，描述功能 + 步骤序号
- 引用：Markdown 内 `![描述文字](TODO_screenshots/voucher-create-step1.png)`（相对路径）

## 工作流

补完一张截图：
1. 删除 `TODO_screenshots/INDEX.md` 表里对应行的「TODO」标记，改为「✓」+ 日期
2. 把截图提交到仓库（同一个 commit 含 .png 与 INDEX.md 修改）
3. 校验：本地 `python -c "from specimen_app.help_dialog import manual_root; print(manual_root())"` 找到目录后启动 Help → 使用说明，确认图能加载

## 已知限制

QTextBrowser 不支持现代 CSS，对图片尺寸 / 圆角 / 阴影的展示能力有限。截图直接用纯 PNG，不加滤镜 / 阴影 / 文字水印。如需标注，用红色矩形或圆框直接画在原图上。
