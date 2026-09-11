import sys
import os
import glob

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
                # Windows exe: chrome-win\chrome.exe
                exe_path = os.path.join(chrom_dir, "chrome-win", "chrome.exe")
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
