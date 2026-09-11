---
name: zotero-table-extractor
description: 从 Zotero 选中条目（依赖 ai4paper-zotero MCP 获取物理路径）或指定本地 PDF / CNKI / 期刊网页中批量/单篇自动抓取并提取结构化表格，支持 PP-StructureV3 定位 + 多方投票 + PaddleOCR-VL-1.6 多模态解析，导出 Excel (.xlsx)。
---

# Zotero Table Extractor

从 Zotero PDF 附件或论文网页中自动识别并提取结构化表格，导出为独立的 Excel (`.xlsx`) 文件。

> **💡 Agent 架构说明**：  
> 当用户要求“提取 Zotero 选中的这篇论文”时，由 Agent 编排层通过 `ai4paper-zotero` MCP 工具（`zotero_get_selected_items`）获取选中条目的物理 PDF 路径，再传递给本技能 CLI（`--pdf <path>`）执行底层提取。

## 提取管线

```text
论文.pdf / 论文网页
       │
       ├─ 1. 在线 HTML 提取（最优先，速度快/原生数字保真）
       │     Elsevier XOCS XML / CNKI / Springer / 通用出版社
       │     若在线表数 < PDF caption 检测数 → 放弃部分预览，执行本地 PDF 提取
       │
       └─ 2. 本地 PDF 提取
              │
              ▼
         pdf-inspector (Rust, ~80ms)
              │
              ├─ PDF 分类: text_based / scanned / mixed
              ├─ 表格页定位: pages_with_tables
              └─ Markdown 表格提取
              │
              ├──────────┬──────────────────┐
              │          │                  │
           Native     Scanned            Mixed
              │          │                  │
              ▼          ▼                  ▼
         多方投票:   PP-StructureV3     多方投票(文本页)
         pdf-inspector 定位 + 切图     + OCR/VLM(扫描页)
         + find_tables + PaddleOCR-VL-1.6
         + text_align  多模态精细解析
         + pdfplumber
         + Camelot
              │
              ├─ 任意两方相似度 >= 0.75 → 判定一致并采用投票结果
              ├─ 相似度 < 0.90 标记为低置信度页 → 触发 PP-StructureV3 + PaddleOCR-VL 在线深度接管
              ├─ 7 阶轻量直通安全后处理（公式注入防御、数值类型推断、多级表头展平、付费墙过滤）
              └─ 跨页续表与多分页自适应合并
              │
              ▼
         Excel (每表独立文件，带标准化表号 label，支持单文件多 Sheet 或分表输出)
```

## 关键组件

| 组件 | 作用 |
|---|---|
| `pdf-inspector` | Rust 库，PDF 分类 + 表格定位 + Markdown 提取，~80ms 高性能 |
| `Camelot` + `pdfplumber` | 结构化表格提取，与原生线框进行多方投票交叉验证 |
| `PyMuPDF find_tables()` | PDF 矢量线框表格提取（零 OCR，高保真） |
| `text_alignment` | 无线框表格的文本坐标对齐与自适应重组 |
| `PP-StructureV3` | 在线版面分析 API，表格定位与局部切图提取 |
| `PaddleOCR-VL-1.6` | 在线多模态大模型 API，针对定位区域进行复杂/跨行跨列结构化解析 |
| `DocLayout-YOLO` | 本地 ONNX 版面检测（离线/无网络时的备用切图定位兜底） |

## 技能目录结构

```text
zotero-table-extractor/
├── SKILL.md                         # 技能说明文档
├── config.json                      # 运行期配置 (含 API Keys，受 .gitignore 保护勿提交)
├── config.example.json              # 配置模板
├── figures/                         # 技能超清架构图 (draw.io 工程源文件及 PNG/SVG 导出)
│   ├── zotero_table_extractor_architecture.drawio
│   ├── zotero_table_extractor_architecture.png
│   └── zotero_table_extractor_architecture.svg
├── models/                          # 本地离线 ONNX 检测模型
│   └── doclayout-yolo-docstructbench-q8-6c25a56c.onnx
├── scratch/                         # 临时诊断脚本与测试套件
└── scripts/                         # 核心提取代码库
    ├── extract_zotero_table.py      # CLI 主入口（支持单篇、目录批量或 .txt 路径清单）
    ├── pdf_table_extractor.py       # 本地 PDF 提取管线（分类+多方投票+两阶段OCR接管）
    ├── pdf_tables.py                # 表格切图定位与本地/结构化导出回退
    ├── ocr_client.py                # PP-StructureV3 & PaddleOCR-VL 在线客户端
    ├── table_postprocess.py         # 7 阶表格后处理流水线（公式转义/类型推断/续表合并）
    ├── excel_export.py              # Excel 规范化导出（格式居中/自适应列宽/元数据拦截）
    ├── models.py                    # 统一领域模型 (Table IR: ExtractedTable & TableCell)
    ├── system_detector.py           # 跨平台系统环境探测与路径自适应（macOS/Windows）
    ├── env_detector.py              # 运行环境依赖与 MCP/GPU 服务检测
    ├── common.py                    # 共享工具函数与 Markdown 表格语法判定
    ├── batch_planner.py             # 批量元数据与年份预分析（生成执行计划）
    ├── batch_run.py                 # 批量并发提取总控（并行调度/断点续传）
    ├── verify_tables.py             # 表格产物批量校验与空表/坏表清理
    ├── login.py                     # 期刊出版社 Cookie 注入与交互式登录向导
    ├── llm_reasoner.py              # LLM 辅助纠错与表格语义重构
    ├── agent_bridge.py              # Agent 交互桥接与进度事件通知
    ├── doclayout_yolo_detector.py   # YOLO 本地版面检测推理器
    ├── pdf_page_filter.py           # 页面轻量启发式预过滤（跳过纯正文无表页）
    ├── playwright_utils.py          # 浏览器环境自适应探测与启动辅助
    ├── table_validator.py           # 表格结构完整性与列数/填充率校验器
    ├── audit_reporter.py            # 8小时全面审查诊断报告生成器
    ├── targeted_audit_reporter.py   # 针对性异常排查报告生成器
    ├── exhaustive_audit_runner.py   # 全库地毯式提取质检执行器
    ├── targeted_audit_runner.py     # 针对特定异常样例文献的专项回归测试器
    ├── deep_8h_iterative_healer.py  # 深度迭代自愈与异常自动修复执行器
    ├── export_drawio_diagram.py     # 原生 draw.io 矢量架构图自动化渲染与导出
    ├── compat/                      # 跨版本兼容性包装层
    └── online/                      # 在线 HTML 提取子包
        ├── __init__.py
        ├── doi_resolver.py          # DOI/arXiv/URL 解析与发布商域名分流
        ├── strategies.py            # 多出版社直连 API 与 Scrapling 爬取策略
        ├── general_html_extractor.py# 通用出版社 Playwright 动态渲染抓取器
        ├── cnki_html_extractor.py   # 中国知网 (CNKI) 自动化滑块绕过与 HTML 阅读提取器
        └── graph.py                 # LangGraph / DAG 多策略并发竞速流水线
```

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

## 目录与版本控制规范

- **敏感凭据与会话隔离**：
  - `config.json` 包含私有 API 密钥，受 `.gitignore` 保护，严禁入库；请通过复制 `config.example.json` 生成本地私有配置；
  - `profiles/` 目录为运行时浏览器登录生成的临时/私有用户数据目录，内含会话 Cookie 与凭证，受 `.gitignore` 保护，严禁打包入库。
- **产物与缓存**：
  - `figures/` 专用于存放项目导出的高分辨率架构图素材；
  - `scratch/` 专用于临时测试与诊断数据脚本，受 `.gitignore` 保护；
  - `models/` 下的 ONNX 权重文件支持运行时按需下载与本地缓存。
