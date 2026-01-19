"""浏览器管理 - 轻量版，Cookie 导入模式"""
import asyncio
import json
import os
import shutil
from datetime import datetime
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, BrowserContext, Playwright
from .config import config
from .database import profile_db
from .proxy_utils import parse_proxy, format_proxy_for_playwright
from .logger import logger


class BrowserManager:
    """浏览器管理器 - Headless 模式，Cookie 导入"""

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._lock = asyncio.Lock()

    async def start(self):
        """启动 Playwright"""
        if self._playwright:
            return
        logger.info("启动 Playwright...")
        self._playwright = await async_playwright().start()
        os.makedirs(config.profiles_dir, exist_ok=True)
        logger.info("Playwright 已启动 (Headless 模式)")

    async def stop(self):
        """停止 Playwright"""
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Playwright 已关闭")

    def _get_profile_dir(self, profile_id: int) -> str:
        """获取 Profile 目录"""
        base_dir = os.path.abspath(config.profiles_dir)
        return os.path.join(base_dir, f"profile_{profile_id}")

    def _get_cookie_file(self, profile_id: int) -> str:
        """获取 Cookie 文件路径"""
        return os.path.join(self._get_profile_dir(profile_id), "cookies.json")

    async def import_cookies(self, profile_id: int, cookies: List[Dict]) -> Dict[str, Any]:
        """导入 Cookie"""
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        profile_dir = self._get_profile_dir(profile_id)
        os.makedirs(profile_dir, exist_ok=True)
        cookie_file = self._get_cookie_file(profile_id)

        # 验证是否包含 session cookie
        session_cookie = None
        for cookie in cookies:
            if cookie.get("name") == config.session_cookie_name:
                session_cookie = cookie
                break

        if not session_cookie:
            return {"success": False, "error": f"Cookie 中未找到 {config.session_cookie_name}"}

        # 保存 cookies
        with open(cookie_file, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)

        # 更新 profile 状态
        await profile_db.update_profile(
            profile_id,
            is_logged_in=1,
            last_token=self._mask_token(session_cookie.get("value", "")),
            last_token_time=datetime.now().isoformat()
        )

        logger.info(f"[{profile['name']}] Cookie 导入成功")
        return {"success": True, "message": "Cookie 导入成功"}

    async def export_cookies(self, profile_id: int) -> Dict[str, Any]:
        """导出 Cookie"""
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        cookie_file = self._get_cookie_file(profile_id)
        if not os.path.exists(cookie_file):
            return {"success": False, "error": "无 Cookie 数据"}

        with open(cookie_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)

        return {"success": True, "cookies": cookies}

    def _mask_token(self, token: str) -> str:
        if not token or len(token) <= 8:
            return token or ""
        return f"{token[:4]}...{token[-4:]}"

    async def _launch_context(self, profile: Dict[str, Any]) -> BrowserContext:
        """启动 Headless 浏览器上下文"""
        if not self._playwright:
            await self.start()

        # 解析代理配置
        proxy = None
        if profile.get("proxy_enabled") and profile.get("proxy_url"):
            proxy_config = parse_proxy(profile["proxy_url"])
            if proxy_config:
                proxy = format_proxy_for_playwright(proxy_config)
                logger.info(f"[{profile['name']}] 使用代理: {proxy['server']}")

        # 启动 Headless 浏览器
        browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            timezone_id="America/New_York",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            proxy=proxy,
        )

        # 加载已保存的 cookies
        cookie_file = self._get_cookie_file(profile["id"])
        if os.path.exists(cookie_file):
            with open(cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            await context.add_cookies(cookies)
            logger.info(f"[{profile['name']}] 已加载 {len(cookies)} 个 Cookie")

        return context

    async def extract_token(self, profile_id: int) -> Optional[str]:
        """提取 Token"""
        async with self._lock:
            profile = await profile_db.get_profile(profile_id)
            if not profile:
                return None

            # 检查是否有 cookie 文件
            cookie_file = self._get_cookie_file(profile_id)
            if not os.path.exists(cookie_file):
                logger.warning(f"[{profile['name']}] 无 Cookie 文件，请先导入")
                return None

            context = None
            browser = None
            try:
                logger.info(f"[{profile['name']}] 启动浏览器提取 Token...")
                
                if not self._playwright:
                    await self.start()

                # 解析代理
                proxy = None
                if profile.get("proxy_enabled") and profile.get("proxy_url"):
                    proxy_config = parse_proxy(profile["proxy_url"])
                    if proxy_config:
                        proxy = format_proxy_for_playwright(proxy_config)

                browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox", 
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ]
                )

                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    locale="en-US",
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    proxy=proxy,
                )

                # 加载 cookies
                with open(cookie_file, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                await context.add_cookies(cookies)

                # 访问页面刷新 token
                page = await context.new_page()
                await page.goto(config.labs_url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2)

                # 提取新 cookies
                new_cookies = await context.cookies("https://labs.google")
                token = None
                for cookie in new_cookies:
                    if cookie["name"] == config.session_cookie_name:
                        token = cookie["value"]
                        break

                if token:
                    # 保存更新后的 cookies
                    all_cookies = await context.cookies()
                    with open(cookie_file, "w", encoding="utf-8") as f:
                        json.dump(all_cookies, f, ensure_ascii=False, indent=2)

                    await profile_db.update_profile(
                        profile_id,
                        is_logged_in=1,
                        last_token=self._mask_token(token),
                        last_token_time=datetime.now().isoformat()
                    )
                    logger.info(f"[{profile['name']}] Token 提取成功")
                else:
                    await profile_db.update_profile(profile_id, is_logged_in=0)
                    logger.warning(f"[{profile['name']}] Token 已失效，请重新导入 Cookie")

                return token

            except Exception as exc:
                logger.error(f"[{profile['name']}] 提取失败: {exc}")
                return None
            finally:
                if context:
                    await context.close()
                if browser:
                    await browser.close()

    async def verify_cookies(self, profile_id: int) -> Dict[str, Any]:
        """验证 Cookie 是否有效"""
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        token = await self.extract_token(profile_id)
        if token:
            return {"success": True, "valid": True, "message": "Cookie 有效"}
        else:
            return {"success": True, "valid": False, "message": "Cookie 已失效，请重新导入"}

    async def delete_profile_data(self, profile_id: int):
        """删除 profile 数据目录"""
        profile_dir = self._get_profile_dir(profile_id)
        if os.path.exists(profile_dir):
            shutil.rmtree(profile_dir)
            logger.info(f"已删除: {profile_dir}")

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_running": self._playwright is not None,
            "mode": "headless",
            "profiles_dir": config.profiles_dir
        }


browser_manager = BrowserManager()
