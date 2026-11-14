#!/usr/bin/env python3
"""
cdp_detector.py — CDP DevToolsActivePort 动态端口探测与 Port Guard 反风控拦截模块。

核心升级点（吸收 eze-is/web-access 核心机制并做 Python 原生深度强化）：
1. 动态端口探测 (DevToolsActivePort)：
   彻底打破硬编码 127.0.0.1:9222。自动扫描 Chromium 家族（Chrome, Edge, Brave, Chromium, Arc, Vivaldi）
   的数据目录，优先按最近修改时间探测活动端口与 WebSocket 路径，内存直连，规避端口冲突。
2. 调试端口主动屏蔽 (CDP Port Guard)：
   通过 Playwright 原生请求拦截与 in-page WebSocket 虚拟代理，将外部网页（如 Cloudflare Turnstile, DataDome, Akamai）
   对本地 127.0.0.1:{cdp_port}、localhost、0.0.0.0、IPv6 loopback 等各变种形式以及 9222 调试端口的
   HTTP/Fetch 与 WebSocket 探测请求伪装为 ConnectionRefused，彻底瓦解反自动化指纹识别。
3. 共享浏览器 Tab 级并发控制：
   提供轻量级进程内信号量与跨进程槽位文件锁（cdp_tab_slot_guard），避免多任务全局排队死锁，
   同时严格限制多子进程批量运行时的活动 Tab 总数，杜绝内存雪崩。
"""

import os
import sys
import re
import json
import time
import shutil
import subprocess
import platform
import tempfile
import threading
import contextlib
import urllib.parse
import urllib.request
from typing import Optional, Dict, Any, List, Set, Union, Tuple

_CDP_TAB_SEMAPHORE = None
_CDP_TAB_MAX_COUNT = 6
_CDP_TAB_LOCK = threading.Lock()


def get_cdp_tab_semaphore(max_tabs: int = 6) -> threading.Semaphore:
    """获取 CDP 标签页级轻量并发信号量，支持多 Tab 隔离并行。"""
    global _CDP_TAB_SEMAPHORE, _CDP_TAB_MAX_COUNT
    with _CDP_TAB_LOCK:
        if _CDP_TAB_SEMAPHORE is None or _CDP_TAB_MAX_COUNT != max_tabs:
            _CDP_TAB_SEMAPHORE = threading.Semaphore(max_tabs)
            _CDP_TAB_MAX_COUNT = max_tabs
        return _CDP_TAB_SEMAPHORE


@contextlib.contextmanager
def cdp_tab_slot_guard(max_tabs: int = 6, timeout: float = 120.0):
    """
    CDP 标签页级轻量并发调度锁（进程内多线程 + 跨进程多子进程双重保护）：
    1. 进程内：获取 threading.Semaphore(max_tabs)，防止同进程内过多并发请求；
    2. 跨进程：通过系统级非阻塞槽位文件锁（fcntl/msvcrt 槽位 0..max_tabs-1），
       确保即使 batch_run.py 启动数十个子进程并发，活动 CDP 浏览器内的并发 Tab 数量
       也严格被限制在 max_tabs 阈值内，避免 Chrome 内存雪崩或崩溃。
    """
    sem = get_cdp_tab_semaphore(max_tabs)
    sem_acquired = sem.acquire(timeout=timeout)
    if not sem_acquired:
        raise TimeoutError("CDP tab concurrency semaphore acquisition timed out.")

    acquired_slot = None
    lock_file = None
    deadline = time.time() + timeout

    try:
        while time.time() < deadline:
            for slot_idx in range(max_tabs):
                slot_path = os.path.join(tempfile.gettempdir(), f"zotero_cdp_tab_slot_{slot_idx}.lock")
                try:
                    f = open(slot_path, "a+")
                    if os.name == "nt":
                        import msvcrt
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired_slot = slot_idx
                    lock_file = f
                    break
                except Exception:
                    try:
                        f.close()
                    except Exception:
                        pass
            if acquired_slot is not None:
                break
            time.sleep(0.05)

        if acquired_slot is None:
            raise TimeoutError("Could not acquire cross-process CDP tab slot lock.")

        yield acquired_slot
    finally:
        if lock_file:
            try:
                if os.name == "nt":
                    import msvcrt
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except Exception:
                pass
        try:
            sem.release()
        except Exception:
            pass


def get_user_data_candidate_dirs(custom_dir: Optional[str] = None) -> List[str]:
    """
    收集当前操作系统下 Chrome、Edge、Brave 等 Chromium 浏览器的 User Data 候选目录。
    """
    candidates = []
    if custom_dir:
        abs_custom = os.path.abspath(os.path.expanduser(custom_dir))
        candidates.append(abs_custom)
        # 若传入的是 Profile 子目录（如 Default），其父级目录也作为候选
        parent = os.path.dirname(abs_custom)
        if os.path.basename(abs_custom).lower() in ("default", "profile 1", "profile 2"):
            candidates.append(parent)

    system = platform.system()
    home = os.path.expanduser("~")

    if system == "Darwin":
        app_support = os.path.join(home, "Library", "Application Support")
        candidates.extend([
            os.path.join(app_support, "Google", "Chrome"),
            os.path.join(app_support, "Google", "Chrome Beta"),
            os.path.join(app_support, "Google", "Chrome Canary"),
            os.path.join(app_support, "Microsoft Edge"),
            os.path.join(app_support, "Microsoft Edge Beta"),
            os.path.join(app_support, "BraveSoftware", "Brave-Browser"),
            os.path.join(app_support, "Chromium"),
            os.path.join(app_support, "Arc", "User Data"),
            os.path.join(app_support, "Vivaldi"),
            os.path.join(home, ".zotero_chrome_debug_profile"),
            os.path.join(home, ".zotero_playwright_profile"),
            os.path.join(tempfile.gettempdir(), "chrome_debug_profile"),
        ])
    elif system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            candidates.extend([
                os.path.join(local_app_data, "Google", "Chrome", "User Data"),
                os.path.join(local_app_data, "Google", "Chrome Beta", "User Data"),
                os.path.join(local_app_data, "Microsoft", "Edge", "User Data"),
                os.path.join(local_app_data, "BraveSoftware", "Brave-Browser", "User Data"),
                os.path.join(local_app_data, "Chromium", "User Data"),
                os.path.join(local_app_data, "Vivaldi", "User Data"),
            ])
        user_profile = os.environ.get("USERPROFILE", home)
        candidates.extend([
            os.path.join(user_profile, ".zotero_chrome_debug_profile"),
            os.path.join(user_profile, ".zotero_playwright_profile"),
            os.path.join(tempfile.gettempdir(), "chrome_debug_profile"),
        ])
    else:  # Linux
        config_dir = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
        candidates.extend([
            os.path.join(config_dir, "google-chrome"),
            os.path.join(config_dir, "google-chrome-beta"),
            os.path.join(config_dir, "microsoft-edge"),
            os.path.join(config_dir, "BraveSoftware", "Brave-Browser"),
            os.path.join(config_dir, "chromium"),
            os.path.join(config_dir, "vivaldi"),
            os.path.join(home, ".zotero_chrome_debug_profile"),
            os.path.join(home, ".zotero_playwright_profile"),
            os.path.join(tempfile.gettempdir(), "chrome_debug_profile"),
        ])

    # 去重并仅保留存在的目录
    seen = set()
    valid_dirs = []
    for d in candidates:
        norm = os.path.normpath(d)
        if norm not in seen and os.path.isdir(norm):
            seen.add(norm)
            valid_dirs.append(norm)

    return valid_dirs


def check_cdp_endpoint_alive(host: str = "127.0.0.1", port: int = 9222, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """
    向 http://{host}:{port}/json/version 发起轻量健康探针。
    若存活并返回有效版本信息，返回包含端口与 WebSocket 路径的字典；否则返回 None。
    杜绝将非 CDP 的随机本地 HTTP 端口误判为浏览器。
    """
    probe_url = f"http://{host}:{port}/json/version"
    try:
        req = urllib.request.Request(probe_url, headers={"User-Agent": "ZoteroTableExtractor-CDPProbe/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                ws_url = data.get("webSocketDebuggerUrl") or ""
                # 校验 CDP 关键指纹，杜绝普通 HTTP 服务假阳性
                if not ws_url and not data.get("Browser") and not data.get("Protocol-Version"):
                    return None
                return {
                    "host": host,
                    "port": port,
                    "cdp_url": f"http://{host}:{port}",
                    "ws_url": ws_url,
                    "browser": data.get("Browser", ""),
                    "protocol_version": data.get("Protocol-Version", ""),
                }
    except Exception:
        pass
    return None


def read_devtools_active_port(file_path: str) -> Optional[Dict[str, Any]]:
    """
    解析 DevToolsActivePort 文件内容。
    文件格式规范：
      第 1 行: 端口号 (如 54321 或 9222)
      第 2 行: WebSocket 路径 (如 /devtools/browser/uuid)
    """
    try:
        if not os.path.isfile(file_path):
            return None
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
        if not lines:
            return None
        port = int(lines[0])
        if not (1 <= port <= 65535):
            return None
        raw_ws = lines[1] if len(lines) > 1 else ""
        ws_path = ("/" + raw_ws.lstrip("/")) if raw_ws else ""
        return {"port": port, "ws_path": ws_path}
    except Exception:
        return None


def find_active_devtools_port(custom_user_data_dir: Optional[str] = None, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """
    通过扫描各 Chromium 浏览器目录下的 DevToolsActivePort 文件，定位当前活动的调试端口。
    自动按文件修改时间由近及远排序，优先探测最新活跃的浏览器实例；
    并通过 /json/version 进行探针鉴活，杜绝历史残留死端口（Stale Port）。
    """
    candidate_dirs = get_user_data_candidate_dirs(custom_user_data_dir)
    discovered_files = []

    for udir in candidate_dirs:
        for fname in ["DevToolsActivePort", os.path.join("Default", "DevToolsActivePort")]:
            active_file = os.path.join(udir, fname)
            if os.path.isfile(active_file):
                try:
                    mtime = os.path.getmtime(active_file)
                except Exception:
                    mtime = 0
                discovered_files.append((mtime, udir, active_file))

    # 按最新修改时间优先探测
    discovered_files.sort(key=lambda x: x[0], reverse=True)

    for _, udir, active_file in discovered_files:
        parsed = read_devtools_active_port(active_file)
        if parsed:
            port = parsed["port"]
            info = check_cdp_endpoint_alive(port=port, timeout=timeout)
            if info:
                info["user_data_dir"] = udir
                info["active_port_file"] = active_file
                if parsed.get("ws_path") and not info.get("ws_url"):
                    info["ws_url"] = f"ws://127.0.0.1:{port}{parsed['ws_path']}"
                return info
    return None


def find_active_cdp_endpoint(timeout: float = 1.0, user_data_dir: Optional[str] = None, config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    综合定位当前可用的 CDP 调试端点（三阶自愈发现）：
    1. 显式配置优先：读取 config / 环境变量 中的 CDP_URL 或 CRAWL4AI_CDP_URL；
    2. 动态端口发现：优先读取 DevToolsActivePort，定位活动中的 Chrome/Edge/Brave 调试实例；
    3. 默认端口兜底：探测 127.0.0.1:9222。
    若发现可用端点，返回字典信息；若无，返回 None。
    """
    if config is None:
        try:
            from .. import common
            config = common.load_config()
        except Exception:
            try:
                import common
                config = common.load_config()
            except Exception:
                config = {}

    # 阶 1: 显式配置（环境变或配置项）
    explicit_url = ""
    if config:
        explicit_url = (config.get("CDP_URL") or config.get("CRAWL4AI_CDP_URL") or "").strip()
    if not explicit_url:
        explicit_url = (os.environ.get("CDP_URL") or os.environ.get("CRAWL4AI_CDP_URL") or "").strip()

    if explicit_url:
        m = re.match(r"^https?://([^:/]+)(?::(\d+))?", explicit_url)
        if m:
            host = m.group(1)
            port = int(m.group(2)) if m.group(2) else 80
            alive = check_cdp_endpoint_alive(host=host, port=port, timeout=timeout)
            if alive:
                alive["source"] = "config"
                return alive

    # 阶 2: 动态 DevToolsActivePort 发现
    dyn = find_active_devtools_port(custom_user_data_dir=user_data_dir, timeout=timeout)
    if dyn:
        dyn["source"] = "devtools_active_port"
        return dyn

    # 阶 3: 默认 9222 端口探测
    probe_9222 = check_cdp_endpoint_alive(port=9222, timeout=timeout)
    if probe_9222:
        probe_9222["source"] = "default_9222"
        return probe_9222

    return None


def get_active_cdp_url(user_data_dir: Optional[str] = None, timeout: float = 1.0, config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """便捷接口：直接返回活动 CDP 的 HTTP URL 字符串（如 'http://127.0.0.1:54321'），无则返回 None。"""
    endpoint = find_active_cdp_endpoint(timeout=timeout, user_data_dir=user_data_dir, config=config)
    return endpoint["cdp_url"] if endpoint else None


# ---------------------------------------------------------------------------
# CDP Port Guard（反调试端口探测与指纹伪装）
# ---------------------------------------------------------------------------

_COMMON_DEBUG_PORTS = {9222, 9229, 9223, 9333}

def install_cdp_port_guard(target: Any, cdp_port: Optional[Union[int, str]] = None, extra_ports: Optional[List[int]] = None) -> bool:
    """
    在 Playwright Page 或 BrowserContext 上安装 CDP Port Guard 拦截器。
    
    【攻防原理】
    Cloudflare Turnstile、DataDome、Akamai 等反爬虫脚本会在页面加载时在前端执行 JS，
    向 127.0.0.1:9222、localhost:9222、0.0.0.0、[::1]、[0:0:0:0:0:0:0:1] 或动态调试端口
    发起 HTTP/Fetch 或 WebSocket 探测。
    如果探测返回 200/404 或 TCP 建立成功，则判定存在自动化 CDP 环境。
    
    本函数在浏览器内部完成双重防御：
    1. HTTP/Fetch/XHR 层：拦截所有针对本地调试端口的请求，并主动伪装返回 'ConnectionRefused'；
    2. WebSocket 层：通过 add_init_script 注入虚拟 WebSocket 拦截代理，针对本地调试端口
       静默触发 1006 Connection Refused 关闭异常，完美模拟未开启端口特征。
    """
    if target is None:
        return False

    guarded_ports: Set[int] = set(_COMMON_DEBUG_PORTS)
    if cdp_port:
        try:
            guarded_ports.add(int(cdp_port))
        except (ValueError, TypeError):
            pass
    if extra_ports:
        for p in extra_ports:
            try:
                guarded_ports.add(int(p))
            except (ValueError, TypeError):
                pass

    ports_regex = "|".join(str(p) for p in sorted(guarded_ports))
    host_pattern = r"(?:127(?:\.\d+){1,3}|0\.0\.0\.0|\[::1\]|\[0*(?::0*){7}1\]|\[::ffff:127(?:\.\d+){1,3}\]|(?:[a-z0-9-]+\.)*(?:localhost|localtest\.me|lvh\.me))"
    pattern = re.compile(
        rf"^(?:https?|wss?)://{host_pattern}:(?:{ports_regex})(?:[/?#].*)?$",
        re.IGNORECASE
    )

    def _block_cdp_probe(route):
        try:
            # 伪装系统级 ConnectionRefused
            route.abort("connectionrefused")
        except Exception:
            try:
                route.abort("failed")
            except Exception:
                pass

    route_installed = False
    try:
        target.route(pattern, _block_cdp_probe)
        route_installed = True
    except Exception:
        pass

    # 注入前端 WebSocket 拦截补丁（封堵反爬虫通过 new WebSocket('ws://127.0.0.1:9222') 探测）
    ws_guard_script = f"""(() => {{
        const OriginalWebSocket = window.WebSocket;
        if (!OriginalWebSocket || window.__cdp_port_guard_installed__) return;
        window.__cdp_port_guard_installed__ = true;
        const guardedPorts = {json.dumps(sorted(list(guarded_ports)))};
        const isGuarded = (url) => {{
            try {{
                const parsed = new URL(url);
                const port = parseInt(parsed.port, 10) || (parsed.protocol === 'wss:' ? 443 : 80);
                if (!guardedPorts.includes(port)) return false;
                const h = parsed.hostname.toLowerCase();
                return h === 'localhost' || h.endsWith('.localhost') || h === '0.0.0.0' ||
                       h.startsWith('127.') || h === '[::1]' || h === '[0:0:0:0:0:0:0:1]' ||
                       h.includes('localtest.me') || h.includes('lvh.me');
            }} catch (e) {{
                return false;
            }}
        }};
        const GuardedWebSocket = function(url, protocols) {{
            if (isGuarded(url)) {{
                const ws = new EventTarget();
                ws.url = url;
                ws.readyState = 3;
                ws.bufferedAmount = 0;
                ws.extensions = '';
                ws.protocol = '';
                ws.binaryType = 'blob';
                ws.send = () => {{ throw new DOMException("WebSocket is already in CLOSING or CLOSED state.", "InvalidStateError"); }};
                ws.close = () => {{}};
                setTimeout(() => {{
                    const errEvt = new Event('error');
                    if (typeof ws.onerror === 'function') ws.onerror(errEvt);
                    ws.dispatchEvent(errEvt);
                    const closeEvt = new CloseEvent('close', {{ wasClean: false, code: 1006, reason: 'Connection refused' }});
                    if (typeof ws.onclose === 'function') ws.onclose(closeEvt);
                    ws.dispatchEvent(closeEvt);
                }}, 0);
                return ws;
            }}
            return new OriginalWebSocket(url, protocols);
        }};
        GuardedWebSocket.prototype = OriginalWebSocket.prototype;
        GuardedWebSocket.CONNECTING = 0;
        GuardedWebSocket.OPEN = 1;
        GuardedWebSocket.CLOSING = 2;
        GuardedWebSocket.CLOSED = 3;
        window.WebSocket = GuardedWebSocket;
    }})();"""

    try:
        if hasattr(target, "add_init_script"):
            target.add_init_script(ws_guard_script)
    except Exception:
        pass

    return route_installed


# ---------------------------------------------------------------------------
# 出版社页面加载增强：Tab 复用、瞬态挑战壳等待、慢速第三方脚本拦截、系统 Chrome / BrowserSkill
# ---------------------------------------------------------------------------

DEFAULT_SLOW_THIRD_PARTY_DOMAINS = frozenset({
    "ajax.googleapis.com",
    "fonts.googleapis.com",
    "scholar.google.com",
    "www.googletagmanager.com",
    "googletagmanager.com",
    "www.google-analytics.com",
    "google-analytics.com",
    "cookie-cdn.cookiepro.com",
    "cdn.cookielaw.org",
    "geolocation.onetrust.com",
    "js.trendmd.com",
})


def merge_blocked_domains(url: str, existing_domains=None, proxy: str = "") -> Set[str]:
    """合并拖慢首屏加载的第三方跟踪/同意弹窗域名（始终豁免目标站点自身主机）。"""
    target_host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    extra = set(DEFAULT_SLOW_THIRD_PARTY_DOMAINS)
    if proxy:
        extra -= {"ajax.googleapis.com", "fonts.googleapis.com"}
    merged = set(existing_domains or ())
    for dom in extra:
        if target_host and (target_host == dom or target_host.endswith("." + dom)):
            continue
        merged.add(dom)
    return merged


def install_slow_tracker_route_blocker(target: Any, target_url: str, proxy: str = "") -> bool:
    """在 Playwright Page 或 Context 上拦截会卡住页面 load 的第三方跟踪/弹窗域。"""
    if target is None or not hasattr(target, "route"):
        return False
    blocked = merge_blocked_domains(target_url, proxy=proxy)
    if not blocked:
        return False

    def _should_abort(req_url: str) -> bool:
        try:
            host = (urllib.parse.urlparse(req_url).hostname or "").lower()
            if not host:
                return False
            return any(host == dom or host.endswith("." + dom) for dom in blocked)
        except Exception:
            return False

    try:
        target.route(lambda u: _should_abort(u), lambda route: route.abort())
        return True
    except Exception:
        return False


def has_system_chrome() -> bool:
    """检测本机是否安装了独立的桌面版 Google Chrome（用于 real_chrome 有头回退）。"""
    for env_var in ("CHROME_PATH", "GOOGLE_CHROME_BIN"):
        candidate = os.environ.get(env_var, "").strip()
        if candidate and os.path.exists(candidate):
            return True
    if sys.platform == "darwin":
        mac_paths = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )
        return any(os.path.exists(p) for p in mac_paths)
    if sys.platform.startswith("win"):
        win_paths = (
            os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        )
        return any(p and os.path.exists(p) for p in win_paths)
    return any(shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chrome"))


def get_publisher_table_wait_selector(url: str) -> str:
    """按出版社域返回更精准的表格/容器等待选择器。"""
    low = (url or "").lower()
    if "sciencedirect.com" in low or "cell.com" in low:
        return "table, .tables, [id^='t0'], [id^='tbl']"
    if "onlinelibrary.wiley.com" in low:
        return "table, .article-table-content"
    if "springer.com" in low or "nature.com" in low:
        return "table, .c-article-table-container, .c-article-table"
    if any(k in low for k in ("geoscienceworld.org", "lyellcollection.org", "academic.oup.com", "silverchair")):
        return "table, .table-wrap, .table-modal"
    if "pubs.acs.org" in low:
        return "table, .NLM_table-wrap"
    if "pubs.rsc.org" in low:
        return "table, .rtable__wrapper, .table_caption"
    if "iopscience.iop.org" in low:
        return "table, .table-wrapper"
    if "mdpi.com" in low:
        return "table, .html-table_show"
    if "tandfonline.com" in low:
        return "table, .tableView"
    if "frontiersin.org" in low:
        return "table, .TableWrap"
    return "table"


def find_matching_cdp_page(browser: Any, target_url: str):
    """
    在已通过 CDP 连接的浏览器上下文中查找匹配标签页（零重复请求直接复用）：
    依次匹配完整 URL、DOI token（10.xxxx/...）、Elsevier PII（S...），以及页内 meta DOI。
    """
    if not browser or not target_url:
        return None
    target_norm = target_url.lower().rstrip("/")
    m_doi = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", target_url)
    doi_token = m_doi.group(0).lower() if m_doi else ""
    m_pii = re.search(r"[sS]\d{16}[0-9a-zA-Z]", target_url)
    pii_token = m_pii.group(0).lower() if m_pii else ""

    for ctx in getattr(browser, "contexts", []) or []:
        for pg in getattr(ctx, "pages", []) or []:
            try:
                pg_url = (pg.url or "").lower().rstrip("/")
                if not pg_url or pg_url == "about:blank" or pg_url.startswith(("chrome://", "edge://")):
                    continue
                if pg_url == target_norm or (len(target_norm) > 20 and (target_norm in pg_url or pg_url in target_norm)):
                    return pg
                if doi_token and doi_token in pg_url:
                    return pg
                if pii_token and pii_token in pg_url:
                    return pg
                if doi_token:
                    try:
                        meta_doi = pg.evaluate(
                            """() => document.querySelector('meta[name*="doi" i], meta[name="citation_doi" i]')?.content || ''"""
                        )
                        if meta_doi and doi_token in str(meta_doi).lower():
                            return pg
                    except Exception:
                        pass
            except Exception:
                continue
    return None


def looks_like_transient_challenge_shell(html_content: Optional[str], current_url: str = "") -> bool:
    """识别值得短时等待的瞬态风控/跳转壳（Turnstile、F5 /tspd/、Elsevier crasolve、AIP loading 等）。"""
    low_url = (current_url or "").lower()
    if "crasolve=1" in low_url or "linkinghub.elsevier.com" in low_url:
        return True
    if not html_content or not isinstance(html_content, str):
        return True
    low = html_content.lower()
    has_body = bool(re.search(r"<body\b", low))
    if not has_body and ("<html" in low or "<head" in low or "<!doctype" in low):
        return True
    if "crasolve" in low[:3000] and "sciencedirect" in (low_url + low[:3000]):
        return True
    if ("/tspd/" in low or "apm_do_not_touch" in low) and len(re.sub(r"<[^>]+>", " ", low).strip()) < 1500:
        return True
    cleaned = re.sub(r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>", " ", html_content, flags=re.IGNORECASE | re.DOTALL)
    vis = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cleaned)).strip().lower()
    if len(vis) < 1500 and any(
        m in vis for m in (
            "just a moment",
            "checking your browser",
            "verify you are human",
            "attention required",
            "enable javascript and cookies to continue",
        )
    ):
        return True
    if ("cf-turnstile" in low or "challenges.cloudflare.com" in low) and len(vis) < 1500:
        return True
    if "pubs.aip.org" in low_url and "loading" in vis and len(vis) < 600:
        return True
    return False


def wait_through_transient_challenge(page: Any, max_wait_s: float = 8.0) -> bool:
    """
    在 Playwright 页面刚加载后检测 Elsevier linkinghub meta-refresh、crasolve、
    Turnstile / F5 /tspd/ 等瞬态挑战壳，执行有界等待并在需要时主动跟踪 meta-refresh。
    返回 True 表示等待期间页面已稳定脱离瞬态壳。
    """
    if page is None:
        return False
    try:
        cur_url = getattr(page, "url", "") or ""
        # 1. Elsevier linkinghub / retrieve/pii meta-refresh 等待与兜底跳转
        if "linkinghub.elsevier.com" in cur_url.lower() or "retrieve/pii" in cur_url.lower():
            for _ in range(5):
                try:
                    page.wait_for_timeout(1500)
                    new_url = getattr(page, "url", "") or ""
                    if new_url and "linkinghub.elsevier.com" not in new_url.lower() and "retrieve/pii" not in new_url.lower():
                        cur_url = new_url
                        break
                except Exception:
                    break
            else:
                try:
                    redir = page.evaluate("""() => {
                        const m = document.querySelector('meta[http-equiv="REFRESH"], meta[http-equiv="refresh"]');
                        if (!m) return null;
                        const match = (m.getAttribute('content') || '').match(/url\\s*=\\s*['"]?([^'";\\s]+)/i);
                        if (!match) return null;
                        let target = match[1];
                        if (target.startsWith('/')) target = window.location.origin + target;
                        return target;
                    }""")
                    if redir and isinstance(redir, str) and redir.startswith("http"):
                        page.goto(redir, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(2000)
                except Exception:
                    pass

        # 2. 有界轮询瞬态风控壳，等待 JS/XHR 就地替换为真实文章 DOM
        probe_html = page.content()
        cur_url = getattr(page, "url", "") or cur_url
        if not looks_like_transient_challenge_shell(probe_html, cur_url):
            return True

        deadline = time.monotonic() + float(max_wait_s)
        while time.monotonic() < deadline:
            try:
                page.wait_for_timeout(500)
                cur_html = page.content()
                cur_url = getattr(page, "url", "") or cur_url
            except Exception:
                break
            if not looks_like_transient_challenge_shell(cur_html, cur_url):
                return True
        return False
    except Exception:
        return False


def find_bsk_executable() -> Optional[str]:
    """探测腾讯 BrowserSkill CLI (`bsk`) 可执行文件路径（未安装时 <1ms 返回 None）。"""
    for env_key in ("BSK_BIN", "BSK_PATH"):
        val = (os.environ.get(env_key) or "").strip()
        if val and os.path.isfile(val):
            return val
    found = shutil.which("bsk")
    if found:
        return found
    home = os.path.expanduser("~")
    for d in [
        os.environ.get("BSK_INSTALL_DIR", ""),
        os.path.join(os.environ.get("BSK_HOME", ""), "bin") if os.environ.get("BSK_HOME") else "",
        os.path.join(home, ".browserskill", "bin"),
        os.path.join(home, ".local", "bin"),
    ]:
        if not d:
            continue
        for name in ("bsk.exe", "bsk"):
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return cand
    return None


def try_fetch_html_via_browserskill(
    url: str,
    wait_selector: Optional[str] = None,
    timeout: float = 30.0,
) -> Tuple[str, Optional[int], str]:
    """
    可选集成 Tencent BrowserSkill (`bsk`) 复用用户日常 Chrome/Edge 已连接会话抓取页面。
    未安装或 daemon 未就绪时静默返回 ("", None, url)。
    """
    exe = find_bsk_executable()
    if not exe or not url:
        return "", None, url
    try:
        st_proc = subprocess.run(
            [exe, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if st_proc.returncode != 0:
            return "", None, url
        st_data = json.loads(st_proc.stdout or "{}")
        if not (st_data.get("ready") or (st_data.get("daemon_running") and st_data.get("browsers"))):
            return "", None, url
    except Exception:
        return "", None, url

    sess_id = None
    try:
        start_proc = subprocess.run(
            [exe, "start", "--no-focus", "--json"],
            capture_output=True,
            text=True,
            timeout=min(10.0, timeout),
        )
        start_data = json.loads(start_proc.stdout or "{}")
        sess_id = start_data.get("session_id") or start_data.get("id")
        if not sess_id:
            return "", None, url

        subprocess.run(
            [exe, "navigate", url, "--session", str(sess_id)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        if wait_selector:
            deadline = time.monotonic() + min(10.0, timeout * 0.4)
            while time.monotonic() < deadline:
                ev = subprocess.run(
                    [exe, "evaluate", f"!!document.querySelector({json.dumps(wait_selector)})", "--session", str(sess_id), "--json"],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                )
                if "true" in (ev.stdout or "").lower():
                    break
                time.sleep(0.5)

        ev_dom = subprocess.run(
            [
                exe,
                "evaluate",
                "JSON.stringify({html: document.documentElement ? document.documentElement.outerHTML : '', url: window.location.href})",
                "--session",
                str(sess_id),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        raw_out = json.loads(ev_dom.stdout or "{}")
        payload = raw_out.get("result", raw_out) if isinstance(raw_out, dict) else {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict) and payload.get("html"):
            return str(payload["html"]), None, str(payload.get("url") or url)
    except Exception:
        pass
    finally:
        if sess_id:
            try:
                subprocess.run([exe, "stop", "--session", str(sess_id)], capture_output=True, timeout=3.0)
            except Exception:
                pass
    return "", None, url
