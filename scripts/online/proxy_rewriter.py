"""online.proxy_rewriter — 机构代理 URL 重写（EZProxy / WebVPN AES-128-CFB）与保守会话校验。

功能：
1. EZProxy URL 重写：支持 `{url}`、`{qurl}`、`{host_dashed}`、`{path}` 模板以及标准 `/login?url=` 前缀，天然幂等；
2. 高校 WebVPN（WrdVPN AES-128-CFB）URL 重写与解码：懒加载 `pycryptodome`（`Crypto.Cipher.AES`），未安装时优雅降级不崩导入；
3. 保守会话校验（`validate_institutional_session`）：识别明确的机构登录墙、SSO 跳转、密码表单与过期 Cookie，避免对正常文章页产生误判。
"""

import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

try:
    from .. import common
except ImportError:
    import common


_DEFAULT_WEBVPN_KEY = "wrdvpnisthebest!"
_DEFAULT_WEBVPN_IV = "wrdvpnisthebest!"

# 永远不走机构代理重写的公共 API / 本地地址 / 解析器域
_NEVER_PROXY_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "doi.org",
    "dx.doi.org",
    "api.elsevier.com",
    "api.springernature.com",
    "api.unpaywall.org",
    "api.crossref.org",
    "api.openalex.org",
    "api.firecrawl.dev",
    "eutils.ncbi.nlm.nih.gov",
    "www.ebi.ac.uk",
    "arxiv.org",
    "ar5iv.labs.arxiv.org",
})

_LOGIN_URL_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/users/sign_in",
    "/cas/login",
    "/authserver/",
    "/idp/",
    "wayf.",
    "shibboleth",
    "carsi.edu.cn",
    "/oauth/authorize",
    "/sso/",
    "wengine-auth",
)

_LOGIN_HTML_MARKERS = (
    "统一身份认证",
    "校园网登录",
    "高校统一认证",
    "shibboleth identity provider",
    "ezproxy login",
    "please log in to ezproxy",
    "webvpn login",
    "session has expired",
    "your session expired",
    "会话已过期",
)

_PASSWORD_INPUT_RE = re.compile(r'<input\b[^>]*\btype\s*=\s*["\']?password["\']?', re.IGNORECASE)


def _is_non_proxyable_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return True
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return True
    host = parsed.hostname.lower()
    if host in _NEVER_PROXY_HOSTS or host.startswith("127."):
        return True
    return False


def rewrite_ezproxy_url(target_url: str, login_url_or_template: str) -> str:
    """
    将目标论文 URL 重写为 EZProxy 机构代理 URL。

    支持以下模板形态：
    - `https://ezproxy.univ.edu/login?url={url}`
    - `https://ezproxy.univ.edu/login?qurl={qurl}` 或 `...qurl={url}`
    - `https://{host_dashed}.ezproxy.univ.edu{path}` 或 `https://{host_dashed}.ezproxy.univ.edu`
    - 裸登录前缀：`https://ezproxy.univ.edu/login` 或 `https://ezproxy.univ.edu/login?url=`
    """
    if not target_url or not login_url_or_template:
        return target_url
    target_url = target_url.strip()
    tpl = login_url_or_template.strip()
    if _is_non_proxyable_url(target_url):
        return target_url

    parsed_target = urllib.parse.urlparse(target_url)
    target_host = (parsed_target.hostname or "").lower()

    # 幂等性检查：若目标 URL 已在同一个 EZProxy 主机/后缀下，直接原样返回
    if "{host_dashed}" in tpl:
        tpl_host = (urllib.parse.urlparse(tpl.replace("{host_dashed}", "placeholder").replace("{path}", "")).hostname or "").lower()
        suffix = tpl_host.replace("placeholder", "")
        if suffix and target_host.endswith(suffix):
            return target_url
    else:
        tpl_host = (urllib.parse.urlparse(tpl.replace("{url}", "").replace("{qurl}", "")).hostname or "").lower()
        if tpl_host and (target_host == tpl_host or target_host.endswith("." + tpl_host)):
            return target_url

    # 1. {host_dashed} 域名前缀重写模式
    if "{host_dashed}" in tpl:
        host_dashed = target_host.replace(".", "-")
        if parsed_target.port:
            host_dashed = f"{host_dashed}-{parsed_target.port}"
        path_part = parsed_target.path or "/"
        if parsed_target.query:
            path_part += f"?{parsed_target.query}"
        if parsed_target.fragment:
            path_part += f"#{parsed_target.fragment}"

        rewritten = tpl.replace("{host_dashed}", host_dashed)
        if "{path}" in rewritten:
            rewritten = rewritten.replace("{path}", path_part)
        else:
            rewritten = rewritten.rstrip("/") + (path_part if path_part.startswith("/") else "/" + path_part)
        return rewritten

    # 2. {qurl} 显式 URL 编码模式
    if "{qurl}" in tpl:
        return tpl.replace("{qurl}", urllib.parse.quote(target_url, safe=""))

    # 3. {url} 模板模式（若前缀为 qurl={url} 则自动 URL 编码）
    if "{url}" in tpl:
        if re.search(r"[?&]qurl=\{url\}", tpl, re.IGNORECASE):
            return tpl.replace("{url}", urllib.parse.quote(target_url, safe=""))
        return tpl.replace("{url}", target_url)

    # 4. 裸 EZProxy login URL 前缀模式
    low_tpl = tpl.lower()
    if low_tpl.endswith("qurl="):
        return tpl + urllib.parse.quote(target_url, safe="")
    if low_tpl.endswith("url="):
        return tpl + target_url
    sep = "&" if "?" in tpl else "?"
    return f"{tpl}{sep}url={target_url}"


def _prepare_aes_16bytes(val: str, default: str) -> bytes:
    raw = (val or default).encode("utf-8")
    if len(raw) < 16:
        raw = raw.ljust(16, b"\x00")
    return raw[:16]


def rewrite_webvpn_url(
    target_url: str,
    base_url: str,
    key: str = _DEFAULT_WEBVPN_KEY,
    iv: str = _DEFAULT_WEBVPN_IV,
    strict: bool = False,
) -> Optional[str]:
    """
    将目标 URL 重写为高校 WebVPN（WrdVPN AES-128-CFB）代理 URL。
    延迟导入 `Crypto.Cipher.AES`（pycryptodome）；未安装时若 strict=False 返回 None（由调用方回退原 URL）。
    """
    if not target_url or not base_url:
        return target_url if not strict else None
    target_url = target_url.strip()
    base_url = base_url.strip().rstrip("/")
    if not base_url.lower().startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    if _is_non_proxyable_url(target_url):
        return target_url

    parsed_base = urllib.parse.urlparse(base_url)
    parsed_target = urllib.parse.urlparse(target_url)
    if parsed_base.hostname and parsed_target.hostname:
        if parsed_target.hostname.lower() == parsed_base.hostname.lower():
            return target_url

    try:
        from Crypto.Cipher import AES
    except ImportError:
        print("[ProxyRewriter] WebVPN 重写需要可选依赖 pycryptodome (`pip install pycryptodome`)，已安全回退直连。")
        if strict:
            raise
        return None

    key_bytes = _prepare_aes_16bytes(key, _DEFAULT_WEBVPN_KEY)
    iv_bytes = _prepare_aes_16bytes(iv, _DEFAULT_WEBVPN_IV)

    hostname = (parsed_target.hostname or "").encode("utf-8")
    cipher = AES.new(key_bytes, AES.MODE_CFB, iv=iv_bytes, segment_size=128)
    encrypted_host = cipher.encrypt(hostname).hex()
    iv_hex = iv_bytes.hex()

    scheme = parsed_target.scheme.lower()
    port = parsed_target.port
    default_port = 443 if scheme == "https" else 80
    proto_seg = f"{scheme}-{port}" if (port and port != default_port) else scheme

    path = parsed_target.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    suffix = path
    if parsed_target.query:
        suffix += f"?{parsed_target.query}"
    if parsed_target.fragment:
        suffix += f"#{parsed_target.fragment}"

    return f"{base_url}/{proto_seg}/{iv_hex}{encrypted_host}{suffix}"


def decode_webvpn_url(
    webvpn_url: str,
    key: str = _DEFAULT_WEBVPN_KEY,
    iv: str = _DEFAULT_WEBVPN_IV,
) -> Optional[str]:
    """将 WrdVPN AES-128-CFB 代理 URL 还原为原始目标 URL（需要 pycryptodome）。"""
    if not webvpn_url:
        return None
    try:
        from Crypto.Cipher import AES
    except ImportError:
        return None

    parsed = urllib.parse.urlparse(webvpn_url.strip())
    m = re.match(r"^/(https?)(?:-(\d+))?/([0-9a-fA-F]{32})([0-9a-fA-F]+)(/.*)?$", parsed.path)
    if not m:
        return None
    scheme, port_str, iv_hex, cipher_hex, rest_path = m.groups()
    key_bytes = _prepare_aes_16bytes(key, _DEFAULT_WEBVPN_KEY)
    try:
        iv_bytes = bytes.fromhex(iv_hex)
        cipher_bytes = bytes.fromhex(cipher_hex)
        cipher = AES.new(key_bytes, AES.MODE_CFB, iv=iv_bytes, segment_size=128)
        host = cipher.decrypt(cipher_bytes).decode("utf-8")
    except Exception:
        return None

    netloc = f"{host}:{port_str}" if port_str else host
    rest_path = rest_path or "/"
    out = f"{scheme}://{netloc}{rest_path}"
    if parsed.query:
        out += f"?{parsed.query}"
    if parsed.fragment:
        out += f"#{parsed.fragment}"
    return out


def is_institutional_proxy_url(url: str, config: Optional[Dict[str, Any]] = None) -> bool:
    """Return True if *url* already points to the configured EZProxy or WebVPN gateway host."""
    if not url or not isinstance(url, str):
        return False
    parsed = urllib.parse.urlparse(url.strip())
    target_host = (parsed.hostname or "").lower()
    if not target_host:
        return False

    cfg = config if isinstance(config, dict) else {}
    if not cfg:
        try:
            cfg = common.load_config() or {}
        except Exception:
            cfg = {}

    if cfg.get("EZPROXY_ENABLED") and cfg.get("EZPROXY_LOGIN_URL"):
        tpl = str(cfg["EZPROXY_LOGIN_URL"]).strip()
        if "{host_dashed}" in tpl:
            tpl_host = (
                urllib.parse.urlparse(
                    tpl.replace("{host_dashed}", "placeholder").replace("{path}", "")
                ).hostname
                or ""
            ).lower()
            suffix = tpl_host.replace("placeholder", "")
            if suffix and target_host.endswith(suffix):
                return True
        else:
            tpl_host = (
                urllib.parse.urlparse(tpl.replace("{url}", "").replace("{qurl}", "")).hostname
                or ""
            ).lower()
            if tpl_host and (target_host == tpl_host or target_host.endswith("." + tpl_host)):
                return True

    if cfg.get("WEBVPN_ENABLED") and cfg.get("WEBVPN_BASE_URL"):
        base_url = str(cfg["WEBVPN_BASE_URL"]).strip().rstrip("/")
        if not base_url.lower().startswith(("http://", "https://")):
            base_url = f"https://{base_url}"
        base_host = (urllib.parse.urlparse(base_url).hostname or "").lower()
        if base_host and target_host == base_host:
            return True

    return False


def maybe_rewrite_institutional_url(url: str, config: Optional[Dict[str, Any]] = None) -> str:
    """
    根据配置按需重写机构代理 URL（默认关闭；仅当显式开启 EZPROXY_ENABLED 或 WEBVPN_ENABLED 时生效）。
    优先级：EZProxy > WebVPN；校园网直连 (IS_CAMPUS_NETWORK=True) 时不自动改写（除非显式只开代理）。
    """
    if not url or _is_non_proxyable_url(url):
        return url

    cfg = config if isinstance(config, dict) else {}
    if not cfg:
        try:
            cfg = common.load_config() or {}
        except Exception:
            cfg = {}

    if cfg.get("EZPROXY_ENABLED") and cfg.get("EZPROXY_LOGIN_URL"):
        return rewrite_ezproxy_url(url, str(cfg["EZPROXY_LOGIN_URL"]))

    if cfg.get("WEBVPN_ENABLED") and cfg.get("WEBVPN_BASE_URL"):
        rewritten = rewrite_webvpn_url(
            url,
            str(cfg["WEBVPN_BASE_URL"]),
            key=str(cfg.get("WEBVPN_KEY") or _DEFAULT_WEBVPN_KEY),
            iv=str(cfg.get("WEBVPN_IV") or _DEFAULT_WEBVPN_IV),
            strict=False,
        )
        if rewritten:
            return rewritten

    return url


def validate_institutional_session(
    html_text: str = "",
    final_url: str = "",
    status_code: Optional[int] = 200,
    cookies: Optional[List[Dict[str, Any]]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """
    保守机构会话有效性校验。

    设计原则（Conservative）：
    - 仅在存在明确登录/SSO 重定向、密码表单、反爬封禁或核心 Cookie 全部过期时标记 invalid；
    - 绝不因普通页面结构差异误杀有效会话。
    返回: {"valid": bool, "reason": str, "needs_relogin": bool}
    """
    if status_code in (401, 407):
        return {"valid": False, "reason": f"http_{status_code}_auth_required", "needs_relogin": True}

    if final_url:
        try:
            _fp = urllib.parse.urlparse(str(final_url).strip())
            _host_path = f"{(_fp.hostname or '').lower()}{(_fp.path or '').lower()}"
        except Exception:
            _host_path = str(final_url).lower()
        for marker in _LOGIN_URL_MARKERS:
            if marker in _host_path:
                return {"valid": False, "reason": f"login_redirect:{marker}", "needs_relogin": True}

    if html_text:
        sample = html_text[:20000]
        low_sample = sample.lower()
        low_full = html_text.lower() if len(html_text) <= 250000 else html_text[:250000].lower()

        if hasattr(common, "is_anti_bot_blocked"):
            try:
                if common.is_anti_bot_blocked("", sample[:6000]):
                    return {"valid": False, "reason": "antibot_blocked", "needs_relogin": False}
            except Exception:
                pass

        # 仅在页面含密码框或明确登录页标题/文案，且整页不含真实数据表格或论文正文容器时判定为登录墙
        has_password_input = bool(_PASSWORD_INPUT_RE.search(sample))
        has_login_text = any(m in low_sample for m in _LOGIN_HTML_MARKERS)
        has_data_table = "<table" in low_full and ("<tbody" in low_full or "<th" in low_full)
        has_article_body = (
            len(html_text) > 30000
            and ("<article" in low_full or "citation_doi" in low_full or "citation_title" in low_full)
        )

        if has_password_input and not has_data_table and not has_article_body:
            return {"valid": False, "reason": "login_password_form", "needs_relogin": True}
        if has_login_text and not has_data_table and not has_article_body:
            return {"valid": False, "reason": "institutional_login_wall", "needs_relogin": True}

    if cookies is not None:
        ts = float(now) if now is not None else time.time()
        dated = []
        for c in cookies:
            if not isinstance(c, dict):
                continue
            exp = c.get("expires")
            if exp is None:
                exp = c.get("expirationDate")
            if exp is not None:
                try:
                    exp_f = float(exp)
                    if exp_f > 0:
                        dated.append(exp_f)
                except (TypeError, ValueError):
                    pass
        if dated and all(exp_f <= ts for exp_f in dated):
            return {"valid": False, "reason": "all_cookies_expired", "needs_relogin": True}

    return {"valid": True, "reason": "ok", "needs_relogin": False}
