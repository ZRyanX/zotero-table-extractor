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
import platform
import tempfile
import threading
import contextlib
import urllib.request
from typing import Optional, Dict, Any, List, Set, Union

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
