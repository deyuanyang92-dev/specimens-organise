# 海洋动物标本图像机器学习物种分类综述与训练方案（2024-2026）

生成日期：2026-05-15  
目标数据：`/mnt/n/codex/specimen-organise/测试数据集` 类似的海洋动物标本照片  
目标用户：专业海洋动物分类学家  
核心目标：把机器学习作为“候选识别、相似图检索、自动填表建议、专家确认”的辅助工具，而不是替代分类学鉴定。

---

## 1. 结论摘要

2024-2026 年，物种图像分类研究的主线已经从“训练一个普通 CNN 分类器”转向以下方向：

1. **生物视觉基础模型**：BioCLIP、BioCLIP 2、Bio-DINO、Insect-Foundation 等开始使用大规模生物图像、分类树、图像-文本对比学习或自监督学习，目标是得到适合生物分类的通用视觉表征。
2. **细粒度视觉分类（FGVC）**：模型强调局部判别结构、mask、attention、多尺度特征。对海洋多毛类、底栖无脊椎动物而言，头部、疣足、刚毛等局部诊断性状比整体轮廓更接近种级鉴定证据。
3. **少样本、长尾、开放世界识别**：真实物种数据天然长尾，稀有种样本少，未知种会出现。2024-2026 的研究更重视 zero-shot、few-shot、embedding 检索、open-set rejection、层级分类和专家验证。
4. **多图、多部位、多证据融合**：一个标本往往有整体照、头部照、疣足照、刚毛显微图。最合理的机器学习单位不是单张图片，而是“标本个体（specimen）+ 多张证据图（views/parts）”。
5. **软件集成与 human-in-the-loop**：AI 输出 Top-k 候选、置信度、证据图、相似历史标本；分类专家确认后写入正式分类字段。模型预测必须可追溯。

对本项目的推荐路线：

- 第一阶段：不要急着训练大模型。先把 TIFF 原图降采样成训练 JPEG，建立 `manifest.csv`，补齐标本级标签和图片部位标签。
- 第二阶段：测试 BioCLIP 2、DINOv2、Bio-DINO 的 embedding 与相似图检索能力。
- 第三阶段：标签足够后，用 `timm` 训练 ConvNeXt-Tiny / EfficientNet-B0 作为监督基线。
- 第四阶段：有头部、疣足、刚毛等细节图后，训练部位识别器和部位专用分类器。
- 第五阶段：做标本级多图融合，把整体照和诊断部位照汇总为一个分类建议。
- 软件集成：新增“AI 识别实验”模块，先写 AI 建议字段，不直接覆盖专家确认字段。

---

## 2. 检索范围与资料来源

用户要求“检索 Google Scholar 等并综述目前研究进展”。Google Scholar 页面通常不适合自动抓取，因此本报告采用等价的学术检索策略：

- Google Scholar 风格关键词：
  - `2024 2025 2026 species identification computer vision deep learning`
  - `biodiversity image classification foundation model BioCLIP 2`
  - `fine-grained visual classification survey 2024 2025`
  - `multi-view species identification image classification`
  - `few-shot fine-grained species classification LifeCLEF FungiCLEF`
  - `taxonomic classification wildlife images hierarchical deep learning`
- 交叉核对来源：
  - CVF / CVPR
  - OpenReview
  - arXiv
  - Springer / Nature / Scientific Reports
  - ScienceDirect
  - MDPI
  - LifeCLEF / ImageCLEF / CEUR-WS
  - GitHub API 元数据
  - Hugging Face 模型卡

重点时间范围：2024-2026。

---

## 3. 当前测试数据集概况

已检查路径：

```text
/mnt/n/codex/specimen-organise/测试数据集
```

当前发现：

- 原始照片目录：`照片/实验室拍照电脑/广西儒艮保护区`
- 原图数量：160 张
- 格式：全部为 `.tif`
- 总量：约 6.2GB
- 单张尺寸：约 `5130 x 3826` 或 `5182 x 3886`
- 内容：黑底标准化标本照，多数带标尺/色卡，主体清楚
- `照片信息.xlsx`：40 条照片关联记录
- `分类信息.xlsx`：目前只有 3 条有种名
- 可直接监督训练的“照片 + 种名”样本：约 4 张
- 文件名可解析出约 25 个疑似类群/形态组缩写，如 `WSC`、`XTC`、`CSC`、`BZC`、`Cirratulidae`

关键判断：

1. 当前图像质量适合训练和检索。
2. 当前正式分类标签严重不足，不适合直接训练物种级监督模型。
3. 文件名缩写可以作为临时分组或标注辅助，但不能替代专家确认的分类标签。
4. 单张 TIFF 太大，训练前必须生成降采样副本，例如最长边 `768` 或 `1024` 的 JPEG/WebP。

---

## 4. 2024-2026 关键研究进展

### 4.1 生物视觉基础模型

#### BioCLIP（CVPR 2024）

BioCLIP 是 2024 年最重要的生物物种图像基础模型之一。论文提出面向生命树（Tree of Life）的视觉-语言基础模型，使用大规模生物图像和分类语义训练。它的价值在于：

- 不只做 ImageNet 式通用分类，而是面向生物分类。
- 可通过拉丁名、通用名、分类层级进行 zero-shot 或 few-shot 识别。
- 适合输出物种、属、科的候选排名。

对本项目意义：

- 第一优先级测试模型。
- 可用于“候选识别 + 相似标本检索”。
- 当前标签少时，比从零训练 CNN 更现实。

来源：

- Paper: https://openaccess.thecvf.com/content/CVPR2024/html/Stevens_BioCLIP_A_Vision_Foundation_Model_for_the_Tree_of_Life_CVPR_2024_paper.html
- GitHub: https://github.com/Imageomics/bioclip

#### BioCLIP 2（2025）

BioCLIP 2 进一步扩大到 TreeOfLife-200M，模型卡和预印本说明其使用约 214M 生物图像训练，并强调层级对比学习产生的生物学 embedding 结构。

重点：

- 更大规模生物图像。
- 更强物种表征。
- embedding 空间可保留种内变异，如性别、生活史阶段、生态性状。

对本项目意义：

- 应优先测试 BioCLIP 2 的 zero-shot、embedding、kNN、线性分类。
- 对头部/疣足/刚毛图也可提取 embedding，但需要验证其是否能识别显微局部结构。

来源：

- arXiv: https://arxiv.org/abs/2505.23883
- Hugging Face: https://huggingface.co/imageomics/bioclip-2

#### Bio-DINO（2026 前后公开模型）

Bio-DINO 是面向生物多样性图像的自监督图像 encoder，采用 DINOv2 风格，模型卡说明其训练在约 31M 生物图像上，覆盖植物、真菌、昆虫、鱼、珊瑚、鸟、哺乳动物等。

对本项目意义：

- 可作为 DINOv2 的生物领域对照。
- 不依赖文本标签，适合做纯视觉相似检索。
- 适合当前标签少但图片质量高的阶段。

来源：

- https://huggingface.co/birder-project/vit_reg4_so150m_p14_ls_dino-v2-bio

#### Insect-Foundation（IJCV 2025）

Insect-Foundation 是昆虫视觉-语言基础模型，说明 2025 年后“类群专用 foundation model”已经成为趋势。

对本项目意义：

- 多毛类/海洋底栖动物未来也可以建立专用 foundation model，但当前数据量不够。
- 目前应先借用 BioCLIP 2 / DINOv2 / Bio-DINO，再逐步积累专家确认数据。

来源：

- https://link.springer.com/article/10.1007/s11263-025-02521-4

### 4.2 自监督视觉表征

#### DINOv2（TMLR 2024）

DINOv2 是强通用自监督视觉模型。它不依赖人工标签学习视觉特征，常用于：

- image embedding
- kNN 检索
- clustering
- linear probe
- 少样本分类
- 下游分类微调

对本项目意义：

- 当前正式标签少，DINOv2 是非常适合的基线。
- 可先做“相似标本检索”：输入一张图，找视觉上最接近的历史专家确认标本。
- 可用标本级分组做 kNN，而不是单图随机验证。

来源：

- Paper: https://openreview.net/forum?id=a68SUt6zFt
- GitHub: https://github.com/facebookresearch/dinov2

#### DINOv3

DINOv3 属于新一代自监督视觉表征方向。由于其发布时间较新，文献和生态仍在快速变化，建议作为第二批对照模型，而不是第一阶段唯一依赖。

来源：

- GitHub: https://github.com/facebookresearch/dinov3

### 4.3 细粒度视觉分类（FGVC）

细粒度视觉分类研究的是“同一大类下相似子类”的区分，例如鸟种、昆虫种、鱼种、菌种、植物种、车型等。海洋多毛类/底栖动物物种识别本质上就是 FGVC。

2024-2026 的 FGVC 进展主要集中在：

- 局部判别区域选择
- Transformer attention
- mask-guided feature learning
- 多尺度特征
- 少样本细粒度识别
- 局部特征与全局特征融合

#### FET-FGVC（Pattern Recognition 2024）

FET-FGVC 提出 Feature-Enhanced Transformer，用动态 Swin Transformer 提取全局特征，用 GCN 分支增强局部特征，并融合全局和局部信息。

对本项目意义：

- 支撑“整体形态 + 诊断局部结构”融合的必要性。
- 头部、疣足、刚毛图应作为局部证据，不应简单混入整体照分类器。

来源：

- https://www.sciencedirect.com/science/article/pii/S0031320324000165

#### MCM-ViT（2025）

MCM-ViT 使用 mask 引导的多尺度 Transformer，强调用分割 mask 减少背景噪声并选择判别区域。

对本项目意义：

- 你的照片有黑底、标尺、色卡。mask 或主体裁剪可能减少模型学习背景/标尺偏差。
- 可测试 `原图分类` vs `主体裁剪后分类`。

来源：

- https://www.sciencedirect.com/science/article/pii/S0045790624008140

#### Few-shot FGVC 综述（2024-2025）

少样本细粒度分类综述把方法分为：

- metric-based：原型网络、kNN、image-to-class similarity
- optimization-based：MAML 等元学习
- transfer learning：预训练模型微调
- feature reconstruction：局部/全局特征重构
- vision-language adaptation：CLIP 类模型适配

对本项目意义：

- 当前每个物种样本有限，应优先使用 embedding、kNN、少样本和迁移学习。
- 不建议从零训练大模型。

来源：

- https://www.mdpi.com/2673-2688/5/1/20
- https://www.sciencedirect.com/science/article/pii/S0957417425006761

### 4.4 生物多样性挑战与数据集

#### LifeCLEF 2025

LifeCLEF 2025 包括 AnimalCLEF、BirdCLEF+、FungiCLEF、GeoLifeCLEF、PlantCLEF，覆盖图像、声音、少样本、个体识别、生态位和多物种场景。

对本项目意义：

- 真实生物识别任务不只是“图片分类”，还包括稀有类群、开放集、多标签、多模态、专家验证。
- FungiCLEF 的 few-shot 设置与稀有海洋物种非常相似。

来源：

- https://www.imageclef.org/LifeCLEF2025
- https://esploro.umontpellier.fr/esploro/outputs/conferenceProceeding/Overview-of-LifeCLEF-2025-Challenges-on/99135050909311?institution=33UDM_INST

#### PlantCLEF 2025

PlantCLEF 2025 把任务设为高分辨率样方图中的多物种识别，提供 1.4M 单株图像训练集和专家标注测试集。

对本项目意义：

- 虽然对象是植物，但其“专家标注、高分辨率、生态图像、多标签”的评估思想值得借鉴。

来源：

- https://arxiv.org/abs/2509.17602

#### FungiCLEF 2025

FungiCLEF 强调 rare fungi 的 few-shot 分类。真菌和海洋底栖动物一样存在形态相似、标注难、样本少的问题。

对本项目意义：

- 可参考 FungiCLEF 的 few-shot protocol、open-set 设计和评价指标。

来源：

- https://ceur-ws.org/Vol-4038/paper_233.pdf

#### Fish-Vista（2024）

Fish-Vista 是鱼类图像数据集，支持 species classification、trait identification、trait segmentation。

对本项目意义：

- 非常接近“物种 + 诊断性状 + 分割”的研究方向。
- 你的头部、疣足、刚毛图可以被设计为 trait/part evidence。

来源：

- https://arxiv.org/abs/2407.08027

#### CrypticBio（2025）

CrypticBio 针对视觉上容易混淆的物种，强调 cryptic species、地理/时间信息和专家验证。

对本项目意义：

- 海洋无脊椎动物常见隐存种、近似种、未定种，AI 必须允许“低置信度”和“未知/待复核”，不能强行给出已知物种。

来源：

- https://arxiv.org/abs/2505.14707
- https://georgianagmanolache.github.io/crypticbio

### 4.5 层级分类与开放世界识别

#### TaxonomyNet（Scientific Reports 2026）

TaxonomyNet 针对 wildlife images 做多分类阶元识别，强调 species、genus、family 等多级输出的一致性。

对本项目意义：

- 分类学软件中不能只输出一个平面 species label。
- 应同时输出门、纲、目、科、属、种，并保证层级一致。
- 当种级不确定时，模型应能可靠输出科级或属级。

来源：

- https://www.nature.com/articles/s41598-025-34944-x

#### Open-world ecological taxonomy classification（2025）

开放世界生态分类关注未知类、长尾类、域偏移和层级分类。

对本项目意义：

- 新种、未描述种、未训练类会出现。软件应提供 `unknown / cf. / aff. / sp.` 机制。
- 模型应能拒识，而不是把未知图像强行归到已知种。

来源：

- https://arxiv.org/abs/2512.18994

### 4.6 现实应用系统

#### SpeciesNet（Google, 2025）

SpeciesNet 是 Google 开源的 camera trap 野生动物识别模型，GitHub 说明其由检测器和分类器组成，SpeciesNet classifier 基于 EfficientNet V2 M，并输出 top-5 物种分类及置信度。

对本项目意义：

- 它证明了“检测/裁剪 + 分类 + Top-k + 置信度 + 标签 rollup”的工程路线。
- 但它面向 camera trap 大型动物，不适合直接识别你的多毛类标本。

来源：

- https://github.com/google/cameratrapai
- https://techcrunch.com/2025/03/03/google-releases-speciesnet-an-ai-model-designed-to-identify-wildlife/

#### PyTorch-Wildlife（2024）

PyTorch-Wildlife 是 Microsoft AI for Good Lab 的野生动物检测/分类框架，适合 camera trap 工作流。

对本项目意义：

- 可参考其数据流水线、检测+分类、批处理设计。
- 模型本身不适合海洋无脊椎动物标本，但软件工程思想可借鉴。

来源：

- https://arxiv.org/abs/2405.12930

---

## 5. GitHub 项目与软件生态（2026-05-15 查询）

以下 star 数为 2026-05-15 通过 GitHub API 查询，近似反映生态成熟度，不代表学术相关性。

| 项目 | Stars | 许可证 | 用途 | 对本项目建议 |
|---|---:|---|---|---|
| `ultralytics/ultralytics` | 57,156 | AGPL-3.0 | YOLO 检测/分割/分类 | 可做部位检测实验；闭源商用需注意 AGPL |
| `facebookresearch/segment-anything` | 54,153 | Apache-2.0 | SAM 分割 | 可做标本主体、局部结构裁剪 |
| `roboflow/supervision` | 38,997 | MIT | 检测/分割后处理与可视化 | 可辅助标注和可视化 |
| `huggingface/pytorch-image-models` | 36,810 | Apache-2.0 | timm 分类模型库 | ConvNeXt/EfficientNet/Swin/ViT 训练首选 |
| `open-mmlab/mmdetection` | 32,688 | Apache-2.0 | 检测框架 | 强但重，后期再用 |
| `Lightning-AI/pytorch-lightning` | 31,137 | Apache-2.0 | 训练框架 | 中大型训练可用 |
| `fastai/fastai` | 28,010 | Apache-2.0 | 快速迁移学习 | 快速原型可用 |
| `HumanSignal/label-studio` | 27,321 | Apache-2.0 | 标注平台 | 可用于部位和分类标签标注 |
| `cvat-ai/cvat` | 15,846 | MIT | 图像标注、检测/分割 | 可用于部位 bbox/mask |
| `mlfoundations/open_clip` | 13,806 | NOASSERTION | CLIP/OpenCLIP | BioCLIP/zero-shot/embedding 生态 |
| `facebookresearch/dinov2` | 12,839 | Apache-2.0 | DINOv2 embedding | 必测 |
| `facebookresearch/dinov3` | 10,392 | NOASSERTION | 新 DINO 系列 | 第二批对照 |
| `voxel51/fiftyone` | 10,708 | Apache-2.0 | 数据集可视化、错误分析 | 强烈建议用于检查混淆样本 |
| `IDEA-Research/GroundingDINO` | 10,108 | Apache-2.0 | 文本提示检测 | 可探索“head/parapodium/chaetae”定位 |
| `open-mmlab/mmpretrain` | 3,842 | Apache-2.0 | 分类训练框架 | 可作为 timm 替代 |
| `Imageomics/bioclip` | 256 | NOASSERTION | BioCLIP | star 不高，但领域相关性最高 |
| `birder-project/birder` | 17 | Apache-2.0 | 生物图像训练框架 / Bio-DINO 相关 | 关注但不作为主工程依赖 |

推荐工程组合：

- 数据标注：Label Studio 或 CVAT
- 数据浏览与错误分析：FiftyOne
- 基础模型测试：BioCLIP 2、DINOv2、Bio-DINO
- 监督训练：timm
- 分割/裁剪：SAM，后期可试 GroundingDINO
- 软件集成：独立 Python 推理服务 + PyQt 主程序调用

---

## 6. 主流方法体系综述

### 6.1 传统迁移学习分类

代表模型：

- EfficientNet / EfficientNetV2
- ConvNeXt / ConvNeXtV2
- ResNet
- MobileNetV3
- Swin Transformer
- ViT

优点：

- 训练流程成熟。
- 对小型 GPU 友好，特别是 EfficientNet-B0、ConvNeXt-Tiny、MobileNetV3。
- 标签足够后能得到稳定的本地分类器。

缺点：

- 需要每个类别足够多专家确认图。
- 容易学习背景、标尺、色卡、拍摄批次等伪特征。
- 对未知类不友好，常强行预测为已知类。

适用阶段：

- 每个目标物种至少 20-50 个标本级样本后。

### 6.2 生物视觉基础模型 zero-shot / few-shot

代表：

- BioCLIP
- BioCLIP 2
- OpenCLIP
- CLIP + 地理/时间/生态 metadata

优点：

- 标签少时仍可测试。
- 可用文本候选词：中文种名、拉丁名、属名、科名、形态描述。
- 适合候选识别和相似图检索。

缺点：

- 对显微局部图、酒精固定标本、黑底实验室图像的泛化需要实测。
- 可能受训练数据类群偏差影响。
- 对近似种/隐存种仍可能混淆。

适用阶段：

- 立即可测。

### 6.3 自监督 embedding + kNN / 线性分类

代表：

- DINOv2
- DINOv3
- Bio-DINO
- MAE / iBOT / self-supervised ViT

优点：

- 不依赖文本标签。
- 适合相似图检索、聚类、低标签分类。
- 可先服务于专家标注：找相似历史标本，辅助确认。

缺点：

- embedding 相似不等于分类学同种。
- 需要专家验证聚类结果。

适用阶段：

- 当前最适合。

### 6.4 细粒度部位/局部证据模型

代表方法：

- part-based FGVC
- attention-based FGVC
- mask-guided ViT
- feature-enhanced Transformer
- multi-scale feature learning

优点：

- 对头部、疣足、刚毛等诊断部位最贴近。
- 可解释性较强：能显示哪个部位图支持候选。

缺点：

- 需要部位标签。
- 部位图拍摄标准需要统一。
- 单一部位可能只能支持属/科，不能总是支持种。

适用阶段：

- 收集细节图后启动。

### 6.5 检测/分割 + 分类两阶段模型

代表：

- SAM
- GroundingDINO
- YOLO
- Mask-guided transformer

流程：

```text
原图 -> 标本主体/部位检测或分割 -> crop/mask -> 分类模型
```

优点：

- 减少黑底、标尺、色卡、标签纸干扰。
- 可以分别裁出头部、疣足、刚毛区域。

缺点：

- 标本细长、透明、破碎、缠绕时分割不一定稳定。
- 刚毛显微图和整体图差异大，可能需要分开模型。

适用阶段：

- 第二阶段以后。

### 6.6 多图/多视角/多实例融合

这是最适合本项目最终目标的方法。

定义：

```text
一个 specimen = 多张图片 = whole + head + parapodium + chaetae + other views
模型输出 specimen-level taxonomic candidates
```

简单融合：

```text
每张图片单独预测 -> 按 view_type 加权平均 -> 标本级 Top-k
```

建议初始权重：

```text
whole: 1.0
head: 1.5
parapodium: 2.0
chaetae: 2.5
microscope: 2.5
```

进阶融合：

- attention pooling
- multiple instance learning
- multi-view contrastive learning
- specimen-level metric learning

优点：

- 符合分类学工作流。
- 能容忍某些部位缺失。
- 能输出证据贡献。

缺点：

- 数据结构要求高。
- 需要 specimen-level split 和 evaluation。

---

## 7. 数据标注体系设计

### 7.1 核心原则

最终训练单位应是“标本个体”，不是单张图片。

错误做法：

```text
species_name/
  image1.tif
  image2.tif
```

这种结构会把整体图、细节图、显微图混在一起，模型不知道图像部位，也无法解释证据。

推荐结构：

```text
specimen_id
image_id
image_path
view_type
body_region
magnification
preparation
species_name
genus_name
family_name
taxon_confidence
expert_status
diagnostic_note
```

### 7.2 图片部位标签

建议标准枚举：

```text
whole
head
parapodium
chaetae
pygidium
dorsal
ventral
anterior
posterior
microscope
label_or_scale
other
unknown
```

多毛类/海洋底栖动物可扩展：

```text
prostomium
peristomium
jaw
branchiae
elytra
notopodium
neuropodium
dorsal_cirrus
ventral_cirrus
compound_chaetae
simple_chaetae
acicula
```

### 7.3 专家确认状态

建议每个分类字段都记录状态：

```text
unlabeled
ai_suggested
expert_confirmed
expert_rejected
needs_review
cf
aff
sp
unknown
```

### 7.4 标注优先级

第一优先级：

- 入库编号
- 图片路径
- 图片部位 `view_type`
- 标本级科/属/种
- 专家确认状态

第二优先级：

- 采集地
- 采集日期
- 保存方式
- 拍摄倍率
- 拍摄设备
- 是否显微图

第三优先级：

- 诊断性状备注
- 局部结构 mask/bbox
- 性状类别标签

---

## 8. 详细训练方案

### 8.1 阶段 0：数据治理

目标：把现有 TIFF 变成可训练、可追溯的数据资产。

步骤：

1. 扫描所有原图。
2. 生成 `manifest.csv`。
3. 计算 SHA256，防止重复和路径变动。
4. 从文件名解析疑似标本号/类群缩写。
5. 读取 Excel 中的照片信息、标本信息、分类信息。
6. 输出缺失报告：
   - 有图片无照片记录
   - 有照片记录但文件不存在
   - 有照片无分类
   - 有分类无照片
   - 同一标本多张图
   - 疑似重复图

建议输出字段：

```text
image_id
sha256
source_path
derived_jpeg_path
filename
specimen_id
tube_code
parsed_group_code
view_type
species_cn
species_latin
genus
family_cn
family_latin
label_status
width
height
file_size
split
notes
```

### 8.2 阶段 1：图像预处理

目标：避免每次训练读取 5k TIFF。

建议生成：

```text
dataset_derivatives/
  jpg_1024/
  jpg_768/
  thumb_256/
```

推荐：

- embedding 检索：`1024` 或 `768`
- 轻量监督训练：`384` 或 `512`
- 快速浏览：`256`

保留原图，不覆盖。

注意：

- 不要用缩略图缓存作为训练图，缓存可能压缩过强。
- 保留 EXIF/原始路径/sha256 映射。
- 黑底和色卡可以先保留，后续做裁剪对比实验。

### 8.3 阶段 2：基础模型 embedding 测试

模型：

- BioCLIP 2
- DINOv2
- Bio-DINO

任务：

1. 提取每张图片 embedding。
2. 做 UMAP/t-SNE 可视化。
3. 按疑似类群缩写观察聚类。
4. 对专家确认样本做 kNN 检索。
5. 输出每张图最相似的 Top-10 历史图。

评价：

```text
same_species_at_1
same_species_at_5
same_genus_at_5
same_family_at_5
expert_accept_rate
```

当前标签少时，先用专家人工判断相似检索是否有用。

### 8.4 阶段 3：zero-shot / few-shot 候选识别

模型：

- BioCLIP 2
- OpenCLIP 对照

候选词设计：

```text
a photo of Perinereis aibuhitensis
a specimen of Perinereis aibuhitensis
a marine polychaete specimen, Perinereis aibuhitensis
沙蚕科 Nereididae
巢沙蚕 Diopatra
欧努菲虫科 Onuphidae
```

建议同时测试：

- 中文种名
- 拉丁种名
- 属名
- 科名
- 形态描述短语

输出：

```text
top1_species
top3_species
top5_species
top1_genus
top1_family
confidence
```

注意：

- 中文名不一定进入模型训练语料，拉丁名通常更稳。
- 对未训练/未收录物种，应允许返回属/科级候选。

### 8.5 阶段 4：监督分类基线

启动条件：

- 每个目标物种至少 20 个专家确认标本，最低 10 个可做 pilot。
- 每个物种最好有多标本，而不是同一标本多张图。
- split 必须按 `specimen_id` 分组，不能同一标本图片同时进入 train 和 val。

模型：

- EfficientNet-B0
- ConvNeXt-Tiny
- MobileNetV3-Large
- Swin-Tiny

训练设置，适合 GTX 1660 SUPER 4GB：

```text
image_size: 384 或 512
batch_size: 4-16
amp: true
epochs: 30-100
optimizer: AdamW
lr: 1e-4 到 5e-4
weight_decay: 0.01
early_stopping: true
class_balanced_sampler: true
```

增强策略：

- 安全增强：
  - resize
  - random crop
  - mild color jitter
  - slight rotation
  - brightness/contrast
- 谨慎使用：
  - horizontal flip：如果左右方向不影响鉴定可用，否则慎用
  - vertical flip：可能破坏解剖方向，通常慎用
  - heavy color jitter：可能破坏体色线索
  - cutmix/mixup：对整体照可试，对局部诊断图要谨慎

评价：

```text
species_top1
species_top3
genus_top1
family_top1
macro_f1
balanced_accuracy
confusion_matrix
calibration_error
low_confidence_reject_accuracy
```

### 8.6 阶段 5：部位识别器

目标：软件导入图片后自动判断这是整体、头部、疣足、刚毛还是显微图。

输入：

```text
image -> view_type
```

模型：

- DINOv2 embedding + LogisticRegression
- EfficientNet-B0
- MobileNetV3

最低数据：

- 每个部位 30 张可做初版。
- 每个部位 100 张以上较稳定。

意义：

- 自动整理照片。
- 为后续部位专用模型分流。
- 软件中可提示用户“这张可能是疣足图，是否确认？”

### 8.7 阶段 6：部位专用分类器

不要一开始把所有图混成一个大分类器。推荐按部位建模型：

```text
whole_encoder
head_encoder
parapodium_encoder
chaetae_encoder
microscope_encoder
```

每个模型输出：

```text
family候选
genus候选
species候选
confidence
embedding
```

为什么这样做：

- 整体照判断体形、体节、颜色、附肢整体排列。
- 头部判断触须、眼点、围口节、颚等。
- 疣足判断背须、腹须、叶片、刚毛束结构。
- 刚毛显微图判断刚毛类型和形态。

### 8.8 阶段 7：标本级多图融合

输入：

```text
specimen_id = {
  whole: [img1, img2],
  head: [img3],
  parapodium: [img4, img5],
  chaetae: [img6]
}
```

第一版融合：

```text
score(taxon) =
  1.0 * mean(whole_scores) +
  1.5 * mean(head_scores) +
  2.0 * mean(parapodium_scores) +
  2.5 * mean(chaetae_scores)
```

第二版融合：

- view-aware attention pooling
- multiple instance learning
- missing-view aware fusion
- specimen-level metric learning

输出：

```text
species_top5
genus_top5
family_top5
evidence_by_view
uncertainty
recommendation: auto_fill / review / reject / unknown
```

---

## 9. 软件集成方案

### 9.1 原则

AI 预测必须是建议，不是正式鉴定。

正式字段：

```text
种名*
种拉丁
属名
科*
科拉丁
目
纲
门
备注
```

AI 建议字段应独立保存：

```text
AI建议种名
AI建议属名
AI建议科名
AI模型名称
AI模型版本
AI置信度
AI Top-k
AI证据图片
AI建议时间
专家确认状态
专家确认人
专家确认时间
```

### 9.2 点击图片后的交互

推荐界面：

```text
AI识别面板
├── 当前图片部位：whole / head / parapodium / chaetae
├── 单图候选 Top-5
├── 标本级候选 Top-5
├── 相似历史标本 Top-10
├── 证据分解：整体、头部、疣足、刚毛分别支持什么
├── 操作按钮：
│   ├── 填入分类信息
│   ├── 只填入科/属
│   ├── 加入待复核
│   ├── 驳回建议
│   └── 查看相似图
```

### 9.3 模型服务

推荐不要把深度学习模型直接塞进 PyQt 主线程。

架构：

```text
PyQt 主程序
  -> 本地 AI 推理服务 FastAPI/CLI
      -> 模型加载
      -> embedding 缓存
      -> top-k 预测
      -> 返回 JSON
```

原因：

- 模型加载慢。
- GPU/CPU 依赖复杂。
- 避免 GUI 卡死。
- 可以独立升级 AI 模块。

### 9.4 缓存

必须缓存：

```text
image_sha256
model_id
embedding_vector
prediction_topk
thumbnail_path
created_at
```

这样同一张 20MB TIFF 不会每次点击都重新推理。

---

## 10. 训练环境建议

当前检查到：

- Windows GPU：NVIDIA GeForce GTX 1660 SUPER，约 4GB 显存
- WSL 当前 CUDA 不可用：`torch.cuda.is_available() = False`
- Python 目前为 3.13，深度学习生态更建议 Python 3.11

推荐新建环境：

```bash
conda create -n marine-cv python=3.11 -y
conda activate marine-cv
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
pip install timm open_clip_torch transformers scikit-learn pandas pillow opencv-python matplotlib seaborn tqdm umap-learn faiss-cpu
```

WSL GPU 检查：

```bash
ls -l /dev/dxg
nvidia-smi
python - <<'PY'
import torch
print(torch.cuda.is_available())
print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else "no cuda")
PY
```

如果 GPU 仍不可用：

1. Windows PowerShell 执行：

```powershell
wsl --update
wsl --shutdown
```

2. 更新 Windows NVIDIA 驱动。
3. 重启 Windows。
4. WSL 内不要安装 Linux NVIDIA kernel driver，只使用 Windows 驱动暴露给 WSL 的 CUDA。

4GB 显存训练建议：

- embedding 提取：batch size 1-8
- ConvNeXt-Tiny / EfficientNet-B0：image size 384/512
- 混合精度：AMP
- 冻结 backbone 或只微调最后几层
- 大模型如 BioCLIP 2 全量微调不建议在本机做

---

## 11. 评估指标

### 11.1 分类学层级指标

```text
family_top1
genus_top1
species_top1
species_top3
species_top5
hierarchical_consistency
```

### 11.2 少样本和长尾指标

```text
macro_f1
balanced_accuracy
per_class_recall
rare_species_recall
```

### 11.3 检索指标

```text
same_species_recall@1
same_species_recall@5
same_genus_recall@5
same_family_recall@10
mean_average_precision
```

### 11.4 专家工作流指标

```text
expert_accept_rate
expert_edit_rate
expert_reject_rate
time_saved_per_specimen
low_confidence_review_rate
```

### 11.5 可靠性指标

```text
calibration_error
unknown_rejection_accuracy
confusion_between_cryptic_groups
out_of_distribution_detection
```

---

## 12. 实验矩阵

### 12.1 当前整体图阶段

| 实验 | 输入 | 模型 | 输出 | 目的 |
|---|---|---|---|---|
| A1 | whole | BioCLIP 2 zero-shot | species/genus/family Top-k | 测试生物基础模型直接能力 |
| A2 | whole | BioCLIP 2 embedding + kNN | 相似图 | 辅助专家标注 |
| A3 | whole | DINOv2 embedding + kNN | 相似图 | 对照通用自监督模型 |
| A4 | whole | Bio-DINO embedding + kNN | 相似图 | 对照生物自监督模型 |
| A5 | whole | ConvNeXt-Tiny | supervised classification | 标签补齐后的监督基线 |

### 12.2 有细节图后

| 实验 | 输入组合 | 目的 |
|---|---|---|
| B1 | whole only | 整体照能到哪个层级 |
| B2 | head only | 头部证据贡献 |
| B3 | parapodium only | 疣足证据贡献 |
| B4 | chaetae only | 刚毛证据贡献 |
| B5 | whole + head | 整体 + 头部是否提升 |
| B6 | whole + parapodium | 整体 + 疣足是否提升 |
| B7 | whole + chaetae | 整体 + 刚毛是否提升 |
| B8 | all views | 标本级多证据融合 |

### 12.3 裁剪/分割实验

| 实验 | 输入 | 方法 | 目的 |
|---|---|---|---|
| C1 | 原图 | 直接分类 | 基线 |
| C2 | 主体 bbox crop | SAM/手工/阈值 | 去除背景与色卡 |
| C3 | 主体 mask | SAM | mask 是否提高细粒度识别 |
| C4 | 局部结构 crop | 专家框选或检测模型 | 验证诊断部位模型 |

---

## 13. 数据量建议

### 13.1 pilot 阶段

```text
10-20 个类群
每类 5-10 个标本
每个标本 1-3 张整体图
```

目标：

- 验证 embedding 和检索是否有用。
- 建立软件交互流程。

### 13.2 第一版可用模型

```text
每个目标物种 20+ 个标本
每个标本 1 张整体图
至少部分标本有头部/疣足/刚毛图
```

目标：

- 训练 ConvNeXt/EfficientNet 监督基线。
- 做 BioCLIP 2 / DINOv2 对照。

### 13.3 较可靠种级模型

```text
每个目标物种 50+ 个标本
每个关键部位 30+ 张专家确认图
每个标本尽量有 whole + 1-3 个诊断部位
```

目标：

- 标本级多图融合。
- 可进入软件辅助鉴定。

---

## 14. 风险与注意事项

1. **数据泄漏**  
   同一标本的多张图片不能同时出现在 train 和 validation。必须按 `specimen_id` 分组切分。

2. **背景伪特征**  
   黑底、标尺、色卡、拍摄批次可能被模型学成“分类特征”。需要测试 crop/mask 版本。

3. **标签质量比模型更重要**  
   当前可训练标签太少。专家确认标签是核心资产。

4. **未知种与近似种**  
   模型必须允许 `unknown / cf. / aff. / sp.`，不能强行输出已知种。

5. **显微图与整体图不是同一分布**  
   刚毛显微图不能简单和整体照混训。

6. **模型置信度不等于鉴定可靠性**  
   需要校准和专家验证。

7. **许可证**  
   `ultralytics` 是 AGPL-3.0，若未来软件闭源或商业分发，应谨慎。`timm`、DINOv2、SAM、Label Studio、FiftyOne 等多为 Apache/MIT，相对友好。

---

## 15. 推荐实施路线

### 第 1 周：数据清点与 manifest

- 扫描 TIFF 原图。
- 生成 JPEG 派生图。
- 建立 `manifest.csv`。
- 输出缺标签报告。
- 把所有现有图标记为 `view_type=whole`。

### 第 2 周：embedding 与相似检索

- 跑 BioCLIP 2 embedding。
- 跑 DINOv2 embedding。
- 做相似图检索页面/表格。
- 分类专家检查 Top-10 相似图是否有用。

### 第 3-4 周：专家补标

- 在软件或表格中补齐：
  - 入库编号
  - 种名/属/科
  - 图片部位
  - 诊断备注
- 重点补齐高频类群和易混类群。

### 第 5 周：监督基线

- 用 `timm` 训练 EfficientNet-B0 和 ConvNeXt-Tiny。
- 与 BioCLIP 2 / DINOv2 kNN 对比。
- 输出混淆矩阵和失败案例。

### 第 6-8 周：细节图扩展

- 增加 head/parapodium/chaetae 图。
- 训练 view_type 分类器。
- 建立部位专用 embedding 检索。

### 第 9-12 周：软件集成

- 在 PyQt 软件中增加 AI 识别实验面板。
- 点击图片后展示：
  - 当前图像部位
  - 单图候选
  - 标本级候选
  - 相似历史标本
  - 一键填入 AI 建议
  - 专家确认/驳回

---

## 16. 最小可行技术栈

```text
数据处理：pandas, openpyxl, Pillow, tifffile
特征提取：BioCLIP 2, DINOv2, Bio-DINO
监督训练：PyTorch, timm
检索：faiss-cpu 或 sklearn NearestNeighbors
可视化：FiftyOne, matplotlib, seaborn, UMAP
标注：Label Studio 或 CVAT
软件集成：PyQt + 本地 FastAPI/CLI 推理服务
```

---

## 17. 第一版目录建议

```text
marine_ai/
  data/
    manifest.csv
    labels_taxonomy.csv
    view_types.csv
  derivatives/
    jpg_1024/
    jpg_512/
    thumb_256/
  embeddings/
    bioclip2/
    dinov2/
    biodino/
  models/
    view_type_classifier/
    convnext_tiny_species/
  reports/
    dataset_audit.html
    retrieval_eval.html
    confusion_matrix.html
  scripts/
    build_manifest.py
    make_derivatives.py
    extract_embeddings.py
    train_supervised.py
    predict_topk.py
```

---

## 18. 推荐优先测试清单

第一优先级：

1. BioCLIP 2 zero-shot
2. BioCLIP 2 embedding + kNN
3. DINOv2 embedding + kNN
4. Bio-DINO embedding + kNN
5. ConvNeXt-Tiny supervised baseline
6. EfficientNet-B0 supervised baseline

第二优先级：

1. SAM 主体裁剪 + 上述模型
2. view_type 分类器
3. Swin-Tiny
4. DINOv3
5. few-shot prototype / metric learning

第三优先级：

1. GroundingDINO 部位定位
2. YOLO 部位检测
3. attention MIL 标本级融合
4. open-set / unknown species rejection

---

## 19. 参考文献与链接

### 生物视觉基础模型

- BioCLIP: A Vision Foundation Model for the Tree of Life. CVPR 2024.  
  https://openaccess.thecvf.com/content/CVPR2024/html/Stevens_BioCLIP_A_Vision_Foundation_Model_for_the_Tree_of_Life_CVPR_2024_paper.html

- BioCLIP 2: Emergent Properties from Scaling Hierarchical Contrastive Learning. arXiv 2025.  
  https://arxiv.org/abs/2505.23883

- BioCLIP 2 Hugging Face model.  
  https://huggingface.co/imageomics/bioclip-2

- Bio-DINO biodiversity image encoder.  
  https://huggingface.co/birder-project/vit_reg4_so150m_p14_ls_dino-v2-bio

- Insect-Foundation: A Foundation Model and Large Multimodal Dataset for Vision-Language Insect Understanding. IJCV 2025.  
  https://link.springer.com/article/10.1007/s11263-025-02521-4

### 自监督与通用视觉模型

- DINOv2: Learning Robust Visual Features without Supervision. TMLR 2024.  
  https://openreview.net/forum?id=a68SUt6zFt

- DINOv2 GitHub.  
  https://github.com/facebookresearch/dinov2

- DINOv3 GitHub.  
  https://github.com/facebookresearch/dinov3

### 细粒度视觉分类

- FET-FGVC: Feature-enhanced transformer for fine-grained visual classification. Pattern Recognition 2024.  
  https://www.sciencedirect.com/science/article/pii/S0031320324000165

- MCM-ViT: Mask-guided context-enhanced multi-scale transformer for fine-grained visual classification. Computer & Electrical Engineering 2025.  
  https://www.sciencedirect.com/science/article/pii/S0045790624008140

- Few-Shot Fine-Grained Image Classification: A Comprehensive Review. 2024.  
  https://www.mdpi.com/2673-2688/5/1/20

- A review of few-shot fine-grained image classification. Expert Systems with Applications 2025.  
  https://www.sciencedirect.com/science/article/pii/S0957417425006761

### 生物多样性挑战与数据集

- LifeCLEF 2025.  
  https://www.imageclef.org/LifeCLEF2025

- Overview of LifeCLEF 2025.  
  https://esploro.umontpellier.fr/esploro/outputs/conferenceProceeding/Overview-of-LifeCLEF-2025-Challenges-on/99135050909311?institution=33UDM_INST

- PlantCLEF 2025.  
  https://arxiv.org/abs/2509.17602

- FungiCLEF 2025 overview.  
  https://ceur-ws.org/Vol-4038/paper_233.pdf

- Fish-Vista.  
  https://arxiv.org/abs/2407.08027

- CrypticBio.  
  https://arxiv.org/abs/2505.14707

### 层级与开放世界分类

- An efficient and consistent framework for multi-rank taxonomic identification in wildlife images. Scientific Reports 2026.  
  https://www.nature.com/articles/s41598-025-34944-x

- Towards AI-Guided Open-World Ecological Taxonomic Classification. arXiv 2025.  
  https://arxiv.org/abs/2512.18994

- CLIP-Driven Few-Shot Species-Recognition Method for Integrating Geographic Information. Remote Sensing 2024.  
  https://www.mdpi.com/2072-4292/16/12/2238

### 工程项目

- timm / PyTorch Image Models.  
  https://github.com/huggingface/pytorch-image-models

- OpenCLIP.  
  https://github.com/mlfoundations/open_clip

- Segment Anything.  
  https://github.com/facebookresearch/segment-anything

- GroundingDINO.  
  https://github.com/IDEA-Research/GroundingDINO

- SpeciesNet / Google CameraTrapAI.  
  https://github.com/google/cameratrapai

- PyTorch-Wildlife.  
  https://arxiv.org/abs/2405.12930

- Label Studio.  
  https://github.com/HumanSignal/label-studio

- CVAT.  
  https://github.com/cvat-ai/cvat

- FiftyOne.  
  https://github.com/voxel51/fiftyone

