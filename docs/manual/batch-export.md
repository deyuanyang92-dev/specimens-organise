# 批量导出

工具栏 → **批量导出**。仿 NCBI Batch Entrez 风格 —— 粘贴入库编号列表批量提取。

![批量导出](TODO_screenshots/batch-export.png)

## 输入

- **凭证号粘贴框**：每行一个，支持中英文逗号 / 分号 / 空格分隔。
- **来源列表**：可改用右键菜单 **批量导出选中** 直接传入。

## 输出选项

- **Excel 工作簿**：多 sheet（标本 / 分类 / 照片清单）
- **照片文件拷贝**：照片导出格式可选
  - 保持原格式（`shutil.copy2`，最快、无重编码）
  - JPG / PNG / TIFF 重编码（Pillow，可设 JPEG 质量、最大边长缩放）
- **打包 ZIP**：勾选后输出单一 zip

## 仅导出照片模式

入库汇总右键 → **导出选中照片** 时进入 `photo_focus=True` 模式：
- 仅勾照片文件
- 隐藏标本信息 / 分类信息 / 照片路径清单等勾选项
- 对话框标题变为「导出选中照片」

## 与 Darwin Core 导出的区别

- **批量导出**：自由格式，给同事 / 上游交付。
- **Darwin Core 导出**：TDWG/GBIF 标准格式，对接 GBIF / iDigBio。见 [Darwin Core 导出](dwc-export.md)。
