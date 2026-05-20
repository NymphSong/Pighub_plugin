import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import List, Tuple, Type

import httpx

from src.manager.async_task_manager import AsyncTask, async_task_manager
from src.plugin_system import BasePlugin, register_plugin, ComponentInfo, ConfigField
from src.common.logger import get_logger
from .commands import TodayPigCommand, RefreshPigCommand

logger = get_logger("Pighub_plugin")


class DailyRestoreCardTask(AsyncTask):
    """每日 0 点恢复群名片任务"""

    def __init__(
        self,
        plugin_dir: str,
        napcat_host: str,
        napcat_port: int,
        napcat_token: str,
        admin_enable: bool = True,
        allowed_groups: List[str] = None,
    ):
        super().__init__(
            task_name="PighubDailyRestore",
            wait_before_start=self._seconds_until_midnight(),
            run_interval=86400,
        )
        self.plugin_dir = plugin_dir
        self.napcat_host = napcat_host
        self.napcat_port = napcat_port
        self.napcat_token = napcat_token
        self.admin_enable = admin_enable
        self.allowed_groups = set(allowed_groups or [])
        logger.info(f"[PighubDaily] 任务将在 {seconds} 秒后首次执行（下一个 0 点）")

    @staticmethod
    def _seconds_until_midnight() -> int:
        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        return int((midnight - now).total_seconds())

    async def run(self):
        cache_path = os.path.join(self.plugin_dir, "user_pig_cache.json")
        if not os.path.exists(cache_path):
            return

        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception as e:
            logger.warning(f"[PighubDaily] 读取缓存失败: {e}")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        to_restore = []
        for key, val in list(cache.items()):
            if val.get("date") == today:
                group_id = key.split(":", 1)[0] if ":" in key else ""
                # 管理员功能关闭 或 不在允许群列表中则跳过
                if not self.admin_enable:
                    del cache[key]
                    continue
                if self.allowed_groups and group_id not in self.allowed_groups:
                    del cache[key]
                    continue
                to_restore.append((key, val.get("original_card", "")))
                del cache[key]

        if not to_restore:
            logger.info("[PighubDaily] 今日无需要恢复的名片")
            return

        logger.info(f"[PighubDaily] 开始恢复 {len(to_restore)} 个用户的名片")
        url = f"http://{self.napcat_host}:{self.napcat_port}/set_group_card"
        headers = {"Content-Type": "application/json"}
        if self.napcat_token:
            headers["Authorization"] = f"Bearer {self.napcat_token}"

        semaphore = asyncio.Semaphore(5)

        async def _restore_one(key: str, card: str, client: httpx.AsyncClient):
            try:
                group_id, user_id = key.split(":", 1)
                resp = await client.post(
                    url,
                    json={
                        "group_id": int(group_id),
                        "user_id": int(user_id),
                        "card": card,
                    },
                    headers=headers,
                )
                data = resp.json()
                if data.get("status") == "ok" or data.get("retcode") == 0:
                    logger.info(f"[PighubDaily] 已恢复 {key} 的名片为: {card}")
                else:
                    logger.warning(f"[PighubDaily] 恢复 {key} 失败: {data}")
            except Exception as e:
                logger.error(f"[PighubDaily] 恢复 {key} 异常: {e}")

        async def _restore_with_limit(key: str, card: str, client: httpx.AsyncClient):
            async with semaphore:
                return await _restore_one(key, card, client)

        async with httpx.AsyncClient(timeout=10.0) as client:
            await asyncio.gather(
                *[_restore_with_limit(k, c, client) for k, c in to_restore]
            )

        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            logger.info("[PighubDaily] 缓存已清理")
        except Exception as e:
            logger.warning(f"[PighubDaily] 保存缓存失败: {e}")


@register_plugin
class PighubPlugin(BasePlugin):
    plugin_name = "Pighub_plugin"
    plugin_version = "1.0.0"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_schema: dict = {
        "general": {
            "enable": ConfigField(type=bool, default=True, description="是否启用 Pighub 插件"),
        },
        "napcat": {
            "enable": ConfigField(type=bool, default=True, description="是否使用 Napcat HTTP API 发送消息"),
            "napcat_host": ConfigField(type=str, default="127.0.0.1", description="NapCat HTTP 服务器地址"),
            "napcat_port": ConfigField(type=int, default=3000, description="NapCat HTTP 服务器端口"),
            "napcat_token": ConfigField(type=str, default="", description="NapCat Access Token"),
        },
        "mock": {
            "enable": ConfigField(type=bool, default=True, description="是否将命令回复添加进数据库（影响麦麦上下文）"),
        },
        "admin": {
            "enable": ConfigField(type=bool, default=True, description="是否启用自动更改群名片的管理员功能"),
            "allowed_groups": ConfigField(type=list, default=[], description="允许使用管理员功能的群号列表，为空则不限制"),
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        host = self.get_config("napcat.napcat_host", "127.0.0.1")
        port = self.get_config("napcat.napcat_port", 3000)
        token = self.get_config("napcat.napcat_token", "")
        admin_enable = self.get_config("admin.enable", True)
        allowed_groups = self.get_config("admin.allowed_groups", [])
        asyncio.create_task(
            self._start_daily_restore(plugin_dir, host, port, token, admin_enable, allowed_groups)
        )

    async def _start_daily_restore(
        self, plugin_dir: str, host: str, port: int, token: str,
        admin_enable: bool, allowed_groups: List[str],
    ):
        await asyncio.sleep(10)
        task = DailyRestoreCardTask(plugin_dir, host, port, token, admin_enable, allowed_groups)
        await async_task_manager.add_task(task)

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (TodayPigCommand.get_command_info(), TodayPigCommand),
            (RefreshPigCommand.get_command_info(), RefreshPigCommand),
        ]
