"""curl_cffi 指纹请求协议登录 labs.google — 走 NextAuth + Google OAuth 流程"""
import json
import re
from typing import Any, Dict, List, Optional

from curl_cffi.requests import AsyncSession

from .config import config
from .logger import logger
from .proxy_utils import parse_proxy

# Google OAuth 所需的 cookie 名称
_GOOGLE_COOKIE_NAMES = ("SID", "HSID", "SSID", "APISID", "SAPISID")


def _parse_google_cookies(raw: str) -> Dict[str, str]:
    """解析 Google cookies 输入，支持 JSON 和纯文本格式"""
    text = (raw or "").strip()
    if not text:
        return {}

    # 尝试 JSON
    try:
        data = json.loads(text)
        if isinstance(data, list):
            result = {}
            for item in data:
                if isinstance(item, dict):
                    name = item.get("name", "")
                    value = item.get("value", "")
                    if name and value:
                        result[name] = value
            return result
        if isinstance(data, dict):
            cookies_list = data.get("cookies")
            if isinstance(cookies_list, list):
                result = {}
                for item in cookies_list:
                    if isinstance(item, dict):
                        name = item.get("name", "")
                        value = item.get("value", "")
                        if name and value:
                            result[name] = value
                return result
            return {k: v for k, v in data.items() if isinstance(v, str) and v}
    except (json.JSONDecodeError, ValueError):
        pass

    # 纯文本格式：name=value; name2=value2
    result = {}
    for part in text.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            name = name.strip()
            value = value.strip()
            if name and value:
                result[name] = value
    return result


def _build_cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _get_set_cookies(headers) -> List[str]:
    """安全获取所有 Set-Cookie 头值"""
    # curl_cffi Headers 支持 getlist()
    if hasattr(headers, "getlist"):
        return headers.getlist("set-cookie") or []
    if hasattr(headers, "get_list"):
        return headers.get_list("set-cookie") or []
    val = headers.get("set-cookie")
    return [val] if val else []


def _merge_cookies(cookies: Dict[str, str], headers) -> None:
    """从响应 Set-Cookie 头合并 cookies"""
    for val in _get_set_cookies(headers):
        parts = val.split(";")[0]
        if "=" in parts:
            name, _, value = parts.partition("=")
            cookies[name.strip()] = value.strip()


def _extract_session_token(headers) -> Optional[str]:
    """从 Set-Cookie 提取 session token"""
    cookie_name = config.session_cookie_name
    for val in _get_set_cookies(headers):
        if val.startswith(f"{cookie_name}="):
            return val.split("=", 1)[1].split(";")[0].strip()
    return None


def _extract_redirect_from_html(text: str) -> Optional[str]:
    """从 HTML 响应中提取跳转 URL（meta refresh / JS location / form action）"""
    # <meta http-equiv="refresh" content="0;url=...">
    m = re.search(r'content\s*=\s*["\']?\d+\s*;\s*url\s*=\s*([^"\'>\s]+)', text, re.IGNORECASE)
    if m:
        return m.group(1)
    # window.location = "..." / location.href = "..." / location.replace("...")
    m = re.search(r'location\.(?:href|replace)\s*\(\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'location\s*=\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m:
        return m.group(1)
    # <form action="..."> 自动提交
    m = re.search(r'<form[^>]*action\s*=\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m:
        return m.group(1)
    # accounts.google.com 页面中的 URL 参数
    m = re.search(r'(https://labs\.google/fx/api/auth/callback/google[^"\'<>\s]*)', text)
    if m:
        return m.group(1)
    # continue 参数
    m = re.search(r'[&?]continue=([^"\'<>\s&]+)', text)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    return None


class ProtocolLogin:
    """curl_cffi 指纹请求协议登录 labs.google"""

    LABS_BASE = "https://labs.google/fx"
    IMPERSONATE = "chrome124"

    def _get_proxy_url(self, proxy_str: Optional[str]) -> Optional[str]:
        if not proxy_str:
            return None
        proxy_config = parse_proxy(proxy_str)
        if not proxy_config:
            return None
        server = proxy_config.get("server", "")
        username = proxy_config.get("username", "")
        password = proxy_config.get("password", "")
        if not server:
            return None
        if username and password:
            # 注入认证信息到 URL
            scheme, _, rest = server.partition("://")
            return f"{scheme}://{username}:{password}@{rest}"
        return server

    async def login(
        self,
        google_cookies_raw: str,
        proxy: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        协议登录。

        输入：Google cookies（JSON 或纯文本，需要 SID/HSID/SSID/APISID/SAPISID）
        输出：{"success": bool, "session_token": str, "error": str}
        """
        google_cookies = _parse_google_cookies(google_cookies_raw)
        has_required = any(name in google_cookies for name in _GOOGLE_COOKIE_NAMES)
        if not has_required:
            return {
                "success": False,
                "error": "未找到有效的 Google cookie（需要 SID/HSID/SSID/APISID/SAPISID 中的至少一个）",
            }

        proxy_url = self._get_proxy_url(proxy)
        session_kwargs = {"impersonate": self.IMPERSONATE}
        if proxy_url:
            session_kwargs["proxy"] = proxy_url

        async with AsyncSession(**session_kwargs) as s:
            try:
                # 步骤1：获取 CSRF token
                logger.info("[协议登录] 获取 CSRF token...")
                resp = await s.get(f"{self.LABS_BASE}/api/auth/csrf")
                if resp.status_code != 200:
                    return {"success": False, "error": f"CSRF 失败: HTTP {resp.status_code}"}

                csrf_token = resp.json().get("csrfToken")
                if not csrf_token:
                    return {"success": False, "error": "CSRF 响应中无 csrfToken"}

                labs_cookies = {}
                _merge_cookies(labs_cookies, resp.headers)

                # 步骤2：POST signin/google → 获取 OAuth 重定向 URL
                logger.info("[协议登录] 请求 Google OAuth URL...")
                resp = await s.post(
                    f"{self.LABS_BASE}/api/auth/signin/google",
                    data={
                        "csrfToken": csrf_token,
                        "callbackUrl": "https://labs.google/fx",
                        "json": "true",
                    },
                    headers={
                        "Referer": self.LABS_BASE,
                        "Origin": "https://labs.google",
                        "Cookie": _build_cookie_header(labs_cookies) if labs_cookies else "",
                    },
                    allow_redirects=False,
                )
                if resp.status_code != 200:
                    return {"success": False, "error": f"Signin 失败: HTTP {resp.status_code}"}

                _merge_cookies(labs_cookies, resp.headers)
                signin_data = resp.json()
                redirect_url = signin_data.get("redirect") or signin_data.get("url")
                if not redirect_url:
                    return {"success": False, "error": f"无重定向 URL: {json.dumps(signin_data)[:200]}"}

                # 添加 login_hint 跳过账号选择器
                if email:
                    from urllib.parse import urlencode, urlparse, parse_qs
                    parsed = urlparse(redirect_url)
                    qs = parse_qs(parsed.query)
                    qs["login_hint"] = [email]
                    new_query = urlencode({k: v[0] for k, v in qs.items()}, doseq=True)
                    redirect_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_query}"
                    logger.info(f"[协议登录] 添加 login_hint={email}")

                from urllib.parse import urljoin

                # 步骤3：用 Google cookies 跟随 OAuth 重定向链
                logger.info("[协议登录] 跟随 Google OAuth 重定向...")
                google_cookie_header = _build_cookie_header(google_cookies)
                callback_url = None
                current_url = redirect_url

                for i in range(10):
                    resp = await s.get(
                        current_url,
                        headers={
                            "Cookie": google_cookie_header,
                            "Referer": "https://labs.google/" if i == 0 else "https://accounts.google.com/",
                        },
                        allow_redirects=False,
                    )
                    location = resp.headers.get("location")

                    # 检查是否有 callback URL
                    check_url = location or ""
                    if "labs.google/fx/api/auth/callback/google" in check_url:
                        callback_url = check_url
                        break

                    if location:
                        logger.info(f"[协议登录] 重定向到: {location[:100]}...")
                        current_url = location
                        continue

                    # 没有 Location 头，尝试从 HTML 提取跳转
                    if resp.status_code == 200:
                        body = resp.text or ""

                        # 检查是否被拒绝
                        if "/v3/signin/rejected" in body or "signin/rejected" in body:
                            return {"success": False, "error": "Google 拒绝登录，Cookies 可能已过期或被风控，请重新导出"}

                        html_redirect = _extract_redirect_from_html(body)
                        if html_redirect:
                            # 相对路径补全为绝对 URL
                            if html_redirect.startswith("/"):
                                html_redirect = urljoin(current_url, html_redirect)
                            logger.info(f"[协议登录] 从 HTML 提取到跳转: {html_redirect[:100]}...")
                            if "labs.google/fx/api/auth/callback/google" in html_redirect:
                                callback_url = html_redirect
                                break
                            current_url = html_redirect
                            continue

                    return {"success": False, "error": f"Google OAuth 未返回重定向（HTTP {resp.status_code}）"}

                if not callback_url:
                    return {"success": False, "error": "Google OAuth 流程中未获得 callback URL"}

                # 步骤4：访问 callback 换取 session cookie
                logger.info("[协议登录] 交换 auth code 换取 session...")
                resp = await s.get(
                    callback_url,
                    headers={
                        "Cookie": _build_cookie_header(labs_cookies),
                        "Referer": "https://accounts.google.com/",
                    },
                    allow_redirects=False,
                )

                session_token = _extract_session_token(resp.headers)

                # callback 可能多次重定向，跟随直到拿到 session token
                for _ in range(5):
                    if session_token:
                        break
                    location = resp.headers.get("location")
                    if not location or resp.status_code not in (301, 302, 303, 307, 308):
                        break
                    _merge_cookies(labs_cookies, resp.headers)
                    resp = await s.get(
                        location,
                        headers={"Cookie": _build_cookie_header(labs_cookies)},
                        allow_redirects=False,
                    )
                    session_token = _extract_session_token(resp.headers)

                if not session_token:
                    return {"success": False, "error": "未获取到 session token，Google session 可能已过期"}

                logger.info("[协议登录] 登录成功")
                return {"success": True, "session_token": session_token}

            except Exception as e:
                logger.error(f"[协议登录] 异常: {e}")
                return {"success": False, "error": str(e)}


protocol_loginer = ProtocolLogin()
