"""Token sync service."""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .browser import browser_manager
from .config import config
from .database import profile_db
from .events import dashboard_events
from .execution import execution_gate
from .logger import logger


class TokenSyncer:
    """Token 同步器。"""

    def __init__(self):
        self._total_sync_count = 0
        self._total_error_count = 0
        self._last_batch_time: Optional[datetime] = None
        self._sync_lock = asyncio.Lock()

    def _normalize_email(self, email: Optional[str]) -> str:
        return (email or "").strip().lower()

    def _parse_time(self, value: Any) -> Optional[datetime]:
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _is_sync_overdue(self, profile: Dict[str, Any], now: Optional[datetime] = None) -> bool:
        """超过刷新间隔或从未同步过的 Profile，仍然需要兜底同步。"""
        last_sync_time = self._parse_time(profile.get("last_sync_time"))
        if not last_sync_time:
            return True

        current_time = now or datetime.now()
        interval_minutes = max(1, int(config.refresh_interval or 60))
        return current_time - last_sync_time >= timedelta(minutes=interval_minutes)

    def _should_sync_profile(
        self,
        profile: Dict[str, Any],
        token_lookup: Dict[str, Dict[str, Any]],
        now: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        email = self._normalize_email(profile.get("email"))
        if not email:
            return True, "未识别邮箱，无法精确检查上游状态"

        token_info = token_lookup.get(email)
        if not token_info:
            return True, "目标端不存在该 Token 记录"

        if not token_info.get("is_active", True):
            return True, "目标端 Token 已失活"

        if token_info.get("needs_refresh"):
            return True, "目标端判定需要刷新"

        if self._is_sync_overdue(profile, now=now):
            return True, f"距离上次同步已超过 {config.refresh_interval} 分钟"

        return False, "目标端 Token 状态正常"

    def _resolve_target(self, profile: Dict[str, Any]) -> Tuple[str, str]:
        """优先使用 Profile 级配置，没有则回退到全局默认值。"""
        flow2api_url = (profile.get("flow2api_url") or config.flow2api_url or "").strip().rstrip("/")
        connection_token = (
            profile.get("connection_token_override") or config.connection_token or ""
        ).strip()
        return flow2api_url, connection_token

    async def _record_sync_result(
        self,
        profile: Dict[str, Any],
        target_url: str,
        success: Optional[bool] = None,
        action: str = "",
        message: str = "",
        email: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        event_status = status or ("success" if success else "error")
        await profile_db.record_sync_event(
            profile_id=profile["id"],
            profile_name=profile["name"],
            email=email or profile.get("email"),
            target_url=target_url,
            status=event_status,
            action=action,
            message=message,
        )
        await dashboard_events.publish(
            "sync_result",
            {
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "status": event_status,
                "target_url": target_url,
                "action": action,
                "message": message,
                "email": email or profile.get("email"),
            },
        )

    async def _update_profile_check_result(
        self,
        profile_id: int,
        result: str,
        checked_at: Optional[str] = None,
        **extra_fields: Any,
    ) -> str:
        timestamp = checked_at or datetime.now().isoformat()
        await profile_db.update_profile(
            profile_id,
            last_check_time=timestamp,
            last_check_result=result,
            **extra_fields,
        )
        return timestamp

    async def _check_tokens_status(
        self,
        flow2api_url: str,
        connection_token: str,
        emails: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """从指定 Flow2API 查询 Token 状态。"""
        if not connection_token:
            return {"success": False, "error": "未配置 CONNECTION_TOKEN"}
        if not flow2api_url:
            return {"success": False, "error": "未配置 Flow2API 地址"}

        url = f"{flow2api_url}/api/plugin/check-tokens"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                payload = {}
                if emails:
                    payload["emails"] = emails

                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {connection_token}",
                    },
                )

                if response.status_code != 200:
                    return {"success": False, "error": f"HTTP {response.status_code}"}

                data = response.json()
                tokens = data.get("tokens", [])
                return {
                    "success": True,
                    "tokens": tokens,
                }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def sync_profile(self, profile_id: int, *, source: str = "manual") -> Dict[str, Any]:
        profile = await profile_db.get_profile(profile_id)
        profile_name = profile.get("name", "") if profile else ""
        async with self._sync_lock:
            async with execution_gate.hold(
                "sync_profile",
                profile_id=profile_id,
                profile_name=profile_name,
                source=source,
            ):
                return await self._sync_profile(profile_id)

    async def _sync_profile(self, profile_id: int) -> Dict[str, Any]:
        """同步单个 Profile。"""
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        flow2api_url, connection_token = self._resolve_target(profile)
        if not flow2api_url or not connection_token:
            error = "未配置完整的 Flow2API 地址或连接 Token"
            await self._update_profile_check_result(
                profile_id,
                f"failed: {error}",
                last_sync_time=datetime.now().isoformat(),
                last_sync_result=f"failed: {error}",
                error_count=profile.get("error_count", 0) + 1,
            )
            self._total_error_count += 1
            await self._record_sync_result(profile, flow2api_url, False, message=error)
            return {"success": False, "error": error, "target_url": flow2api_url}

        logger.info(f"[{profile['name']}] 开始同步 -> {flow2api_url}")

        # 优先协议刷新（有 google_cookies 就用）
        token = None
        google_cookies = profile.get("google_cookies")
        if google_cookies:
            from .protocol_login import protocol_loginer
            proxy_url = profile.get("proxy_url") if profile.get("proxy_enabled") else None
            logger.info(f"[{profile['name']}] 协议刷新...")
            login_result = await protocol_loginer.login(google_cookies, proxy=proxy_url, email=profile.get("email"))
            if login_result.get("success"):
                token = login_result["session_token"]
                logger.info(f"[{profile['name']}] 协议刷新成功")
            else:
                logger.warning(f"[{profile['name']}] 协议刷新失败: {login_result.get('error')}，回退浏览器")
                await profile_db.update_profile(profile_id, google_cookies=None)

        if not token:
            token = await browser_manager.extract_token(profile_id)
        if not token:
            error = "无法提取 Token，请先登录"
            await self._update_profile_check_result(
                profile_id,
                last_sync_time=datetime.now().isoformat(),
                result="failed: no token",
                last_sync_result="failed: no token",
                error_count=profile.get("error_count", 0) + 1,
            )
            self._total_error_count += 1
            await self._record_sync_result(profile, flow2api_url, False, message=error)
            return {"success": False, "error": error, "target_url": flow2api_url}

        logger.info(f"[{profile['name']}] 提取到 Token: {token[:20]}...{token[-10:]}")
        result = await self._push_to_flow2api(token, flow2api_url, connection_token)

        if result["success"]:
            success_result = f"success: {result.get('action', 'synced')}"
            await self._update_profile_check_result(
                profile_id,
                success_result,
                email=result.get("email", profile.get("email")),
                last_sync_time=datetime.now().isoformat(),
                last_sync_result=success_result,
                sync_count=profile.get("sync_count", 0) + 1,
            )
            self._total_sync_count += 1
            logger.info(f"[{profile['name']}] 同步成功")
            await self._record_sync_result(
                profile,
                flow2api_url,
                True,
                action=result.get("action", "synced"),
                message=result.get("message", ""),
                email=result.get("email"),
            )
        else:
            error_result = f"failed: {result.get('error', 'unknown')}"
            await self._update_profile_check_result(
                profile_id,
                error_result,
                last_sync_time=datetime.now().isoformat(),
                last_sync_result=error_result,
                error_count=profile.get("error_count", 0) + 1,
            )
            self._total_error_count += 1
            logger.error(f"[{profile['name']}] 同步失败: {result.get('error')}")
            await self._record_sync_result(
                profile,
                flow2api_url,
                False,
                message=result.get("error", "unknown"),
            )

        return {**result, "target_url": flow2api_url}

    async def sync_all_profiles(self, *, source: str = "manual") -> Dict[str, Any]:
        """同步所有活跃 Profile（智能模式：按目标地址分组刷新）。"""
        async with self._sync_lock:
            async with execution_gate.hold("sync_all", source=source):
                logger.info("=" * 40)
                logger.info("开始智能同步...")

                self._last_batch_time = datetime.now()
                profiles = await profile_db.get_active_profiles()

                if not profiles:
                    result = {"success": True, "total": 0, "synced": 0, "skipped": 0, "results": []}
                    await dashboard_events.publish("sync_batch", result)
                    return result

                grouped_profiles: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
                invalid_profiles: List[Dict[str, Any]] = []

                for profile in profiles:
                    flow2api_url, connection_token = self._resolve_target(profile)
                    if not flow2api_url or not connection_token:
                        invalid_profiles.append(profile)
                        continue
                    grouped_profiles[(flow2api_url, connection_token)].append(profile)

                results: List[Dict[str, Any]] = []
                success_count = 0
                error_count = 0
                skipped_count = 0
                now = datetime.now()

                for profile in invalid_profiles:
                    flow2api_url, _ = self._resolve_target(profile)
                    error = "未配置完整的 Flow2API 地址或连接 Token"
                    await self._update_profile_check_result(
                        profile["id"],
                        f"failed: {error}",
                        last_sync_time=datetime.now().isoformat(),
                        last_sync_result=f"failed: {error}",
                        error_count=profile.get("error_count", 0) + 1,
                    )
                    self._total_error_count += 1
                    await self._record_sync_result(profile, flow2api_url, False, message=error)
                    results.append(
                        {
                            "profile_id": profile["id"],
                            "profile_name": profile["name"],
                            "success": False,
                            "error": error,
                            "target_url": flow2api_url,
                        }
                    )
                    error_count += 1

                for (flow2api_url, connection_token), target_profiles in grouped_profiles.items():
                    profile_emails = [profile["email"] for profile in target_profiles if profile.get("email")]
                    check_result = await self._check_tokens_status(
                        flow2api_url,
                        connection_token,
                        profile_emails or None,
                    )

                    if not check_result["success"]:
                        logger.warning(
                            f"[{flow2api_url}] 无法查询 token 状态: {check_result.get('error')}，回退到该目标全量同步"
                        )
                        group_result = await self._sync_profiles_force(target_profiles)
                        results.extend(group_result["results"])
                        success_count += group_result["success_count"]
                        error_count += group_result["error_count"]
                        continue

                    token_lookup = {
                        self._normalize_email(token.get("email")): token
                        for token in check_result.get("tokens", [])
                        if self._normalize_email(token.get("email"))
                    }

                    for profile in target_profiles:
                        should_sync, reason = self._should_sync_profile(profile, token_lookup, now=now)
                        if should_sync:
                            logger.info(f"[{profile['name']}] 满足同步条件: {reason}")
                            result = await self._sync_profile(profile["id"])
                            results.append(
                                {
                                    "profile_id": profile["id"],
                                    "profile_name": profile["name"],
                                    **result,
                                }
                            )
                            if result["success"]:
                                success_count += 1
                            else:
                                error_count += 1
                        else:
                            skipped_count += 1
                            logger.info(f"[{profile['name']}] {reason}，跳过")
                            await self._update_profile_check_result(
                                profile["id"],
                                f"skipped: {reason}",
                                checked_at=now.isoformat(),
                            )
                            await self._record_sync_result(
                                profile,
                                flow2api_url,
                                action="skipped",
                                message=reason,
                                status="skipped",
                            )

                logger.info(
                    f"智能同步完成: 成功 {success_count}, 失败 {error_count}, 跳过 {skipped_count}"
                )

                result = {
                    "success": True,
                    "total": len(profiles),
                    "synced": success_count + error_count,
                    "success_count": success_count,
                    "error_count": error_count,
                    "skipped": skipped_count,
                    "results": results,
                }
                await dashboard_events.publish("sync_batch", result)
                return result

    async def _sync_profiles_force(self, profiles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """强制同步指定 Profile 列表。"""
        results = []
        success_count = 0
        error_count = 0

        for profile in profiles:
            result = await self._sync_profile(profile["id"])
            results.append(
                {
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    **result,
                }
            )
            if result["success"]:
                success_count += 1
            else:
                error_count += 1

        return {
            "results": results,
            "success_count": success_count,
            "error_count": error_count,
        }

    async def _sync_all_profiles_force(self) -> Dict[str, Any]:
        """强制同步所有 Profile（不检查过期状态）。"""
        profiles = await profile_db.get_active_profiles()
        group_result = await self._sync_profiles_force(profiles)

        logger.info(
            f"强制同步完成: 成功 {group_result['success_count']}, 失败 {group_result['error_count']}"
        )

        return {
            "success": True,
            "total": len(profiles),
            "success_count": group_result["success_count"],
            "error_count": group_result["error_count"],
            "results": group_result["results"],
        }

    async def _push_to_flow2api(
        self,
        session_token: str,
        flow2api_url: str,
        connection_token: str,
    ) -> Dict[str, Any]:
        """推送到指定 Flow2API。"""
        if not connection_token:
            return {"success": False, "error": "未配置 CONNECTION_TOKEN"}
        if not flow2api_url:
            return {"success": False, "error": "未配置 Flow2API 地址"}

        url = f"{flow2api_url}/api/plugin/update-token"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    url,
                    json={"session_token": session_token},
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {connection_token}",
                    },
                )

                if response.status_code != 200:
                    return {"success": False, "error": f"HTTP {response.status_code}"}

                data = response.json()
                message = data.get("message", "")
                email = None
                if " for " in message:
                    email = message.split(" for ")[-1]

                return {
                    "success": True,
                    "action": data.get("action"),
                    "message": message,
                    "email": email,
                }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def get_status(self) -> Dict[str, Any]:
        return {
            "total_sync_count": self._total_sync_count,
            "total_error_count": self._total_error_count,
            "last_batch_time": self._last_batch_time.isoformat() if self._last_batch_time else None,
            "flow2api_url": config.flow2api_url,
            "has_connection_token": bool(config.connection_token),
            "refresh_interval_minutes": config.refresh_interval,
        }


token_syncer = TokenSyncer()
