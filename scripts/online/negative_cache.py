"""online.negative_cache — 跨进程 SQLite WAL 负缓存（仅缓存反爬/403/429 瞬态封禁）。

设计要点：
- 以 (channel, publisher_prefix/domain) 为主键，带可配置 TTL（默认 1800s，429 上限 600s）；
- 每次读写独立 open/close SQLite 连接，启用 WAL + busy_timeout，彻底规避 Windows 文件锁与多子进程并发冲突；
- 严格错误分类：仅缓存明确的反爬拦截 / HTTP 403(非订阅权限) / HTTP 429，绝不污染 404/410/401/402/正常付费墙页面。
"""

import os
import re
import socket
import sqlite3
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

try:
    from .. import common
except ImportError:
    import common


DEFAULT_NEGATIVE_CACHE_TTL_S = 1800
DEFAULT_RATE_LIMIT_TTL_S = 120
DEFAULT_TRANSIENT_CHALLENGE_TTL_S = 120

_REDIRECTOR_HOSTS = frozenset({
    "doi.org",
    "dx.doi.org",
    "hdl.handle.net",
    "chndoi.org",
    "www.chndoi.org",
    "chinadoi.cn",
    "www.chinadoi.cn",
    "linkinghub.elsevier.com",
    "chooser.crossref.org",
})

_NON_CACHEABLE_STATUS_CODES = frozenset({400, 401, 402, 404, 410, 451})

_PAYWALL_OR_DEAD_PATTERNS = (
    "page not found",
    "404 not found",
    "not entitled",
    "entitlement_error",
    "authorization_error",
    "insufficient_privileges",
    "purchase pdf",
    "buy this article",
    "rent this article",
    "subscribe to",
    "subscription required",
    "institutional access",
    "sign in to view",
    "log in to access",
    "paywall",
    "ezproxy login",
    "please log in to ezproxy",
    "webvpn login",
    "统一身份认证",
    "校园网登录",
    "高校统一认证",
    "shibboleth identity provider",
    "session has expired",
    "your session expired",
    "会话已过期",
)

# High-precision anti-bot challenge signatures safe to match even on HTTP 200 challenge shells
_STRICT_HTML_ANTIBOT_PATTERNS = (
    ("cf-turnstile", "antibot_turnstile"),
    ("challenges.cloudflare.com", "antibot_turnstile"),
    ("__cf_chl_", "antibot_cloudflare"),
    ("cf-browser-verification", "antibot_cloudflare"),
    ("just a moment", "antibot_cloudflare"),
    ("checking your browser", "antibot_cloudflare"),
    ("attention required! | cloudflare", "antibot_cloudflare"),
    ("radware", "antibot_radware"),
    ("bot manager", "antibot_bot_manager"),
    ("bm-verify", "antibot_akamai"),
    ("datadome", "antibot_datadome"),
    ("perimeterx", "antibot_perimeterx"),
    ("incapsula", "antibot_imperva"),
    ("imperva", "antibot_imperva"),
    ("/tspd/", "antibot_f5_tspd"),
    ("apm_do_not_touch", "antibot_f5_tspd"),
    ("are you a robot", "antibot_captcha"),
    ("verify you are human", "antibot_captcha"),
    ("429 too many requests", "http_429"),
)

# Broader markers only trusted in error_msg or non-200 error responses (never raw 200 HTML where
# cdnjs.cloudflare.com / akamaihd.net / recaptcha widgets routinely appear on normal article pages)
_ERROR_OR_NON200_ANTIBOT_PATTERNS = (
    ("cloudflare", "antibot_cloudflare"),
    ("attention required", "antibot_cloudflare"),
    ("akamai", "antibot_akamai"),
    ("captcha", "antibot_captcha"),
    ("access denied", "antibot_access_denied"),
    ("too many requests", "http_429"),
    ("rate limit exceeded", "http_429"),
)

_DOI_PREFIX_RE = re.compile(r"(10\.\d{4,9})/", re.IGNORECASE)


def _is_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    if (os.getenv("ZTE_DISABLE_NEGATIVE_CACHE") or "").strip().lower() in ("1", "true", "yes"):
        return False
    cfg = config if isinstance(config, dict) else {}
    if not cfg:
        try:
            cfg = common.load_config() or {}
        except Exception:
            cfg = {}
    return bool(cfg.get("NEGATIVE_CACHE_ENABLED", True))


def get_negative_cache_db_path(db_path: Optional[str] = None, config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Resolve the SQLite WAL negative cache file path."""
    if db_path:
        return os.path.abspath(os.path.expanduser(db_path))
    env_path = (os.getenv("ZTE_NEGATIVE_CACHE_DB_PATH") or "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))
    cfg = config if isinstance(config, dict) else {}
    if not cfg:
        try:
            cfg = common.load_config() or {}
        except Exception:
            cfg = {}
    cfg_path = str(cfg.get("NEGATIVE_CACHE_DB_PATH") or "").strip()
    if cfg_path:
        return os.path.abspath(os.path.expanduser(cfg_path))
    # Avoid leaking persistent state across unrelated offline tests unless explicitly configured
    if getattr(socket, "_zotero_offline", False):
        return None
    home = os.path.expanduser("~")
    return os.path.join(home, ".zotero_table_extractor", "online_negative_cache.sqlite")


def extract_target_key(url: Optional[str] = None, doi: Optional[str] = None) -> str:
    """Normalize target URL/DOI into a publisher domain or DOI prefix key."""
    if url:
        try:
            host = (urllib.parse.urlparse(str(url)).hostname or "").lower().strip()
        except Exception:
            host = ""
        if host.startswith("www."):
            host = host[4:]
        if host and host not in _REDIRECTOR_HOSTS and not host.startswith("127.") and host != "localhost":
            return f"domain:{host}"

    for cand in (doi, url):
        if not cand:
            continue
        m = _DOI_PREFIX_RE.search(str(cand))
        if m:
            return f"prefix:{m.group(1).lower()}"
    return ""


def should_negative_cache(
    status_code: Optional[int] = None,
    error_msg: str = "",
    html_snippet: str = "",
) -> Tuple[bool, str]:
    """
    严格判断是否属于可负缓存的反爬 / 403 / 429 瞬态封禁。
    绝不缓存 404/410/401/402、机构登录墙、正常付费墙或含 CDN 脚本的普通 200 页面。
    """
    if status_code in _NON_CACHEABLE_STATUS_CODES:
        return False, ""

    # HTTP 429 明确属于限流，不因页面页脚文案误拒
    if status_code == 429:
        return True, "http_429"

    err_low = (error_msg or "").lower()
    html_low = (html_snippet or "")[:12000].lower()
    combined = f"{err_low}\n{html_low}"

    # 显式排除 404/付费墙/订阅权限/机构登录墙页面，避免缓存污染
    if any(p in combined for p in _PAYWALL_OR_DEAD_PATTERNS):
        return False, ""

    if status_code == 403:
        return True, "http_403_antibot"

    # Full academic article 200 pages (with citation_doi/citation_title) must never pollute negative cache
    if status_code == 200 and html_low and (
        'name="citation_doi"' in html_low
        or "name='citation_doi'" in html_low
        or 'name="citation_title"' in html_low
        or "name='citation_title'" in html_low
        or 'name="dc.identifier"' in html_low
    ):
        return False, ""

    if html_snippet and hasattr(common, "is_anti_bot_blocked"):
        try:
            if common.is_anti_bot_blocked("", html_snippet[:12000]):
                if "bm-verify" in html_low:
                    return True, "antibot_akamai"
                return True, "antibot_challenge"
        except Exception:
            pass

    for marker, reason in _STRICT_HTML_ANTIBOT_PATTERNS:
        if marker in combined:
            if status_code is None or status_code in (200, 403, 429, 503):
                return True, reason

    # 宽泛反爬词仅匹配 error_msg 或非 200 错误页，防止 cdnjs.cloudflare.com / akamaihd.net 误杀正常 200 论文页
    broad_haystack = err_low if status_code == 200 else combined
    for marker, reason in _ERROR_OR_NON200_ANTIBOT_PATTERNS:
        if marker in broad_haystack:
            if status_code is None or status_code in (200, 403, 429, 503):
                return True, reason

    if status_code == 503 and any(k in combined for k in ("cloudflare", "ddos", "bot", "rate limit", "service unavailable")):
        return True, "http_503_antibot"

    return False, ""


def _open_conn(resolved_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(resolved_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(resolved_path, timeout=3.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS negative_cache (
            channel TEXT NOT NULL,
            target_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            status_code INTEGER,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            PRIMARY KEY (channel, target_key)
        )
        """
    )
    return conn


def is_negatively_cached(
    channel: str,
    url: Optional[str] = None,
    doi: Optional[str] = None,
    db_path: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Return cached negative entry dict if channel+target is currently blocked, else None."""
    if not channel or not _is_enabled(config):
        return None
    target_key = extract_target_key(url=url, doi=doi)
    if not target_key:
        return None
    resolved_path = get_negative_cache_db_path(db_path=db_path, config=config)
    if not resolved_path or not os.path.isfile(resolved_path):
        return None

    ts = float(now) if now is not None else time.time()
    conn = None
    try:
        conn = _open_conn(resolved_path)
        row = conn.execute(
            "SELECT reason, status_code, created_at, expires_at FROM negative_cache WHERE channel = ? AND target_key = ?",
            (channel.strip().lower(), target_key),
        ).fetchone()
        if not row:
            return None
        reason, status_code, created_at, expires_at = row
        if float(expires_at) <= ts:
            conn.execute(
                "DELETE FROM negative_cache WHERE channel = ? AND target_key = ?",
                (channel.strip().lower(), target_key),
            )
            conn.commit()
            return None
        return {
            "channel": channel.strip().lower(),
            "target_key": target_key,
            "reason": reason,
            "status_code": status_code,
            "created_at": created_at,
            "expires_at": expires_at,
            "remaining_s": max(0.0, float(expires_at) - ts),
        }
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def record_negative_cache(
    channel: str,
    reason: str = "",
    url: Optional[str] = None,
    doi: Optional[str] = None,
    status_code: Optional[int] = None,
    error_msg: str = "",
    html_snippet: str = "",
    ttl_s: Optional[int] = None,
    db_path: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
    force: bool = False,
) -> bool:
    """
    写入跨进程负缓存。默认必须通过 should_negative_cache 严格校验（避免 404/付费墙污染）。
    """
    if not channel or not _is_enabled(config):
        return False
    target_key = extract_target_key(url=url, doi=doi)
    if not target_key:
        return False

    matched_reason = reason
    if not force:
        ok, detected = should_negative_cache(
            status_code=status_code,
            error_msg=error_msg or reason,
            html_snippet=html_snippet,
        )
        if not ok:
            return False
        matched_reason = detected or reason or "antibot"
    elif not matched_reason:
        matched_reason = "antibot"

    resolved_path = get_negative_cache_db_path(db_path=db_path, config=config)
    if not resolved_path:
        return False

    if ttl_s is None:
        cfg = config if isinstance(config, dict) else {}
        if not cfg:
            try:
                cfg = common.load_config() or {}
            except Exception:
                cfg = {}
        try:
            ttl_s = int(cfg.get("NEGATIVE_CACHE_TTL_S") or DEFAULT_NEGATIVE_CACHE_TTL_S)
        except (TypeError, ValueError):
            ttl_s = DEFAULT_NEGATIVE_CACHE_TTL_S
        if status_code == 429 or matched_reason == "http_429":
            ttl_s = min(ttl_s, DEFAULT_RATE_LIMIT_TTL_S)
        elif status_code == 200 and matched_reason in ("antibot_akamai", "antibot_challenge"):
            ttl_s = min(ttl_s, DEFAULT_TRANSIENT_CHALLENGE_TTL_S)

    if ttl_s <= 0:
        return False

    ts = float(now) if now is not None else time.time()
    expires_at = ts + float(ttl_s)
    conn = None
    try:
        conn = _open_conn(resolved_path)
        conn.execute(
            """
            INSERT INTO negative_cache (channel, target_key, reason, status_code, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, target_key) DO UPDATE SET
                reason = excluded.reason,
                status_code = excluded.status_code,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """,
            (channel.strip().lower(), target_key, matched_reason, status_code, ts, expires_at),
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def clear_negative_cache(
    channel: Optional[str] = None,
    url: Optional[str] = None,
    doi: Optional[str] = None,
    db_path: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> int:
    """Clear matching entries (or entire table if no filters given). Returns deleted row count."""
    resolved_path = get_negative_cache_db_path(db_path=db_path, config=config)
    if not resolved_path or not os.path.isfile(resolved_path):
        return 0
    target_key = extract_target_key(url=url, doi=doi) if (url or doi) else ""
    conn = None
    try:
        conn = _open_conn(resolved_path)
        if channel and target_key:
            cur = conn.execute(
                "DELETE FROM negative_cache WHERE channel = ? AND target_key = ?",
                (channel.strip().lower(), target_key),
            )
        elif channel:
            cur = conn.execute(
                "DELETE FROM negative_cache WHERE channel = ?",
                (channel.strip().lower(),),
            )
        elif target_key:
            cur = conn.execute(
                "DELETE FROM negative_cache WHERE target_key = ?",
                (target_key,),
            )
        else:
            cur = conn.execute("DELETE FROM negative_cache")
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception:
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
