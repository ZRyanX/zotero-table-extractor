import atexit
import contextlib
import glob
import os
import signal
import subprocess
import sys
import threading

def find_playwright_chromium():
    """
    Automatically detects the Playwright Chromium executable path on macOS, Windows, and Linux.
    Returns the absolute path to the executable, or None if not found.
    """
    try:
        import json
        script_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(script_dir)
        config_path = os.path.join(parent_dir, "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                custom_path = config.get("PLAYWRIGHT_EXECUTABLE_PATH")
                if custom_path and os.path.exists(custom_path):
                    return custom_path
    except Exception:
        pass

    # 1. First, check if custom env var is set
    env_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    # Determine default ms-playwright directories
    home = os.path.expanduser("~")
    possible_roots = []

    if sys.platform.startswith("win"):
        # Windows
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            possible_roots.append(os.path.join(local_app_data, "ms-playwright"))
        possible_roots.append(os.path.join(home, "AppData", "Local", "ms-playwright"))
    elif sys.platform == "darwin":
        # macOS
        possible_roots.append(os.path.join(home, "Library", "Caches", "ms-playwright"))
    else:
        # Linux / other
        possible_roots.append(os.path.join(home, ".cache", "ms-playwright"))

    # Also check if PLAYWRIGHT_BROWSERS_PATH is specified
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path:
        possible_roots.insert(0, browsers_path)

    for root in possible_roots:
        if not os.path.isdir(root):
            continue

        # Look for chromium-* directories
        chromium_dirs = glob.glob(os.path.join(root, "chromium-*"))
        if not chromium_dirs:
            continue
        
        # Sort to prioritize newer revisions (e.g. chromium-1223 over chromium-1208)
        def extract_rev(path):
            name = os.path.basename(path)
            parts = name.split('-')
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1])
            return 0
        
        chromium_dirs.sort(key=extract_rev, reverse=True)

        for chrom_dir in chromium_dirs:
            # Platform specific executable search
            if sys.platform.startswith("win"):
                # Windows exe: chrome-win64\chrome.exe (Playwright >=1.49 / Patchright) or chrome-win\chrome.exe
                for sub_dir in ("chrome-win64", "chrome-win"):
                    exe_path = os.path.join(chrom_dir, sub_dir, "chrome.exe")
                    if os.path.exists(exe_path):
                        return exe_path
            elif sys.platform == "darwin":
                # macOS app
                app_glob = os.path.join(
                    chrom_dir, 
                    "chrome-mac*", 
                    "Google Chrome for Testing.app", 
                    "Contents", 
                    "MacOS", 
                    "Google Chrome for Testing"
                )
                matches = glob.glob(app_glob)
                if matches and os.path.exists(matches[0]):
                    return matches[0]
                
                # Fallback to walk inside the directory
                for r, d, f in os.walk(chrom_dir):
                    if "Google Chrome for Testing" in f:
                        test_path = os.path.join(r, "Google Chrome for Testing")
                        if os.path.exists(test_path) and os.access(test_path, os.X_OK):
                            if "Contents/MacOS" in test_path:
                                return test_path
            else:
                # Linux: chrome-linux/chrome
                exe_path = os.path.join(chrom_dir, "chrome-linux", "chrome")
                if os.path.exists(exe_path):
                    return exe_path

    return None


def resolve_sync_playwright(
    module_sync_pw=None,
    default_playwright_sync=None,
    prefer_patchright: bool = True,
):
    """
    选择同步浏览器引擎：
    - 若调用方模块级 `sync_playwright` 被外部/测试替换（monkeypatch），始终优先尊重替换后的实现；
    - 自启独立浏览器（非外部 CDP 接入）场景下，优先使用 `patchright.sync_api.sync_playwright`
      （规避 Runtime.enable 等反自动化检测指纹），未安装或禁用时自动回退 `playwright.sync_api.sync_playwright`。

    返回 `(sync_playwright_callable, engine_name)`，engine_name ∈ {"custom", "patchright", "playwright"}。
    """
    if module_sync_pw is not None and module_sync_pw is not default_playwright_sync:
        return module_sync_pw, "custom"

    disable_pr = (os.environ.get("ZTE_DISABLE_PATCHRIGHT") or "").strip().lower() in ("1", "true", "yes")
    if prefer_patchright and not disable_pr:
        try:
            from patchright.sync_api import sync_playwright as _pr_sync_playwright
            return _pr_sync_playwright, "patchright"
        except Exception:
            pass

    if default_playwright_sync is not None:
        return default_playwright_sync, "playwright"
    if module_sync_pw is not None:
        return module_sync_pw, "playwright"
    from playwright.sync_api import sync_playwright as _pw_sync_playwright
    return _pw_sync_playwright, "playwright"


def extract_browser_pid(browser=None, context=None, playwright_obj=None):
    """Best-effort extract underlying browser/driver OS PID for zombie process-tree cleanup."""
    candidates = []
    for obj in (browser, context, playwright_obj):
        if obj is None:
            continue
        for attr_chain in (
            ("_impl_obj", "_browser_process", "pid"),
            ("_browser", "_impl_obj", "_browser_process", "pid"),
            ("_impl_obj", "_connection", "_transport", "_proc", "pid"),
            ("_connection", "_transport", "_proc", "pid"),
            ("pid",),
        ):
            cur = obj
            for attr in attr_chain:
                cur = getattr(cur, attr, None)
                if cur is None:
                    break
            if isinstance(cur, int) and cur > 0 and cur != os.getpid():
                candidates.append(cur)
    return candidates[0] if candidates else None


def kill_process_tree(pid) -> bool:
    """
    强制终止指定 PID 及其全部子进程树（Windows 下使用 `taskkill /F /T /PID` 防止僵尸 Chrome）。
    永不误杀当前 Python 进程。
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0 or pid_int == os.getpid():
        return False

    try:
        if os.name == "nt":
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid_int)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return res.returncode == 0
        try:
            os.kill(pid_int, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except Exception:
            pass
        try:
            os.kill(pid_int, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except Exception:
            pass
        return True
    except Exception:
        return False


def close_browser_safely(browser=None, context=None, proc_pid=None, force_kill_on_error: bool = True) -> None:
    """Safely close Playwright/Patchright browser/context and clean up Windows process tree if needed."""
    pid = proc_pid or extract_browser_pid(browser=browser, context=context)
    close_failed = False

    if browser is not None:
        try:
            browser.close()
        except Exception:
            close_failed = True
    elif context is not None:
        try:
            context.close()
        except Exception:
            close_failed = True

    if pid and close_failed and force_kill_on_error:
        kill_process_tree(pid)


class ThreadAffineBrowserPool:
    """
    线程亲和（thread-affine）独立浏览器复用池：
    - Playwright/Patchright sync API 严格绑定创建线程（greenlet affinity），本池通过 threading.local()
      在同一线程内复用已启动的无头/有头独立浏览器实例，每次任务仅创建并释放隔离 BrowserContext；
    - 记录所有活跃驱动/浏览器进程 PID，并在异常、回收或进程退出（atexit）时自动清理进程树，防止 Windows 僵尸 Chrome。
    """

    def __init__(self):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._registry = {}  # thread_ident -> entry dict

    def _entry_key(self, engine_name, headless, exec_path, launch_kwargs=None):
        norm_kwargs = ()
        if isinstance(launch_kwargs, dict) and launch_kwargs:
            items = []
            for k in sorted(launch_kwargs.keys()):
                if k in ("headless", "executable_path"):
                    continue
                v = launch_kwargs[k]
                if isinstance(v, list):
                    v = tuple(v)
                elif isinstance(v, dict):
                    v = tuple(sorted(v.items()))
                items.append((k, v))
            norm_kwargs = tuple(items)
        return (str(engine_name), bool(headless), str(exec_path or ""), norm_kwargs)

    def _dispose_entry(self, entry, force_kill: bool = False):
        if not entry:
            return
        browser = entry.get("browser")
        pw_cm = entry.get("pw_cm")
        pid = entry.get("pid") or extract_browser_pid(browser=browser, playwright_obj=entry.get("pw"))
        had_err = False
        if browser is not None:
            try:
                browser.close()
            except Exception:
                had_err = True
        if pw_cm is not None:
            try:
                pw_cm.__exit__(None, None, None)
            except Exception:
                had_err = True
        if pid and (had_err or force_kill):
            kill_process_tree(pid)

    def _is_entry_alive(self, entry) -> bool:
        if not entry:
            return False
        browser = entry.get("browser")
        if browser is None:
            return False
        is_conn = getattr(browser, "is_connected", None)
        if callable(is_conn):
            try:
                return bool(is_conn())
            except Exception:
                return False
        return True

    def close_current_thread(self, force_kill: bool = False):
        tid = threading.get_ident()
        entry = getattr(self._local, "entry", None)
        self._local.entry = None
        with self._lock:
            reg_entry = self._registry.pop(tid, None)
        self._dispose_entry(entry or reg_entry, force_kill=force_kill)

    def close_all(self, force_kill: bool = False):
        with self._lock:
            items = list(self._registry.items())
            self._registry.clear()
        cur_tid = threading.get_ident()
        for tid, entry in items:
            if tid == cur_tid:
                self._local.entry = None
                self._dispose_entry(entry, force_kill=force_kill)
            else:
                # 跨线程退出时 sync API 不可跨线程调用 close()，直接清理底层 OS 进程树
                pid = entry.get("pid")
                if pid:
                    kill_process_tree(pid)

    @contextlib.contextmanager
    def acquire_context(
        self,
        sync_pw_factory,
        engine_name: str = "playwright",
        headless: bool = True,
        exec_path=None,
        launch_kwargs=None,
        context_kwargs=None,
    ):
        """Acquire a fresh isolated BrowserContext from the current thread's pooled browser."""
        key = self._entry_key(engine_name, headless, exec_path, launch_kwargs=launch_kwargs)
        entry = getattr(self._local, "entry", None)

        if entry is not None and (entry.get("key") != key or not self._is_entry_alive(entry)):
            self.close_current_thread(force_kill=True)
            entry = None

        if entry is None:
            pw_cm = sync_pw_factory()
            pw = pw_cm.__enter__()
            try:
                l_kwargs = dict(launch_kwargs or {})
                l_kwargs["headless"] = headless
                if exec_path and "executable_path" not in l_kwargs and "channel" not in l_kwargs:
                    l_kwargs["executable_path"] = exec_path
                browser = pw.chromium.launch(**l_kwargs)
            except Exception:
                try:
                    pw_cm.__exit__(None, None, None)
                except Exception:
                    pass
                raise
            pid = extract_browser_pid(browser=browser, playwright_obj=pw)
            entry = {
                "key": key,
                "pw_cm": pw_cm,
                "pw": pw,
                "browser": browser,
                "pid": pid,
            }
            self._local.entry = entry
            with self._lock:
                self._registry[threading.get_ident()] = entry

        browser = entry["browser"]
        context = browser.new_context(**dict(context_kwargs or {}))
        try:
            yield browser, context
        except Exception:
            if not self._is_entry_alive(entry):
                self.close_current_thread(force_kill=True)
            raise
        finally:
            try:
                context.close()
            except Exception:
                pass


_THREAD_BROWSER_POOL = ThreadAffineBrowserPool()
atexit.register(_THREAD_BROWSER_POOL.close_all)


def get_thread_browser_pool() -> ThreadAffineBrowserPool:
    return _THREAD_BROWSER_POOL
