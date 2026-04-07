"""浏览器管理 v3.1 - 持久化上下文 + VNC登录 + Headless刷新"""
import asyncio
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, BrowserContext, Playwright
from .config import config
from .database import profile_db
from .proxy_utils import parse_proxy, format_proxy_for_playwright
from .logger import logger

try:
    import pyautogui
    import pygetwindow as pygetwindow
    import win32con
    import win32gui

    pyautogui.FAILSAFE = False
    DESKTOP_AUTOMATION_AVAILABLE = True
except Exception:
    pyautogui = None
    pygetwindow = None
    win32con = None
    win32gui = None
    DESKTOP_AUTOMATION_AVAILABLE = False


# 内存优化参数
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--disable-features=TranslateUI",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--single-process",  # 单进程模式，省内存
    "--max_old_space_size=128",  # 限制 V8 内存
    "--js-flags=--max-old-space-size=128",
]

LOGIN_BROWSER_ARGS = BROWSER_ARGS[:6] + ["--disable-blink-features=AutomationControlled"]

BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
BUTTON_CANDIDATE_SELECTORS = "button, [role='button'], input[type='submit'], input[type='button'], a[role='button'], cr-button"
ACCOUNT_INPUT_SELECTORS = [
    "#identifierId",
    "input[name='identifier']",
    "input[autocomplete='username']",
    "input[autocomplete='email']",
    "input[type='email']",
    "input[type='tel']",
]
PASSWORD_INPUT_SELECTORS = [
    "input[name='Passwd']",
    "input[autocomplete='current-password']",
    "input[autocomplete='password']",
    "input[type='password']",
]
ACCOUNT_SUBMIT_SELECTORS = [
    "#identifierNext",
    "#identifierNext button",
    "[id='identifierNext'] button",
]
PASSWORD_SUBMIT_SELECTORS = [
    "#passwordNext",
    "#passwordNext button",
    "[id='passwordNext'] button",
]

SUPERVISOR_CONF = "/etc/supervisor/conf.d/supervisord.conf"
VNC_START_ORDER = ("xvfb", "fluxbox", "x11vnc", "novnc")
VNC_STOP_ORDER = ("novnc", "x11vnc", "fluxbox", "xvfb")


class BrowserManager:
    """浏览器管理器 - 持久化上下文"""

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._active_context: Optional[BrowserContext] = None
        self._active_profile_id: Optional[int] = None
        self._lock = asyncio.Lock()

    async def start(self):
        """启动 Playwright"""
        if self._playwright:
            return
        logger.info("启动 Playwright...")
        self._playwright = await async_playwright().start()
        os.makedirs(config.profiles_dir, exist_ok=True)
        logger.info("Playwright 已启动")

    async def stop(self):
        """停止"""
        await self._close_active()
        await self._stop_vnc_stack()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    def _supervisorctl(self, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
        exe = shutil.which("supervisorctl")
        if not exe:
            raise RuntimeError("supervisorctl not found")
        cmd = [exe, "-c", SUPERVISOR_CONF, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)

    def _get_supervisor_status(self) -> Dict[str, str]:
        try:
            cp = self._supervisorctl("status", timeout=8.0)
        except Exception:
            return {}

        status: Dict[str, str] = {}
        for line in (cp.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                status[parts[0]] = parts[1]
        return status

    async def _ensure_vnc_stack(self) -> bool:
        if not config.enable_vnc:
            return False

        status = self._get_supervisor_status()
        for prog in VNC_START_ORDER:
            if status.get(prog) == "RUNNING":
                continue
            try:
                cp = self._supervisorctl("start", prog, timeout=20.0)
                if cp.returncode != 0:
                    logger.warning(f"启动 {prog} 失败: {(cp.stdout or '').strip()} {(cp.stderr or '').strip()}")
                    return False
            except Exception as e:
                logger.warning(f"启动 {prog} 异常: {e}")
                return False

            if prog == "xvfb":
                await asyncio.sleep(0.4)

        return True

    async def _stop_vnc_stack(self) -> None:
        if not config.enable_vnc:
            return

        for prog in VNC_STOP_ORDER:
            try:
                self._supervisorctl("stop", prog, timeout=10.0)
            except Exception:
                pass

    async def _close_active(self):
        """关闭当前浏览器"""
        if self._active_context:
            try:
                await self._active_context.close()
            except Exception:
                pass
            self._active_context = None
            self._active_profile_id = None
            logger.info("浏览器已关闭")

    def _get_profile_dir(self, profile_id: int) -> str:
        """获取 Profile 持久化目录"""
        return os.path.join(os.path.abspath(config.profiles_dir), f"profile_{profile_id}")

    def _clean_locks(self, profile_dir: str):
        """清理 Chromium 锁文件"""
        lock_files = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
        for lock in lock_files:
            lock_path = os.path.join(profile_dir, lock)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                    logger.info(f"已清理锁文件: {lock}")
                except Exception:
                    pass

    def _mask_token(self, token: str) -> str:
        if not token or len(token) <= 8:
            return token or ""
        return f"{token[:4]}...{token[-4:]}"

    def _normalize_email(self, value: str) -> str:
        return str(value or "").strip().lower()

    def _extract_email_from_text(self, text: str) -> Optional[str]:
        content = str(text or "")
        for match in EMAIL_PATTERN.findall(content):
            normalized = self._normalize_email(match)
            if normalized:
                return normalized
        return None

    def _resolve_known_email(self, profile: Dict[str, Any], body_text: str = "") -> Optional[str]:
        stored_email = self._normalize_email(str(profile.get("email") or ""))
        if stored_email:
            return stored_email

        page_email = self._extract_email_from_text(body_text)
        if page_email:
            return page_email

        login_account = self._normalize_email(str(profile.get("login_account") or ""))
        if login_account and EMAIL_PATTERN.fullmatch(login_account):
            return login_account

        return None

    async def _get_proxy(self, profile: Dict[str, Any]) -> Optional[Dict]:
        """获取代理配置"""
        if profile.get("proxy_enabled") and profile.get("proxy_url"):
            proxy_config = parse_proxy(profile["proxy_url"])
            if proxy_config:
                proxy = format_proxy_for_playwright(proxy_config)
                logger.info(f"[{profile['name']}] 使用代理: {proxy['server']}")
                return proxy
        return None

    async def _safe_page_text(self, page) -> str:
        try:
            body = page.locator("body").first
            if await body.count() <= 0:
                return ""
            return str(await body.inner_text(timeout=2000) or "")
        except Exception:
            return ""

    def _text_contains_any(self, text: str, patterns: List[str]) -> bool:
        lowered = str(text or "").lower()
        if not lowered:
            return False
        return any(str(pattern or "").strip().lower() in lowered for pattern in patterns if str(pattern or "").strip())

    async def _get_locator_search_text(self, locator) -> str:
        parts: List[str] = []

        try:
            parts.append(str(await locator.inner_text(timeout=1000) or ""))
        except Exception:
            pass

        try:
            parts.append(str(await locator.text_content(timeout=1000) or ""))
        except Exception:
            pass

        for attr in ("value", "aria-label", "title", "name", "data-identifier", "data-email"):
            try:
                parts.append(str(await locator.get_attribute(attr) or ""))
            except Exception:
                pass

        return " ".join(part.strip() for part in parts if str(part or "").strip())

    async def _click_first_visible(self, page, selectors: List[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() <= 0 or not await locator.is_visible():
                    continue
                await locator.click(timeout=5000)
                await asyncio.sleep(1)
                return True
            except Exception:
                continue
        return False

    async def _has_visible_selector(self, page, selectors: List[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_page_progress(
        self,
        page,
        previous_url: str,
        current_selectors: List[str],
        success_selectors: Optional[List[str]] = None,
        attempts: int = 5,
    ) -> bool:
        success_selectors = success_selectors or []
        for _ in range(max(1, attempts)):
            if str(page.url or "") != previous_url:
                return True
            if success_selectors and await self._has_visible_selector(page, success_selectors):
                return True
            if current_selectors and not await self._has_visible_selector(page, current_selectors):
                return True
            await asyncio.sleep(0.4)
        return False

    async def _click_button_by_text(self, page, patterns: List[str]) -> bool:
        escaped = [re.escape(str(pattern or "").strip()) for pattern in patterns if str(pattern or "").strip()]
        if not escaped:
            return False

        regex = re.compile("|".join(escaped), re.IGNORECASE)
        try:
            candidates = page.locator(BUTTON_CANDIDATE_SELECTORS)
            count = min(await candidates.count(), 100)
        except Exception:
            return False

        for index in range(count):
            try:
                locator = candidates.nth(index)
                if not await locator.is_visible():
                    continue
                label = str(await self._get_locator_search_text(locator) or "").strip()
                if not label or not regex.search(label):
                    continue
                await locator.click(timeout=5000)
                await asyncio.sleep(1)
                return True
            except Exception:
                continue
        return False

    async def _click_text_if_visible(self, page, patterns: List[str]) -> bool:
        for pattern in patterns:
            text = str(pattern or "").strip()
            if not text:
                continue
            try:
                candidate_buttons = page.locator(BUTTON_CANDIDATE_SELECTORS)
                count = min(await candidate_buttons.count(), 40)
                for index in range(count):
                    locator = candidate_buttons.nth(index)
                    if not await locator.is_visible():
                        continue
                    label = str(await self._get_locator_search_text(locator) or "").strip()
                    if text.lower() not in label.lower():
                        continue
                    await locator.click(timeout=5000)
                    await asyncio.sleep(1)
                    return True
                locator = page.get_by_text(text, exact=False).first
                if await locator.count() <= 0:
                    continue
                if not await locator.is_visible():
                    continue
                await locator.click(timeout=5000)
                await asyncio.sleep(1)
                return True
            except Exception:
                continue
        return False

    async def _fill_and_submit_first_visible(
        self,
        page,
        selectors: List[str],
        value: str,
        *,
        submit_selectors: Optional[List[str]] = None,
        submit_patterns: Optional[List[str]] = None,
        success_selectors: Optional[List[str]] = None,
    ) -> bool:
        if not str(value or "").strip():
            return False

        submit_selectors = submit_selectors or []
        submit_patterns = submit_patterns or []
        success_selectors = success_selectors or []

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() <= 0 or not await locator.is_visible():
                    continue
                previous_url = str(page.url or "")
                await locator.click(timeout=5000)
                await locator.fill("", timeout=5000)
                await locator.fill(value, timeout=5000)
                await asyncio.sleep(0.4)

                try:
                    await locator.press("Enter", timeout=3000)
                    if await self._wait_for_page_progress(page, previous_url, selectors, success_selectors):
                        return True
                except Exception:
                    pass

                if submit_selectors and await self._click_first_visible(page, submit_selectors):
                    if await self._wait_for_page_progress(page, previous_url, selectors, success_selectors):
                        return True

                if submit_patterns and await self._click_button_by_text(page, submit_patterns):
                    if await self._wait_for_page_progress(page, previous_url, selectors, success_selectors):
                        return True
            except Exception:
                continue
        return False

    def _detect_login_blocker(self, body_text: str) -> Optional[str]:
        text = str(body_text or "")
        lowered = text.lower()
        if not lowered:
            return None

        blocked_markers = [
            (
                [
                    "wrong password",
                    "密码错误",
                    "密码不正确",
                ],
                "登录密码错误，请检查后重试",
            ),
            (
                [
                    "couldn’t find your google account",
                    "couldn't find your google account",
                    "找不到您的 google 账号",
                    "输入有效的电子邮件地址或电话号码",
                ],
                "登录账号不存在或无法识别",
            ),
            (
                [
                    "2-step verification",
                    "verify it’s you",
                    "verify it's you",
                    "check your phone",
                    "验证您本人身份",
                    "两步验证",
                    "两步驟驗證",
                ],
                "该账号需要人工完成二次验证，请改用手动登录",
            ),
            (
                [
                    "too many failed attempts",
                    "尝试次数过多",
                    "稍后再试",
                    "try again later",
                ],
                "登录尝试过多，请稍后再试",
            ),
            (
                [
                    "enter the characters",
                    "不是您的计算机？请使用访客模式登录",
                    "confirm you’re not a robot",
                    "确认您不是机器人",
                ],
                "登录过程中需要额外人工验证，请改用手动登录",
            ),
        ]

        for markers, message in blocked_markers:
            if any(marker.lower() in lowered for marker in markers):
                return message
        return None

    async def _click_account_choice(self, page, login_account: str) -> bool:
        normalized_account = self._normalize_email(login_account or "")
        if not normalized_account:
            return False

        attr_selectors = [
            f'[data-identifier="{normalized_account}"]',
            f'[data-email="{normalized_account}"]',
        ]
        if await self._click_first_visible(page, attr_selectors):
            return True

        try:
            candidates = page.locator("button, [role='button'], li, div[data-identifier], div[data-email]")
            count = min(await candidates.count(), 80)
        except Exception:
            count = 0

        for index in range(count):
            try:
                locator = candidates.nth(index)
                if not await locator.is_visible():
                    continue
                label = self._normalize_email(await self._get_locator_search_text(locator))
                if normalized_account not in label:
                    continue
                await locator.click(timeout=5000)
                await asyncio.sleep(1)
                return True
            except Exception:
                continue

        return await self._click_text_if_visible(page, [login_account])

    async def _handle_chromium_signin_prompt(self, page, body_text: str) -> bool:
        text = str(body_text or "")
        markers = [
            "Sign in to Chromium",
            "登录 Chromium",
            "Set up a work profile",
            "设置工作资料",
            "Use Chromium without an account",
            "Continue as",
        ]
        if not self._text_contains_any(text, markers):
            return False

        if await self._click_button_by_text(page, ["Continue as", "继续作为", "以此身份继续", "Continue", "続行", "계속", "Continuar"]):
            return True
        if await self._click_button_by_text(
            page,
            ["Use Chromium without an account", "不使用账号", "不使用帳號", "暂不登录", "以后再说", "Not now", "アカウントなし", "계정 없이", "Sin cuenta", "Plus tard"],
        ):
            return True
        return False

    async def _handle_managed_profile_prompt(self, page, body_text: str) -> bool:
        text = str(body_text or "")
        markers = [
            "Continue to work in this profile",
            "This profile will be managed",
            "Your organization manages this profile",
            "Create a work profile",
            "Separate browsing for work",
            "You're signing in with a managed account",
            "Set up your new profile",
            "此资料将受到管理",
            "该资料将受到管理",
        ]
        if not self._text_contains_any(text, markers):
            return False

        return await self._click_button_by_text(
            page,
            [
                "Continue to work in this profile",
                "Continue",
                "继续",
                "I understand",
                "我了解",
                "我瞭解",
                "Confirm",
                "确认",
                "Create profile",
                "创建资料",
            ],
        )

    async def _handle_profile_data_choice_prompt(self, page, body_text: str) -> bool:
        text = str(body_text or "")
        markers = [
            "How do you want to handle your existing browsing data",
            "Keep existing browsing data separate",
            "Continue using this profile",
            "Use existing data",
            "Create new profile",
            "您想如何处理现有的资料数据",
        ]
        if not self._text_contains_any(text, markers):
            return False

        if await self._click_text_if_visible(
            page,
            [
                "Continue using this profile",
                "Use existing data",
                "Keep existing browsing data separate",
                "继续使用此资料",
                "继续使用这个资料",
            ],
        ):
            return True

        return await self._click_button_by_text(
            page,
            ["Continue", "继续", "Confirm", "确认", "Create new profile", "创建新资料"],
        )

    async def _handle_browser_settings_prompts(self, page, body_text: str) -> bool:
        text = str(body_text or "")

        positive_markers = [
            "Turn on sync",
            "Sync and personalize",
            "Save and continue",
            "Sync your stuff",
            "Save time by syncing",
            "开启同步",
            "同步和个性化",
            "保存并继续",
        ]
        dismiss_markers = [
            "Make Chrome your default browser",
            "Make Chromium your default browser",
            "Help improve Chrome",
            "Help improve Chromium",
            "Import bookmarks and settings",
            "Set as default",
            "默认浏览器",
            "导入书签",
            "帮助改进",
        ]

        if self._text_contains_any(text, positive_markers):
            return await self._click_button_by_text(
                page,
                [
                    "Save and continue",
                    "Yes, I'm in",
                    "Turn on sync",
                    "Continue",
                    "继续",
                    "保存并继续",
                    "开启同步",
                    "保存して続行",
                    "동기화 켜기",
                    "Guardar y continuar",
                ],
            )

        if self._text_contains_any(text, dismiss_markers):
            return await self._click_button_by_text(
                page,
                ["Not now", "No thanks", "Skip", "以后再说", "暂不", "跳过", "後で", "나중에", "Ahora no", "Non merci"],
            )

        return False

    async def _advance_google_login(self, page, login_account: str, login_password: str) -> bool:
        if await self._click_button_by_text(page, ["Use another account", "使用其他账号", "使用其他帳戶", "別のアカウントを使用", "다른 계정 사용", "Usar otra cuenta", "Utiliser un autre compte"]):
            return True
        if await self._click_account_choice(page, login_account):
            return True
        if await self._fill_and_submit_first_visible(
            page,
            ACCOUNT_INPUT_SELECTORS,
            login_account,
            submit_selectors=ACCOUNT_SUBMIT_SELECTORS,
            submit_patterns=["下一步", "Next", "继续", "Continue", "次へ", "다음", "Siguiente", "Suivant", "Weiter", "Avançar"],
            success_selectors=PASSWORD_INPUT_SELECTORS,
        ):
            return True
        if await self._fill_and_submit_first_visible(
            page,
            PASSWORD_INPUT_SELECTORS,
            login_password,
            submit_selectors=PASSWORD_SUBMIT_SELECTORS,
            submit_patterns=["下一步", "Next", "继续", "Continue", "登录", "Sign in", "次へ", "続行", "로그인", "Iniciar sesión", "Connexion", "Anmelden", "Fazer login", "Войти"],
            success_selectors=["[href*='labs.google']", "[data-test-id='profile-menu-button']"],
        ):
            return True
        return False

    async def _install_page_route(self, page) -> None:
        async def _route(route, request):
            try:
                if request.resource_type in BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        try:
            await page.route("**/*", _route)
        except Exception:
            pass

    async def _focus_browser_window_for_native_prompt(self) -> bool:
        if os.name != "nt" or not DESKTOP_AUTOMATION_AVAILABLE or pygetwindow is None:
            return False

        title_keywords = [
            "Flow - Chromium",
            "Chromium",
            "Google Chrome",
        ]
        for keyword in title_keywords:
            try:
                windows = [window for window in pygetwindow.getWindowsWithTitle(keyword) if getattr(window, "title", "")]
            except Exception:
                continue
            if not windows:
                continue

            window = windows[0]
            hwnd = getattr(window, "_hWnd", None)
            try:
                window.restore()
            except Exception:
                pass
            try:
                window.activate()
            except Exception:
                if hwnd and win32gui is not None and win32con is not None:
                    try:
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        win32gui.SetForegroundWindow(hwnd)
                    except Exception:
                        pass
            await asyncio.sleep(0.8)
            return True
        return False

    async def _handle_native_chrome_profile_prompts(self) -> bool:
        if os.name != "nt" or not DESKTOP_AUTOMATION_AVAILABLE or pyautogui is None:
            return False
        if not await self._focus_browser_window_for_native_prompt():
            return False

        acted = False
        sequences = [
            ("enter",),
            ("tab", "enter"),
            ("shift+tab", "enter"),
            ("esc",),
        ]
        for sequence in sequences:
            try:
                for key in sequence:
                    if key == "shift+tab":
                        pyautogui.hotkey("shift", "tab")
                    else:
                        pyautogui.press(key)
                    await asyncio.sleep(0.8)
                acted = True
            except Exception:
                return acted
        return acted

    async def _handle_managed_account_prompts(self, page, body_text: str) -> bool:
        if await self._click_button_by_text(page, ["Sign in with Google"]):
            return True

        if await self._handle_chromium_signin_prompt(page, body_text):
            return True
        if await self._handle_managed_profile_prompt(page, body_text):
            return True
        if await self._handle_profile_data_choice_prompt(page, body_text):
            return True
        if await self._handle_browser_settings_prompts(page, body_text):
            return True

        if await self._click_button_by_text(
            page,
            [
                "Continue to work in this profile",
                "Continue as",
                "Save and continue",
                "我瞭解",
                "我了解",
                "I understand",
                "确认",
                "Confirm",
                "继续",
                "Continue",
                "続行",
                "계속",
                "Continuar",
                "Bestätigen",
            ],
        ):
            return True

        return False

    async def _handle_labs_onboarding(self, page, body_text: str) -> bool:
        text = str(body_text or "")
        if await page.locator("#marketing-emails").count() > 0 or await page.locator("#research-emails").count() > 0:
            for selector in ("#marketing-emails", "#research-emails"):
                try:
                    checkbox = page.locator(selector).first
                    if await checkbox.count() <= 0 or not await checkbox.is_visible():
                        continue
                    checked = str(await checkbox.get_attribute("aria-checked") or "").strip().lower() == "true"
                    if not checked:
                        await checkbox.click(timeout=5000)
                        await asyncio.sleep(0.3)
                except Exception:
                    continue
            if await self._click_button_by_text(page, ["下一步", "Next", "继续", "Continue", "次へ", "다음", "Siguiente", "Suivant"]):
                return True

        onboarding_markers = [
            "体验 AI 工具的创造力",
            "Experience the creativity",
            "查看我们的《隐私权政策》",
            "隐私权政策",
            "Privacy Policy",
            "Your data and labs.google/fx",
            "Welcome to",
            "Get started",
            "Start using",
            "Introducing",
        ]
        if any(marker.lower() in text.lower() for marker in onboarding_markers):
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            await asyncio.sleep(0.5)
            if await self._click_button_by_text(
                page,
                ["继续", "Continue", "同意", "Agree", "下一步", "Next", "Got it", "Done", "Skip", "Accept", "Start", "OK", "次へ", "同意する", "다음", "동의", "Aceptar", "Accepter", "Akzeptieren", "Начать"],
            ):
                return True

        return False

    async def _is_labs_session_ready(self, page, body_text: str) -> bool:
        url = str(page.url or "").lower()
        if "labs.google" not in url:
            return False
        if "accounts.google.com" in url:
            return False

        text = str(body_text or "")
        blocked_markers = [
            "体验 AI 工具的创造力",
            "Experience the creativity",
            "查看我们的《隐私权政策》",
            "Privacy Policy",
            "登录 Chrome",
            "Sign in to Chromium",
            "Set up a work profile",
            "Use Chromium without an account",
            "Continue to work in this profile",
            "How do you want to handle your existing browsing data",
            "Turn on sync",
            "Save and continue",
            "Sign in with Google",
            "Too many failed attempts",
        ]
        if any(marker.lower() in text.lower() for marker in blocked_markers):
            return False

        try:
            if await page.locator(", ".join(ACCOUNT_INPUT_SELECTORS + PASSWORD_INPUT_SELECTORS)).count() > 0:
                return False
        except Exception:
            return False

        return True

    async def _settle_labs_session(self, profile: Dict[str, Any], context: BrowserContext, page) -> Optional[str]:
        native_prompt_attempts = 0
        for _ in range(40):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass

            body_text = await self._safe_page_text(page)

            if native_prompt_attempts < 3 and not str(body_text or "").strip() and await self._handle_native_chrome_profile_prompts():
                native_prompt_attempts += 1
                logger.info(f"[{profile['name']}] 已处理 Chromium 原生资料提示")
                continue

            if await self._handle_managed_account_prompts(page, body_text):
                logger.info(f"[{profile['name']}] 已处理 Google / 资料确认提示")
                continue

            if await self._handle_labs_onboarding(page, body_text):
                logger.info(f"[{profile['name']}] 已处理 labs 首次引导")
                continue

            if await self._is_labs_session_ready(page, body_text):
                logger.info(f"[{profile['name']}] labs 会话页面已就绪")
                break

            await asyncio.sleep(1.0)

        token = await self._get_session_cookie(context)
        deadline = asyncio.get_running_loop().time() + 12.0
        while asyncio.get_running_loop().time() < deadline:
            token = await self._get_session_cookie(context)
            if token:
                break
            await asyncio.sleep(0.5)

        if not token:
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            token = await self._get_session_cookie(context)

        if token:
            for _ in range(4):
                body_text = await self._safe_page_text(page)
                if await self._handle_managed_account_prompts(page, body_text):
                    continue
                if await self._handle_labs_onboarding(page, body_text):
                    continue
                break
        return token

    async def _persist_login_state(
        self,
        profile_id: int,
        token: Optional[str],
        email: Optional[str] = None,
        is_logged_in: Optional[bool] = None,
    ) -> None:
        logged_in = bool(token) if is_logged_in is None else bool(is_logged_in)
        update_data: Dict[str, Any] = {"is_logged_in": 1 if logged_in else 0}
        if token:
            update_data["last_token"] = self._mask_token(token)
            update_data["last_token_time"] = datetime.now().isoformat()
            update_data["login_method"] = "browser"
        normalized_email = self._normalize_email(email or "")
        if normalized_email:
            update_data["email"] = normalized_email
        await profile_db.update_profile(profile_id, **update_data)

    async def _save_google_cookies_from_context(
        self,
        profile_id: int,
        context: BrowserContext,
    ) -> None:
        """从浏览器上下文提取 Google cookies 并存储，用于后续协议刷新"""
        try:
            all_google_cookies = []
            for domain in [".google.com", "accounts.google.com"]:
                try:
                    cookies = await context.cookies(f"https://{domain}")
                    all_google_cookies.extend(cookies)
                except Exception:
                    pass

            if not all_google_cookies:
                return

            # 转为 JSON 存储
            google_cookies_json = json.dumps(all_google_cookies)
            await profile_db.update_profile(profile_id, google_cookies=google_cookies_json)
            logger.info(f"[Profile {profile_id}] 已从浏览器提取 {len(all_google_cookies)} 个 Google cookies 用于协议刷新")
        except Exception as e:
            logger.warning(f"[Profile {profile_id}] 提取 Google cookies 失败: {e}")

    def _parse_cookies_payload(self, cookies_json: str) -> List[Dict[str, Any]]:
        data = json.loads(cookies_json)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            cookies = data.get("cookies")
            if isinstance(cookies, list):
                return cookies
        return []

    def _to_playwright_cookies(self, cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in cookies:
            if not isinstance(c, dict):
                continue

            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue

            domain = c.get("domain") or c.get("host")
            url = c.get("url")
            path = c.get("path") or "/"

            if isinstance(domain, str) and "://" in domain:
                domain = None

            cookie: Dict[str, Any] = {"name": str(name), "value": str(value)}

            if c.get("httpOnly") is not None:
                cookie["httpOnly"] = bool(c.get("httpOnly"))
            if c.get("secure") is not None:
                cookie["secure"] = bool(c.get("secure"))

            expires = c.get("expires")
            if expires is None:
                expires = c.get("expirationDate") or c.get("expiry")
            if expires is not None:
                try:
                    cookie["expires"] = float(expires)
                except (TypeError, ValueError):
                    pass

            same_site = c.get("sameSite")
            if isinstance(same_site, str):
                m = same_site.strip().lower()
                if m in {"lax"}:
                    cookie["sameSite"] = "Lax"
                elif m in {"strict"}:
                    cookie["sameSite"] = "Strict"
                elif m in {"none", "no_restriction"}:
                    cookie["sameSite"] = "None"

            if isinstance(url, str) and url.startswith("http"):
                cookie["url"] = url
            elif isinstance(domain, str) and domain:
                cookie["domain"] = domain
                cookie["path"] = str(path)
            else:
                continue

            out.append(cookie)
        return out

    async def _get_session_cookie(self, context: BrowserContext) -> Optional[str]:
        try:
            cookies = await context.cookies("https://labs.google")
        except Exception:
            cookies = await context.cookies()

        for cookie in cookies:
            if cookie.get("name") == config.session_cookie_name:
                return cookie.get("value")
        return None

    async def import_cookies(self, profile_id: int, cookies_json: str) -> Dict[str, Any]:
        """导入 Cookie（JSON），写入到持久化 profile 中"""
        if len(cookies_json) > 300_000:
            return {"success": False, "error": "Cookie 内容过大（建议只导出 labs.google 域名的 Cookie）"}

        async with self._lock:
            profile = await profile_db.get_profile(profile_id)
            if not profile:
                return {"success": False, "error": "Profile 不存在"}

            try:
                raw = self._parse_cookies_payload(cookies_json)
            except Exception as e:
                return {"success": False, "error": f"Cookie JSON 解析失败: {e}"}

            if not raw:
                return {"success": False, "error": "未识别到 Cookie 列表（请粘贴 JSON 数组或包含 cookies 字段的对象）"}

            cookies = self._to_playwright_cookies(raw)
            if not cookies:
                return {"success": False, "error": "Cookie 列表为空或格式不支持（至少需要 name/value/domain+path 或 url）"}

            context = None
            try:
                if not self._playwright:
                    await self.start()

                profile_dir = self._get_profile_dir(profile_id)
                os.makedirs(profile_dir, exist_ok=True)
                self._clean_locks(profile_dir)
                proxy = await self._get_proxy(profile)

                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,
                    viewport={"width": 1024, "height": 768},
                    locale="en-US",
                    timezone_id="America/New_York",
                    proxy=proxy,
                    args=BROWSER_ARGS,
                    ignore_default_args=["--enable-automation"],
                )

                await context.add_cookies(cookies)

                # 导入后访问 labs 页面刷新 session
                token = await self._extract_from_context(profile, context)

                return {
                    "success": True,
                    "imported": len(cookies),
                    "raw_count": len(raw),
                    "has_token": bool(token),
                }

            except Exception as e:
                logger.error(f"[{profile['name']}] Cookie 导入失败: {e}")
                return {"success": False, "error": str(e)}
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass

    async def export_cookies(self, profile_id: int) -> Dict[str, Any]:
        """导出 labs.google 域名 Cookie，格式与导入接口兼容。"""
        async with self._lock:
            profile = await profile_db.get_profile(profile_id)
            if not profile:
                return {"success": False, "error": "Profile 不存在"}

            context = None
            try:
                if self._active_profile_id == profile_id and self._active_context:
                    cookies = await self._active_context.cookies("https://labs.google")
                else:
                    profile_dir = self._get_profile_dir(profile_id)
                    if not os.path.exists(profile_dir):
                        return {"success": False, "error": "无持久化数据，请先登录或导入会话数据"}

                    if not self._playwright:
                        await self.start()

                    self._clean_locks(profile_dir)
                    proxy = await self._get_proxy(profile)
                    context = await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=True,
                        viewport={"width": 1024, "height": 768},
                        locale="en-US",
                        timezone_id="America/New_York",
                        proxy=proxy,
                        args=BROWSER_ARGS,
                        ignore_default_args=["--enable-automation"],
                    )
                    cookies = await context.cookies("https://labs.google")

                if not cookies:
                    return {"success": False, "error": "当前账号暂无可导出的 Cookie"}

                return {
                    "success": True,
                    "kind": "session",
                    "source": "active_context" if self._active_profile_id == profile_id and self._active_context else "browser_profile",
                    "profile_id": profile_id,
                    "profile_name": profile.get("name") or "",
                    "cookies": cookies,
                    "cookie_count": len(cookies),
                    "count": len(cookies),
                    "cookies_json": json.dumps(cookies, ensure_ascii=False, indent=2),
                    "has_token": any(c.get("name") == config.session_cookie_name for c in cookies),
                }

            except Exception as e:
                logger.error(f"[{profile['name']}] Cookie 导出失败: {e}")
                return {"success": False, "error": str(e)}
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass

    async def launch_for_login(self, profile_id: int) -> bool:
        """启动浏览器用于 VNC 登录（非 headless）"""
        if not config.enable_vnc:
            logger.warning("已禁用 VNC 登录（设置 ENABLE_VNC=1 可启用）")
            return False
        async with self._lock:
            await self._close_active()

            profile = await profile_db.get_profile(profile_id)
            if not profile:
                logger.error(f"Profile {profile_id} 不存在")
                return False

            try:
                if not self._playwright:
                    await self.start()

                ok = await self._ensure_vnc_stack()
                if not ok:
                    logger.error(f"[{profile['name']}] VNC 服务启动失败")
                    return False

                profile_dir = self._get_profile_dir(profile_id)
                os.makedirs(profile_dir, exist_ok=True)
                self._clean_locks(profile_dir)  # 清理锁文件
                proxy = await self._get_proxy(profile)

                # 非 headless，用于 VNC 登录
                self._active_context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=False,  # VNC 可见
                    viewport={"width": 1024, "height": 768},
                    locale="en-US",
                    timezone_id="America/New_York",
                    proxy=proxy,
                    args=LOGIN_BROWSER_ARGS,
                    ignore_default_args=["--enable-automation"],
                )
                self._active_profile_id = profile_id

                page = self._active_context.pages[0] if self._active_context.pages else await self._active_context.new_page()
                await page.goto(config.labs_url, wait_until="domcontentloaded")

                logger.info(f"[{profile['name']}] 浏览器已启动，请通过 VNC 登录")
                return True

            except Exception as e:
                logger.error(f"[{profile['name']}] 启动失败: {e}")
                return False

    async def close_browser(self, profile_id: int) -> Dict[str, Any]:
        """关闭浏览器并保存状态"""
        async with self._lock:
            if self._active_profile_id != profile_id:
                return {"success": False, "error": "该 Profile 浏览器未运行"}

            if self._active_context:
                # 检查登录状态
                is_logged_in = False
                profile = await profile_db.get_profile(profile_id)
                try:
                    cookies = await self._active_context.cookies("https://labs.google")
                    is_logged_in = any(c["name"] == config.session_cookie_name for c in cookies)
                except Exception:
                    pass

                await self._persist_login_state(
                    profile_id,
                    None,
                    email=self._resolve_known_email(profile or {}),
                    is_logged_in=is_logged_in,
                )
                if is_logged_in:
                    await self._save_google_cookies_from_context(profile_id, self._active_context)
                await self._close_active()
                await self._stop_vnc_stack()

                status = "已登录" if is_logged_in else "未登录"
                logger.info(f"Profile {profile_id} 浏览器已关闭，状态: {status}")
                return {"success": True, "is_logged_in": is_logged_in}

            return {"success": True}

    async def extract_token(self, profile_id: int) -> Optional[str]:
        """提取 Token（Headless 模式，使用持久化上下文）"""
        async with self._lock:
            profile = await profile_db.get_profile(profile_id)
            if not profile:
                return None

            profile_dir = self._get_profile_dir(profile_id)

            # 检查是否有持久化数据
            if not os.path.exists(profile_dir):
                logger.warning(f"[{profile['name']}] 无持久化数据，请先登录")
                return None

            # 如果当前 profile 浏览器正在运行（VNC 登录中），直接提取
            if self._active_profile_id == profile_id and self._active_context:
                return await self._extract_from_context(profile, self._active_context)

            # 否则用 headless 模式启动
            context = None
            try:
                if not self._playwright:
                    await self.start()

                profile_dir = self._get_profile_dir(profile_id)
                self._clean_locks(profile_dir)  # 清理锁文件
                proxy = await self._get_proxy(profile)

                logger.info(f"[{profile['name']}] Headless 模式提取 Token...")

                # Headless + 持久化上下文
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,  # Headless 省资源
                    viewport={"width": 1024, "height": 768},
                    locale="en-US",
                    timezone_id="America/New_York",
                    proxy=proxy,
                    args=BROWSER_ARGS,  # 完整内存优化参数
                    ignore_default_args=["--enable-automation"],
                )

                token = await self._extract_from_context(profile, context)
                return token

            except Exception as e:
                logger.error(f"[{profile['name']}] 提取失败: {e}")
                return None
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                    logger.info(f"[{profile['name']}] Headless 浏览器已关闭")

    async def _extract_from_context(self, profile: Dict[str, Any], context: BrowserContext) -> Optional[str]:
        """从上下文提取 Token（通过 signin 页面刷新 session）"""
        page = None
        try:
            page = await context.new_page()
            await self._install_page_route(page)

            # 访问 labs 页面，必要时自动推进 Google / 托管资料 / labs 首次引导。
            logger.info(f"[{profile['name']}] 访问 {config.labs_url} 刷新 session...")
            await page.goto(config.labs_url, wait_until="domcontentloaded", timeout=60000)

            token = await self._settle_labs_session(profile, context, page)
            body_text = await self._safe_page_text(page)

            await self._persist_login_state(
                profile["id"],
                token,
                email=self._resolve_known_email(profile, body_text),
            )
            if token:
                logger.info(f"[{profile['name']}] Token 提取成功")
                await self._save_google_cookies_from_context(profile["id"], context)
            else:
                logger.warning(f"[{profile['name']}] 未找到 Token，会话可能已过期")

            return token

        except Exception as e:
            logger.error(f"[{profile['name']}] 提取异常: {e}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def auto_login(self, profile_id: int) -> Dict[str, Any]:
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        login_account = str(profile.get("login_account") or "").strip()
        login_password = str(profile.get("login_password") or "").strip()
        if not login_account or not login_password:
            return {"success": False, "error": "请先为该账号配置登录账号和登录密码"}

        async with self._lock:
            await self._close_active()

            context = None
            page = None
            use_vnc = False
            try:
                if not self._playwright:
                    await self.start()

                profile_dir = self._get_profile_dir(profile_id)
                os.makedirs(profile_dir, exist_ok=True)
                self._clean_locks(profile_dir)
                proxy = await self._get_proxy(profile)

                if config.enable_vnc:
                    use_vnc = await self._ensure_vnc_stack()

                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=not use_vnc,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                    timezone_id="America/New_York",
                    proxy=proxy,
                    args=LOGIN_BROWSER_ARGS if use_vnc else BROWSER_ARGS,
                    ignore_default_args=["--enable-automation"],
                )

                page = context.pages[0] if context.pages else await context.new_page()
                await self._install_page_route(page)
                await page.goto(config.login_url, wait_until="domcontentloaded", timeout=60000)

                for _ in range(45):
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=3000)
                    except Exception:
                        pass

                    body_text = await self._safe_page_text(page)
                    blocker = self._detect_login_blocker(body_text)
                    if blocker:
                        await self._persist_login_state(
                            profile_id,
                            None,
                            email=self._resolve_known_email(profile, body_text),
                        )
                        return {"success": False, "error": blocker, "requires_manual_action": True}

                    if use_vnc and not str(body_text or "").strip() and await self._handle_native_chrome_profile_prompts():
                        continue

                    if await self._handle_managed_account_prompts(page, body_text):
                        continue

                    if await self._advance_google_login(page, login_account, login_password):
                        continue

                    if await self._handle_labs_onboarding(page, body_text):
                        continue

                    if await self._is_labs_session_ready(page, body_text):
                        break

                    await asyncio.sleep(1.0)

                token = await self._settle_labs_session(profile, context, page)
                body_text = await self._safe_page_text(page)
                await self._persist_login_state(
                    profile_id,
                    token,
                    email=self._resolve_known_email(profile, body_text),
                )
                if not token:
                    return {"success": False, "error": "未获取到会话令牌，请改用手动登录"}

                await self._save_google_cookies_from_context(profile_id, context)

                return {
                    "success": True,
                    "is_logged_in": True,
                    "has_token": True,
                    "profile_name": profile["name"],
                }

            except Exception as e:
                logger.error(f"[{profile['name']}] 自动登录失败: {e}")
                return {"success": False, "error": str(e)}
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if use_vnc:
                    await self._stop_vnc_stack()

    async def check_login_status(self, profile_id: int) -> Dict[str, Any]:
        """检查登录状态"""
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        token = await self.peek_token(profile_id)
        await self._persist_login_state(
            profile_id,
            token,
            email=self._resolve_known_email(profile),
        )
        return {
            "success": True,
            "is_logged_in": token is not None,
            "profile_name": profile["name"]
        }

    async def peek_token(self, profile_id: int) -> Optional[str]:
        """轻量获取 token（不访问页面，仅读取 cookie）"""
        async with self._lock:
            profile = await profile_db.get_profile(profile_id)
            if not profile:
                return None

            profile_dir = self._get_profile_dir(profile_id)
            if not os.path.exists(profile_dir):
                return None

            if self._active_profile_id == profile_id and self._active_context:
                return await self._get_session_cookie(self._active_context)

            context = None
            try:
                if not self._playwright:
                    await self.start()

                self._clean_locks(profile_dir)
                proxy = await self._get_proxy(profile)
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,
                    viewport={"width": 1024, "height": 768},
                    locale="en-US",
                    timezone_id="America/New_York",
                    proxy=proxy,
                    args=BROWSER_ARGS,
                    ignore_default_args=["--enable-automation"],
                )
                return await self._get_session_cookie(context)
            except Exception:
                return None
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass

    async def delete_profile_data(self, profile_id: int):
        """删除 profile 数据"""
        profile_dir = self._get_profile_dir(profile_id)
        if os.path.exists(profile_dir):
            shutil.rmtree(profile_dir)
            logger.info(f"已删除: {profile_dir}")

    def get_active_profile_id(self) -> Optional[int]:
        return self._active_profile_id

    def get_status(self) -> Dict[str, Any]:
        status = self._get_supervisor_status()
        vnc_stack_running = all(status.get(p) == "RUNNING" for p in ("xvfb", "x11vnc", "novnc")) if status else False
        return {
            "is_running": self._playwright is not None,
            "active_profile_id": self._active_profile_id,
            "has_active_browser": self._active_context is not None,
            "profiles_dir": config.profiles_dir,
            "enable_vnc": bool(config.enable_vnc),
            "vnc_stack_running": bool(vnc_stack_running),
        }


browser_manager = BrowserManager()
