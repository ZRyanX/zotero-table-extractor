---
name: zotero-table-extractor
description: 从 Zotero 选中条目（依赖 ai4paper-zotero MCP 获取物理路径）或指定本地 PDF / 远程 PDF / 期刊网页 / CNKI 中单篇或批量提取结构化科学表格。采用「在线 HTML/XML 分层竞速（arXiv/PMC JATS/Elsevier XOCS/CNKI/CDP 动态端口与 Port Guard/OA 门控 Firecrawl/同伴凭据协同）+ 本地 PDF 主抽取（TurboOCR 无损旋转自愈 + Tagged PDF 结构树 + MinerU CLI/API + 本地数值门禁）+ 9 引擎投票与 IBM TEDS 仲裁 + DocLayout-YOLO/PP-DocLayoutV3 + RapidTable/PaddleOCR-VL-1.6/TeleOCR/Docling 离线兜底 + SMART 语义表头展平 + 跨页续表 4-Way/DP 合并 + LLM 语义等价门禁 + CellRichText 上下标导出 + 可选 PyZotero/Better-Notes 联动」，导出出版级 Excel (.xlsx)。
---

# Zotero Table Extractor

从 Zotero PDF 附件、本地 PDF、远程 PDF URL 或学术期刊网页中自动识别、核验并提取结构化表格，导出为出版级 Excel (`.xlsx`) 文件。

> **💡 Agent 架构说明**：  
> 当用户要求“提取 Zotero 选中的这篇论文”时，由 Agent 编排层通过 `ai4paper-zotero` MCP 工具（`zotero_get_selected_items`）获取选中条目的物理 PDF 路径（及可选 DOI/URL/标题），再传递给本技能 CLI（`--pdf <path>`）执行底层提取。

## 提取管线

```text
论文.pdf / 远程 PDF URL / 论文网页 URL
       │
       ├─ 0. 预检门控与无损预处理
       │     ├─ 2020 年前中文文献门控 (is_pre_2020_chinese_paper) → 直通本地 PDF 管线
       │     ├─ 远程 PDF 流式下载与 %PDF- 魔数校验 (_download_remote_pdf，拒收 HTML 错存)
       │     └─ Tier-1 双轨无损页面旋转自愈 (doc_orientation_detector: 矢量方向 + doc_ori.onnx)
       │          ├─ Native 旋转页：无损重写 /Rotate，保留矢量文本层与 Tagged 结构树
       │          └─ 纯扫描旋转页：150 DPI 旋转烘焙 + Tier-2 切图正向矫正
       │
       ├─ 1. 在线 HTML / XML 分层竞速（默认 race=True，--sequential 可切换串行 DAG）
       │     ├─ Tier-1 (HTTP/API 竞速，权威全文优先礼让窗口):
       │     │     arXiv HTML / ar5iv (LaTeXML) · NCBI PMC (JATS XML)
       │     │     Elsevier XOCS XML (X-ELS-APIKey + 可选 X-ELS-InstToken)
       │     │     DirectHTTP → ImpersonatedHTTP (curl_cffi) · Unpaywall (DirectHTTP 优先并跳过 PDF)
       │     ├─ Tier-2 (云/独立浏览器竞速):
       │     │     Crawl4AI · Firecrawl (仅 OA 门控 + 403 熔断) · Scrapling (有界会话 + cookie 注入)
       │     ├─ Tier-3 (共享浏览器与反风控):
       │     │     Playwright (DevToolsActivePort 动态 CDP 探测 + Port Guard + 6 槽位跨进程锁
       │     │     + 慢脚本拦截 + BrowserSkill(bsk) 兜底 + 出版社 DOM 适配器 + 30s/20s 附表有界提取)
       │     │     CNKI 专线：ScraplingCNKI ∥ Crawl4AICNKI → PlaywrightCNKI (含 chndoi.org 动态重路由)
       │     ├─ Tier-4 (学术检索尾流): Europe PMC / Crossref 预印本 / PaperFetch / BrowserAct
       │     └─ 完备性核验：比对 PDF 全文预期表号集合与扫描页线索；
       │        若完整 → 单次原子落盘直接完成；若缺失/截断 → 暂存高保真在线表，交由本地 PDF 补漏并互补合并
       │
       └─ 2. 本地 PDF 深度提取管线
              │
              ▼
         多信号候选表格页挖掘（高覆盖防漏检，全篇无页数截断）
              ├─ 全文表号与续表声明账本 (scan_pdf_table_declarations / Declaration Ledger)
              ├─ Tagged PDF / PDF/UA 语义结构树检测 (pdf-inspector)
              ├─ pdf-inspector 分类与 Markdown 表格定位 (~5-80ms)
              ├─ 全图扫描页 / 图像密集页检测
              ├─ 分块自适应矢量 find_tables() + find_tables(strategy="text") + probe_disjoint_native_tables()
              └─ 5 维跨页续表追溯 (续表词法 / 密集纯数据 / 矢量网格 / 纯图延续 / 稀疏几何列对齐)
              │
              ├──────────────────────┬──────────────────┐
              │                      │                  │
           Native                 Scanned             Mixed
              │                      │                  │
              ▼                      ▼                  ▼
      生产主抽取管线:           PaddleOCR-VL-1.6   主抽取(文本页)
      1. Tagged PDF 结构树     整页多模态解析     + 视觉/OCR(扫描页)
         (Fast-path 快速通道)  + Docling 离线备用  (纯图页自动豁免矢量过滤)
      2. MinerU 结构化主抽取
         (CLI/REST 双模 + flash-extract + DoH CDN 自愈 + page_ranges 子集页码重映射)
      3. 本地双向数值完整性门禁 (table_validator + 0-1000/points 坐标归一化)
              │
              ├─ 门禁未通过 / 页面未覆盖 / USE_LEGACY_VOTING=true
              │    ──► 定向局部修复与 9 引擎多方投票备用管线 (three_way_vote):
              │        (pdf-inspector [MD+结构树] + PyMuPDF4LLM + find_tables + text_alignment
              │         + pdfplumber + Camelot + GMFT [TATR] + img2table + RapidTable)
              │        • 2D bbox 空间重叠 (IoU/IoM) + 表号防冲突聚类 + IBM TEDS 拓扑仲裁
              │
              ├─ 3 阶视觉 AI 接管修复（仅对未通过校验/漏表页）:
              │    ① RapidTable 本地 ONNX 快速挽救
              │    ② DocLayout-YOLO + PP-DocLayoutV3 双检集成 / PP-StructureV3 切图
              │       → PaddleOCR-VL-1.6 / TeleOCR (vLLM) (含 Tier-2 旋转纠偏)
              │    ③ IBM Docling (TableFormer) 本地 CPU/Metal 离线接管
              │    • 严格质量防劣退门禁 (_should_replace_with_ocr) 与表级精细替换
              │
              ├─ 7 阶轻量安全后处理 (table_postprocess.py)
              │    (MultiIndex 保护 / 碎片过滤 / SMART 语义表头展平 / 挤压展开
              │     / 公式注入防御与类型推断 / olmOCR 2 尾注剥离 / 表号与双语标题规范化)
              ├─ 跨页续表 4-Way 联合判定与 DeepDoc/MinerU 3-Way 单调 DP 列对齐合并
              │    (无法置信对齐时保留独立片段；与在线表格按标准化主键无损互补合并)
              ├─ 可选 LLM 语义重构与等价性门禁 (arXiv 2603.18652，支持无 Key 本地服务端点)
              │
              ▼
         Excel 导出 (openpyxl CellRichText 学术上下标与化学式保真，_v2/_v3 防撞原子写入)
              │
              ▼
         可选自动回写挂载至 Zotero & Better-Notes 富文本预览笔记 (--sync-zotero)
```

## 关键组件（多模态与多引擎协同体系）

| 组件 | 类别与作用 | 说明 |
|---|---|---|
| [`arXiv HTML (LaTeXML)`](https://arxiv.org/html/) | 原生网络解析 | arXiv 官方 HTML 与 ar5iv 结构化表格原生抓取，免除 OCR 误差 |
| [`NCBI PMC (JATS XML)`](https://eutils.ncbi.nlm.nih.gov/) | 原生网络解析 | PubMed Central 开放获取 JATS XML 原生全文字段解析，含完整 colspan/rowspan 与尾注 |
| `Elsevier XOCS XML` | 原生网络解析 | 通过 `X-ELS-APIKey` 与可选 `X-ELS-InstToken` 直取原生 XML，支撑 PII→DOI 反查 |
| `CDP 动态端口与 Port Guard` | 在线反风控 | 扫描 Chromium `DevToolsActivePort` 内存复用校园网认证，拦截 localhost 端口探测并实施 6 槽位并发控制 |
| [`pdf-inspector`](https://github.com/firecrawl/pdf-inspector) | 外部库调用 | Rust 库，PDF 分类 (~5-10ms) + Tagged 结构树解析 + Markdown 表格定位与提取 |
| [`PyMuPDF4LLM`](https://github.com/pymupdf/PyMuPDF4LLM) | 外部库调用 | Artifex 出品，原生将 PDF 转化为 LLM-ready Markdown 表格，9 引擎投票候选方 |
| `PyMuPDF find_tables()` | 外部库调用 | PDF 矢量线框与无框文本 (`strategy="text"`) 表格提取，支持适配 `rotation_matrix` |
| [`IBM TEDS`](https://github.com/ibm-aur-nlp/PubTabNet) | 评测度量实现 | Zhang-Shasha 树编辑距离拓扑相似度算法 (`scripts/teds.py`)，完整保留 colspan/rowspan 拓扑对比 |
| [`GMFT`](https://github.com/conjuncts/gmft) | 外部库适配 | 基于 TATR (Table Transformer) 的深度视觉提取，擅长多层复合表头与跨列单元格对齐 |
| [`img2table`](https://github.com/xavctn/img2table) | 外部库适配 | 基于 OpenCV 形态学的纯 Python 方案，统一 200 DPI 像素坐标至 72 DPI PDF 点坐标，免 Ghostscript |
| `text_alignment` | 本地算法实现 | 无线框表格的文本坐标对齐与 PDF-Extract-Kit 启发式 K-Means 列边界聚类 |
| `Camelot` | 外部库调用 | 基于 OpenCV 的 Lattice 晶格与 Stream 流式双策略提取（含流式包含过滤） |
| `pdfplumber` | 外部库调用 | 基于字符精细包围盒与视觉线条的表格抽取，参与多方交叉验证 |
| `MinerU (CLI + REST API)` | 结构化主抽取 | 集成 `mineru-open-api` CLI 与 Python REST 双模调度、零 Token `flash-extract`、DoH 弹性 CDN 重连与子集页码重映射 |
| `PaddleOCR-VL-1.6` / `PP-StructureV3` | 在线 API / 切图 | AIStudio 在线版面切图与多模态大模型结构化解析（支持按本地方向器可用性动态控制云端方向分类） |
| `TeleOCR` | 在线/私有 VLM | vLLM / OpenAI-compatible 视觉推理网关，接入统一 `OCRBackendRegistry` 多云容灾 |
| [`IBM Docling (TableFormer)`](https://github.com/DS4SD/docling) | 外部库适配 | IBM 开源高保真表格结构模型，纯 CPU/Metal 本地离线备用兜底，支持离线 HuggingFace 缓存探测 |
| [`RapidAI RapidTable`](https://github.com/RapidAI/TableStructureRec) | 外部库适配 | 适配 `TableCls` 有线/无线分流、`SLANet-Plus` (ONNX) 与 `UniTable` (PyTorch)，兼具投票候选与本地快速挽救层 |
| `DocLayout-YOLO` & [`PP-DocLayoutV3`](https://github.com/PaddlePaddle/PaddleOCR) | 本地 ONNX 集成 | DocLayout-YOLO (1280px ONNX) + PP-DocLayoutV3 (25 类多边形实例分割) 双检集成 (`ensemble`)，macOS 优先 CoreML |
| `TurboOCR (doc_ori.onnx)` | 本地 ONNX 方向自愈 | PP-LCNet_x1_0 方向模型 (~6.47MB) + 零时延矢量文本流双轨判别，SHA256 原子下载校验 |
| `olmOCR 2 边界与尾注分离` | 思想借鉴与本地实现 | 将 `* p < 0.05`、`Note:`、`注:` 等尾注从数据矩阵剥离至 `footnotes` 属性，保护编号数据行不被误剥 |
| `openpyxl CellRichText` | 本地格式渲染 | 原生 Excel 单元格富文本渲染，高保真呈现 `10^{-3}`、`CaCO_3`、`H_2O` 等学术上下标 |
| `MinerU 4-Way & DeepDoc DP` | 思想借鉴与本地实现 | 4-Way 跨页续表联合判定 + 3-Way 单调动态规划列对齐，支持横向分块拼接与小表保护 |
| [`OmniDocBench (CVPR 2025)`](https://github.com/opendatalab/OmniDocBench) | 评测度量体系 | 标准化表格 TEDS、严格插值 PR 曲线 mAP (`IoU@0.50/0.75/0.50:0.95`) 与端到端召回率评测 |
| `LLM 语义重构与等价门禁` | 本地评估与修复 | 支持 OpenAI-compatible、无 Key 本地服务端点 (Ollama/vLLM) 与 Agent Bridge；遵循 arXiv 2603.18652 三维语义等价验收 |
| `SMART 语义表头展平` | 本地算法实现 | 层级路径展平 (`Category > Subcategory > Field [Unit]`)，保留完整父子分类血缘并消解冗余 |
| [`PyZotero & Better-Notes`](https://github.com/urschrei/pyzotero) | 生态联动扩展 | 生态联动扩展（默认关闭），WAL 安全快照定位条目 Key，自动挂载 `.xlsx` 并生成 Better-Notes 富文本卡片 |

## 漏检自查与完整性保障 (Self-Audit)

为杜绝文献表格漏检、误剔与劣退，本技能在管线全生命周期内构建了严密的多重自查闭环：

1. **表号声明预期清单扫描 (`Declaration Ledger`)**：
   - 提取前轻量扫描 PDF 全文（含正文与附录，全篇扫描无页数人为截断），识别标准表号声明 (`Table 1`, `表1`, `Tab. 1`, `Table S1`, `Extended Data Table 1`, `TABLE III` 等) 与跨页续表标记 (`表1（续）`, `Table 1 (cont.)`, `Table 2 Continued`, `cont'd`, `续上表`)，同时屏蔽目录页与参考文献页误判。
   - 维护全文预期表号集合与出现频次账本，确保声明表号与提取产物一一闭环对应。
2. **候选页动态探测与补漏机制**：
   - 当声明表格数超出初步候选页总数时，自动启动全文回扫补齐缺失候选页；
   - 对无绘图/矢量线框页面 (`has_drawings=False`) 及绘图未检出线框页面，自动继续通过 `find_tables(strategy="text")` 与 `probe_disjoint_native_tables` 探测潜在无框文本表；
   - 当单页出现多个表题声明时，放宽候选页表格配额并校验每页去重后的真实表数，支持单页提取多张独立表格。
3. **质量对比防劣退门禁 (`_should_replace_with_ocr`)**：
   - OCR 结果不能盲目替换原生表格：替换前严格对比行数、非空单元格填充率、原生样本身份与数值完整性；
   - 若 OCR 结果为空、行数严重萎缩或质量劣于原生解析，坚决拒绝替换，保留原生表格作为降级兜底；对无法对齐的跨页续表保留独立片段，禁止按行号盲目拼接。
4. **单行/单列与章节碎片保护**：
   - 管道全流程（质量检验、多方投票、后处理、Excel 导出）完整支持单行表（如基线汇总、参数对照）与单列表（如指标序列）；
   - 在多级章节文献中，无表题的合法表格片段统一导出为 `unlabelled_N.xlsx`，杜绝静默丢弃；同名冲突或远隔独立表自动按 `_v2`/`_v3` 或 `_p{N}` 避让写入，防止静默覆盖。

## 技能目录结构

```text
zotero-table-extractor/
├── README.md                         # 英文主说明文档（含中文说明链接）
├── README.zh-CN.md                   # 简体中文完整说明文档
├── SKILL.md                          # AI 技能协议定义与架构/CLI 参考（本文件）
├── LICENSE                           # Apache License 2.0 开源协议
├── setup_env.py                      # 🚀 跨平台一键环境与全量依赖安装/就绪自检脚本 (Win/macOS/Linux)
├── update.py                         # 🔄 技能在线更新与个人配置绝对防护主入口
├── setup_paths.py                    # 🛠 跨平台路径、环境配置与模型校验下载向导
├── requirements.txt                  # Python 核心与可选依赖清单
├── config.example.json               # 运行期配置模板（含 74 项标准配置键）
├── config.json                       # 运行期私有配置 (含 API Keys，受 .gitignore 保护勿提交)
├── .gitignore                        # Git 忽略规则（隔离 config.json、profiles、缓存等）
├── figures/                          # 架构图源文件及矢量/位图导出
│   ├── zotero_table_extractor_architecture.drawio
│   ├── zotero_table_extractor_architecture.png
│   └── zotero_table_extractor_architecture.svg
├── models/                           # 本地离线 ONNX 检测与方向模型
│   ├── doclayout_yolo_docstructbench_imgsz1280_2501.onnx
│   └── doc_ori.onnx                  # TurboOCR PP-LCNet 页面与切图方向模型 (~6.47MB)
├── benchmarks/                       # 5 组异质表格基准评测套件 (fixture / live)
│   ├── run_benchmark.py
│   ├── pipelines.py
│   ├── evaluator.py
│   ├── generate_benchmark_papers.py
│   ├── ground_truth/
│   ├── papers/
│   └── reports/
├── tests/                            # 离线隔离回归测试套件 (python tests/run_offline.py)
│   ├── run_offline.py
│   ├── offline_guard.py
│   └── test_*.py
└── scripts/                          # 核心提取代码库
    ├── updater.py                    # 🛡️ 在线安全更新与智能配置迁移/回滚引擎
    ├── setup_paths.py                # 🛠 路径配置向导入口薄壳
    ├── extract_zotero_table.py       # CLI 主入口（单篇、目录批量、.txt 清单或远程 URL）
    ├── pdf_table_extractor.py        # 本地 PDF 提取总控（结构树/MinerU/9引擎投票/3阶视觉接管）
    ├── pdf_tables.py                 # 表格切图定位、rotation_matrix 几何归一化与导出回退
    ├── ocr_client.py                 # MinerU (CLI+API)、PaddleOCR-VL、PP-StructureV3、TeleOCR 与 Docling 客户端
    ├── doclayout_yolo_detector.py    # DocLayout-YOLO + PP-DocLayoutV3 双检集成推理器
    ├── doc_orientation_detector.py   # TurboOCR 方向检测与双轨两阶旋转自愈器
    ├── rapid_table_extractor.py      # RapidAI TableStructureRec + RapidTable 本地双模型引擎
    ├── pdf_page_filter.py            # 页面轻量启发式预过滤（跳过纯正文无表页）
    ├── table_postprocess.py          # 7 阶表格后处理流水线（SMART展平/尾注剥离/DP续表合并）
    ├── table_validator.py            # 表号声明账本、数值完整性门禁与漏表交叉核验
    ├── teds.py                       # 纯 Python IBM TEDS 树编辑距离拓扑相似度实现
    ├── omnidocbench.py               # OmniDocBench (CVPR 2025) 表格与版面标准化评测体系
    ├── excel_export.py               # 出版级 Excel 导出（CellRichText 上下标/自适应列宽/防撞写入）
    ├── models.py                     # 统一领域模型 (Table IR: ExtractedTable & TableCell)
    ├── system_detector.py            # 跨平台系统环境探测与路径自适应（macOS/Linux/Windows）
    ├── env_detector.py               # 环境探测兼容包装层（system_detector 的向后兼容别名）
    ├── common.py                     # 配置加载（含环境变量与同伴凭据协同）、文件锁、WAL 快照工具
    ├── playwright_utils.py           # Playwright Chromium 可执行文件自适应探测工具
    ├── batch_planner.py              # 批量元数据、年份与出版社分组预分析规划器
    ├── batch_run.py                  # Zotero 全库批量并发提取总控（WAL 快照 + 子进程隔离）
    ├── verify_tables.py              # 表格产物批量校验与空表/坏表清理
    ├── login.py                      # 期刊出版社 Cookie 注入、CDP 桥接与 CNKI 登录向导
    ├── llm_reasoner.py               # LLM 辅助结构重构与语义等价性门禁 (arXiv 2603.18652)
    ├── mineru_popo.py                # 可选 MinerU-Popo 4B 文档级后处理钩子（默认关闭，auto 自动探测 CUDA / Apple M）
    ├── zotero_sync.py                # 🔄 PyZotero 与 Better-Notes 生态联动（附件挂载与笔记生成）
    ├── agent_bridge.py               # Agent 交互桥接与进度事件通知
    ├── package_release.py            # Release 资产安全打包与 SHA256 校验工具
    ├── export_drawio_diagram.py      # draw.io 矢量架构图渲染导出脚本
    ├── audit/                        # 质量审查与诊断工具包统一导出命名空间
    ├── audit_reporter.py             # 全面审查诊断报告生成器
    ├── targeted_audit_reporter.py    # 针对性异常排查报告生成器
    ├── exhaustive_audit_runner.py    # 全库地毯式提取质检执行器
    ├── targeted_audit_runner.py      # 针对特定异常样例文献的专项回归测试器
    ├── deep_8h_iterative_healer.py   # 长效迭代自愈与异常自动修复执行器
    ├── compat/                       # 跨版本兼容性包装层
    └── online/                       # 在线 HTML / XML 提取子包
        ├── __init__.py
        ├── cdp_detector.py           # DevToolsActivePort 动态探测、CDP Port Guard 与 6 槽位并发控制
        ├── doi_resolver.py           # DOI/PII/arXiv/PMCID/chndoi 解析与 Zotero WAL 查询
        ├── strategies.py             # 多出版社直连 API、OA 门控 Firecrawl、Scrapling 与 Crawl4AI 策略
        ├── general_html_extractor.py # 通用出版社 Playwright 动态渲染与分出版社 DOM 适配器
        ├── cnki_html_extractor.py    # 中国知网 (CNKI) 自动化滑块绕过与 HTML 阅读提取器
        └── graph.py                  # 分层并发竞速 (race=True) 与串行 DAG 流水线编排
```

## CLI 使用说明

### 1. 单篇论文或批量目录/清单提取 (`extract_zotero_table.py`)

```bash
# 基础用法：提取指定 PDF 并保存到输出目录
python scripts/extract_zotero_table.py --pdf /path/to/paper.pdf --output /path/to/output_dir

# 通过在线论文 URL 或 DOI 链接提取，并指定单文件多 Sheet 与 LLM 语义修复
python scripts/extract_zotero_table.py --pdf https://doi.org/10.1016/j.precamres.2021.106450 --output /path/to/tables.xlsx --single-file --enable-llm

# 仅走本地 PDF 提取，跳过出版社网页抓取
python scripts/extract_zotero_table.py --pdf /path/to/paper.pdf --output /path/to/output_dir --pdf-only

# 仅走在线 HTML/XML 抓取，不回退本地 PDF
python scripts/extract_zotero_table.py --pdf /path/to/paper.pdf --output /path/to/output_dir --online-only

# 批量提取目录中的所有 PDF 或 .txt 路径清单（每行可为本地 PDF 路径或 http(s):// URL）
python scripts/extract_zotero_table.py --pdf /path/to/pdfs_dir --output /path/to/all_tables --workers 4
```

| 参数 | 类型 | 默认值 | 作用说明 |
| :--- | :--- | :--- | :--- |
| `--pdf` | 字符串 | 必须 | 待提取 PDF 文件路径、PDF 目录、远程 `http(s)://` URL 或 `.txt` 路径清单 |
| `--output` | 字符串 | 必须 | 导出结果目录路径；当路径以 `.xlsx` 结尾或启用 `--single-file` 时写入单文件 |
| `--doi` | 字符串 | `None` | 预解析文献 DOI（用于在线 HTML/XML 直取优先调度与补全） |
| `--url` | 字符串 | `None` | 预解析论文网页 URL |
| `--title` | 字符串 | `None` | 预解析论文标题（辅助在线检索与 Zotero 元数据匹配） |
| `--headers` | 字符串 | `None` | 自定义强制表头列名（逗号分隔） |
| `--table-idx` | 字符串 | `"all"` | 提取指定索引表格：`"all"`、`"first"` 或 0-based 整数序号 |
| `--db-path` | 字符串 | `None` | 可选 `zotero.sqlite` 数据库路径 |
| `--firecrawl-key` | 字符串 | `None` | 可选覆盖 Firecrawl API Key |
| `--online-only` | 标志 | `False` | 仅抓取在线 HTML/XML 表格，不执行本地 PDF 提取回退 |
| `--pdf-only` | 标志 | `False` | 跳过出版社网页抓取，直接进入 PDF 提取流程（MinerU/OCR 仍可能调用云端服务） |
| `--skip-supplementary` | 标志 | `False` | 跳过 Supplementary Material / 附录表格 |
| `--single-file` | 标志 | `False` | 将提取到的所有表格汇聚写入单个 `.xlsx` 文件（多个 Sheet） |
| `--enable-llm` | 标志 | `False` | 启用大模型语义推理，纠正深层多级复合表头并执行语义等价门禁 |
| `--mineru-popo` | 字符串 | `None` | 可选 MinerU-Popo 后处理钩子模式：`off`（默认关闭）、`auto`（检测到 CUDA / Apple Silicon M 系列时自动开启）、`on`（强制开启） |
| `--enable-popo` | 标志 | `False` | 快捷开启硬件感知版 MinerU-Popo 后处理钩子（等效 `--mineru-popo auto`） |
| `--sync-zotero` | 标志 | `False` | 启用 PyZotero 与 Better-Notes 生态联动（挂载 `.xlsx` 附件并生成预览笔记） |
| `--zotero-api-key` | 字符串 | `None` | 覆盖 Zotero Web API 访问密钥 |
| `--zotero-user-id` | 字符串 | `None` | 覆盖 Zotero 用户/库 ID |
| `--ocr-engine` | 字符串 | `"auto"` | OCR/VLM 引擎首选项：`auto`、`paddleocr`、`docling`、`mineru` |
| `--cnki-strategy` | 字符串 | `"auto"` | 知网提取策略：`auto`、`scrapling`、`playwright` |
| `--headed` | 标志 | `False` | 以有头可见窗口启动 Playwright 浏览器 |
| `--user-data-dir` | 字符串 | `None` | 自定义浏览器用户会话 Profile 目录 |
| `--workers` | 整数 | `4` | 目录/`.txt` 清单批量提取时的并发工作线程数 |
| `--sequential` | 标志 | `False` | 禁用单篇在线分层竞速模式，回退串行 DAG 流程 |

### 2. Zotero 全库批量并发总控 (`batch_run.py`)

```bash
# 默认开启在线竞速∥本地抽取，断点续传处理未完成的 Zotero 文献
python scripts/batch_run.py --resume --workers 4

# 复检已有产物目录，按表号覆盖率自动补齐遗漏表格
python scripts/batch_run.py --recheck --workers 2

# 跳过在线网页抓取，仅执行 PDF 提取
python scripts/batch_run.py --pdf-only --workers 4
```

| 参数 | 类型 | 默认值 | 作用说明 |
| :--- | :--- | :--- | :--- |
| `--workers` | 整数 | `4` | 并行子进程数量 |
| `--online` / `--no-online` | 标志 | `True` | 是否启用在线 HTML ∥ PDF 竞速模式（默认启用） |
| `--pdf-only` | 标志 | `False` | 禁用在线提取，等效 `--no-online`，直接走 PDF 提取流程 |
| `--skip-supplementary` | 标志 | `False` | 跳过补充材料与附录表格（默认完整提取） |
| `--sequential` | 标志 | `False` | 传递给子进程：禁用在线竞速，走串行 DAG |
| `--resume` | 标志 | `False` | 断点续跑：跳过已有有效 `.xlsx` 产物的文献 |
| `--recheck` | 标志 | `False` | 复检模式：核验已有目录中的表号覆盖率，仅重跑缺失或空结果文献 |
| `--timeout` | 整数 | `300` | 单篇文献处理超时上限（秒） |
| `--limit` | 整数 | `0` | 仅处理前 N 篇（0 表示全量执行） |
| `--paper-tables` | 字符串 | `None` | 覆盖结果输出根目录（优先于 `config.json` 与平台默认路径） |
| `--db-path` | 字符串 | `None` | 覆盖 `zotero.sqlite` 数据库路径 |

### 3. 路径初始化与模型准备 (`setup_paths.py`)

```bash
# 交互式引导配置（跨平台自动探测 Zotero、输出目录与可选 API Key）
python setup_paths.py

# 非交互式一键保存默认探测路径 / 查看配置状态 / 校验并下载本地 ONNX 模型
python setup_paths.py --auto
python setup_paths.py --show
python setup_paths.py --download-models
```

| 参数 | 作用说明 |
| :--- | :--- |
| `--auto` | 非交互式一键探测并保存推荐路径 |
| `--show` / `--status` | 查看当前各项路径配置与文件存在性状态 |
| `--reset` | 预备份后将 `config.json` 重置为 `config.example.json` 模板 |
| `--download-models` | 校验并自动下载缺失的 `doclayout_yolo_docstructbench_imgsz1280_2501.onnx` 与 `doc_ori.onnx` |
| `--update` / `--check-update` / `--rollback` / `--list-backups` | 代理调用技能在线安全更新与配置快照回滚能力 |

### 4. 技能在线安全更新与配置防护 (`update.py`)

```bash
python update.py                 # 一键在线安全更新（Git / Release 双模，严密保护个人密钥与路径）
python update.py --check         # 只读检测远端更新与缺失配置项
python update.py --dry-run       # 演练模式模拟更新全流程，不修改磁盘文件
python update.py --rollback      # 回滚至最近一次备份的配置状态
python update.py --list-backups  # 查看历史配置快照清单
python update.py --merge-only    # 仅将 config.example.json 新项安全并入 config.json
python update.py --check --json  # 结构化 JSON 输出（适合 Agent/CI 调用）
```

| 参数 | 类型 | 默认值 | 作用说明 |
| :--- | :--- | :--- | :--- |
| `--check` | 标志 | `False` | 仅检测更新状态与缺失字段，不执行任何写入 |
| `--update` | 标志 | `False` | 显式执行在线更新与配置合并（无参数时默认行为） |
| `--dry-run` | 标志 | `False` | 模拟演练更新全流程，不修改文件 |
| `--force` | 标志 | `False` | 强制拉取更新并重置已跟踪代码文件 |
| `--rollback` | 字符串 | `"latest"` | 回滚 `config.json` 至最近或指定 ID 的快照 |
| `--list-backups` | 标志 | `False` | 列出所有可用的历史配置快照 |
| `--merge-only` | 标志 | `False` | 仅将 `config.example.json` 中的新项安全并入 `config.json` |
| `--install-deps` | 标志 | `False` | 更新后自动运行 `pip install -r requirements.txt` |
| `--method` | 字符串 | `"auto"` | 指定更新渠道：`auto`、`git`、`release` |
| `--json` | 标志 | `False` | 以结构化 JSON 输出结果 |

### 5. 产物核验与交互式登录 (`verify_tables.py` / `login.py`)

```bash
python scripts/verify_tables.py --root "/path/to/output_dir" [--fix] [--verbose]
python scripts/login.py cnki        # 知网可见浏览器登录并保存 Cookies
python scripts/login.py publishers  # 主流学术出版社 Chrome CDP 调试桥接登录
python scripts/login.py crawl4ai    # Crawl4AI 免检测登录与 Cookie 导出
```

## 配置项（`config.json`）

`config.json` 支持环境变量覆盖（如 `FIRECRAWL_API_KEY`、`ELSEVIER_API_KEY`、`ELSEVIER_INSTTOKEN`、`IS_CAMPUS_NETWORK`、`UNPAYWALL_EMAIL`、`CROSSREF_EMAIL`、`OPENALEX_API_KEY`、`CORE_API_KEY`、`PADDLEOCR_ACCESS_TOKEN` / `PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN`、`MINERU_TOKEN`、`MINERU_API_KEY`、`MINERU_API_BASE`、`MINERU_USE_CLI`、`MINERU_CLI_PATH`、`MINERU_MODEL`、`MINERU_AGENT_BACKEND`、`MINERU_POLL_TIMEOUT`、`TELEOCR_API_BASE`、`TELEOCR_MODEL`、`TELEOCR_API_KEY`、`OCR_ENGINE`、`DOCLAYOUT_ENGINE`、`MINERU_TABLE_MERGE_ENABLE`、`OPENAI_API_KEY` / `LLM_API_KEY`、`OPENAI_API_BASE` / `LLM_API_BASE`、`LLM_MODEL`、`LLM_TABLE_REASONER_ENABLED`、`ZOTERO_SYNC_ENABLED` 等），并在主配置留空时自动探测同生态同伴工具（`~/.scansci-pdf/config.json`、`~/.journal_supp_downloader_profile/config.json`、`journal-supp-downloader/.env`）的脱敏学术凭据（可通过 `ZTE_DISABLE_COMPANION_CREDS=1` 关闭）。

| 配置 Key | 类型 | 默认值 / 示例 | 说明 |
| :--- | :--- | :--- | :--- |
| `PAPER_TABLES_DIR` | string | `""` | 批量输出表格的默认根目录（为空时自适应系统默认路径） |
| `ZOTERO_DIR` | string | `""` | 本地 Zotero 数据目录路径 |
| `ZOTERO_DB_PATH` | string | `""` | 本地 `zotero.sqlite` 数据库路径 |
| `ZOTERO_STORAGE_DIR` | string | `""` | 本地 Zotero `storage/` 附件目录路径 |
| `SUPP_DOWNLOADER_SCRIPT` | string | `""` | 可选外部附表下载脚本路径 (`journal_downloader.py`) |
| `ELSEVIER_API_KEY` | string | `""` | Elsevier 机构官方 API Key (`X-ELS-APIKey`) |
| `ELSEVIER_INSTTOKEN` | string | `""` | Elsevier 机构通行证 Token (`X-ELS-InstToken`) |
| `IS_CAMPUS_NETWORK` | boolean | `false` | 显式标记当前处于高校/机构校园网授权环境 |
| `UNPAYWALL_EMAIL` | string | `""` | Unpaywall API Polite Pool 联系邮箱 |
| `CROSSREF_EMAIL` | string | `""` | Crossref API Polite Pool 联系邮箱 |
| `OPENALEX_API_KEY` | string | `""` | OpenAlex API 密钥（传递给附表/学术检索协同链路） |
| `CORE_API_KEY` | string | `""` | CORE 开放获取学术库 API 密钥 |
| `FIRECRAWL_API_KEY` | string | `""` | Firecrawl 爬取 API Key（仅对已确认 OA 文献启用） |
| `FIRECRAWL_MAX_AGE_MS` | int | `86400000` | Firecrawl 页面缓存生命周期 (毫秒) |
| `FIRECRAWL_PAGE_TIMEOUT_MS` | int | `30000` | Firecrawl 单页请求超时时间 (毫秒) |
| `FIRECRAWL_CACHE_DIR` | string | `"~/.zotero_firecrawl_cache"` | Firecrawl 本地缓存目录 |
| `FIRECRAWL_CACHE_TTL_S` | int | `86400` | Firecrawl 本地缓存过期秒数 |
| `CNKI_STRATEGY` | string | `"auto"` | 知网提取策略 (`auto`, `scrapling`, `playwright`) |
| `PLAYWRIGHT_HEADED` | boolean | `false` | 是否以有头模式启动 Playwright 浏览器 |
| `PLAYWRIGHT_USER_DATA_DIR` | string | `""` | 自定义 Playwright 用户会话目录（自动持久化 Cookie） |
| `PLAYWRIGHT_USER_DATA_DIR_MAC` | string | `""` | macOS 专属 Playwright 用户会话目录覆盖 |
| `PLAYWRIGHT_USER_DATA_DIR_WINDOWS` | string | `""` | Windows 专属 Playwright 用户会话目录覆盖 |
| `PLAYWRIGHT_EXECUTABLE_PATH` | string | `""` | 自定义系统 Chrome/Chromium 可执行文件路径 |
| `CDP_URL` | string | `""` | 通用浏览器 CDP 远程调试地址（为空时自动探测 `DevToolsActivePort`） |
| `CRAWL4AI_CDP_URL` | string | `""` | Crawl4AI CDP 远程调试地址 |
| `CRAWL4AI_PAGE_TIMEOUT_MS` | int | `45000` | Crawl4AI 单页抓取超时 (毫秒) |
| `CRAWL4AI_MAX_CONCURRENT` | int | `2` | Crawl4AI 最大并发页面数 |
| `PADDLEOCR_MCP_MODEL` | string | `"PaddleOCR-VL-1.6"` | PaddleOCR 远端模型名称 |
| `PADDLEOCR_MCP_PPOCR_SOURCE` | string | `"aistudio"` | PP-OCR 历史兼容来源标识（默认 `aistudio`；实际引擎路由由 `OCR_ENGINE` 控制） |
| `PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN` | string | `""` | 百度 AIStudio Access Token |
| `PP_STRUCTURE_MODEL` | string | `"PP-StructureV3"` | PP-Structure 在线定位版面模型名称 |
| `USE_PP_STRUCTURE` | boolean | `true` | 是否启用 PP-Structure 作为表格前置粗定位 |
| `UV_INDEX_URL` | string | `"https://pypi.tuna.tsinghua.edu.cn/simple"` | Python 包安装索引镜像源 |
| `DOCLAYOUT_ENGINE` | string | `"ensemble"` | 本地版面检测引擎 (`yolo`, `pp_doclayoutv3`, `ensemble`) |
| `DOCLAYOUT_YOLO_ENABLED` | boolean | `true` | 是否启用本地 DocLayout-YOLO 离线检测 |
| `PP_DOCLAYOUT_ENABLED` | boolean | `true` | 是否启用 PP-DocLayoutV3 25 类细粒度版面识别与多边形贴合 |
| `DOCLAYOUT_MODEL_DIR` | string | `"./models"` | 本地视觉模型权重存放目录 |
| `DOCLAYOUT_CONF_THRESHOLD` | float | `0.25` | 目标检测框置信度阈值 |
| `OCR_ENGINE` | string | `"auto"` | 首选 OCR/VLM 引擎 (`auto`, `paddleocr`, `docling`, `mineru`) |
| `MINERU_API_KEY` | string | `""` | MinerU API 密钥（与 `MINERU_TOKEN` 双向自动同步） |
| `MINERU_TOKEN` | string | `""` | MinerU API Token（亦支持读取 `~/.mineru/config.yaml`） |
| `MINERU_API_BASE` | string | `"https://mineru.net"` | MinerU API 服务端点地址 |
| `MINERU_USE_CLI` | string | `"auto"` | MinerU 执行方式：`auto` (优先 CLI 失败回退 REST)、`cli`、`api` |
| `MINERU_CLI_PATH` | string | `""` | 可选自定义 `mineru-open-api` 二进制路径 |
| `MINERU_MODEL` | string | `"vlm"` | MinerU 解析模型 (`vlm`, `pipeline`, `html`) |
| `MINERU_AGENT_BACKEND` | string | `"vlm"` | MinerU 零 Token Agent 备用接口优先后端 (`vlm`, `pipeline`) |
| `MINERU_ENABLE_FLASH_EXTRACT` | boolean | `true` | 无 Token 小文件优先尝试 `flash-extract`，未出表自动回退 Agent API |
| `MINERU_TABLE_MERGE_ENABLE` | boolean | `true` | 是否启用 MinerU 4-way 联合判定与小表保护跨页续表合并 |
| `MINERU_POPO_ENABLED` | boolean / string | `false` | 可选 MinerU-Popo 文档级后处理钩子：`false`（默认关闭）、`"auto"`（识别到 CUDA / Apple Silicon M 系列时自动开启）、`true`（显式开启） |
| `MINERU_POPO_AUTO_HARDWARE` | boolean | `true` | 本地推理硬件保护：未配置远端 vLLM 时仅允许在 CUDA 或 Apple Silicon M 系列上加载本地 4B 模型 |
| `MINERU_POPO_BACKEND` | string | `"auto"` | MinerU-Popo 推理后端：`auto`、`vllm`、`native_repo`、`transformers`（兼容 `POPO_INFERENCE_BACKEND`） |
| `MINERU_POPO_MODEL_PATH` | string | `"DreamEternal/MinerU-Popo"` | MinerU-Popo 本地权重路径或 HuggingFace 仓库 ID（兼容 `POPO_MODEL_PATH`） |
| `MINERU_POPO_VLLM_URL` | string | `""` | MinerU-Popo vLLM / OpenAI-Compatible 服务地址（兼容 `POPO_VLLM_URL`） |
| `MINERU_POPO_VLLM_API_KEY` | string | `""` | MinerU-Popo vLLM 鉴权密钥（兼容 `POPO_VLLM_API_KEY`） |
| `MINERU_POPO_MODEL_NAME` | string | `"Popo"` | vLLM 服务端模型名称（兼容 `POPO_MODEL_NAME`） |
| `MINERU_POPO_REPO_PATH` | string | `""` | 可选本地克隆的 `opendatalab/MinerU-Popo` 仓库路径 |
| `MINERU_POPO_TIMEOUT` | int | `60` | MinerU-Popo 单次推理超时秒数 |
| `MINERU_POPO_ALLOW_DOWNLOAD` | boolean | `false` | 本地 `transformers` 模式是否允许自动联网下载 4B 权重 |
| `TELEOCR_API_BASE` | string | `""` | TeleOCR vLLM / OpenAI-compatible 视觉推理服务 URL |
| `TELEOCR_MODEL` | string | `"TeleOCR"` | TeleOCR 模型标识符 |
| `TELEOCR_API_KEY` | string | `""` | TeleOCR API 鉴权密钥 |
| `DOCLING_ENABLED` | boolean | `true` | 是否启用 IBM Docling (TableFormer) 深度表格提取引擎 |
| `DOCLING_OFFLINE_FALLBACK` | boolean | `true` | 是否启用 Docling 作为云端 VLM 受阻时的本地离线降级引擎 |
| `DOCLING_IMAGES_SCALE` | float | `2.0` | IBM Docling PDF 页面渲染缩放倍率 |
| `GMFT_ENABLED` | boolean | `true` | 是否启用 GMFT (TATR Transformer) 参与多方投票 |
| `IMG2TABLE_ENABLED` | boolean | `true` | 是否启用 img2table 纯 OpenCV 方案参与多方投票 |
| `PYMUPDF4LLM_ENABLED` | boolean | `true` | 是否启用 PyMuPDF4LLM（可选包存在时）辅助提取 |
| `RAPID_TABLE_ENABLED` | boolean | `true` | 是否启用 RapidAI RapidTable 本地结构识别引擎参与投票与快速挽救 |
| `TEDS_METRIC_ENABLED` | boolean | `true` | 是否启用 TEDS 表格结构相似度度量自检 |
| `OMNIDOCBENCH_ENABLED` | boolean | `true` | 是否开启 OmniDocBench (CVPR 2025) 标准化度量自检 |
| `SMART_HEADER_FLATTEN_ENABLED` | boolean | `true` | 是否启用 SMART 多级表头语义展平与层级路径推理 |
| `ARXIV_HTML_ENABLED` | boolean | `true` | 是否优先尝试 arXiv HTML / ar5iv 直连抽取 |
| `NCBI_PMC_ENABLED` | boolean | `true` | 是否优先尝试 NCBI PMC JATS XML 抽取 |
| `LLM_ENABLED` | boolean | `false` | 是否开启 LLM 复杂语义推断、表格重构与语义等价验收 |
| `LLM_API_BASE` | string | `"https://api.openai.com/v1"` | OpenAI 兼容服务端点（支持 `localhost` 本地推理服务免 Key 调用） |
| `LLM_API_KEY` | string | `""` | LLM 鉴权密钥 |
| `LLM_MODEL` | string | `"gpt-4o-mini"` | 语义处理选用的 LLM 模型 |
| `LLM_TIMEOUT` | int | `60` | LLM 调用超时秒数 |
| `ZOTERO_SYNC_ENABLED` | boolean | `false` | 是否启用 PyZotero 与 Better-Notes 生态联动（默认严格关闭） |
| `ZOTERO_API_KEY` | string | `""` | Zotero 官方 Web API 访问密钥 |
| `ZOTERO_USER_ID` | string | `""` | Zotero 用户 ID 或群组 ID |
| `ZOTERO_LIBRARY_TYPE` | string | `"user"` | Zotero 库类型 (`user` 或 `group`) |
| `ZOTERO_BETTER_NOTES_ENABLED` | boolean | `true` | 同步时是否生成 Better-Notes 规范富文本预览笔记 |
| `ZOTERO_AUTO_ATTACH_XLSX` | boolean | `true` | 同步时是否将导出的 Excel 文件作为子附件挂载至对应条目 |

> **说明**：代码同时支持可选环境变量/配置开关 `USE_LEGACY_VOTING=true`（强制跳过 MinerU 主抽取直走 9 引擎投票）、`USE_DOC_ORIENTATION_CLASSIFY`（显式覆盖云端 OCR 方向分类）、`ZOTERO_ATTACHMENTS_BASE_DIR`（Linked PDF 附件根目录）、`RAPID_TABLE_UNITABLE=0`（禁用 UniTable PyTorch 无线表路径）与 `AGENT_BRIDGE_DIR`（自定义 Agent 推理桥接目录）。

## 目录与版本控制规范

- **回归验证**：使用 `python tests/run_offline.py`，在临时副本中运行离线测试，隔离个人配置、网络连接和外部服务进程。真实浏览器测试默认跳过；单独执行需设置 `ZOTERO_RUN_LIVE_BROWSER=1`。
- **评测口径**：`benchmarks/run_benchmark.py --mineru-mode fixture` 使用从标准答案生成的模拟缓存，仅验证解析、质量检查与续表后处理；`--mineru-mode live` 会提交 PDF 到 MinerU，忽略模拟缓存。不得用 `fixture` 模式的准确率或耗时声称真实模型识别效果或服务加速倍数。
- **无法对齐的续表**：重复行标识不足以证明横向对应关系时，保留独立片段；不得将按行号盲目拼接的结果当作已核验数据。
- **敏感凭据与会话隔离**：
  - `config.json` 包含私有 API 密钥，受 `.gitignore` 保护，严禁提交入库；请通过复制 `config.example.json` 或运行 `python setup_paths.py` 生成本地配置；
  - `profiles/` 与浏览器 Profile 目录含会话 Cookie 与凭证，受 `.gitignore` 保护，严禁打包入库（`scripts/package_release.py` 会强制审计拦截）。
- **产物与缓存**：
  - `figures/` 专用于存放项目架构图源文件与渲染导出；
  - `scratch/` 专用于临时诊断脚本，受 `.gitignore` 保护；
  - `models/` 下的 ONNX 权重支持通过 `python setup_paths.py --download-models` 或运行时按需下载校验。
