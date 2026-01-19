"""Token Updater 主入口 v3.0 - 轻量版"""
import asyncio
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from .api import app
from .browser import browser_manager
from .updater import token_syncer
from .database import profile_db
from .config import config
from .logger import logger


scheduler = AsyncIOScheduler()
SYNC_JOB_ID = "token_sync"


async def scheduled_sync():
    """定时同步任务"""
    logger.info("=== 定时同步任务触发 ===")

    if not config.connection_token:
        logger.warning("未配置 CONNECTION_TOKEN，跳过本次同步")
        return

    profiles = await profile_db.get_logged_in_profiles()
    if not profiles:
        logger.warning("没有已登录的 Profile，跳过本次同步")
        return

    await token_syncer.sync_all_profiles()


async def startup():
    """启动时初始化"""
    logger.info("=" * 60)
    logger.info("Flow2API Token Updater v3.0 - 轻量版")
    logger.info("Cookie 导入模式")
    logger.info("=" * 60)

    await profile_db.init()
    logger.info("数据库初始化完成")

    await browser_manager.start()

    scheduler.add_job(
        scheduled_sync,
        trigger=IntervalTrigger(minutes=config.refresh_interval),
        id=SYNC_JOB_ID,
        replace_existing=True
    )
    scheduler.start()

    app.state.scheduler = scheduler
    app.state.sync_job_id = SYNC_JOB_ID

    logger.info(f"定时任务已启动: 每 {config.refresh_interval} 分钟执行一次")
    logger.info(f"Flow2API URL: {config.flow2api_url}")
    logger.info(f"API 端口: {config.api_port}")
    logger.info("")
    logger.info("管理界面: http://localhost:8002")
    logger.info("")


async def shutdown():
    """关闭时清理"""
    logger.info("正在关闭...")
    if scheduler.running:
        scheduler.shutdown()
    await browser_manager.stop()


@app.on_event("startup")
async def on_startup():
    await startup()


@app.on_event("shutdown")
async def on_shutdown():
    await shutdown()


def main():
    """主函数"""
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=config.api_port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
