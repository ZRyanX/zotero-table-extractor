#!/usr/bin/env python3
"""
export_drawio_diagram.py — 生成 Zotero Table Extractor 全景详细架构流程图并调用 draw.io 导出为图片。
涵盖 v1.6.0 全部核心架构升级：
1. 七擎原生 PDF 多方投票融合矩阵 (GMFT + img2table + Tagged PDF + Camelot + pdfplumber + find_tables + text_alignment)
2. 双轨多模态 AI 视觉接管体系 (主轨 PP-StructureV3 + PaddleOCR-VL-1.6，备轨 IBM Docling TableFormer 本地离线备用)
3. DeepDoc / MinerU 列特征投影与单调 DP 保序对齐跨页续表合并引擎
4. PyZotero 与 Zotero-Better-Notes 生态深度集成 (自动附件挂载与 HTML 预览卡片生成)
"""

import os
import sys
import subprocess
import xml.etree.ElementTree as ET

# 路径定义
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIGURES_DIR = os.path.join(SKILL_ROOT, "figures")
os.makedirs(FIGURES_DIR, exist_ok=True)

def find_drawio_binary():
    import shutil
    candidate_bins = [
        "/Applications/draw.io.app/Contents/MacOS/draw.io",
        os.path.expanduser("~/Applications/draw.io.app/Contents/MacOS/draw.io"),
        r"C:\Program Files\draw.io\draw.io.exe",
        r"C:\Program Files (x86)\draw.io\draw.io.exe",
        "drawio",
    ]
    for c in candidate_bins:
        if os.path.isfile(c) or shutil.which(c):
            return c
    return "/Applications/draw.io.app/Contents/MacOS/draw.io"

DRAWIO_BIN = os.environ.get("DRAWIO_BIN") or find_drawio_binary()

ARTIFACT_DIR = os.environ.get("ANTIGRAVITY_ARTIFACT_DIR")
if ARTIFACT_DIR:
    ARTIFACT_FIG_DIR = os.path.join(ARTIFACT_DIR, "figures")
    os.makedirs(ARTIFACT_FIG_DIR, exist_ok=True)
    ARTIFACT_PNG = os.path.join(ARTIFACT_FIG_DIR, "zotero_table_extractor_architecture.png")
    ARTIFACT_SVG = os.path.join(ARTIFACT_FIG_DIR, "zotero_table_extractor_architecture.svg")
else:
    ARTIFACT_FIG_DIR = None
    ARTIFACT_PNG = None
    ARTIFACT_SVG = None

DRAWIO_FILE = os.path.join(FIGURES_DIR, "zotero_table_extractor_architecture.drawio")
PNG_FILE = os.path.join(FIGURES_DIR, "zotero_table_extractor_architecture.png")
SVG_FILE = os.path.join(FIGURES_DIR, "zotero_table_extractor_architecture.svg")


def build_architecture_drawio_xml() -> str:
    """构建包含 7 大核心层次、38+ 细分组件与完整数据流转的 draw.io XML (v1.6.0)。"""
    xml = r"""<mxfile host="app.diagrams.net" agent="drawio-mcp" version="24.0.0">
  <diagram id="zotero-table-extractor-arch" name="Zotero Table Extractor 全景详细架构流程图 (v1.6.0)">
    <mxGraphModel dx="2400" dy="1600" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="2480" pageHeight="1320" math="0" shadow="0">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>

        <!-- ===================================================================== -->
        <!-- 顶部全局主标题横幅 -->
        <!-- ===================================================================== -->
        <mxCell id="banner" value="Zotero Table Extractor 全景技术架构与数据流转详细流程图 (v1.6.0)&#xa;多源自适应提取 · 七擎原生多方投票 · 双轨多模态AI视觉接管 (PaddleOCR-VL + Docling) · DeepDoc 单调DP跨页缝合 · Zotero/Better-Notes 生态闭环" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#0F172A;strokeColor=#1E293B;fontColor=#F8FAFC;fontSize=17;fontStyle=1;fontFamily=PingFang SC,SimHei,sans-serif;spacingTop=4;shadow=1;" vertex="1" parent="1">
          <mxGeometry x="40" y="15" width="2390" height="60" as="geometry"/>
        </mxCell>

        <!-- ===================================================================== -->
        <!-- 第 1 列: 任务入口与调度层 -->
        <!-- ===================================================================== -->
        <mxCell id="col1" value="一、任务入口与调度层 (Entry &amp; Scheduling)" style="swimlane;whiteSpace=wrap;html=1;startSize=36;fillColor=#EBF8FF;strokeColor=#3182CE;fontColor=#1E3A8A;fontSize=13;fontStyle=1;rounded=1;arcSize=4;fontFamily=PingFang SC,SimHei;" vertex="1" parent="1">
          <mxGeometry x="40" y="105" width="310" height="1170" as="geometry"/>
        </mxCell>
        <mxCell id="c1_in" value="&lt;b style='font-size:12px;'&gt;【用户多态输入源】&lt;/b&gt;&lt;br/&gt;• Zotero 条目 / 本地 PDF 路径&lt;br/&gt;• 批量目录 / 清单文件 (.txt)&lt;br/&gt;• DOI / arXiv / 期刊网页 URL" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#DBEAFE;strokeColor=#93C5FD;fontColor=#1E40AF;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col1">
          <mxGeometry x="15" y="55" width="280" height="75" as="geometry"/>
        </mxCell>
        <mxCell id="c1_cli" value="&lt;b style='font-size:12px;'&gt;主 CLI 调度器&lt;/b&gt;&lt;br/&gt;&lt;i&gt;extract_zotero_table.py&lt;/i&gt;&lt;br/&gt;• 命令行解析 (--pdf, --output, --headers)&lt;br/&gt;• 模式分支: --online-only / --pdf-only&lt;br/&gt;• OCR引擎选择: --ocr-engine (auto/paddle/docling)&lt;br/&gt;• Zotero生态联动开关: --sync-zotero" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#BFDBFE;fontColor=#1E3A8A;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col1">
          <mxGeometry x="15" y="150" width="280" height="115" as="geometry"/>
        </mxCell>
        <mxCell id="c1_batch" value="&lt;b style='font-size:12px;'&gt;全库批量并发总控&lt;/b&gt;&lt;br/&gt;&lt;i&gt;batch_run.py&lt;/i&gt;&lt;br/&gt;• SQLite 只读快照克隆 (消除 DB 锁)&lt;br/&gt;• difflib.SequenceMatcher 论文名对齐&lt;br/&gt;• sys.executable 多进程隔离并发池&lt;br/&gt;• 单篇超时熔断 (默认 300s) &amp; 错误隔离" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#BFDBFE;fontColor=#1E3A8A;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col1">
          <mxGeometry x="15" y="285" width="280" height="115" as="geometry"/>
        </mxCell>
        <mxCell id="c1_plan" value="&lt;b style='font-size:12px;'&gt;批量预分析规划器&lt;/b&gt;&lt;br/&gt;&lt;i&gt;batch_planner.py&lt;/i&gt;&lt;br/&gt;• 文献元数据预扫描 (DOI/标题/年份)&lt;br/&gt;• is_pre_2020_chinese_paper 门禁判定&lt;br/&gt;  (2020前知网老文献直通本地 PDF 管线)&lt;br/&gt;• 构造 Plan 计划字典注入执行子进程" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#BFDBFE;fontColor=#1E3A8A;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col1">
          <mxGeometry x="15" y="430" width="280" height="110" as="geometry"/>
        </mxCell>
        <mxCell id="c1_supp" value="&lt;b style='font-size:12px;'&gt;外部附表下载器联动&lt;/b&gt;&lt;br/&gt;&lt;i&gt;journal-supp-downloader&lt;/i&gt;&lt;br/&gt;• 检测到独立附表资源时拉起外部技能&lt;br/&gt;• 下载 Supplementary Materials&lt;br/&gt;• 提取并输出独立表格至专用子目录" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#EFF6FF;strokeColor=#93C5FD;fontColor=#1D4ED8;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col1">
          <mxGeometry x="15" y="570" width="280" height="95" as="geometry"/>
        </mxCell>

        <!-- ===================================================================== -->
        <!-- 第 2 列: 跨平台基础与 Table IR 领域模型 -->
        <!-- ===================================================================== -->
        <mxCell id="col2" value="二、跨平台基础与领域模型 (Base &amp; IR)" style="swimlane;whiteSpace=wrap;html=1;startSize=36;fillColor=#F3E8FF;strokeColor=#9333EA;fontColor=#581C87;fontSize=13;fontStyle=1;rounded=1;arcSize=4;fontFamily=PingFang SC,SimHei;" vertex="1" parent="1">
          <mxGeometry x="380" y="105" width="310" height="1170" as="geometry"/>
        </mxCell>
        <mxCell id="c2_sys" value="&lt;b style='font-size:12px;'&gt;跨平台环境检测与自适应&lt;/b&gt;&lt;br/&gt;&lt;i&gt;system_detector.py / env_detector.py&lt;/i&gt;&lt;br/&gt;• OS 检测: macOS (Darwin) / Windows / Linux&lt;br/&gt;• macOS 原生保护: 历史路径 100% 保持不变&lt;br/&gt;• Windows 语义映射: D:\AIHub / %USERPROFILE%&lt;br/&gt;• Windows 终端 UTF-8 注入 (消除 GBK 崩溃)&lt;br/&gt;• adapt_path 通用路径无缝转换" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#D8B4FE;fontColor=#581C87;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col2">
          <mxGeometry x="15" y="55" width="280" height="135" as="geometry"/>
        </mxCell>
        <mxCell id="c2_common" value="&lt;b style='font-size:12px;'&gt;共享基础设施服务&lt;/b&gt;&lt;br/&gt;&lt;i&gt;common.py&lt;/i&gt;&lt;br/&gt;• load_config: 环境变量覆盖敏感密钥&lt;br/&gt;• browser_extraction_lock: 浏览器调试锁&lt;br/&gt;• 跨平台文件锁: fcntl (Unix) / msvcrt (Win)&lt;br/&gt;• clean_table_filename: 跨平台文件名脱敏" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#D8B4FE;fontColor=#581C87;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col2">
          <mxGeometry x="15" y="210" width="280" height="115" as="geometry"/>
        </mxCell>
        <mxCell id="c2_ir" value="&lt;b style='font-size:12px;'&gt;Table IR 统一领域模型&lt;/b&gt;&lt;br/&gt;&lt;i&gt;models.py&lt;/i&gt;&lt;br/&gt;• ExtractedTable: 核心表格实体&lt;br/&gt;  (df, label, title, page_idx, bbox, source)&lt;br/&gt;• TableCell: 细粒度行列与合并属性模型&lt;br/&gt;• sync_attrs: 模型属性与 df.attrs 双向同步&lt;br/&gt;• 多态支持: 字典访问协议 + DataFrame 代理" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FAF5FF;strokeColor=#C084FC;fontColor=#6B21A8;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col2">
          <mxGeometry x="15" y="345" width="280" height="130" as="geometry"/>
        </mxCell>
        <mxCell id="c2_trans" value="&lt;b style='font-size:12px;'&gt;多态反序列化适配层&lt;/b&gt;&lt;br/&gt;&lt;i&gt;ExtractedTable.from_any()&lt;/i&gt;&lt;br/&gt;• 兼容裸 DataFrame / Dict / ExtractedTable&lt;br/&gt;• 贯穿 Online / Native / OCR / Export 全流转&lt;br/&gt;• 解决全流程属性丢失与结构断裂问题" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#D8B4FE;fontColor=#581C87;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col2">
          <mxGeometry x="15" y="495" width="280" height="95" as="geometry"/>
        </mxCell>

        <!-- ===================================================================== -->
        <!-- 第 3 列: 在线 HTML 提取管线 -->
        <!-- ===================================================================== -->
        <mxCell id="col3" value="三、在线 HTML 提取管线 (Online Pipeline)" style="swimlane;whiteSpace=wrap;html=1;startSize=36;fillColor=#ECFDF5;strokeColor=#059669;fontColor=#064E3B;fontSize=13;fontStyle=1;rounded=1;arcSize=4;fontFamily=PingFang SC,SimHei;" vertex="1" parent="1">
          <mxGeometry x="720" y="105" width="310" height="1170" as="geometry"/>
        </mxCell>
        <mxCell id="c3_doi" value="&lt;b style='font-size:12px;'&gt;文献元数据智能解析探针&lt;/b&gt;&lt;br/&gt;&lt;i&gt;online/doi_resolver.py&lt;/i&gt;&lt;br/&gt;• PDF 首页纯文本正则提取 DOI / arXiv&lt;br/&gt;• Zotero 本地 SQLite 缓存快速命中&lt;br/&gt;• Crossref API 补全在线出版商元数据" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#6EE7B7;fontColor=#065F46;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col3">
          <mxGeometry x="15" y="55" width="280" height="95" as="geometry"/>
        </mxCell>
        <mxCell id="c3_route" value="&lt;b style='font-size:12px;'&gt;在线分流路由总控&lt;/b&gt;&lt;br/&gt;&lt;i&gt;online/graph.py&lt;/i&gt;&lt;br/&gt;• 出版商适配分发 (Elsevier/CNKI/Springer等)&lt;br/&gt;• race 竞速机制: 在线 HTML ∥ 本地 OCR&lt;br/&gt;• 表格数量完备性检查: 发现漏表降级本地" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#D1FAE5;strokeColor=#34D399;fontColor=#047857;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col3">
          <mxGeometry x="15" y="170" width="280" height="100" as="geometry"/>
        </mxCell>
        <mxCell id="c3_els" value="&lt;b style='font-size:12px;'&gt;Elsevier 专线直通提取&lt;/b&gt;&lt;br/&gt;&lt;i&gt;online/strategies.py (XOCS)&lt;/i&gt;&lt;br/&gt;• Elsevier XOCS 官方结构化 XML 接口&lt;br/&gt;• 提取原生 XML 表格节点 (100% 零误差)&lt;br/&gt;• 自动解析复合表头与多级行组" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#6EE7B7;fontColor=#065F46;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col3">
          <mxGeometry x="15" y="290" width="280" height="95" as="geometry"/>
        </mxCell>
        <mxCell id="c3_cnki" value="&lt;b style='font-size:12px;'&gt;CNKI 知网专用提取引擎&lt;/b&gt;&lt;br/&gt;&lt;i&gt;online/cnki_html_extractor.py&lt;/i&gt;&lt;br/&gt;• Scrapling 极速无头爬取 / Playwright 渲染&lt;br/&gt;• 动态滑块验证码绕过 &amp; Cookie 保持&lt;br/&gt;• 提取 HTML 数据表格并恢复地质专用字符" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#6EE7B7;fontColor=#065F46;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col3">
          <mxGeometry x="15" y="405" width="280" height="95" as="geometry"/>
        </mxCell>
        <mxCell id="c3_gen" value="&lt;b style='font-size:12px;'&gt;通用外文期刊 HTML 提取&lt;/b&gt;&lt;br/&gt;&lt;i&gt;online/general_html_extractor.py&lt;/i&gt;&lt;br/&gt;• Springer / Wiley / MDPI / ACS / RSC&lt;br/&gt;• HTML DOM 解析, rowspan/colspan 展平&lt;br/&gt;• 词边界罗马数字匹配 &amp; 目录表过滤排除" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#6EE7B7;fontColor=#065F46;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col3">
          <mxGeometry x="15" y="520" width="280" height="95" as="geometry"/>
        </mxCell>
        <mxCell id="c3_supp" value="&lt;b style='font-size:12px;'&gt;网页在线附表抽取器&lt;/b&gt;&lt;br/&gt;&lt;i&gt;online/strategies.py (Supplementary)&lt;/i&gt;&lt;br/&gt;• 探测并直接解析 Supplementary Tables&lt;br/&gt;• 跨页独立存储 &amp; 附表标签标准化" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#6EE7B7;fontColor=#065F46;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col3">
          <mxGeometry x="15" y="635" width="280" height="85" as="geometry"/>
        </mxCell>

        <!-- ===================================================================== -->
        <!-- 第 4 列: 本地 PDF 多级提取引擎 (7-Engine Voting & Dual AI) -->
        <!-- ===================================================================== -->
        <mxCell id="col4" value="四、本地 PDF 提取引擎 (7-Engine &amp; Dual AI)" style="swimlane;whiteSpace=wrap;html=1;startSize=36;fillColor=#FFFBEB;strokeColor=#D97706;fontColor=#78350F;fontSize=13;fontStyle=1;rounded=1;arcSize=4;fontFamily=PingFang SC,SimHei;" vertex="1" parent="1">
          <mxGeometry x="1060" y="105" width="340" height="1170" as="geometry"/>
        </mxCell>
        <mxCell id="c4_pte" value="&lt;b style='font-size:12px;'&gt;本地 PDF 提取总控调度&lt;/b&gt;&lt;br/&gt;&lt;i&gt;pdf_table_extractor.py&lt;/i&gt;&lt;br/&gt;• scan_pdf_table_declarations 全书表声明扫描&lt;br/&gt;• 全图扫描页检测 (words &lt; 25 且 images &gt; 0)&lt;br/&gt;• PyMuPDF 句柄 try...finally 安全析构防泄漏" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FEF3C7;strokeColor=#FCD34D;fontColor=#92400E;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col4">
          <mxGeometry x="15" y="55" width="310" height="80" as="geometry"/>
        </mxCell>
        <mxCell id="c4_vote" value="&lt;b style='font-size:12px;'&gt;七擎原生投票与仲裁矩阵 (Native 7-Engine)&lt;/b&gt;&lt;br/&gt;&lt;b&gt;1. Tagged PDF 结构树&lt;/b&gt;: 出版商语义节点绝对优先&lt;br/&gt;&lt;b&gt;2. GMFT (TATR)&lt;/b&gt;: Transformer 解析多级表头/跨列跨行&lt;br/&gt;&lt;b&gt;3. img2table&lt;/b&gt;: OpenCV 纯 Python 免 Ghostscript 依赖&lt;br/&gt;&lt;b&gt;4. Camelot&lt;/b&gt;: Lattice网格 / Stream文本流 (解析缓存)&lt;br/&gt;&lt;b&gt;5. pdfplumber&lt;/b&gt;: 矢量线框与单元格字符精准合并&lt;br/&gt;&lt;b&gt;6. PyMuPDF&lt;/b&gt;: page.find_tables() 极速矢状表格识别&lt;br/&gt;&lt;b&gt;7. text_alignment&lt;/b&gt;: 无线框文本空间聚类重建&lt;br/&gt;&lt;b style='color:#B45309;'&gt;→ three_way_vote 多方加权多数决仲裁&lt;/b&gt;&lt;br/&gt;  相似度 &gt;= 75% 达成共识采纳；列挤压自适应降级" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#FCD34D;fontColor=#92400E;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col4">
          <mxGeometry x="15" y="155" width="310" height="200" as="geometry"/>
        </mxCell>
        <mxCell id="c4_crops" value="&lt;b style='font-size:12px;'&gt;DocLayout-YOLO 目标切图与版面分析&lt;/b&gt;&lt;br/&gt;&lt;i&gt;doclayout_yolo_detector.py / pdf_tables.py&lt;/i&gt;&lt;br/&gt;• DocLayout-YOLO 本地优先切图定位 (单表高精裁剪)&lt;br/&gt;• PP-StructureV3 坐标切图补扫缺失区域&lt;br/&gt;• TurboOCR (doc_ori.onnx) 方向校准 (90°/180°/270°)" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#FCD34D;fontColor=#92400E;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col4">
          <mxGeometry x="15" y="370" width="310" height="105" as="geometry"/>
        </mxCell>
        <mxCell id="c4_dual_ai" value="&lt;b style='font-size:12px;'&gt;双轨视觉 AI 接管体系 (复杂表 / 扫描件)&lt;/b&gt;&lt;br/&gt;&lt;i&gt;ocr_client.py / pdf_table_extractor.py&lt;/i&gt;&lt;br/&gt;• &lt;b&gt;【主轨】PP-StructureV3 + PaddleOCR-VL-1.6&lt;/b&gt;&lt;br/&gt;  网页端全页/切图多模态大模型 (精准 HTML Table)&lt;br/&gt;• &lt;b&gt;【备轨】IBM Docling (TableFormer)&lt;/b&gt;&lt;br/&gt;  本地离线深度学习表格模型 (双活备用零外部依赖)&lt;br/&gt;• 云端 API 超时/限流/无网时自动无缝切换离线接管" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFF7ED;strokeColor=#FDBA74;fontColor=#C2410C;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col4">
          <mxGeometry x="15" y="495" width="310" height="165" as="geometry"/>
        </mxCell>
        <mxCell id="c4_vlm_parse" value="&lt;b style='font-size:12px;'&gt;VLM / Docling 结构化表格受限解析&lt;/b&gt;&lt;br/&gt;&lt;i&gt;parse_structured_vlm_content&lt;/i&gt;&lt;br/&gt;• 严格边界截断: 消除连续表跨表文本污染&lt;br/&gt;• 纯插图/图注识别过滤 (图4 矿物生成顺序图)&lt;br/&gt;• 中英双语表题优先保留中文 Caption" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#FCD34D;fontColor=#92400E;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col4">
          <mxGeometry x="15" y="675" width="310" height="95" as="geometry"/>
        </mxCell>
        <mxCell id="c4_reconcile" value="&lt;b style='font-size:12px;'&gt;全文表题与语种精准校准&lt;/b&gt;&lt;br/&gt;&lt;i&gt;reconcile_table_captions&lt;/i&gt;&lt;br/&gt;• 基于 PDF 文本层真实 Caption 空间坐标 (y0)&lt;br/&gt;• 语种感知 (中文文献杜绝 Table 1 泛化占位符)&lt;br/&gt;• 原生与 OCR 混合表号全文对齐与冲突消解" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FEF3C7;strokeColor=#F59E0B;fontColor=#78350F;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col4">
          <mxGeometry x="15" y="785" width="310" height="105" as="geometry"/>
        </mxCell>

        <!-- ===================================================================== -->
        <!-- 第 5 列: 质量门禁与自愈闭环 -->
        <!-- ===================================================================== -->
        <mxCell id="col5" value="五、质量门禁与自愈闭环 (Validator)" style="swimlane;whiteSpace=wrap;html=1;startSize=36;fillColor=#FEF2F2;strokeColor=#EF4444;fontColor=#7F1D1D;fontSize=13;fontStyle=1;rounded=1;arcSize=4;fontFamily=PingFang SC,SimHei;" vertex="1" parent="1">
          <mxGeometry x="1430" y="105" width="310" height="1170" as="geometry"/>
        </mxCell>
        <mxCell id="c5_pipe" value="&lt;b style='font-size:12px;'&gt;全流程原生检验总控&lt;/b&gt;&lt;br/&gt;&lt;i&gt;table_validator.py&lt;/i&gt;&lt;br/&gt;• validate_native_extraction_pipeline&lt;br/&gt;• 扫描 PDF 全书表声明并核验实际提取数&lt;br/&gt;• 连续性检查: 发现缺失表号直接打回 OCR" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FEE2E2;strokeColor=#F87171;fontColor=#991B1B;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col5">
          <mxGeometry x="15" y="55" width="280" height="95" as="geometry"/>
        </mxCell>
        <mxCell id="c5_rules" value="&lt;b style='font-size:12px;'&gt;深度单表数据质检规则&lt;/b&gt;&lt;br/&gt;&lt;i&gt;validate_native_table_dataframe&lt;/i&gt;&lt;br/&gt;• &lt;b&gt;尺寸规则&lt;/b&gt;: 必须满足 行&gt;=2 且 列&gt;=2&lt;br/&gt;• &lt;b&gt;切分规则&lt;/b&gt;: 排除空列&gt;=2 或 单字符碎列&gt;=2&lt;br/&gt;• &lt;b&gt;语义规则&lt;/b&gt;: 排除相图/散点图坐标轴残留 (kbar, T°C)&lt;br/&gt;• &lt;b&gt;内容规则&lt;/b&gt;: 排除版权、作者简介与参考文献&lt;br/&gt;&lt;b style='color:#DC2626;'&gt;→ 质检未通过直接打回，触发切图+双轨AI&lt;/b&gt;" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#FCA5A5;fontColor=#7F1D1D;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col5">
          <mxGeometry x="15" y="165" width="280" height="145" as="geometry"/>
        </mxCell>
        <mxCell id="c5_expand" value="&lt;b style='font-size:12px;'&gt;续表跨页上下文扩张机制&lt;/b&gt;&lt;br/&gt;&lt;i&gt;expanded_ocr_pages&lt;/i&gt;&lt;br/&gt;• 探测到续表声明时，自动向前后页扩张&lt;br/&gt;• 确保跨页连续分块完整送入 OCR/VLM 模型&lt;br/&gt;• 杜绝跨页续表上半截提取、下半截漏提" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#FCA5A5;fontColor=#7F1D1D;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col5">
          <mxGeometry x="15" y="330" width="280" height="95" as="geometry"/>
        </mxCell>
        <mxCell id="c5_agent" value="&lt;b style='font-size:12px;'&gt;Agent 协同自愈挽救闭环&lt;/b&gt;&lt;br/&gt;&lt;i&gt;agent_bridge.py / llm_reasoner.py&lt;/i&gt;&lt;br/&gt;• 极端崩塌表格派发异步自愈任务&lt;br/&gt;• LLM 推理辅助修复表头断裂与语义缺失&lt;br/&gt;• 轮询超时熔断与安全降级保护" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFF1F2;strokeColor=#FDA4AF;fontColor=#9F1239;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col5">
          <mxGeometry x="15" y="445" width="280" height="95" as="geometry"/>
        </mxCell>

        <!-- ===================================================================== -->
        <!-- 第 6 列: 流水线后处理与跨页拼接 (DeepDoc / MinerU DP) -->
        <!-- ===================================================================== -->
        <mxCell id="col6" value="六、流水线后处理与跨页拼接 (Post-process)" style="swimlane;whiteSpace=wrap;html=1;startSize=36;fillColor=#F0FDFA;strokeColor=#0D9488;fontColor=#134E4A;fontSize=13;fontStyle=1;rounded=1;arcSize=4;fontFamily=PingFang SC,SimHei;" vertex="1" parent="1">
          <mxGeometry x="1770" y="105" width="320" height="1170" as="geometry"/>
        </mxCell>
        <mxCell id="c6_post" value="&lt;b style='font-size:12px;'&gt;7阶安全后处理流水线总控&lt;/b&gt;&lt;br/&gt;&lt;i&gt;postprocess_dataframe&lt;/i&gt;&lt;br/&gt;• ExtractedTable 统一对象多态解耦透传&lt;br/&gt;• 付费墙/登录拦截表前置识别与隔离丢弃&lt;br/&gt;• &lt;b&gt;阶段A (VLM直通)&lt;/b&gt;: MultiIndex 高精直通跨页合并&lt;br/&gt;• &lt;b&gt;原生规则清洗&lt;/b&gt;: 依次经过碎片过滤、展开与脱敏" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#CCFBF1;strokeColor=#5EEAD4;fontColor=#115E59;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col6">
          <mxGeometry x="15" y="55" width="290" height="90" as="geometry"/>
        </mxCell>
        <mxCell id="c6_frag" value="&lt;b style='font-size:12px;'&gt;阶段B: 碎片表门禁&lt;/b&gt;&lt;br/&gt;列数 &gt; 30 且 填充率 &lt; 30% 视为无效碎片直接丢弃" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#5EEAD4;fontColor=#115E59;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col6">
          <mxGeometry x="15" y="165" width="290" height="55" as="geometry"/>
        </mxCell>
        <mxCell id="c6_header" value="&lt;b style='font-size:12px;'&gt;阶段C: 表头探测与多层重构&lt;/b&gt;&lt;br/&gt;&lt;i&gt;_detect_and_promote_header&lt;/i&gt;&lt;br/&gt;• 扫描前 80 行寻找真实科学关键词 (样品/含量/th)&lt;br/&gt;• 剥离误卷入的段落与期刊眉题&lt;br/&gt;• 两级复合子表头重塑合并 (Parent_Child)" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#5EEAD4;fontColor=#115E59;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col6">
          <mxGeometry x="15" y="240" width="290" height="95" as="geometry"/>
        </mxCell>
        <mxCell id="c6_expand" value="&lt;b style='font-size:12px;'&gt;阶段D: 数据单元格展开&lt;/b&gt;&lt;br/&gt;• expand_squeezed_columns (多数值紧缩列拆分)&lt;br/&gt;• expand_multiline_subrows (多行合并单元格展开)&lt;br/&gt;• populate_section_categories (地质矿石层位继承)" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#5EEAD4;fontColor=#115E59;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col6">
          <mxGeometry x="15" y="350" width="290" height="85" as="geometry"/>
        </mxCell>
        <mxCell id="c6_clean" value="&lt;b style='font-size:12px;'&gt;阶段E: 噪声清除与数值/公式转义&lt;/b&gt;&lt;br/&gt;• _filter_noise_and_placeholders (清除 Unnamed)&lt;br/&gt;• 剥离嵌入的 (continued) 噪声行&lt;br/&gt;• try_numeric (地学正负数/小数安全类型转换)&lt;br/&gt;• escape_formula (防 Excel 注入: =,+,-,@ 前置单引号)" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#5EEAD4;fontColor=#115E59;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col6">
          <mxGeometry x="15" y="450" width="290" height="100" as="geometry"/>
        </mxCell>
        <mxCell id="c6_merge" value="&lt;b style='font-size:12px;'&gt;跨页续表合并与单调 DP 对齐引擎&lt;/b&gt;&lt;br/&gt;&lt;i&gt;merge_continuation_tables / table_postprocess.py&lt;/i&gt;&lt;br/&gt;• &lt;b&gt;DeepDoc / MinerU 列特征投影&lt;/b&gt; (数值比/空值率/均长)&lt;br/&gt;• &lt;b&gt;单调动态规划 (Monotone DP) 保序对齐&lt;/b&gt;，杜绝跨页列乱序&lt;br/&gt;• 容错多页贯通拼接: 补齐缺失列、剥离重复表头/单位行/检出限&lt;br/&gt;• combine_df_group 纵向拼接 &amp; is_horizontal 横向分块合并" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#E6FFFA;strokeColor=#2DD4BF;fontColor=#0F766E;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col6">
          <mxGeometry x="15" y="570" width="290" height="150" as="geometry"/>
        </mxCell>

        <!-- ===================================================================== -->
        <!-- 第 7 列: 导出持久化与生态集成 (Export & Zotero Ecosystem) -->
        <!-- ===================================================================== -->
        <mxCell id="col7" value="七、导出持久化与生态集成 (Export &amp; Ecosystem)" style="swimlane;whiteSpace=wrap;html=1;startSize=36;fillColor=#FAF5FF;strokeColor=#7C3AED;fontColor=#4C1D95;fontSize=13;fontStyle=1;rounded=1;arcSize=4;fontFamily=PingFang SC,SimHei;" vertex="1" parent="1">
          <mxGeometry x="2120" y="105" width="310" height="1170" as="geometry"/>
        </mxCell>
        <mxCell id="c7_save" value="&lt;b style='font-size:12px;'&gt;多模式 Excel 导出总控&lt;/b&gt;&lt;br/&gt;&lt;i&gt;excel_export.py&lt;/i&gt;&lt;br/&gt;• save_tables_to_excel&lt;br/&gt;• is_supplementary 附表过滤判定&lt;br/&gt;• is_metadata_table 论文元数据/参考文献过滤" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#EDE9FE;strokeColor=#A78BFA;fontColor=#5B21B6;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col7">
          <mxGeometry x="15" y="55" width="280" height="85" as="geometry"/>
        </mxCell>
        <mxCell id="c7_modes" value="&lt;b style='font-size:12px;'&gt;双轨输出模式支持&lt;/b&gt;&lt;br/&gt;&lt;b&gt;1. 单文件多工作表模式 (.xlsx)&lt;/b&gt;&lt;br/&gt;   • 工作表安全命名与去重&lt;br/&gt;&lt;b&gt;2. 分目录独立文件模式&lt;/b&gt;&lt;br/&gt;   • Table 1.xlsx, Table 2.xlsx&lt;br/&gt;   • 自动生成 table_captions.txt 索引清单" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#C4B5FD;fontColor=#5B21B6;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col7">
          <mxGeometry x="15" y="155" width="280" height="105" as="geometry"/>
        </mxCell>
        <mxCell id="c7_autofit" value="&lt;b style='font-size:12px;'&gt;列宽自适应与系统预览兼容&lt;/b&gt;&lt;br/&gt;&lt;i&gt;autofit_excel_columns&lt;/i&gt;&lt;br/&gt;• openpyxl 采样前 200 行动态计算最佳列宽&lt;br/&gt;• 注入 Dublin Core 属性元数据&lt;br/&gt;• macOS QuickLook / Office 预览完美兼容" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#C4B5FD;fontColor=#5B21B6;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col7">
          <mxGeometry x="15" y="275" width="280" height="90" as="geometry"/>
        </mxCell>
        <mxCell id="c7_out" value="&lt;b style='font-size:13px;color:#15803D;'&gt;🎯 最终出版级 Excel 产物 (.xlsx)&lt;/b&gt;&lt;br/&gt;• 完备表号与中英文表题标注&lt;br/&gt;• 规范斑马纹与自适应列宽排版&lt;br/&gt;• 嵌入元数据与公式安全转义" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#DCFCE7;strokeColor=#86EFAC;fontColor=#166534;fontSize=11;align=center;" vertex="1" parent="col7">
          <mxGeometry x="15" y="380" width="280" height="95" as="geometry"/>
        </mxCell>
        <mxCell id="c7_sync" value="&lt;b style='font-size:12px;'&gt;PyZotero &amp; Better-Notes 生态集成&lt;/b&gt;&lt;br/&gt;&lt;i&gt;zotero_sync.py (v1.6.0 新增)&lt;/i&gt;&lt;br/&gt;• 本地 zotero.sqlite 逆向溯源 Parent Item Key&lt;br/&gt;• 自动挂载 .xlsx 表格为 Zotero 条目子附件&lt;br/&gt;• 自动生成 Better-Notes 富文本 HTML 预览卡片&lt;br/&gt;• 默认安全关闭保护 (ZOTERO_SYNC_ENABLED = false)" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#F3E8FF;strokeColor=#C084FC;fontColor=#6B21A8;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col7">
          <mxGeometry x="15" y="490" width="280" height="135" as="geometry"/>
        </mxCell>
        <mxCell id="c7_audit" value="&lt;b style='font-size:12px;'&gt;全周期质量审计工具集&lt;/b&gt;&lt;br/&gt;• &lt;i&gt;verify_tables.py&lt;/i&gt;: 提取结果核验&lt;br/&gt;• &lt;i&gt;exhaustive_audit_runner.py&lt;/i&gt;: 全库巡检&lt;br/&gt;• &lt;i&gt;targeted_audit_runner.py&lt;/i&gt;: 定向核查&lt;br/&gt;• &lt;i&gt;deep_8h_iterative_healer.py&lt;/i&gt;: 8h长效自愈&lt;br/&gt;• &lt;i&gt;targeted_audit_reporter.py&lt;/i&gt;: 报告生成" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#FAF5FF;strokeColor=#A78BFA;fontColor=#5B21B6;fontSize=11;align=left;spacingLeft=10;" vertex="1" parent="col7">
          <mxGeometry x="15" y="640" width="280" height="130" as="geometry"/>
        </mxCell>

        <!-- ===================================================================== -->
        <!-- 主干业务流转连线 (Connectors) -->
        <!-- ===================================================================== -->

        <!-- Col 1: 任务入口调度流 (单篇直通、批量规划并发与附表拉起闭环) -->
        <mxCell id="e1" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#2563EB;strokeWidth=2;" edge="1" source="c1_in" target="c1_cli" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e1_batch" value="批量清单/全库输入" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#3B82F6;strokeWidth=1.5;dashed=1;fontColor=#1D4ED8;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c1_in" target="c1_batch" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="45" y="197"/>
              <mxPoint x="45" y="447"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e1_plan" value="预分析规划注入" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#2563EB;strokeWidth=1.5;fontColor=#1D4ED8;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c1_plan" target="c1_batch" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e1_dispatch" value="并发派发单篇" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#2563EB;strokeWidth=2;fontColor=#1D4ED8;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c1_batch" target="c1_cli" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="345" y="447"/>
              <mxPoint x="345" y="312"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e1_supp" value="附表资源拉起" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#1D4ED8;strokeWidth=1.5;dashed=1;fontColor=#1D4ED8;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c1_cli" target="c1_supp" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="362" y="335"/>
              <mxPoint x="362" y="722"/>
            </Array>
          </mxGeometry>
        </mxCell>

        <!-- Col 2: 基础与领域模型关联流 -->
        <mxCell id="e2_support" value="跨平台环境注入" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#9333EA;strokeWidth=1.5;dashed=1;fontColor=#7E22CE;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c2_sys" target="c1_cli" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="362" y="227"/>
              <mxPoint x="362" y="275"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e2_ir_flow" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#9333EA;strokeWidth=1.5;" edge="1" source="c2_common" target="c2_ir" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e2_trans_flow" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#9333EA;strokeWidth=1.5;" edge="1" source="c2_ir" target="c2_trans" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>

        <!-- 主调度分流: 经走廊1-2右转北上跨列(x=354, x=372 -> y=82, y=92)，100% 杜绝穿透 Col 1 c1_in 与 Col 2 卡片 -->
        <mxCell id="e2" value="优先在线尝试" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#059669;strokeWidth=2;fontColor=#065F46;fontSize=11;labelBackgroundColor=#FFFFFF;" edge="1" source="c1_cli" target="c3_doi" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="354" y="265"/>
              <mxPoint x="354" y="82"/>
              <mxPoint x="810" y="82"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e7" value="漏表/无在线/PDF-Only" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#D97706;strokeWidth=2;fontColor=#B45309;fontSize=11;labelBackgroundColor=#FFFFFF;" edge="1" source="c1_cli" target="c4_pte" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="372" y="295"/>
              <mxPoint x="372" y="92"/>
              <mxPoint x="1230" y="92"/>
            </Array>
          </mxGeometry>
        </mxCell>

        <!-- Col 3: 在线 HTML 提取管线流转 (走 Col 2-3 走廊独立分发，杜绝垂直穿透) -->
        <mxCell id="e3" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#059669;strokeWidth=2;" edge="1" source="c3_doi" target="c3_route" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e4" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#059669;strokeWidth=1.5;" edge="1" source="c3_route" target="c3_els" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e5" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#059669;strokeWidth=1.5;" edge="1" source="c3_route" target="c3_cnki" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="710" y="325"/>
              <mxPoint x="710" y="557"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e6" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#059669;strokeWidth=1.5;" edge="1" source="c3_route" target="c3_gen" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="700" y="340"/>
              <mxPoint x="700" y="672"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e_c3_supp" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#059669;strokeWidth=1.5;" edge="1" source="c3_route" target="c3_supp" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="692" y="355"/>
              <mxPoint x="692" y="782"/>
            </Array>
          </mxGeometry>
        </mxCell>

        <!-- Col 4: 本地 PDF 7 引擎与双轨 AI 流转 -->
        <mxCell id="e8" value="七擎原生提取" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#D97706;strokeWidth=2;fontColor=#92400E;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c4_pte" target="c4_vote" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e9" value="低相似度/复杂表/扫描件" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#DC2626;strokeWidth=2;fontColor=#DC2626;fontSize=11;labelBackgroundColor=#FFFFFF;" edge="1" source="c4_vote" target="c4_crops" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e10" value="目标切图 / 全页图像" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#EA580C;strokeWidth=2;fontColor=#C2410C;fontSize=11;labelBackgroundColor=#FFFFFF;" edge="1" source="c4_crops" target="c4_dual_ai" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e11" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#EA580C;strokeWidth=1.5;" edge="1" source="c4_dual_ai" target="c4_vlm_parse" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <!-- e12: 高置信度多数决共识，走 Col4 左侧独立通道 (x=1045)，label 设在下方空白区 (x=0.75)，100% 杜绝与卡片重叠 -->
        <mxCell id="e12" value="多数决共识流 (相似度&gt;=75%)" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#16A34A;strokeWidth=2;fontColor=#16A34A;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c4_vote" target="c4_reconcile" parent="1">
          <mxGeometry x="0.75" relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="1045" y="360"/>
              <mxPoint x="1045" y="942"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e13" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#EA580C;strokeWidth=2;" edge="1" source="c4_vlm_parse" target="c4_reconcile" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>

        <!-- Col 4 与 Col 5 跨列通道隔离: e14(合格前向流, x=1420) 与 e16(质检打回重做, x=1410) 双轨平行无碰撞 -->
        <mxCell id="e14" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#EA580C;strokeWidth=2;" edge="1" source="c4_reconcile" target="c5_pipe" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="1420" y="942"/>
              <mxPoint x="1420" y="207"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e15" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#EF4444;strokeWidth=2;" edge="1" source="c5_pipe" target="c5_rules" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e16" value="质检打回重做" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#DC2626;strokeWidth=1.5;fontColor=#DC2626;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c5_rules" target="c4_crops" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="1410" y="342"/>
              <mxPoint x="1410" y="527"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e_expand" value="续表上下文扩张" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#EF4444;strokeWidth=1.5;fontColor=#991B1B;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c5_rules" target="c5_expand" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e_agent" value="极端崩塌自愈挽救" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#E11D48;strokeWidth=1.5;dashed=1;fontColor=#BE123C;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c5_expand" target="c5_agent" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>

        <!-- Col 5-6 汇流进后处理: e17(质检流 x=1748, y=85, 目标x=1910) 与 e18(在线流 x=1762, y=95, 目标x=1950) 彻底分离 -->
        <mxCell id="e17" value="质检合格表格流" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#0D9488;strokeWidth=2;fontColor=#0D9488;fontSize=11;labelBackgroundColor=#FFFFFF;" edge="1" source="c5_rules" target="c6_post" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="1748" y="342"/>
              <mxPoint x="1748" y="85"/>
              <mxPoint x="1910" y="85"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <mxCell id="e18" value="在线高精 HTML 表格直通" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#059669;strokeWidth=2;fontColor=#059669;fontSize=11;labelBackgroundColor=#FFFFFF;" edge="1" source="c3_gen" target="c6_post" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="1022" y="1020"/>
              <mxPoint x="1762" y="1020"/>
              <mxPoint x="1762" y="95"/>
              <mxPoint x="1950" y="95"/>
            </Array>
          </mxGeometry>
        </mxCell>

        <!-- Col 6 内部清洗流: 原生流逐级直下，VLM 直通走右侧专线 (x=2082, y=720) 严谨入 c6_merge -->
        <mxCell id="e19" value="原生清洗流" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#0D9488;strokeWidth=2;fontColor=#0F766E;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c6_post" target="c6_frag" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e20" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#0D9488;strokeWidth=1.5;" edge="1" source="c6_frag" target="c6_header" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e21" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#0D9488;strokeWidth=1.5;" edge="1" source="c6_header" target="c6_expand" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e22" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#0D9488;strokeWidth=1.5;" edge="1" source="c6_expand" target="c6_clean" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e23" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#0D9488;strokeWidth=2;" edge="1" source="c6_clean" target="c6_merge" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e24" value="VLM直通流" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#0D9488;strokeWidth=2;fontColor=#0F766E;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c6_post" target="c6_merge" parent="1">
          <mxGeometry relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="2082" y="205"/>
              <mxPoint x="2082" y="720"/>
            </Array>
          </mxGeometry>
        </mxCell>
        <!-- e25: 续表合并完成流，从 c6_merge(y=775) 出发，走 Col 6-7 走廊 (x=2105) 注入 c7_save(y=202.5) -->
        <mxCell id="e25" value="单调DP保序完成表格流" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#7C3AED;strokeWidth=2.5;fontColor=#5B21B6;fontSize=11;labelBackgroundColor=#FFFFFF;" edge="1" source="c6_merge" target="c7_save" parent="1">
          <mxGeometry x="-0.65" relative="1" as="geometry">
            <Array as="points">
              <mxPoint x="2105" y="775"/>
              <mxPoint x="2105" y="202"/>
            </Array>
          </mxGeometry>
        </mxCell>

        <!-- Col 7: 导出与生态闭环 -->
        <mxCell id="e26" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#7C3AED;strokeWidth=2;" edge="1" source="c7_save" target="c7_modes" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e27" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#7C3AED;strokeWidth=2;" edge="1" source="c7_modes" target="c7_autofit" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e28" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#16A34A;strokeWidth=2.5;" edge="1" source="c7_autofit" target="c7_out" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e29" value="可选生态闭环挂载" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#7C3AED;strokeWidth=2;fontColor=#6B21A8;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c7_out" target="c7_sync" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="e30" value="全周期质量审计与自愈核验" style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;strokeColor=#9333EA;strokeWidth=1.5;dashed=1;fontColor=#6B21A8;fontSize=10;labelBackgroundColor=#FFFFFF;" edge="1" source="c7_sync" target="c7_audit" parent="1">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>

      </root>
    </mxGraphModel>
  </diagram>
</mxfile>"""
    return xml


def export_images():
    print(f"[1/4] 正在生成 draw.io 架构流程图 XML 文件: {DRAWIO_FILE}")
    xml_content = build_architecture_drawio_xml()
    try:
        ET.fromstring(xml_content)
        print("  ✓ XML 格式验证通过 (Well-formed XML)")
    except Exception as e:
        print(f"  ✗ XML 解析校验异常: {e}")
        raise

    with open(DRAWIO_FILE, "w", encoding="utf-8") as f:
        f.write(xml_content)

    print(f"[2/4] 正在调用 draw.io 导出高清 PNG 图片 (2x 缩放)...")
    cmd_png = [
        DRAWIO_BIN,
        "-x",
        "-f", "png",
        "--scale", "2",
        "-b", "20",
        "-o", PNG_FILE,
        DRAWIO_FILE
    ]
    ret_png = subprocess.run(cmd_png, capture_output=True, text=True)
    if ret_png.returncode == 0:
        print(f"  ✓ 成功导出 PNG 图片: {PNG_FILE}")
    else:
        print(f"  ✗ 导出 PNG 异常: {ret_png.stderr}")

    print(f"[3/4] 正在调用 draw.io 导出矢量 SVG 图像...")
    cmd_svg = [
        DRAWIO_BIN,
        "-x",
        "-f", "svg",
        "--embed-svg-fonts", "true",
        "-b", "20",
        "-o", SVG_FILE,
        DRAWIO_FILE
    ]
    ret_svg = subprocess.run(cmd_svg, capture_output=True, text=True)
    if ret_svg.returncode == 0:
        print(f"  ✓ 成功导出 SVG 图像: {SVG_FILE}")
    else:
        print(f"  ✗ 导出 SVG 异常: {ret_svg.stderr}")

    if ARTIFACT_PNG and ARTIFACT_SVG:
        print(f"[4/4] 正在同步图像成果至 Artifacts 目录...")
        import shutil
        shutil.copy2(PNG_FILE, ARTIFACT_PNG)
        shutil.copy2(SVG_FILE, ARTIFACT_SVG)
        print(f"  ✓ 已同步至: {ARTIFACT_PNG}")
        print(f"  ✓ 已同步至: {ARTIFACT_SVG}")
    else:
        print("[4/4] 未设置 ANTIGRAVITY_ARTIFACT_DIR，跳过 Artifacts 副本同步。")

    print("\n全部导出任务执行完成！")


if __name__ == "__main__":
    export_images()
