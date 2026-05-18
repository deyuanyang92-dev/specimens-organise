# 关于

**标本入库管理** v{{version}}

PyQt5 桌面端 · 生物标本 Excel 数据管理工具。

## 设计目标

- 标本数据 100% 存放在普通 Excel 文件，工作区文件夹可整体拷贝 / 备份。
- 兼容性优先 —— 旧工作区永远能被新版软件打开，重要升级提供降级途径。
- 字段说明 / 物种预设 / 帮助文档 **随软件分发**，离线可用。
- 多人协作通过任务包 / 收件箱聚合实现，零中心服务器。

## 致谢

- [PyQt5](https://www.qt.io/qt-for-python) — GUI 框架
- [openpyxl](https://openpyxl.readthedocs.io/) — Excel 读写
- [Pillow](https://python-pillow.org/) — 图像处理
- [tifffile](https://github.com/cgohlke/tifffile) — TIFF 解码
- [markdown](https://python-markdown.github.io/) — 文档渲染
- [WoRMS REST API](https://www.marinespecies.org/aphia.php?p=webservice) — 海洋物种分类
- [Darwin Core (TDWG)](https://dwc.tdwg.org/) — 生物多样性数据标准

## 反馈

应用内 **菜单栏 → 帮助 → 关于 → 复制系统信息** 一键收集环境信息，贴到 Issue / 邮件即可。
