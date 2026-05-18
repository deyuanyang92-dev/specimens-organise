# DwC 导出 + EXIF 回填（A1/A2）

> v0.5.0+ 新增。专为对接国际生物多样性数据网络 + 减少人工录入采集元数据而设。

## 1. Darwin Core Archive 导出（A1）

### 用途

把工作区导出成 **TDWG/GBIF 通用格式**，可：
- 上传到 GBIF 节点 / iDigBio / VertNet 等数据聚合器
- 用 GBIF IPT / iDigBio Validator 校验数据质量
- 与国际同行交换标本数据
- 用 R/Python 生态库（rgbif、pygbif、dwca-reader）直接读

### 操作

1. 打开中心机工作区
2. 菜单 **工具 → 导出 Darwin Core Archive…**
3. 选保存位置（默认文件名带日期与计数）
4. 填可选元数据：
   - 数据集标题（写到 eml.xml，可空，默认 `Specimen Inventory Workspace Export`）
   - 数据集创建者（可空）
5. 完成 → 得到 zip，含：
   - `occurrence.txt` — 标本记录 + 分类（Tab 分隔，DwC 字段名为列头）
   - `multimedia.txt` — 照片元数据（Audubon Core 扩展）
   - `meta.xml` — archive 结构描述（DwC-A 规范要求）
   - `eml.xml` — 数据集元数据（EML 2.2.0）

### 字段映射

| 工作区字段 | DwC term |
|------------|----------|
| 入库编号* | occurrenceID + catalogNumber |
| 管内编号* | fieldNumber |
| 保存方式 | preparations |
| 采集日期 | eventDate |
| 采集地点缩写* | verbatimLocality |
| 信息录入人员 | recordedBy（注：A3 字段补全后会拆出独立"采集人"字段，更符合 DwC 语义）|
| 核对人员 | identifiedBy |
| 备注 | occurrenceRemarks |
| 种拉丁 | scientificName |
| 种名* | vernacularName |
| 属名 | genus |
| 科拉丁 | family |
| 目/纲/门 | order/class/phylum |
| 分类备注 | identificationRemarks |

照片 → Audubon Core Multimedia 扩展：identifier（文件名）/ accessURI（file:// URI）/ description / fileFormat（MIME）/ hashFunction（"SHA-256"）/ hashValue（SHA256 hex）。

### 数据质量校验

导出后可用：
- https://tools.gbif.org/dwca-validator/ — GBIF 官方在线校验器
- `pip install dwca-reader` — Python 库本地校验
- iDigBio 的 IDigBio Validator

发现问题在工作区修正后重新导出即可。

### 已知差距（A3 字段补全后改进）

当前导出**缺**这些重要 DwC 字段（数据库内无对应字段）：
- `decimalLatitude` / `decimalLongitude` — GPS 十进制经纬度
- `coordinateUncertaintyInMeters` — 坐标精度
- `samplingProtocol` — 采集方法（如 "拖网"、"浅潜捕捞"）
- `habitat` — 生境描述
- `associatedSequences` — GenBank/SRA 等关联序列
- 独立的"采集人"（DwC.recordedBy）字段（当前用"信息录入人员"代替，语义略偏）

A3 字段补全（涉及 schema 改动）将弥补这些。

---

## 2. EXIF 自动回填采集日期（A2）

### 用途

野外标本拍照时，相机自动记录拍摄时间到 EXIF DateTimeOriginal 字段；
此工具一键把照片 EXIF 的拍摄日期回填到 voucher 的"采集日期"，**只填空字段不覆盖**。

### 操作

1. 打开工作区
2. 菜单 **工具 → 从 EXIF 批量回填采集日期…**
3. 应用扫"有照片但采集日期为空"的 voucher
4. 弹窗确认 → 点是
5. 自动从每个 voucher 第一张照片读 EXIF，填回采集日期
6. 完成弹消息：已回填 / 无 EXIF / 无法访问 三个计数

### 不会做的事

- **不会**覆盖已有的采集日期（用户手填的值始终被尊重）
- **不会**修改照片文件本身
- **不会**触发其它字段自动推导

如果要强制覆盖已有日期，请直接修改 voucher 字段；A2 本身设计为"补缺"。

### EXIF 抽取支持的字段

`extract_exif(path)` 还能抽（但当前 UI 只回填采集日期）：
- `event_date` / `event_time` — DateTimeOriginal
- `latitude` / `longitude` — GPS 十进制度（自动处理 N/S/E/W）
- `altitude` — 海拔（米）
- `camera_make` / `camera_model` — 设备信息

待 A3 字段补全（新增 decimalLatitude / decimalLongitude / coordinateUncertaintyInMeters 列）后，UI 可同时回填 GPS 信息。

### 容错

- 照片不是 JPEG/TIFF / 无 EXIF / 损坏 → 跳过该 voucher
- PIL 不可用 → 返回空 dict 不抛
- DateTimeOriginal 格式异常 → 跳过

---

## 3. 推荐工作流

**新工作区一开始就用**：
1. 录入员录入照片时（M1-M5 流程）→ 照片归档完毕
2. 主管聚合后 → 工具 → 从 EXIF 批量回填采集日期 → 大量 voucher 的采集日期自动补完
3. 待发表数据时 → 工具 → 导出 Darwin Core Archive → 上传 GBIF

**已有大量旧数据**：
1. 工具 → 升级工作区到多人协作格式（M5）— 一次性
2. 工具 → 从 EXIF 批量回填采集日期 — 一次性补完历史空白
3. 工具 → 导出 Darwin Core Archive — 一次性发布
