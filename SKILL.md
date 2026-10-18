---
name: zotero-table-extractor
description: 从 Zotero 选中条目（依赖 ai4paper-zotero MCP 获取物理路径）或指定本地 PDF / CNKI / 期刊网页中批量/单篇自动抓取并提取结构化表格，支持多引擎投票与 IBM TEDS 拓扑度量、arXiv HTML / NCBI PMC JATS XML 原生直取、DocLayout 版面切图 + PaddleOCR-VL-1.6 在线多模态解析、IBM Docling (TableFormer) 本地离线备用降级、openpyxl CellRichText 学术上下标渲染、边界与尾注分离启发式，以及可选 PyZotero / Better-Notes 生态联动，导出出版级 Excel (.xlsx)。
---

# Zotero Table Extractor

从 Zotero PDF 附件或论文网页中自动识别并提取结构化表格，导出为独立的 Excel (`.xlsx`) 文件。

> **💡 Agent 架构说明**：  
> 当用户要求“提取 Zotero 选中的这篇论文”时，由 Agent 编排层通过 `ai4paper-zotero` MCP 工具（`zotero_get_selected_items`）获取选中条目的物理 PDF 路径，再传递给本技能 CLI（`--pdf <path>`）执行底层提取。

## 提取管线

```text
论文.pdf / 论文网页
       │
       ├─ 1. 在线 HTML / XML 原生直取（最优先，免除 OCR 误差）
       │     arXiv 官方 HTML / ar5iv LaTeXML
       │     NCBI PMC (PubMed Central) Open Access JATS XML
       │     Elsevier XOCS XML / CNKI / Springer / 通用出版社
       │     核验在线提取表号集合与数量；若存在缺失表号 → 保留已抓取在线高保真表，由本地 PDF 管线精准补漏
       │
       └─ 2. 本地 PDF 提取
              │
              ▼
         多信号候选表格页挖掘（高覆盖防漏检）
              ├─ 文本 Caption / 续表声明扫描
              ├─ Tagged PDF / PDF/UA 语义结构树检测
              ├─ pdf-inspector 分类与 Markdown 表格定位
              ├─ 全图扫描页 / 图像密集页检测
              ├─ 矢量线框与文本表格全篇自适应探测 (有绘图页线框未检出时继续跑文本与离散列探测)
              └─ 跨页续表多模态追溯 (关键词/纯数据密度/网格线/图片/无框稀疏多列对齐)
              │
              ├──────────────────────┬──────────────────┐
              │                      │                  │
           Native                 Scanned             Mixed
              │                      │                  │
              ▼                      ▼                  ▼
      生产主抽取管线:           DocLayout-YOLO      主抽取(文本页)
      1. Tagged PDF 结构树     + PP-StructureV3   + OCR/VLM(扫描页)
         (Fast-path 快速通道)    局部切图与补扫     (首选 PaddleOCR-VL
      2. MinerU 结构化主抽取    (首选 PaddleOCR-VL  离线 Docling TableFormer)
         (含 page_ranges 选页)   离线 Docling)
      3. 本地数值完整性门禁
         (table_validator 校验)
              │
              ├─ 门禁未通过/页面未覆盖/启用 legacy ──► 定向局部修复与 8 引擎投票备用管线:
              │    (pdf-inspector + PyMuPDF4LLM + find_tables + text_align
              │     + pdfplumber + Camelot + GMFT (TATR) + img2table + RapidTable)
              │
              ├─ 2D bbox 空间重叠 (IoU/IoM) + 表号标签防冲突聚类，杜绝同页多表误合并
              ├─ IBM TEDS (树编辑距离拓扑度量) + 矩阵相似度加权判定一致性 (结构树优先)
              ├─ 严格质量门禁 (_should_replace_with_ocr)：严禁少行少列 OCR 替换非退化原生表
              ├─ 表级精细对齐与替换策略：精准替换瑕疵表，保留同页未受干扰的有效原生表并正确标记已处理
              ├─ 7 阶轻量直通安全后处理（公式注入防御、数值类型推断、多级表头展平、DeepDoc/MinerU 启发式对齐）
              ├─ 参考 olmOCR 2 设计的边界与尾注分离启发式：隔离表题/数据矩阵/尾注 (* p < 0.05, Note: ..., 注: ...)
              └─ 跨页续表自适应合并（智能解析描述性续表标题，严格排除虚假表号冲突）
              │
              ▼
         Excel 导出 (openpyxl CellRichText 学术上下标与化学式保真，补漏合并写入原子替换) ──► 可选自动回写挂载至 Zotero & Better-Notes 笔记
```

## 关键组件（多模态与多引擎协同体系）

| 组件 | 类别与作用 | 说明 |
|---|---|---|
| [`arXiv HTML (LaTeXML)`](https://arxiv.org/html/) | 原生网络解析 | arXiv 官方 HTML 与 ar5iv 结构化表格原生抓取，免除 OCR 误差 |
| [`NCBI PMC (JATS XML)`](https://eutils.ncbi.nlm.nih.gov/) | 原生网络解析 | PubMed Central 开放获取 JATS XML 原生全文字段解析，含完整 colspan/rowspan 与尾注 |
| [`pdf-inspector`](https://github.com/firecrawl/pdf-inspector) | 外部库调用 | Rust 库，PDF 分类 + 表格定位 + Markdown 结构化提取，~80ms 高性能 |
| [`PyMuPDF4LLM`](https://github.com/pymupdf/PyMuPDF4LLM) | 外部库调用 | Artifex 出品，原生将 PDF 转化为 LLM-ready Markdown 表格，8 引擎投票主力候选方 |
| `PyMuPDF find_tables()` | 外部库调用 | PDF 矢量线框与无框文本表格提取（零 OCR，高保真），支持全篇网格线分析 |
| [`IBM TEDS`](https://github.com/ibm-aur-nlp/PubTabNet) | 评测度量实现 | 树编辑距离拓扑相似度算法与标准评估类，完整保留 colspan/rowspan 拓扑对比 |
| [`GMFT`](https://github.com/conjuncts/gmft) | 外部库适配 | 基于 TATR (Table Transformer) 的深度视觉提取，擅长多层复合表头与跨列单元格对齐 |
| [`img2table`](https://github.com/xavctn/img2table) | 外部库适配 | 基于 OpenCV 形态学的纯 Python 方案，已统一 200 DPI 像素坐标至 72 DPI PDF 点坐标 |
| `text_alignment` | 本地算法实现 | 无线框表格的文本坐标对齐与自适应重组（支持学位论文/无网格线表） |
| `Camelot` | 外部库调用 | 基于 OpenCV 的晶格 (lattice) 与流式 (stream) 结构化表格提取 |
| `pdfplumber` | 外部库调用 | 基于字符精细包围盒与视觉线条的表格抽取，参与多方交叉验证 |
| `PaddleOCR-VL-1.6` / `PP-StructureV3` | 在线 API / 切图 | 在线多模态大模型与版面切图 API，复杂/跨行跨列与扫描页结构化多模态解析（首选在线主引擎） |
| [`IBM Docling (TableFormer)`](https://github.com/DS4SD/docling) | 外部库适配 | IBM 开源高保真表格结构模型，按页面文字层动态启闭 OCR，保留 HTML 结构并归一化坐标 |
| [`RapidAI RapidTable`](https://github.com/RapidAI/TableStructureRec) | 外部库适配 | 适配官方接口与 `pred_htmls`，支持 SLANet-Plus 与 UniTable，作为多方投票参选方 |
| `PDF-Extract-Kit 启发式对齐` | 思想借鉴与本地实现 | 参考 PDF-Extract-Kit 设计思路的轻量启发式：行起始 x 分布 + K-Means 列边界自适应聚类 |
| [`PP-DocLayoutV3`](https://github.com/PaddlePaddle/PaddleOCR) | 本地 ONNX 适配 | 25 类细粒度版面识别，真实 ONNX 推理并与 YOLO 双检融合互补 |
| `DocLayout-YOLO` | 本地 ONNX 推理 | 本地超快切图定位，支持与 PP-DocLayoutV3 双检融合 |
| `olmOCR 2 边界与尾注分离` | 思想借鉴与本地实现 | 参考 olmOCR 设计思想的学术表格边界与注释行剥离算法，将 `* p < 0.05` / `Note:` 等抽离存入 `footnotes` 属性 |
| `openpyxl CellRichText` | 本地格式渲染 | 原生 Excel 单元格富文本渲染，高保真呈现 `10^{-3}`、`CaCO_3`、`H_2O` 等学术上下标 |
| `MinerU 4-Way 跨页续表判定` | 思想借鉴与本地实现 | 参考 MinerU/DeepDoc 启发式多维判定：列名 Jaccard + 页宽比 + 表头一致性 + (续)标记命中 |
| `MinerU Open API CLI 集成` | 官方生态 CLI 赋能 | 集成官方 `mineru-open-api`，一键代管文档上传、异步轮询、结果下载与解压，双引擎智能调度，无 CLI 时优雅平滑回退至纯 Python REST 客户端 |
| [`OmniDocBench (CVPR 2025)`](https://github.com/opendatalab/OmniDocBench) | 评测度量体系 | 标准化表格 TEDS、表格检测 mAP (严格 PR 曲线面积) 与端到端召回率评测 |
| `LLM 语义化表格评测` | 本地评估算法 | 单元格数值完整性、表头语义一致性与元组关联性判分，用于大模型修复验收与语义等价判定 |
| `OCR-Hub 后端注册表与多云容灾` | 框架机制 | 可插拔统一后端（PaddleOCR-VL / MinerU 2.5 / TeleOCR / Docling / RapidTable），动态能力发现与健康探测 |
| `SMART 语义表头展平` | 本地算法实现 | 伪代码推理与层级路径格式 (`Category > Subcategory > Field [Unit]`)，保留完整上下文血缘 |
| [`PyZotero & Better-Notes`](https://github.com/urschrei/pyzotero) | 生态联动扩展 | 生态联动扩展（默认关闭），自动将导出 Excel 挂载为附件并生成 Better-Notes 概览卡片 |



## 漏检自查与完整性保障 (Self-Audit)

为杜绝文献表格漏检、误剔与劣退，本技能在管线全生命周期内构建了严密的多重自查闭环：

1. **表号声明预期清单扫描 (`Declaration Ledger`)**：
   - 提取前轻量扫描 PDF 全文（含正文与附录，全篇扫描无页数人为截断），识别标准表号声明 (`Table 1`, `表1`, `Tab. 1`, `Table S1`, `TABLE III` 等) 与跨页续表标记 (`表1（续）`, `Table 1 (cont.)`, `Table 2 Continued`, `cont'd`, `续上表`)。
   - 维护全文预期表号集合与出现频次账本，确保声明表号与提取产物一一闭环对应。
2. **候选页动态探测与补漏机制**：
   - 当声明表格数超出初步候选页总数时，自动启动全文回扫补齐缺失候选页；
   - 对无绘图/矢量线框页面 (`has_drawings=False`)，自动通过文本策略 (`strategy="text"`) 及无线框对齐探测潜在文本表；
   - 当单页出现多个表题声明时，放宽候选页表格配额，支持单页提取多张独立表格。
3. **质量对比防劣退门禁 (`_should_replace_with_ocr`)**：
   - OCR 结果不能盲目替换原生表格：替换前严格对比行数、非空单元格填充率与低质量标志；
   - 若 OCR 结果为空、行数严重萎缩或质量劣于原生解析，坚决拒绝替换，保留原生表格作为降级兜底。
4. **单行/单列与章节碎片保护**：
   - 管道全流程（质量检验、多方投票、后处理、Excel 导出）完整支持单行表（如基线汇总、参数对照）与单列表（如指标序列）；
   - 在多级章节文献中，无表题的零散表格碎片统一导出为 `unlabelled_N.xlsx`，杜绝静默丢弃。

## 技能目录结构

```text
zotero-table-extractor/
├── SKILL.md                         # 技能说明文档
├── update.py                        # 🔄 技能在线更新与个人配置绝对防护主入口
├── setup_paths.py                   # 🛠 跨平台路径与环境交互式初始化配置向导
├── config.json                      # 运行期配置 (含 API Keys，受 .gitignore 保护勿提交)
├── config.example.json              # 配置模板
├── figures/                         # 技能超清架构图 (draw.io 工程源文件及 PNG/SVG 导出)
│   ├── zotero_table_extractor_architecture.drawio
│   ├── zotero_table_extractor_architecture.png
│   └── zotero_table_extractor_architecture.svg
├── models/                          # 本地离线 ONNX 检测模型
│   ├── doclayout_yolo_docstructbench_imgsz1280_2501.onnx
│   └── doc_ori.onnx                 # TurboOCR PP-LCNet 页面与文本方向模型 (~6.47MB)
├── scratch/                         # 临时诊断脚本与测试套件
└── scripts/                         # 核心提取代码库
    ├── updater.py                   # 🛡️ 在线安全更新与智能配置迁移/回滚引擎
    ├── setup_paths.py               # 🛠 路径配置向导入口薄壳
    ├── extract_zotero_table.py      # CLI 主入口（支持单篇、目录批量或 .txt 路径清单）
    ├── pdf_table_extractor.py       # 本地 PDF 提取管线（分类+多方投票+两阶段OCR接管）
    ├── pdf_tables.py                # 表格切图定位与本地/结构化导出回退
    ├── ocr_client.py                # PP-StructureV3 & PaddleOCR-VL 在线客户端
    ├── table_postprocess.py         # 7 阶表格后处理流水线（公式转义/类型推断/续表合并）
    ├── excel_export.py              # Excel 规范化导出（格式居中/自适应列宽/元数据拦截）
    ├── models.py                    # 统一领域模型 (Table IR: ExtractedTable & TableCell)
    ├── system_detector.py           # 跨平台系统环境探测与路径自适应（macOS/Windows）
    ├── env_detector.py              # 环境探测兼容包装层（system_detector 的向后兼容别名）
    ├── common.py                    # 共享工具函数与 Markdown 表格语法判定
    ├── batch_planner.py             # 批量元数据与年份预分析（生成执行计划）
    ├── batch_run.py                 # 批量并发提取总控（并行调度/断点续传）
    ├── verify_tables.py             # 表格产物批量校验与空表/坏表清理
    ├── login.py                     # 期刊出版社 Cookie 注入与交互式登录向导
    ├── llm_reasoner.py              # LLM 辅助纠错与表格语义重构
    ├── zotero_sync.py               # 🔄 PyZotero 与 Better-Notes 生态联动（附件挂载与笔记生成）
    ├── agent_bridge.py              # Agent 交互桥接与进度事件通知
    ├── doclayout_yolo_detector.py   # YOLO 本地版面检测推理器
    ├── doc_orientation_detector.py  # TurboOCR 方向检测与双轨两阶旋转自愈器
    ├── pdf_page_filter.py           # 页面轻量启发式预过滤（跳过纯正文无表页）
    ├── rapid_table_extractor.py     # RapidAI TableStructureRec + RapidTable 本地双模型表格结构识别引擎
    ├── omnidocbench.py              # OmniDocBench (CVPR 2025) 表格与版面标准化评测度量体系
    ├── table_validator.py           # 表格结构完整性与列数/填充率校验器
    ├── export_drawio_diagram.py     # 原生 draw.io 矢量架构图自动化渲染与导出
    ├── audit/                       # 质量审查与诊断工具包统一导出命名空间
    │   └── __init__.py              # 审查诊断子包导出
    ├── audit_reporter.py            # 全面审查诊断报告生成器
    ├── targeted_audit_reporter.py   # 针对性异常排查报告生成器
    ├── exhaustive_audit_runner.py   # 全库地毯式提取质检执行器
    ├── targeted_audit_runner.py     # 针对特定异常样例文献的专项回归测试器
    ├── deep_8h_iterative_healer.py  # 深度迭代自愈与异常自动修复执行器
    ├── compat/                      # 跨版本兼容性包装层
    └── online/                      # 在线 HTML 提取子包
        ├── __init__.py
        ├── doi_resolver.py          # DOI/arXiv/URL 解析与发布商域名分流
        ├── strategies.py            # 多出版社直连 API 与 Scrapling 爬取策略
        ├── general_html_extractor.py# 通用出版社 Playwright 动态渲染抓取器
        ├── cnki_html_extractor.py   # 中国知网 (CNKI) 自动化滑块绕过与 HTML 阅读提取器
        └── graph.py                 # LangGraph / DAG 多策略并发竞速流水线
```

## CLI 使用说明

### 1. 单篇论文表格提取 (`extract_zotero_table.py`)

```bash
# 基础用法：提取指定 PDF 并保存到指定目录或 Excel 文件
python scripts/extract_zotero_table.py --pdf /path/to/paper.pdf --output /path/to/output_dir

# 保存为单文件多 Sheet 格式，并开启 LLM 复杂表头语义修复
python scripts/extract_zotero_table.py --pdf /path/to/paper.pdf --output /path/to/tables.xlsx --single-file --enable-llm

# 仅走本地 PDF 提取，跳过在线 HTML 抓取
python scripts/extract_zotero_table.py --pdf /path/to/paper.pdf --output /path/to/output_dir --pdf-only

# 仅走在线 HTML 抓取，不回退本地 PDF
python scripts/extract_zotero_table.py --pdf /path/to/paper.pdf --output /path/to/output_dir --online-only

# 批量提取目录中的所有 PDF 或 .txt 清单
python scripts/extract_zotero_table.py --pdf /path/to/pdfs_dir --output /path/to/all_tables --workers 4
```

| 参数 | 类型 | 默认值 | 作用说明 |
| :--- | :--- | :--- | :--- |
| `--pdf` | 字符串 | 必须 | 待提取 PDF 文件路径、PDF 目录或包含 PDF 路径的 `.txt` 清单 |
| `--output` | 字符串 | 必须 | 导出结果目录路径；当 `--single-file` 时可为目标 `.xlsx` 文件路径 |
| `--single-file` | 标志 | `False` | 将提取到的所有表格汇聚写入单个 Excel 文件（多个 Sheet） |
| `--enable-llm` | 标志 | `False` | 启用大模型语义分析，纠正断裂列名与深层多级复合表头 |
| `--table-idx` | 字符串 | `"all"` | 提取指定索引表格：`"all"`, `"first"`, 或具体 0-based 整数序号 |
| `--pdf-only` | 标志 | `False` | 跳过在线抓取竞速，直接从本地 PDF 运行多方投票提取 |
| `--online-only` | 标志 | `False` | 仅抓取在线 HTML/XML 表格，不执行本地 PDF 提取回退 |
| `--skip-supplementary` | 标志 | `False` | 跳过 Supplementary Material 附录表格 |
| `--workers` | 整数 | `4` | 目录批量提取时的并发工作线程数 |
| `--headers` | 字符串 | `None` | 自定义强制表头列名（逗号分隔） |
| `--sync-zotero` | 标志 | `False` | 启用 PyZotero 与 Better-Notes 生态联动（自动挂载 Excel 并生成预览卡片） |
| `--zotero-api-key` | 字符串 | `None` | 覆盖 Zotero Web API 访问密钥 |
| `--zotero-user-id` | 字符串 | `None` | 覆盖 Zotero 用户/库 ID |
| `--ocr-engine` | 字符串 | `"auto"` | OCR 引擎首选项：`"auto"`, `"paddleocr"`, `"docling"`, `"mineru"` |

> [!NOTE]
> **新引擎与生态联动配置项 (`config.json`)**：
> - `DOCLING_ENABLED` (布尔, 默认 `true`): 启用 IBM Docling (TableFormer) 深度表格结构提取引擎。
> - `DOCLING_OFFLINE_FALLBACK` (布尔, 默认 `true`): 当首选云端主引擎 (PP-StructureV3 / PaddleOCR-VL) 队列已满、网络受阻或无 Token 时，无缝启动本地 Docling 离线备用降级。
> - `GMFT_ENABLED` (布尔, 默认 `true`): 启用 GMFT (TATR-based 结构 Transformer) 作为原生 PDF 多方投票的权威参选方，专精多层表头与跨列合并。
> - `IMG2TABLE_ENABLED` (布尔, 默认 `true`): 启用 img2table 纯 Python OpenCV 提取方案，完全解耦外部 Ghostscript 依赖。
> - `ZOTERO_SYNC_ENABLED` (布尔, **默认关闭 `false`**): 可选生态联动总开关。开启后自动反向追溯本地条目 Key，将导出的 `.xlsx` 作为子附件挂载至 Zotero 条目，并生成符合 Zotero-Better-Notes 规范的独立富文本概览笔记。


### 2. 批量并发总控 (`batch_run.py`)

```bash
# 断点续传处理未完成的任务
python scripts/batch_run.py --resume --workers 4

# 重新复检已有产物文件夹，补齐遗漏表格
python scripts/batch_run.py --recheck --workers 2

# 纯离线纯 PDF 批量提取
python scripts/batch_run.py --pdf-only --workers 4
```

| 参数 | 类型 | 默认值 | 作用说明 |
| :--- | :--- | :--- | :--- |
| `--resume` | 标志 | `False` | 断点续跑：跳过已有产物目录中提取成功的论文任务 |
| `--recheck` | 标志 | `False` | 复检模式：针对已有产物重新校验并补齐缺失表格 |
| `--workers` | 整数 | `4` | 并发处理线程数 |
| `--pdf-only` | 标志 | `False` | 批量运行时跳过在线网络抓取，纯本地 PDF 提取 |
| `--timeout` | 整数 | `300` | 单篇文献处理超时上限（秒） |
| `--limit` | 整数 | `0` | 限制处理文献篇数（0 表示全量执行） |

### 3. 技能在线安全更新与配置防护 (`update.py` / `setup_paths.py --update`)

```bash
# 一键在线安全更新（支持 Git 与 Release 归档双模，严格保留个人 API Keys 与自定义路径）
python update.py

# 仅检查远端更新与本地缺失的新配置项（只读探测，不改动文件）
python update.py --check

# 演练模式模拟更新全流程（不产生磁盘写入）
python update.py --dry-run

# 一键回滚至最近一次备份的配置状态
python update.py --rollback

# 查看所有历史快照清单
python update.py --list-backups

# 仅将 config.example.json 最新模板项安全并入 config.json（不拉取代码）
python update.py --merge-only

# 以结构化 JSON 输出结果（专为 Agent 与自动化流程设计）
python update.py --check --json
```

| 参数 | 类型 | 默认值 | 作用说明 |
| :--- | :--- | :--- | :--- |
| `--check` | 标志 | `False` | 仅检测更新状态与缺失字段，不执行任何写入 |
| `--dry-run` | 标志 | `False` | 模拟演练更新全流程，不修改文件 |
| `--rollback` | 字符串 | `"latest"` | 回滚 config.json 至最近或指定 ID 的快照 |
| `--list-backups` | 标志 | `False` | 列出所有可用的历史配置快照 |
| `--merge-only` | 标志 | `False` | 仅将 config.example.json 中的新项安全并入 config.json |
| `--force` | 标志 | `False` | 强制拉取更新并重置跟踪代码文件 |
| `--install-deps` | 标志 | `False` | 更新后自动运行 pip install -r requirements.txt |
| `--json` | 标志 | `False` | 以结构化 JSON 输出结果，便于 Agent 与脚本集成 |

## 配置项（config.json）

| 配置 Key | 类型 | 默认值 / 示例 | 说明 |
| :--- | :--- | :--- | :--- |
| `FIRECRAWL_API_KEY` | string | `""` | Firecrawl 爬取 API Key |
| `ELSEVIER_API_KEY` | string | `""` | Elsevier 机构官方 API Key |
| `CNKI_STRATEGY` | string | `"auto"` | 知网提取策略 (`auto`, `scrapling`, `playwright`) |
| `PLAYWRIGHT_HEADED` | boolean | `false` | 是否以有头模式启动 Playwright 浏览器 |
| `PLAYWRIGHT_USER_DATA_DIR` | string | `""` | 自定义 Playwright 用户会话目录（自动持久化 Cookie） |
| `PLAYWRIGHT_EXECUTABLE_PATH` | string | `""` | 自定义系统 Chrome/Chromium 可执行文件路径 |
| `PADDLEOCR_MCP_MODEL` | string | `"PaddleOCR-VL-1.6"` | PaddleOCR 远端模型名称 |
| `PADDLEOCR_MCP_PPOCR_SOURCE` | string | `"aistudio"` | PP-OCR 运行来源 (`aistudio` / `local`) |
| `PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN` | string | `""` | 百度 AIStudio Access Token |
| `UV_INDEX_URL` | string | 清华源镜像 | Python 包安装索引镜像源 |
| `FIRECRAWL_MAX_AGE_MS` | int | `86400000` | Firecrawl 页面缓存生命周期 (毫秒) |
| `FIRECRAWL_PAGE_TIMEOUT_MS` | int | `30000` | Firecrawl 单页请求超时时间 |
| `CRAWL4AI_CDP_URL` | string | `""` | Crawl4AI CDP 远程调试连接地址 |
| `DOCLAYOUT_YOLO_ENABLED` | boolean | `true` | 是否启用本地 DocLayout-YOLO 离线检测 |
| `DOCLAYOUT_MODEL_DIR` | string | `"./models"` | YOLO 模型权重存放目录 |
| `DOCLAYOUT_CONF_THRESHOLD` | float | `0.25` | 目标检测框置信度阈值 |
| `PP_STRUCTURE_MODEL` | string | `"PP-StructureV3"` | PP-Structure 在线定位版面模型名称 |
| `USE_PP_STRUCTURE` | boolean | `true` | 是否启用 PP-Structure 作为表格前置粗定位 |
| `LLM_ENABLED` | boolean | `false` | 是否开启 LLM 复杂语义推断与表格清洗 |
| `LLM_API_BASE` | string | OpenAI 兼容地址 | LLM API 服务端点 |
| `LLM_API_KEY` | string | `""` | LLM 鉴权密钥 |
| `LLM_MODEL` | string | `"gpt-4o-mini"` | 语义处理选用的 LLM 模型 |
| `LLM_TIMEOUT` | int | `60` | LLM 调用超时秒数 |
| `DOCLING_ENABLED` | boolean | `true` | 是否启用 IBM Docling (TableFormer) 深度表格提取引擎 |
| `DOCLING_OFFLINE_FALLBACK` | boolean | `true` | 是否启用 Docling 作为 PaddleOCR-VL 离线备用降级引擎 |
| `GMFT_ENABLED` | boolean | `true` | 是否启用 GMFT (TATR Transformer) 参与 Native PDF 多方投票 |
| `IMG2TABLE_ENABLED` | boolean | `true` | 是否启用 img2table 纯 OpenCV 方案参与 Native PDF 多方投票 |
| `OCR_ENGINE` | string | `"auto"` | 首选 OCR 引擎 (`auto`, `paddleocr`, `docling`, `mineru`) |
| `ZOTERO_SYNC_ENABLED` | boolean | `false` | 是否启用 PyZotero 与 Better-Notes 生态联动（默认严格关闭） |
| `ZOTERO_API_KEY` | string | `""` | Zotero 官方 Web API 访问密钥 |
| `ZOTERO_USER_ID` | string | `""` | Zotero 用户 ID 或群组 ID |
| `ZOTERO_LIBRARY_TYPE` | string | `"user"` | Zotero 库类型 (`user` 或 `group`) |
| `ZOTERO_BETTER_NOTES_ENABLED` | boolean | `true` | 同步时是否生成 Better-Notes 规范的富文本预览笔记 |
| `ZOTERO_AUTO_ATTACH_XLSX` | boolean | `true` | 同步时是否将导出的 Excel 文件作为子附件挂载至对应条目 |
| `RAPID_TABLE_ENABLED` | boolean | `true` | 是否启用 RapidAI RapidTable 本地 ONNX 结构识别引擎参与多方投票 |
| `DOCLAYOUT_ENGINE` | string | `"ensemble"` | 本地版面检测引擎选择 (`yolo`, `pp_doclayoutv3`, `ensemble`) |
| `PP_DOCLAYOUT_ENABLED` | boolean | `true` | 是否启用 PP-DocLayoutV3 25 类细粒度版面识别与多边形贴合 |
| `MINERU_TABLE_MERGE_ENABLE` | boolean | `true` | 是否启用 MinerU 4-way 联合判定与小表保护跨页续表合并总开关 |
| `MINERU_USE_CLI` | string | `"auto"` | MinerU 执行方式：`"auto"` (优先 CLI，失败自动回退 REST API), `"cli"` (仅 CLI), `"api"` (仅 Python REST) |
| `MINERU_CLI_PATH` | string | `""` | 可选自定义 `mineru-open-api` 二进制路径（为空时自动探测 PATH/NVM/Go 安装路径） |
| `MINERU_TOKEN` | string | `""` | MinerU API Token（与 `MINERU_API_KEY` 等价，亦支持读取 `~/.mineru/config.yaml`） |
| `TELEOCR_API_BASE` | string | `""` | TeleOCR vLLM / OpenAI-compatible 视觉推理服务 URL |
| `TELEOCR_MODEL` | string | `"TeleOCR"` | TeleOCR 模型标识符 |
| `OMNIDOCBENCH_ENABLED` | boolean | `true` | 是否开启 OmniDocBench (CVPR 2025) 标准化度量自检 |
| `SMART_HEADER_FLATTEN_ENABLED` | boolean | `true` | 是否启用 SMART 多级表头语义展平与伪代码推理模式 |

## 目录与版本控制规范

- **敏感凭据与会话隔离**：
  - `config.json` 包含私有 API 密钥，受 `.gitignore` 保护，严禁入库；请通过复制 `config.example.json` 生成本地私有配置；
  - `profiles/` 目录为运行时浏览器登录生成的临时/私有用户数据目录，内含会话 Cookie 与凭证，受 `.gitignore` 保护，严禁打包入库。
- **产物与缓存**：
  - `figures/` 专用于存放项目导出的高分辨率架构图素材；
  - `scratch/` 专用于临时测试与诊断数据脚本，受 `.gitignore` 保护；
  - `models/` 下的 ONNX 权重文件支持运行时按需下载与本地缓存。
