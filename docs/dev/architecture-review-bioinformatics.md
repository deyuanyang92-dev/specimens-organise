# 多人协作设计 — 生物信息专家评价（M1-M5）

> 评价对象：v0.5.0+ M1-M5 多人协作功能（收件箱聚合 / 前缀分人 / 任务包 / 段守护 / 旧版升级）。
> 评价视角：与 Specify / Arctos / Symbiota / EMu / GBIF DwC-A 对照，按 FAIR 数据原则与
> GBIF Best Practices for Publishing Biodiversity Data 框架。

## 1. 整体定位

```
重 ←──────────────────────────────────────────────────────→ 轻
Specify7      Arctos       Symbiota     GBIF IPT      本方案       Excel + 人肉合并
(中心 DB)    (云 Postgres) (Web 门户)   (DwC-A 异步)   (文件+manifest) (无机制)
                                            ↑                ↑
                                    本方案借鉴最多 ──┘
```

本方案 = **DwC-A 异步 harvest 协议**（子目录 + manifest）+ **Arctos 前缀分人模式**
（多机构 / 多录入员自治）+ **Symbiota 合并后审核**（不阻塞聚合），三者综合下沉到
Excel 文件层。与 Specify Workbench 的"中心实时分配编号 + 离线申请"反例划清界线 ——
避免网络往返延迟与该方案的已知 bug。

## 2. 优点（与生物信息领域最佳实践对照）

| 设计要点 | 是否对齐领域共识 | 出处 |
|----------|------------------|------|
| 内容寻址（照片 SHA256 去重 + 标本记录指纹）| ✅ 与 IPFS/Git/Data-Lake 通用做法一致 | GBIF Identifying potentially related records |
| 不可变审计日志（修改记录 + 操作记录 + 编号分发记录 + 数据版本快照 四路冗余）| ✅ 符合 FAIR 数据原则 "Reusable" + 标本生命周期可回溯 | TDWG Audubon Core, ABCD |
| 离线优先（offline-first），异步合并 | ✅ 野外采样回所主流工作流 | GBIF DwC-A harvest 模型 |
| 前缀分人（assignee → 独立系列）| ✅ Arctos GUID 前缀的本地化版本 | Arctos handbook GUID Best Practice |
| 不引入中心 DB；纯文件协议 | ✅ 小团队场景；与 Symbiota 2 离线化方向一致 | Symbiota2 paper |
| 合并前强制 snapshot + 一键回退 | ✅ 标本数据"录入慢、丢失代价大"的工程响应 | 一般馆藏数据管理实践 |
| 降级模式包容旧版 | ✅ 关键迁移友好 — 不强制全员同步升级 | 软件工程一般原则 |
| 跨 voucher 同 SHA256 → 审核而非自动合并 | ✅ Symbiota duplicate-cluster 工具的精神（人审核为主）| Symbiota Docs Duplicate Catalog Numbers |
| 段守护（manifest.voucher_range 校验）| ✅ "信任录入员自律" → "机器验证"，类 LIMS 批次门控 | 实验室 LIMS 通用 |

## 3. 弱点 / 改进点

### A. 短期建议（建议下个版本做）

**A1. Darwin Core (DwC) 导出**
- 现状：自定义中文列（入库编号*、管内编号*、采集地点缩写*）。
- 缺口：要发表数据 / 推送 GBIF / 共享给国际同行 → 必须映射到 DwC（occurrenceID、catalogNumber、scientificName、recordedBy、eventDate、decimalLatitude、decimalLongitude 等）。
- 建议：加 `specimen_app/dwc_export.py`，把中心机数据一键打成 DwC-A zip（`meta.xml` + `occurrence.txt` + `multimedia.txt`）。
- 工作量：~500 行 + 字段映射表 + 测试。
- 优先级：**最高**。缺它无法外联生物多样性数据网络。

**A2. EXIF 自动抽取**
- 现状：照片归档存 SHA256/大小/文件名；EXIF 元数据（拍摄时间、GPS、设备）未自动入库。
- 缺口：野外标本拍照的 GPS+时间是关键 metadata（对应 DwC.eventDate / decimalLatitude），人工录易错。
- 建议：`add_photo()` 时调 `Pillow.ExifTags` 抽 EXIF 自动填充对应标本字段（用户可改）。
- 工作量：~150 行。

**A3. 采集元数据字段补全**
- 现状：`SPECIMEN_HEADERS` 缺 recordedBy（采集人）/ samplingProtocol（采集方法）/ coordinateUncertaintyInMeters（坐标精度）/ 海拔深度 / habitat（生境）/ 保存温度 / associatedSequences（GenBank 链接）。
- 建议：把 `分类信息` 表拆为"标本采集元数据"+"分类鉴定"两表（或在 `标本信息` 加列），对齐 DwC Occurrence + Event。
- 工作量：~200 行 + UI 表单扩展；需 schema bump + 旧数据迁移（M5 升级流程可复用）。

### B. 中期建议（半年内）

**B1. 标本-标本关系建模**
- 现状：每条 voucher 独立；无 母-子（个体 → DNA 提取物）、同采集事件（一次拖网得 50 个体）等关系。
- DwC 等价物：`parentEventID` / `associatedOccurrences` / `materialSampleID`。
- 建议：加 `数据/关系.xlsx`（source_voucher, target_voucher, relation_type, note）。

**B2. 持久标识符（PID）发布管道**
- 现状：`record_id` 是本地 UUID，不能被外部解引用。
- DwC 推荐：UUID + PURL 前缀，形如 `https://purl.org/specimen/{record_id}` → 跳转展示页。
- 建议：DwC 导出包附 PURL 字段；待真有发布需求再上 PURL 注册（PURL.org 免费）。

**B3. 照片相似度去重（弥补 SHA256 局限）**
- 现状：同照片重编码（JPEG 不同压缩）→ 不同 SHA256 → 视为不同。
- GBIF 做法：感知哈希（pHash）+ 字段相似度 clustering。
- 建议：duplicates/ 审核流程加 pHash 计算，xlsx 含"严格重复"/"疑似重复"两档。
- 工作量：~200 行（依赖 `imagehash` 库，需评估 PyInstaller 兼容性）。

### C. 长期建议（一年以上 / 视团队规模）

**C1. WoRMS / Catalogue of Life 权威分类同步** — `worms_client.py` 已存在（v0.5.0+），离线缓存已有 ✅。进一步可与 AphiaID 双向链接。

**C2. 版本化标本记录** — 旧值在 `修改记录.xlsx` 但无"v1/v2 持久 URL"。对小团队过度设计。

**C3. 数据切片 DOI** — 合并后中心快照可注册 DataCite DOI（如发表数据集需要）。工作量大，按需。

**C4. 跨机构 schema 协商** — DwC 已解决：双方导出 DwC-A 后用第三方工具合。**不必内置**。

## 4. 与姊妹项目协同

未来若 BiodivSurvey app（采样记录 app）串联本工具：
- BiodivSurvey 采样事件 → eventID
- 本工具录入标本时填 eventID → DwC 导出时自动连关
- SHA256/eventID 桥接，不必合库

## 5. 评价总结

**适用场景内非常成熟**：≤5 人中文小馆藏离线录入 + 中心合并 = **工程上接近最优**。
关键点全覆盖：数据安全（snapshot/快照/审计四路）、编号唯一性（前缀分人 + 段守护 + 指纹兜底）、
照片去重（SHA256 + 跨 voucher 审核）、向后兼容（不动 schema）、迁移友好（降级模式）。

**与国际生物信息标准对齐有差距但非阻塞**：DwC 字段映射 / PID 注册 / EXIF 抽取
是当前最大缺口。若数据仅内部使用、不上 GBIF / 不发表论文，可忽略；若有外联需求，
**优先做 A1 DwC 导出**。

### 评分（参照 GBIF Best Practices for Publishing Biodiversity Data）

| 维度 | 评分（10 分制） | 说明 |
|------|-----------------|------|
| 数据安全性 | 9 | 四路审计 + 快照 + 一键回退 |
| 离线支持 | 10 | 完全离线，对齐 DwC-A 异步 harvest |
| 多人协作 | 9 | 前缀分人 + 段守护 + 重复审核 |
| 标准合规（DwC/Audubon Core）| 5 | 内部数据完整但缺 DwC 字段映射 |
| 可发布性（GBIF/PURL）| 4 | 无 PID 管道 / 无 DwC-A 一键导出 |
| 长期可维护性 | 8 | Excel + 文件协议，简单透明 |
| **总分** | **45/60** | 小团队内部使用强；外联需 DwC 导出补 |
