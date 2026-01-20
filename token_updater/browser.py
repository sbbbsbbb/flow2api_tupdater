"""浏览器管理 v3.1 - 持久化上下文 + VNC登录 + Headless刷新"""
import asyncio
import os
import shutil
from datetime import datetime
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, BrowserContext, Playwright
from .config import config
from .database import profile_db
from .proxy_utils import parse_proxy, format_proxy_for_playwright
from .logger import logger


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
    "--no-first-run",
    "--no-default-browser-check",
    "--single-process",  # 单进程模式，省内存
    "--memory-pressure-off",
    "--max_old_space_size=128",  # 限制 V8 内存
    "--js-flags=--max-old-space-size=128",
]


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
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

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

    async def _get_proxy(self, profile: Dict[str, Any]) -> Optional[Dict]:
        """获取代理配置"""
        if profile.get("proxy_enabled") and profile.get("proxy_url"):
            proxy_config = parse_proxy(profile["proxy_url"])
            if proxy_config:
                proxy = format_proxy_for_playwright(proxy_config)
                logger.info(f"[{profile['name']}] 使用代理: {proxy['server']}")
                return proxy
        return None

    async def launch_for_login(self, profile_id: int) -> bool:
        """启动浏览器用于 VNC 登录（非 headless）"""
        async with self._lock:
            await self._close_active()

            profile = await profile_db.get_profile(profile_id)
            if not profile:
                logger.error(f"Profile {profile_id} 不存在")
                return False

            try:
                if not self._playwright:
                    await self.start()

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
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    proxy=proxy,
                    args=BROWSER_ARGS[:6],  # 登录时不用单进程模式
                    ignore_default_args=["--enable-automation"],
                )
                self._active_profile_id = profile_id

                page = self._active_context.pages[0] if self._active_context.pages else await self._active_context.new_page()
                await page.goto(config.login_url, wait_until="networkidle")

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
                try:
                    cookies = await self._active_context.cookies("https://labs.google")
                    is_logged_in = any(c["name"] == config.session_cookie_name for c in cookies)
                except Exception:
                    pass

                await profile_db.update_profile(profile_id, is_logged_in=int(is_logged_in))
                await self._close_active()

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
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
        """从上下文提取 Token"""
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            # 访问页面触发 session 刷新
            logger.info(f"[{profile['name']}] 访问 {config.labs_url} 刷新 session...")
            await page.goto(config.labs_url, wait_until="networkidle", timeout=60000)
            
            # 等待页面完全加载，确保 cookie 已更新
            await asyncio.sleep(3)
            
            # 再次等待网络空闲，确保所有认证请求完成
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # 提取 cookie
            cookies = await context.cookies("https://labs.google")
            token = None
            for cookie in cookies:
                if cookie["name"] == config.session_cookie_name:
                    token = cookie["value"]
                    break

            if token:
                await profile_db.update_profile(
                    profile["id"],
                    is_logged_in=1,
                    last_token=self._mask_token(token),
                    last_token_time=datetime.now().isoformat()
                )
                logger.info(f"[{profile['name']}] Token 提取成功")
            else:
                await profile_db.update_profile(profile["id"], is_logged_in=0)
                logger.warning(f"[{profile['name']}] 未找到 Token，会话可能已过期")

            return token

        except Exception as e:
            logger.error(f"[{profile['name']}] 提取异常: {e}")
            return None

    async def check_login_status(self, profile_id: int) -> Dict[str, Any]:
        """检查登录状态"""
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        token = await self.extract_token(profile_id)
        return {
            "success": True,
            "is_logged_in": token is not None,
            "profile_name": profile["name"]
        }

    async def delete_profile_data(self, profile_id: int):
        """删除 profile 数据"""
        profile_dir = self._get_profile_dir(profile_id)
        if os.path.exists(profile_dir):
            shutil.rmtree(profile_dir)
            logger.info(f"已删除: {profile_dir}")

    def get_active_profile_id(self) -> Optional[int]:
        return self._active_profile_id

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_running": self._playwright is not None,
            "active_profile_id": self._active_profile_id,
            "has_active_browser": self._active_context is not None,
            "profiles_dir": config.profiles_dir
        }


browser_manager = BrowserManager()
