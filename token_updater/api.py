"""Token Updater API v3.0 - 轻量版"""
import secrets
import time
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Depends, Header, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
from .browser import browser_manager
from .updater import token_syncer
from .database import profile_db
from .proxy_utils import validate_proxy_format
from .config import config
from .logger import logger
import json

app = FastAPI(title="Flow2API Token Updater", version="3.0.0")

cors_origins = config.cors_origins
cors_allow_credentials = config.cors_allow_credentials
if "*" in cors_origins and cors_allow_credentials:
    cors_allow_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins if cors_origins else ["*"],
    allow_credentials=cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_sessions: dict[str, float] = {}

MAX_PROFILE_NAME_LEN = 64
MAX_REMARK_LEN = 200
MAX_PROXY_LEN = 512


def _session_ttl_seconds() -> int:
    ttl_minutes = config.session_ttl_minutes
    if ttl_minutes <= 0:
        return 0
    return max(60, ttl_minutes * 60)


def _prune_sessions(now: float | None = None) -> None:
    if now is None:
        now = time.time()
    expired = [token for token, exp in active_sessions.items() if exp and exp <= now]
    for token in expired:
        active_sessions.pop(token, None)


def _validate_profile_name(name: str) -> str:
    clean = name.strip()
    if not clean:
        raise HTTPException(status_code=400, detail="名称不能为空")
    if len(clean) > MAX_PROFILE_NAME_LEN:
        raise HTTPException(status_code=400, detail="名称过长")
    return clean


def _validate_remark(remark: str) -> str:
    clean = remark.strip()
    if len(clean) > MAX_REMARK_LEN:
        raise HTTPException(status_code=400, detail="备注过长")
    return clean


def _validate_proxy(proxy_url: str) -> str:
    clean = proxy_url.strip()
    if not clean:
        return ""
    if len(clean) > MAX_PROXY_LEN:
        raise HTTPException(status_code=400, detail="代理地址过长")
    valid, msg = validate_proxy_format(clean)
    if not valid:
        raise HTTPException(status_code=400, detail=f"代理格式错误: {msg}")
    return clean


# ========== Models ==========

class LoginRequest(BaseModel):
    password: str

class CreateProfileRequest(BaseModel):
    name: str
    remark: Optional[str] = ""
    proxy_url: Optional[str] = ""

class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    remark: Optional[str] = None
    is_active: Optional[bool] = None
    proxy_url: Optional[str] = None
    proxy_enabled: Optional[bool] = None

class UpdateConfigRequest(BaseModel):
    flow2api_url: Optional[str] = None
    connection_token: Optional[str] = None
    refresh_interval: Optional[int] = None

class ImportCookiesRequest(BaseModel):
    cookies: List[dict]


# ========== Auth ==========

async def verify_session(authorization: str = Header(None)):
    """验证 Web UI session"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization[7:]
    now = time.time()
    _prune_sessions(now)
    expiry = active_sessions.get(token)
    if expiry is None:
        raise HTTPException(status_code=401, detail="登录已过期")
    if expiry and expiry <= now:
        active_sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="登录已过期")
    return token


async def verify_api_key(x_api_key: str = Header(None)):
    """验证外部 API Key"""
    if not config.api_key:
        raise HTTPException(status_code=500, detail="未配置 API_KEY")
    if not x_api_key or not secrets.compare_digest(x_api_key, config.api_key):
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return x_api_key


@app.post("/api/login")
async def login(request: LoginRequest):
    if not config.admin_password:
        raise HTTPException(status_code=500, detail="未设置 ADMIN_PASSWORD")
    if not secrets.compare_digest(request.password, config.admin_password):
        raise HTTPException(status_code=401, detail="密码错误")

    session_token = secrets.token_urlsafe(32)
    ttl_seconds = _session_ttl_seconds()
    expiry = time.time() + ttl_seconds if ttl_seconds else 0
    active_sessions[session_token] = expiry
    return {"success": True, "token": session_token}


@app.post("/api/logout")
async def logout(token: str = Depends(verify_session)):
    active_sessions.pop(token, None)
    return {"success": True}


@app.get("/api/auth/check")
async def check_auth():
    return {"need_password": bool(config.admin_password)}


# ========== Static ==========

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse("/app/token_updater/static/index.html")


# ========== Status ==========

@app.get("/api/status")
async def get_status(token: str = Depends(verify_session)):
    profiles = await profile_db.get_all_profiles()
    return {
        "browser": browser_manager.get_status(),
        "syncer": token_syncer.get_status(),
        "profiles": {
            "total": len(profiles),
            "logged_in": sum(1 for p in profiles if p.get("is_logged_in")),
            "active": sum(1 for p in profiles if p.get("is_active"))
        },
        "config": {
            "flow2api_url": config.flow2api_url,
            "refresh_interval": config.refresh_interval,
            "has_connection_token": bool(config.connection_token),
            "has_api_key": bool(config.api_key)
        },
        "version": "3.0.0",
        "mode": "cookie-import"
    }


# ========== Profiles ==========

@app.get("/api/profiles")
async def get_profiles(token: str = Depends(verify_session)):
    profiles = await profile_db.get_all_profiles()
    for p in profiles:
        if p.get("proxy_url"):
            valid, msg = validate_proxy_format(p["proxy_url"])
            p["proxy_status"] = msg
            p["proxy_valid"] = bool(valid)
    return profiles


@app.post("/api/profiles")
async def create_profile(request: CreateProfileRequest, token: str = Depends(verify_session)):
    name = _validate_profile_name(request.name)
    remark = _validate_remark(request.remark or "")
    proxy_url = _validate_proxy(request.proxy_url or "")

    if await profile_db.get_profile_by_name(name):
        raise HTTPException(status_code=400, detail="名称已存在")

    profile_id = await profile_db.add_profile(name, remark, proxy_url)
    return {"success": True, "profile_id": profile_id}


@app.get("/api/profiles/{profile_id}")
async def get_profile(profile_id: int, token: str = Depends(verify_session)):
    profile = await profile_db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="不存在")
    return profile


@app.put("/api/profiles/{profile_id}")
async def update_profile(profile_id: int, request: UpdateProfileRequest, token: str = Depends(verify_session)):
    profile = await profile_db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="不存在")

    update_data = {}
    if request.name is not None:
        new_name = _validate_profile_name(request.name)
        existing = await profile_db.get_profile_by_name(new_name)
        if existing and existing.get("id") != profile_id:
            raise HTTPException(status_code=400, detail="名称已存在")
        update_data["name"] = new_name
    if request.remark is not None:
        update_data["remark"] = _validate_remark(request.remark)
    if request.is_active is not None:
        update_data["is_active"] = int(request.is_active)
    if request.proxy_url is not None:
        update_data["proxy_url"] = _validate_proxy(request.proxy_url)
    if request.proxy_enabled is not None:
        update_data["proxy_enabled"] = int(request.proxy_enabled)

    if update_data:
        await profile_db.update_profile(profile_id, **update_data)
    return {"success": True}


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: int, token: str = Depends(verify_session)):
    profile = await profile_db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="不存在")

    await browser_manager.delete_profile_data(profile_id)
    await profile_db.delete_profile(profile_id)
    return {"success": True}


@app.post("/api/profiles/{profile_id}/enable")
async def enable_profile(profile_id: int, token: str = Depends(verify_session)):
    await profile_db.update_profile(profile_id, is_active=1)
    return {"success": True}


@app.post("/api/profiles/{profile_id}/disable")
async def disable_profile(profile_id: int, token: str = Depends(verify_session)):
    await profile_db.update_profile(profile_id, is_active=0)
    return {"success": True}


# ========== Cookie 管理 ==========

@app.post("/api/profiles/{profile_id}/cookies")
async def import_cookies(profile_id: int, request: ImportCookiesRequest, token: str = Depends(verify_session)):
    """导入 Cookie (JSON 格式)"""
    result = await browser_manager.import_cookies(profile_id, request.cookies)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/profiles/{profile_id}/cookies/upload")
async def upload_cookies(profile_id: int, file: UploadFile = File(...), token: str = Depends(verify_session)):
    """上传 Cookie 文件"""
    try:
        content = await file.read()
        cookies = json.loads(content.decode("utf-8"))
        
        # 支持多种格式
        if isinstance(cookies, dict) and "cookies" in cookies:
            cookies = cookies["cookies"]
        if not isinstance(cookies, list):
            raise HTTPException(status_code=400, detail="Cookie 格式错误，需要数组格式")
            
        result = await browser_manager.import_cookies(profile_id, cookies)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON 解析失败")


@app.get("/api/profiles/{profile_id}/cookies")
async def export_cookies(profile_id: int, token: str = Depends(verify_session)):
    """导出 Cookie"""
    result = await browser_manager.export_cookies(profile_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/profiles/{profile_id}/cookies/verify")
async def verify_cookies(profile_id: int, token: str = Depends(verify_session)):
    """验证 Cookie 是否有效"""
    return await browser_manager.verify_cookies(profile_id)


# ========== Token ==========

@app.post("/api/profiles/{profile_id}/extract")
async def extract_token(profile_id: int, token: str = Depends(verify_session)):
    extracted = await browser_manager.extract_token(profile_id)
    if extracted:
        return {"success": True, "token_length": len(extracted)}
    return {"success": False, "message": "未找到 Token，请导入有效 Cookie"}


@app.post("/api/profiles/{profile_id}/sync")
async def sync_profile(profile_id: int, token: str = Depends(verify_session)):
    return await token_syncer.sync_profile(profile_id)


@app.post("/api/sync-all")
async def sync_all_profiles(token: str = Depends(verify_session)):
    return await token_syncer.sync_all_profiles()


# ========== Config ==========

@app.get("/api/config")
async def get_config(token: str = Depends(verify_session)):
    return {
        "flow2api_url": config.flow2api_url,
        "refresh_interval": config.refresh_interval,
        "has_connection_token": bool(config.connection_token),
        "connection_token_preview": f"{config.connection_token[:10]}..." if config.connection_token else "",
        "has_api_key": bool(config.api_key),
        "api_key_preview": f"{config.api_key[:10]}..." if config.api_key else ""
    }


@app.post("/api/config")
async def update_config(request: UpdateConfigRequest, api_request: Request, token: str = Depends(verify_session)):
    old_refresh_interval = config.refresh_interval
    updated = False

    if request.flow2api_url is not None:
        value = request.flow2api_url.strip()
        if not value:
            raise HTTPException(status_code=400, detail="Flow2API 地址不能为空")
        config.flow2api_url = value
        updated = True
    if request.connection_token is not None:
        config.connection_token = request.connection_token.strip()
        updated = True
    if request.refresh_interval is not None:
        if request.refresh_interval < 1 or request.refresh_interval > 1440:
            raise HTTPException(status_code=400, detail="刷新间隔需在 1-1440 分钟之间")
        config.refresh_interval = request.refresh_interval
        updated = True

    if updated:
        config.save()

    if request.refresh_interval is not None and config.refresh_interval != old_refresh_interval:
        scheduler = getattr(api_request.app.state, "scheduler", None)
        job_id = getattr(api_request.app.state, "sync_job_id", "token_sync")
        if scheduler:
            try:
                scheduler.reschedule_job(job_id, trigger=IntervalTrigger(minutes=config.refresh_interval))
            except Exception as exc:
                logger.warning(f"更新定时任务失败: {exc}")

    return {"success": True}


# ========== 外部 API ==========

@app.get("/v1/profiles")
async def ext_list_profiles(api_key: str = Depends(verify_api_key)):
    """外部 API: 列出所有 Profile"""
    profiles = await profile_db.get_all_profiles()
    return {
        "profiles": [
            {
                "id": p["id"],
                "name": p["name"],
                "email": p.get("email"),
                "is_logged_in": bool(p.get("is_logged_in")),
                "is_active": bool(p.get("is_active"))
            }
            for p in profiles
        ]
    }


@app.get("/v1/profiles/{profile_id}/token")
async def ext_get_token(profile_id: int, api_key: str = Depends(verify_api_key)):
    """外部 API: 获取 Token"""
    profile = await profile_db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    if not profile.get("is_active"):
        raise HTTPException(status_code=400, detail="Profile is disabled")

    token_value = await browser_manager.extract_token(profile_id)
    if not token_value:
        raise HTTPException(status_code=400, detail="Failed to extract token, please import cookies first")

    return {
        "success": True,
        "profile_id": profile_id,
        "profile_name": profile["name"],
        "email": profile.get("email"),
        "session_token": token_value
    }


@app.post("/v1/profiles/{profile_id}/sync")
async def ext_sync_profile(profile_id: int, api_key: str = Depends(verify_api_key)):
    """外部 API: 同步 Profile"""
    profile = await profile_db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return await token_syncer.sync_profile(profile_id)


@app.get("/v1/profiles/by-name/{name}/token")
async def ext_get_token_by_name(name: str, api_key: str = Depends(verify_api_key)):
    """外部 API: 通过名称获取 Token"""
    profile = await profile_db.get_profile_by_name(name)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return await ext_get_token(profile["id"], api_key)


# ========== Health ==========

@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.0.0"}
