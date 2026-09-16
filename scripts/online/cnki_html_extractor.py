import os
import random
import time
import pandas as pd
from playwright.sync_api import sync_playwright

try:
    from ..playwright_utils import find_playwright_chromium
except ImportError:
    try:
        from playwright_utils import find_playwright_chromium
    except ImportError:
        find_playwright_chromium = lambda: None


# Shared helpers (text cleaning, table filtering, cookie parsing)
try:
    from ..common import clean_text, escape_formula, is_metadata_table, clean_table_filename, load_cookies_from_file, df_map
except ImportError:
    from common import clean_text, escape_formula, is_metadata_table, clean_table_filename, load_cookies_from_file, df_map

def simulate_human_drag(page, handler_selector, distance):
    handler = page.wait_for_selector(handler_selector, timeout=15000)
    box = handler.bounding_box()
    if not box:
        raise Exception("Could not find bounding box for slider handler.")
        
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    
    print(f"[知网滑块模拟] 发现滑块中心点: X={start_x:.1f}, Y={start_y:.1f}，需拖拽距离: {distance:.1f}px")
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    
    # 模拟人类手拖动轨迹：非等速、带抖动
    steps = random.randint(30, 50)
    for i in range(1, steps + 1):
        t = i / steps
        # Easing function (ease-in-out)
        ease_t = 3 * t**2 - 2 * t**3 if t < 1 else 1
        
        target_x = start_x + distance * ease_t
        target_y = start_y + random.uniform(-1.5, 1.5)
        
        page.mouse.move(target_x, target_y)
        time.sleep(random.uniform(0.006, 0.018))
        
    # 终点释放
    page.mouse.move(start_x + distance, start_y)
    time.sleep(0.1)
    page.mouse.up()
    print("[知网滑块模拟] 拖动模拟完成，等待验证结果。")

def extract_tables_from_page(page, table_idx="all"):
    """Extracts tables from the current page and converts them to DataFrames."""
    tables = page.query_selector_all("table")
    print(f"[提取] 页面中检测到 {len(tables)} 个表格。")
    
    all_dfs = []
    for t_idx, t in enumerate(tables):
        # Extract the raw table title from the DOM
        raw_title = ""
        try:
            raw_title = t.evaluate("""
                (table) => {
                    // 1. Try caption
                    let caption = table.querySelector("caption");
                    if (caption && caption.innerText.trim()) {
                        return caption.innerText.trim();
                    }
                    
                    // 2. Look at previous siblings (up to 3 elements)
                    let prev = table.previousElementSibling;
                    for (let i = 0; i < 3 && prev; i++) {
                        let text = prev.innerText || prev.textContent || "";
                        text = text.trim();
                        if (text && (text.includes("表") || text.toLowerCase().includes("table") || text.toLowerCase().includes("supplementary material") || text.toLowerCase().includes("additional table") || text.toLowerCase().includes("additional file"))) {
                            return text;
                        }
                        prev = prev.previousElementSibling;
                    }
                    
                    // 3. Look at parent's previous siblings
                    let parent = table.parentElement;
                    if (parent) {
                        let pPrev = parent.previousElementSibling;
                        for (let i = 0; i < 2 && pPrev; i++) {
                            let text = pPrev.innerText || pPrev.textContent || "";
                            text = text.trim();
                            if (text && (text.includes("表") || text.toLowerCase().includes("table") || text.toLowerCase().includes("supplementary material") || text.toLowerCase().includes("additional table") || text.toLowerCase().includes("additional file"))) {
                                return text;
                            }
                            pPrev = pPrev.previousElementSibling;
                        }
                    }
                    return "";
                }
            """)
        except Exception as e:
            print(f"Warning: Failed to fetch table title from DOM: {e}")
            
        rows_data = []
        rows = t.query_selector_all("tr")
        for r in rows:
            cells = r.query_selector_all("td, th")
            cells_text = [clean_text(c.inner_text()) for c in cells]
            if any(cells_text):
                rows_data.append(cells_text)
                
        if rows_data:
            # Pad row columns
            max_cols = max(len(r) for r in rows_data)
            for i in range(len(rows_data)):
                if len(rows_data[i]) < max_cols:
                    rows_data[i] = rows_data[i] + [""] * (max_cols - len(rows_data[i]))
                    
            header_row = rows_data[0]
            data_rows = rows_data[1:]
            
            # Make headers unique
            seen = {}
            unique_header = []
            for col in header_row:
                if not col:
                    col = "Unnamed"
                if col in seen:
                    seen[col] += 1
                    unique_header.append(f"{col}_{seen[col]}")
                else:
                    seen[col] = 0
                    unique_header.append(col)
                    
            df = pd.DataFrame(data_rows, columns=unique_header)
            
            # Check if this is a metadata/references table
            if is_metadata_table(df):
                print(f"  -> 表格 {t_idx+1}: 检测为文献信息/收稿日期/参考文献表，自动跳过输出。")
                continue
                
            # Escape formulas
            df.columns = [escape_formula(c) for c in df.columns]
            df = df_map(df, escape_formula)
                
            # Set custom title attribute
            cleaned_title = clean_table_filename(raw_title, t_idx + 1)
            df.attrs['table_title'] = cleaned_title
            
            all_dfs.append(df)
            print(f"  -> 表格 {t_idx+1}: 成功提取 {len(data_rows)} 行，{len(unique_header)} 列。 标题: {cleaned_title}")
            
    if not all_dfs:
        return []
        
    if table_idx == "all":
        return all_dfs
    elif table_idx == "first":
        return [all_dfs[0]]
    else:
        try:
            idx = int(table_idx)
            if idx < len(all_dfs):
                return [all_dfs[idx]]
        except ValueError:
            pass
        return all_dfs

def extract_cnki_html_tables(url, cookies_path=None, table_idx="all", headed=False, user_data_dir=None):
    """
    Automated pipeline to bypass CNKI slider and extract tables from HTML Reader page.
    """
    headless = not headed
    print(f"[知网自动化] 启动 HTML 阅读提取器 (headless={headless}, user_data_dir={user_data_dir})...")
    
    exec_path = find_playwright_chromium()
    if exec_path:
        print(f"[Playwright] 使用自动识别的 Chromium 路径: {exec_path}")
    connected_via_cdp = False
    with sync_playwright() as p:
        browser = None
        try:
            import urllib.request
            # Check if port 9222 is open and listening
            with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1.5) as response:
                if response.status == 200:
                    print("[知网自动化] 检测到有运行在 9222 端口的 Chrome 进程。正在进行 CDP 桥接...")
                    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.new_page()
                    connected_via_cdp = True
                    print("[知网自动化] CDP 桥接成功！将使用您当前活动的物理 Chrome 窗口（可直接免去验证码）。")
        except Exception:
            pass

        if not connected_via_cdp:
            if user_data_dir:
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=headless,
                    executable_path=exec_path,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                page = context.pages[0] if context.pages else context.new_page()
                cookies_json = os.path.join(user_data_dir, "cookies.json")
                if os.path.exists(cookies_json):
                    try:
                        import json
                        with open(cookies_json, 'r', encoding='utf-8') as f:
                            c_list = json.load(f)
                        if c_list:
                            context.add_cookies(c_list)
                            print(f"[知网自动化] 从 cookies.json 注入了 {len(c_list)} 个会话 Cookies。")
                    except Exception:
                        pass
            else:
                browser = p.chromium.launch(headless=headless, executable_path=exec_path)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                page = context.new_page()

        # 拦截 mailto:, tel: 等外部系统协议，防止 macOS 弹出系统 Mail.app 邮件应用
        try:
            page.route(
                lambda u: any(u.lower().startswith(p) for p in ["mailto:", "tel:", "sms:", "itms:", "macappstore:"]),
                lambda route: route.abort()
            )
        except Exception:
            pass
            
        # Load cookies
        if cookies_path and os.path.exists(cookies_path):
            cookies = load_cookies_from_file(cookies_path)
            if cookies:
                try:
                    context.add_cookies(cookies)
                    print(f"[知网自动化] 已加载 {len(cookies)} 个 Cookie。")
                except Exception as e:
                    print(f"[知网自动化] 加载 Cookie 报错: {e}")
                    
        print(f"[知网自动化] 正在载入详情页: {url}")
        page.goto(url, wait_until="networkidle", timeout=60000)
        
        # ── China DOI (chndoi.org / chinadoi.cn) Multi-resolution Chooser ──
        if "chndoi.org" in page.url or "chinadoi.cn" in page.url or page.locator("text=多重解析地址").count() > 0:
            print("[知网自动化] 检测到 China DOI 多重解析页面，自动提取【境内】知网地址...")
            try:
                link_el = page.locator("li:has-text('境内') a, a:has-text('境内')").first
                if link_el.count() > 0:
                    dom_url = link_el.get_attribute("href")
                    if dom_url:
                        print(f"[知网自动化] 自动跳转至境内地址: {dom_url}")
                        page.goto(dom_url, wait_until="networkidle", timeout=60000)
            except Exception:
                pass

        # Click HTML Reading button
        html_button = page.locator("a:has-text('HTML阅读')").first
        if not html_button.is_visible(timeout=5000):
            print("[知网自动化] 未找到 'HTML阅读' 按钮，可能未登录或无阅读权限。")
            if connected_via_cdp:
                try:
                    page.close()
                except Exception:
                    pass
            elif not user_data_dir:
                browser.close()
            else:
                context.close()
            return [], None
            
        print("[知网自动化] 发现 'HTML阅读' 按钮，正在跳转至阅读页面...")
        with context.expect_page() as new_page_info:
            html_button.click()
            
        new_page = new_page_info.value
        new_page.wait_for_load_state("networkidle", timeout=60000)
        new_page.wait_for_timeout(3000)
        
        # Check for slider verification
        wrapper = new_page.query_selector(".slider-wrapper")
        if wrapper:
            print("[知网自动化] 检测到滑动验证，正在启动自动滑块模拟...")
            wrapper_box = wrapper.bounding_box()
            handler = new_page.query_selector("#js-handler")
            handler_box = handler.bounding_box()
            
            if wrapper_box and handler_box:
                drag_distance = wrapper_box["width"] - handler_box["width"] + 10
                try:
                    simulate_human_drag(new_page, "#js-handler", drag_distance)
                    new_page.wait_for_timeout(5000) # Wait for page contents to render
                except Exception as e:
                    print(f"【错误】模拟滑动失败: {e}")
            else:
                print("【错误】未能获取滑块的布局尺寸，跳过滑动。")
        else:
            print("[知网自动化] 无需滑动验证，直接提取表格。")
            
        # Parse tables and get paper title from the page title
        page_title = new_page.title()
        if page_title:
            for pattern in [" - 中国知网", " - 手机知网", " - kns.cnki.net", "-中国知网", "-手机知网", "HTML阅读-", "HTML阅读_"]:
                if pattern in page_title:
                    page_title = page_title.replace(pattern, "").strip()
        print(f"[知网自动化] 获取到文献标题: {page_title}")
        
        dfs = extract_tables_from_page(new_page, table_idx)
        
        if connected_via_cdp:
            try:
                page.close()
                new_page.close()
            except Exception:
                pass
        elif not user_data_dir:
            browser.close()
        else:
            context.close()
            
        return dfs, page_title
