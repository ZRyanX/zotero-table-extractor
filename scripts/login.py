#!/usr/bin/env python3
"""
login.py — 统一的登录与 Cookie 配置入口

子命令：
  cnki        打开可见浏览器登录知网，自动捕获 Cookies（原 login_cnki.py）
  publishers  Chrome 调试桥接 / CDP 登录向导（原 login_publishers.py）
  crawl4ai    Crawl4AI 免检测登录与 Cookie 导出（原 crawl4ai_login_publishers.py）

旧脚本 login_cnki.py / login_publishers.py / crawl4ai_login_publishers.py
保留为兼容薄壳，功能与本入口完全一致。

用法：
  python3 scripts/login.py cnki
  python3 scripts/login.py publishers
  python3 scripts/login.py crawl4ai
"""

import sys
import os
import json
import subprocess
import time
import asyncio

scripts_dir = os.path.dirname(os.path.abspath(__file__))
if scripts_dir not in sys.path:
    sys.path.append(scripts_dir)

# Shared helpers (cookie paths, config)
try:
    from . import common
except ImportError:
    import common

# Re-exported for backward compatibility (thin wrappers & scratch scripts import these)
get_cnki_cookies_path = common.get_cnki_cookies_path
get_crawl4ai_cookies_path = common.get_crawl4ai_cookies_path

try:
    from .playwright_utils import find_playwright_chromium
except ImportError:
    try:
        from playwright_utils import find_playwright_chromium
    except ImportError:
        find_playwright_chromium = lambda: None

PUBLISHERS = {
    "A": ("中国知网 (CNKI)", "https://www.cnki.net/"),
    "B": ("Springer Link", "https://link.springer.com/"),
    "C": ("Elsevier / ScienceDirect", "https://www.sciencedirect.com/"),
    "D": ("Wiley Online Library", "https://onlinelibrary.wiley.com/"),
    "E": ("Nature", "https://www.nature.com/"),
    "F": ("IEEE Xplore", "https://ieeexplore.ieee.org/"),
    "G": ("GeoSciWorld", "https://pubs.geoscienceworld.org/"),
    "H": ("Taylor & Francis", "https://www.tandfonline.com/"),
    "I": ("MDPI", "https://www.mdpi.com/"),
}

_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
]
_STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "window.chrome = {runtime: {}};"  # Fake chrome object
)


def load_skill_config():
    config_path = common.get_config_path()
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f), config_path
        except Exception:
            pass
    return {}, config_path


def _find_windows_chrome():
    """在 Windows 上定位 Chrome 可执行文件：注册表 App Paths 优先，候选路径兜底。"""
    candidates = []
    try:
        import winreg
        for hkey in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hkey, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                    val, _ = winreg.QueryValueEx(key, "")
                    if val:
                        candidates.append(val)
            except Exception:
                pass
    except Exception:
        pass
    local = os.environ.get("LOCALAPPDATA", "")
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def launch_chrome_debug_mode(profile_dir):
    """Launches the user's everyday Google Chrome in debugging mode on port 9222."""
    import platform
    system = platform.system()

    if system == "Darwin":
        cmd = f'"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --user-data-dir="{profile_dir}"'
        subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif system == "Windows":
        chrome_exe = _find_windows_chrome()
        if not chrome_exe:
            print("错误：未能定位 Chrome 可执行文件（注册表与常见安装路径均未找到）。")
            return False
        cmd = f'start "" "{chrome_exe}" --remote-debugging-port=9222 --user-data-dir="{profile_dir}"'
        subprocess.Popen(cmd, shell=True)
    else:  # Linux
        cmd = f'google-chrome --remote-debugging-port=9222 --user-data-dir="{profile_dir}"'
        subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("正在拉起 Chrome 调试版本体...")
    # Wait for the port to open (up to 5 seconds)
    for _ in range(10):
        time.sleep(0.5)
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1.0) as response:
                if response.status == 200:
                    print("Chrome 调试端口 9222 开启成功！")
                    return True
        except Exception:
            pass
    print("警告：Chrome 开启超时，请确保您的电脑上已安装 Chrome 并关闭了该调试配置下的窗口。")
    return False


# ---------------------------------------------------------------------------
# CNKI 子命令（原 login_cnki.py）
# ---------------------------------------------------------------------------

def cnki_main():
    """打开可见浏览器登录知网并自动捕获 Cookies。返回是否成功。"""
    try:
        from . import online
    except ImportError:
        try:
            import online
        except ImportError:
            print("Error: Could not import online package. Please verify scripts directory structure.", file=sys.stderr)
            return False
    print("启动知网 Cookie 配置向导...")
    return online.interactive_cnki_cookie_capture()


# ---------------------------------------------------------------------------
# publishers 子命令（原 login_publishers.py）
# ---------------------------------------------------------------------------

def publishers_main():
    from playwright.sync_api import sync_playwright

    home = os.path.expanduser("~")
    default_profile_dir = os.path.join(home, ".zotero_playwright_profile")
    default_chrome_debug_profile = os.path.join(home, ".zotero_chrome_debug_profile")

    print("==================================================")
    print("    欢迎使用文献出版社登录与 Cookie 配置向导")
    print("==================================================")
    print("【重要提示】")
    print("  对于大部分用户，如果您平时使用 Chrome/Edge 浏览器，")
    print("  表格提取脚本在运行中会自动安全克隆您的当前浏览器登录 Session（免登录/免验证）。")
    print("  本向导仅在以下情况下有必要运行：")
    print("    - 您不使用 Chrome/Edge 作为日常浏览器（例如使用 Safari/Firefox）")
    print("    - 系统安全策略限制了 Chrome 配置文件读取，导致提取脚本无法自动同步状态")
    print("    - 您在服务器/无头(headless)环境上运行，需要提前生成便携式 cookies.json 配置文件")
    print("==================================================")
    print("请选择操作类型：")
    print("  1. 启动 Chrome 调试本体并自动打开所有相关出版社网站 (推荐，用于首次登录/机构授权)")
    print("  2. 从已运行在 9222 端口的 Chrome 浏览器导入所有已登录的 Cookie (免验证码且完全避开 Cloudflare)")
    print("  3. 一次性依次打开所有网站 (在自动化 Chrome Testing 浏览器中)")
    print("  4. 自定义 URL (在 Chrome Testing 中)")
    print("\n  或者单独在自动化测试浏览器中打开以下网站：")
    for key, (name, url) in PUBLISHERS.items():
        print(f"    {key}. {name}")

    choice = input("\n请输入序号或字母 (默认 1): ").strip().upper() or "1"

    exec_path = find_playwright_chromium()
    config, config_path = load_skill_config()

    # Update config.json to point to our default profile dir if empty
    if not config.get("PLAYWRIGHT_USER_DATA_DIR"):
        config["PLAYWRIGHT_USER_DATA_DIR"] = default_profile_dir
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            print(f"已更新 config.json 中的 PLAYWRIGHT_USER_DATA_DIR 为: {default_profile_dir}")
        except Exception as e:
            print(f"警告：更新 config.json 失败: {e}")

    # Choice 1: Start Chrome debug body and open all publisher sites
    if choice == "1":
        success = launch_chrome_debug_mode(default_chrome_debug_profile)
        if not success:
            return

        print("\n正在通过 CDP 连接并打开各大出版社网站...")
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                context = browser.contexts[0] if browser.contexts else browser.new_context()

                # Open pages for all publishers
                urls = [url for name, url in PUBLISHERS.values()]
                for i, url in enumerate(urls):
                    try:
                        if i == 0:
                            page = context.pages[0] if context.pages else context.new_page()
                            page.goto(url)
                        else:
                            page = context.new_page()
                            page.goto(url)
                    except Exception as e:
                        print(f"警告：打开 {url} 失败: {e}")

                print("\n==================================================")
                print("  已在 Chrome 浏览器本体中打开所有网站标签页。")
                print("  1. 请在页面中进行登录/授权。")
                print("  2. 全部完成后，直接【关闭整个 Chrome 浏览器窗口】。")
                print("  程序将自动提取并保存您的登录 Cookies。")
                print("==================================================\n")

                closed = [False]
                def on_close(ctx):
                    closed[0] = True
                context.on("close", on_close)

                last_valid_cookies = []
                while not closed[0]:
                    try:
                        cookies = context.cookies()
                        if cookies:
                            last_valid_cookies = cookies
                        time.sleep(0.5)
                    except Exception:
                        break

                # Export CNKI cookies
                cnki_cookies = [c for c in last_valid_cookies if "cnki.net" in c.get("domain", "") or "cnki.com.cn" in c.get("domain", "")]
                if cnki_cookies:
                    cnki_path = get_cnki_cookies_path()
                    with open(cnki_path, "w", encoding="utf-8") as f:
                        json.dump(cnki_cookies, f, indent=2, ensure_ascii=False)
                    print(f"[知网适配] 已成功提取并导出 {len(cnki_cookies)} 个知网 Cookies 到：{cnki_path}")

                # Save general cookies json
                zotero_cookies_path = os.path.join(default_profile_dir, "cookies.json")
                os.makedirs(default_profile_dir, exist_ok=True)
                with open(zotero_cookies_path, "w", encoding="utf-8") as f:
                    json.dump(last_valid_cookies, f, indent=2, ensure_ascii=False)
                print(f"[通用适配] 已成功保存 {len(last_valid_cookies)} 个 Cookies 至：{zotero_cookies_path}")
                print("登录会话已成功保存！")
            except Exception as e:
                print(f"CDP 连接错误: {e}")
        return

    # Choice 2: Import cookies from already running Chrome
    elif choice == "2":
        print("\n正在连接到运行在 9222 端口的 Google Chrome 浏览器...")
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                cookies = context.cookies()
                if not cookies:
                    print("错误：未从 Chrome 浏览器中捕获到任何 Cookie，请确保已打开网站。")
                    return

                # Export CNKI cookies
                cnki_cookies = [c for c in cookies if "cnki.net" in c.get("domain", "") or "cnki.com.cn" in c.get("domain", "")]
                if cnki_cookies:
                    cnki_path = get_cnki_cookies_path()
                    with open(cnki_path, "w", encoding="utf-8") as f:
                        json.dump(cnki_cookies, f, indent=2, ensure_ascii=False)
                    print(f"[知网适配] 已成功提取并导出 {len(cnki_cookies)} 个知网 Cookies 到：{cnki_path}")

                # Save general cookies json
                zotero_cookies_path = os.path.join(default_profile_dir, "cookies.json")
                os.makedirs(default_profile_dir, exist_ok=True)
                with open(zotero_cookies_path, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, indent=2, ensure_ascii=False)
                print(f"[通用适配] 已成功提取 {len(cookies)} 个全局 Cookies 并保存至：{zotero_cookies_path}")
                print("现在所有 Playwright 抓取流程将能自动复用您的登录会话！")
            except Exception as e:
                print(f"连接或获取失败: {e}")
                print("\n请确保您在终端运行以下命令开启调试模式的 Chrome 本体：")
                print("  macOS:  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222 --user-data-dir=\"$HOME/.zotero_chrome_debug_profile\"")
                print("  Windows: \"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\" --remote-debugging-port=9222 --user-data-dir=\"C:\\Users\\YourName\\.zotero_chrome_debug_profile\"")
        return

    # Regular Playwright Chrome Testing launching
    urls = []
    if choice in PUBLISHERS:
        name, url = PUBLISHERS[choice]
        urls = [url]
        print(f"\n您选择了单独打开: {name}")
    elif choice == "3":
        urls = [url for name, url in PUBLISHERS.values()]
        print("\n您选择了在 Chrome Testing 中依次打开所有出版社网站。")
    elif choice == "4":
        custom_url = input("请输入自定义 URL: ").strip()
        if not custom_url.startswith(("http://", "https://")):
            custom_url = "https://" + custom_url
        urls = [custom_url]
    else:
        print("无效的选择，默认打开知网。")
        urls = ["https://www.cnki.net/"]

    print("\n==================================================")
    print("  1. 正在启动可见的 Chrome Testing 窗口...")
    print("  2. 请在网页中登录您的账号，进行授权。")
    print("  3. 确认完成后，直接【关闭浏览器窗口】。")
    print("==================================================\n")

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                default_profile_dir,
                headless=False,
                executable_path=exec_path,
                args=_STEALTH_ARGS,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
        except Exception as e:
            print(f"启动失败: {e}")
            print("尝试不使用缓存目录启动...")
            context = p.chromium.launch_persistent_context(
                "",
                headless=False,
                executable_path=exec_path,
                args=_STEALTH_ARGS,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )

        # Inject cookies from cookies.json if present
        cookies_json = os.path.join(default_profile_dir, "cookies.json")
        if os.path.exists(cookies_json):
            try:
                with open(cookies_json, 'r', encoding='utf-8') as f:
                    c_list = json.load(f)
                if c_list:
                    for c in c_list:
                        try:
                            context.add_cookies([c])
                        except Exception:
                            pass
                    print(f"[验证向导] 已从 cookies.json 恢复并注入了 {len(c_list)} 个会话 Cookies 到 Chrome Testing 中！")
            except Exception as e:
                print(f"警告：载入 Cookies 失败: {e}")

        # Open pages
        for i, url in enumerate(urls):
            try:
                if i == 0:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.add_init_script(_STEALTH_INIT_SCRIPT)
                    page.goto(url)
                else:
                    page = context.new_page()
                    page.add_init_script(_STEALTH_INIT_SCRIPT)
                    page.goto(url)
            except Exception as e:
                print(f"警告：载入网站 {url} 失败: {e}，将继续打开其余网站。")

        # Wait for context close
        closed = [False]
        def on_close(ctx):
            closed[0] = True
        context.on("close", on_close)

        last_valid_cookies = []
        while not closed[0]:
            try:
                cookies = context.cookies()
                if cookies:
                    last_valid_cookies = cookies
                time.sleep(0.5)
            except Exception:
                break

        # Export CNKI cookies if found
        cnki_cookies = [c for c in last_valid_cookies if "cnki.net" in c.get("domain", "") or "cnki.com.cn" in c.get("domain", "")]
        if cnki_cookies:
            cnki_path = get_cnki_cookies_path()
            try:
                with open(cnki_path, "w", encoding="utf-8") as f:
                    json.dump(cnki_cookies, f, indent=2, ensure_ascii=False)
                print(f"\n[知网适配] 已单独导出 {len(cnki_cookies)} 个知网 Cookies 到：{cnki_path}")
            except Exception as e:
                print(f"\n[知网适配] 导出知网 Cookies 失败: {e}")

        print("\n浏览器已关闭，登录会话已成功保存。")


# ---------------------------------------------------------------------------
# crawl4ai 子命令（原 crawl4ai_login_publishers.py）
# ---------------------------------------------------------------------------

async def get_all_cookies_via_cdp(ws_url):
    import websockets
    print("正在通过 WebSocket 连接 Chrome 调试接口...")
    async with websockets.connect(ws_url, max_size=10*1024*1024) as ws:
        # Enable Network
        await ws.send(json.dumps({
            "id": 1,
            "method": "Network.enable",
            "params": {}
        }))
        await ws.recv()

        # Get all cookies
        await ws.send(json.dumps({
            "id": 2,
            "method": "Storage.getCookies",
            "params": {}
        }))

        raw_resp = await ws.recv()
        resp = json.loads(raw_resp)
        if "error" in resp:
            raise RuntimeError(f"CDP error: {resp['error']}")

        cookies = resp.get("result", {}).get("cookies", [])
        return cookies


async def open_tabs_via_cdp(ws_url, urls):
    import websockets
    print("正在为您在新窗口中打开各大出版社网站...")
    async with websockets.connect(ws_url) as ws:
        for i, url in enumerate(urls):
            await ws.send(json.dumps({
                "id": 100 + i,
                "method": "Target.createTarget",
                "params": {"url": url}
            }))
            await ws.recv()


def crawl4ai_main():
    import requests

    cookies_path = get_crawl4ai_cookies_path()
    home = os.path.expanduser("~")
    default_profile_dir = os.path.join(home, ".zotero_playwright_profile")
    zotero_cookies_path = os.path.join(default_profile_dir, "cookies.json")

    print("==================================================")
    print("    Crawl4AI 免检测文献出版社登录与 Cookie 导出工具")
    print("==================================================")
    print("本工具将连接到您的日常 Google Chrome 浏览器并提取登录 Cookies。")
    print("由于 Cloudflare 等系统的人机验证会拦截自动化软件，请按照以下步骤操作：")
    print("\n步骤 1：启动您的 Chrome 浏览器（开启调试端口）：")
    print("  - 请完全关闭当前运行的 Chrome 浏览器。")
    print("  - 在终端（Terminal）中运行以下命令重新启动 Chrome：")
    print('    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222')
    print("\n步骤 2：在新打开的 Chrome 窗口中完成登录/通过人机验证。")
    print("==================================================")

    # Wait for the debugging port to be open
    input("请在终端运行上述命令启动 Chrome 后，按 [Enter] 键继续连接...")

    ws_url = None
    for _ in range(5):
        try:
            resp = requests.get("http://127.0.0.1:9222/json/version", timeout=2).json()
            ws_url = resp.get("webSocketDebuggerUrl")
            if ws_url:
                print("成功连接到 Chrome 调试端口！")
                break
        except Exception:
            print("正在尝试连接到 127.0.0.1:9222 ...")
            time.sleep(1)

    if not ws_url:
        print("错误：无法连接到 Chrome 调试端口，请确认已按照步骤 1 启动 Chrome 并开启了 9222 端口。")
        return

    # Select publishers
    print("\n请选择要自动打开的出版社标签页：")
    print("  0. 自动打开所有出版社标签页 (推荐)")
    print("  S. 跳过自动打开，我将手动在 Chrome 中操作")
    for key, (name, url) in PUBLISHERS.items():
        print(f"  {key}. {name}")

    choice = input("\n请输入选择 (默认 0): ").strip().upper() or "0"

    if choice == "0":
        urls = [url for name, url in PUBLISHERS.values()]
        asyncio.run(open_tabs_via_cdp(ws_url, urls))
    elif choice in PUBLISHERS:
        urls = [PUBLISHERS[choice][1]]
        asyncio.run(open_tabs_via_cdp(ws_url, urls))
    elif choice == "S":
        print("已跳过自动打开网页。")
    else:
        print("无效的选择。")
        return

    print("\n==================================================")
    print("  【请在 Chrome 浏览器中操作】")
    print("  1. 在 Chrome 中完成您需要登录的出版社账号登录或机构认证。")
    print("  2. 请在 Chrome 中手动点击并通过所有人机验证（如 Cloudflare Turnstile）。")
    print("  3. 认证成功、网页加载完毕后，回到这里。")
    print("==================================================")
    input("\n请在完成所有登录和验证后，按 [Enter] 键开始导出 Cookies...")

    try:
        raw_cookies = asyncio.run(get_all_cookies_via_cdp(ws_url))
        if not raw_cookies:
            print("未在浏览器中找到任何 Cookies！")
            return

        # Clean cookies for Playwright compatibility
        valid_keys = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
        cookies = []
        for c in raw_cookies:
            if isinstance(c, dict):
                cleaned_c = {k: c[k] for k in valid_keys if k in c}
                cookies.append(cleaned_c)

        # Save to crawl4ai cookies
        with open(cookies_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)
        print(f"\n[Crawl4AI] 已成功导出 {len(cookies)} 个 Cookies 至：{cookies_path}")

        # CNKI compat
        cnki_cookies = [c for c in cookies if "cnki.net" in c.get("domain", "") or "cnki.com.cn" in c.get("domain", "")]
        if cnki_cookies:
            cnki_path = get_cnki_cookies_path()
            with open(cnki_path, "w", encoding="utf-8") as f:
                json.dump(cnki_cookies, f, indent=2, ensure_ascii=False)
            print(f"[知网适配] 已导出 {len(cnki_cookies)} 个知网 Cookies 到：{cnki_path}")

        # General compat
        os.makedirs(default_profile_dir, exist_ok=True)
        with open(zotero_cookies_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)
        print(f"[通用适配] 已导出 {len(cookies)} 个 Cookies 至：{zotero_cookies_path}")

        print("\n所有 Cookie 会话已成功保存！您可以关闭 Chrome 调试浏览器并继续进行提取了。")
    except Exception as e:
        print(f"导出 Cookies 失败: {e}")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="统一的登录与 Cookie 配置入口（知网 / 出版社桥接 / Crawl4AI）"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("cnki", help="打开可见浏览器登录知网，自动捕获 Cookies")
    sub.add_parser("publishers", help="Chrome 调试桥接 / CDP 登录向导")
    sub.add_parser("crawl4ai", help="Crawl4AI 免检测登录与 Cookie 导出")
    args = parser.parse_args(argv)

    if args.command == "cnki":
        return 0 if cnki_main() else 1
    elif args.command == "publishers":
        publishers_main()
        return 0
    elif args.command == "crawl4ai":
        crawl4ai_main()
        return 0
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
