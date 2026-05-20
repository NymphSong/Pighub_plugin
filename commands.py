import os
import json
import base64
import random
import time
import uuid
from datetime import datetime
from typing import Tuple, Optional, List

import httpx
from maim_message import Seg

from src.plugin_system import BaseCommand
from src.common.logger import get_logger
from src.common.data_models.message_data_model import ReplyContentType
from src.config.config import global_config

logger = get_logger("Pighub_plugin")

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif')


class PigCommandBase(BaseCommand):
    """猪猪命令基类，包含公共方法"""

    def _get_plugin_dir(self) -> str:
        return os.path.dirname(os.path.abspath(__file__))

    def _load_text_map(self) -> dict:
        path = os.path.join(self._get_plugin_dir(), "text.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            result = {}
            for item in data:
                if isinstance(item, dict) and item.get("success") and "text" in item:
                    result[item["filename"]] = item["text"]
            return result
        except Exception as e:
            logger.warning(f"加载 text.json 失败: {e}")
            return {}

    def _get_image_files(self) -> list:
        data_dir = os.path.join(self._get_plugin_dir(), "data")
        if not os.path.isdir(data_dir):
            return []
        return [f for f in os.listdir(data_dir) if f.lower().endswith(IMAGE_EXTS)]

    def _image_to_base64(self, image_path: str) -> str:
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')

    def _get_cache_path(self) -> str:
        return os.path.join(self._get_plugin_dir(), "user_pig_cache.json")

    def _load_user_cache(self) -> dict:
        path = self._get_cache_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_user_cache(self, cache: dict):
        path = self._get_cache_path()
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存用户缓存失败: {e}")

    def _extract_at_users(self) -> List[Tuple[str, Optional[str]]]:
        """从消息段中提取被 @ 的用户ID和名称
        返回 [(user_id, name), ...]，name 可能为 None
        """
        segments: List[Seg] = []
        if self.message.message_segment.type == "seglist":
            segments = self.message.message_segment.data
        else:
            segments = [self.message.message_segment]

        at_users: List[Tuple[str, Optional[str]]] = []
        for seg in segments:
            # 情况1：原始 at 类型 Seg，字典中可能包含 name（群名片/昵称）
            if seg.type == "at":
                data = seg.data
                if isinstance(data, dict):
                    user_id = str(data.get("qq", ""))
                    name = data.get("name")
                    if user_id and user_id != "all":
                        at_users.append((user_id, name))
                elif isinstance(data, str) and data and data != "all":
                    at_users.append((data, None))
                continue

            # 情况2：text 类型中的 @<name:qq_id> 格式
            if seg.type == "text":
                data = seg.data
                if not isinstance(data, str):
                    continue
                import re
                for match in re.finditer(r"@<(.+?):(\d+)>", data):
                    name = match.group(1)
                    user_id = match.group(2)
                    at_users.append((user_id, name))
                # 也兼容旧的纯 @qq 格式
                if data.startswith("@") and ":" in data:
                    stripped = data.strip("@<>")
                    parts = stripped.split(":")
                    if len(parts) >= 2:
                        user_id = parts[-1].strip()
                        name = parts[0].strip() if len(parts) == 2 else ":".join(parts[:-1]).strip()
                        if user_id.isdigit():
                            at_users.append((user_id, name))
        return at_users

    async def _send_via_napcat(
        self,
        img_b64: str,
        basename: str,
        text: str,
        at_user_id: str,
        group_id: str,
        at_user_name: Optional[str] = None,
    ) -> bool:
        """发送消息，支持 Napcat 直接发送和 MaiBot 原生逻辑"""
        use_napcat = self.get_config("napcat.enable", True)

        if not use_napcat:
            # 使用 MaiBot 原生逻辑：分两条消息发送（避开 VLM 识图）
            # 优先使用传入的 name（从 type="at" Seg 字典中获取），不再调用 Napcat API
            nickname = at_user_name or at_user_id
            full_text = f"@{nickname}\n【{basename}】\n{text}"
            try:
                await self.send_text(full_text)
                await self.send_custom("imageurl", f"base64://{img_b64}")
                return True
            except Exception as e:
                logger.warning(f"[Pighub] 分条发送失败: {e}")
                return False

        # 使用 NapCat HTTP API 直接发送
        host = self.get_config("napcat.napcat_host", "127.0.0.1")
        port = self.get_config("napcat.napcat_port", 3000)
        token = self.get_config("napcat.napcat_token", "")

        user_id = None
        if group_id == "private":
            user_id = self.message.chat_stream.user_info.user_id

        img_uri = f"base64://{img_b64}"

        msg_segments = [
            {"type": "at", "data": {"qq": str(at_user_id)}},
            {"type": "text", "data": {"text": "\n"}},
            {"type": "image", "data": {"file": img_uri}},
            {"type": "text", "data": {"text": f"\n【{basename}】\n{text}"}},
        ]

        if group_id != "private":
            url = f"http://{host}:{port}/send_group_msg"
            try:
                gid = int(group_id)
            except (ValueError, TypeError):
                gid = str(group_id)
            payload = {"group_id": gid, "message": msg_segments}
        elif user_id:
            url = f"http://{host}:{port}/send_private_msg"
            try:
                uid = int(user_id)
            except (ValueError, TypeError):
                uid = str(user_id)
            payload = {"user_id": uid, "message": msg_segments}
        else:
            logger.error("[Pighub] 无法确定发送目标")
            return False

        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("status") == "ok" or result.get("retcode") == 0:
                        return True
                    else:
                        logger.warning(f"[Pighub] NapCat 返回错误: {result}")
                        return False
                else:
                    logger.warning(f"[Pighub] NapCat HTTP {resp.status_code}: {resp.text[:500]}")
                    return False
        except Exception as e:
            logger.error(f"[Pighub] NapCat 发送失败: {e}")
            return False

    async def _mock_store_reply(self, reply_text: str) -> None:
        """把 bot 的回复 mock 进数据库，避免上下文中出现'未回复的@消息'"""
        if not self.get_config("mock.enable", True):
            return
        try:
            from src.chat.message_receive.message import MessageSending
            from src.chat.message_receive.storage import MessageStorage
            from maim_message import UserInfo

            chat_stream = self.message.chat_stream
            if not chat_stream:
                return

            bot_id = str(getattr(global_config.bot, "qq_account", "bot"))
            bot_nick = str(getattr(global_config.bot, "nickname", "麦麦"))
            platform = getattr(chat_stream, "platform", "qq")

            bot_user_info = UserInfo(
                user_id=bot_id,
                user_nickname=bot_nick,
                platform=platform,
            )

            msg = MessageSending(
                message_id=f"pighub_{uuid.uuid4().hex[:12]}",
                chat_stream=chat_stream,
                bot_user_info=bot_user_info,
                sender_info=None,
                message_segment=Seg(type="text", data=reply_text),
                display_message=reply_text,
            )

            # 手动设置 processed_plain_text，绕过 process() 里的 VLM 识图
            msg.processed_plain_text = reply_text

            await MessageStorage.store_message(msg, chat_stream)
            logger.info("[Pighub] 已 mock 存储 bot 回复到数据库")

        except Exception as e:
            logger.warning(f"[Pighub] mock 存储 bot 回复失败: {e}")

    async def _get_user_current_card(self, group_id: str, user_id: str) -> str:
        """获取用户当前群名片"""
        host = self.get_config("napcat.napcat_host", "127.0.0.1")
        port = self.get_config("napcat.napcat_port", 3000)
        token = self.get_config("napcat.napcat_token", "")

        url = f"http://{host}:{port}/get_group_member_info"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={
                    "group_id": int(group_id),
                    "user_id": int(user_id),
                    "no_cache": True,
                }, headers=headers)
                data = resp.json()
                if data.get("status") == "ok" or data.get("retcode") == 0:
                    member_data = data.get("data", {})
                    return member_data.get("card") or member_data.get("nickname", "")
        except Exception as e:
            logger.warning(f"[Pighub] 获取用户群名片失败: {e}")
        return ""

    def _check_admin_permission(self, group_id: str) -> bool:
        """检查当前群是否允许使用管理员功能（修改群名片）"""
        if not self.get_config("admin.enable", True):
            return False
        allowed = self.get_config("admin.allowed_groups", [])
        if not allowed:
            return True
        return str(group_id) in [str(g) for g in allowed]

    async def _set_user_card(self, group_id: str, user_id: str, card: str) -> bool:
        """调用 NapCat API 设置用户群名片"""
        if not self._check_admin_permission(group_id):
            logger.debug(f"[Pighub] 群 {group_id} 未开启管理员权限，跳过修改群名片")
            return False
        host = self.get_config("napcat.napcat_host", "127.0.0.1")
        port = self.get_config("napcat.napcat_port", 3000)
        token = self.get_config("napcat.napcat_token", "")

        url = f"http://{host}:{port}/set_group_card"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={
                    "group_id": int(group_id),
                    "user_id": int(user_id),
                    "card": card,
                }, headers=headers)
                data = resp.json()
                if data.get("status") == "ok" or data.get("retcode") == 0:
                    logger.info(f"[Pighub] 群名片设置成功: 群={group_id}, 用户={user_id}, 名片={card}")
                    return True
                else:
                    logger.warning(f"[Pighub] 群名片设置失败: {data}")
                    return False
        except Exception as e:
            logger.error(f"[Pighub] 设置群名片异常: {e}")
            return False

    def _get_group_id(self) -> str:
        """获取当前群号，私聊返回 private"""
        chat_stream = self.message.chat_stream
        if chat_stream and chat_stream.group_info and chat_stream.group_info.group_id:
            return str(chat_stream.group_info.group_id)
        return "private"

    async def _draw_and_send(
        self,
        target_user_id: str,
        group_id: str,
        skip_cache: bool = False,
        original_card: Optional[str] = None,
        target_user_name: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], bool]:
        """抽取并发送猪猪。skip_cache=True 时强制重新抽取（用于刷新）"""
        today = datetime.now().strftime("%Y-%m-%d")
        cache = self._load_user_cache()
        cache_key = f"{group_id}:{target_user_id}"

        # 1. 尝试读缓存（仅当不跳过缓存时）
        if not skip_cache:
            user_cache = cache.get(cache_key)
            if user_cache and user_cache.get("date") == today:
                logger.info(f"[Pighub] 群 {group_id} 用户 {target_user_id} 今日猪猪已缓存，直接返回")
                cached_filename = user_cache["filename"]
                cached_text = user_cache["text"]
                basename = os.path.splitext(cached_filename)[0]
                data_dir = os.path.join(self._get_plugin_dir(), "data")
                image_path = os.path.join(data_dir, cached_filename)
                if os.path.exists(image_path):
                    img_b64 = self._image_to_base64(image_path)
                    success = await self._send_via_napcat(
                        img_b64, basename, cached_text, target_user_id, group_id,
                        at_user_name=target_user_name,
                    )
                    if success:
                        await self._mock_store_reply(f"[@{target_user_id}] 今日猪猪：【{basename}】\n{cached_text}")
                        return True, f"返回了今日猪猪: {basename}", True
                    else:
                        await self.send_text("猪猪图片发送失败了，请检查 NapCat 连接配置。")
                        return True, None, True
                else:
                    logger.warning(f"[Pighub] 缓存图片不存在: {image_path}，重新抽取")

        # 2. 随机抽取
        text_map = self._load_text_map()
        images = self._get_image_files()
        if not images:
            await self.send_text("没有找到猪猪图片哦")
            return True, None, True

        chosen = random.choice(images)
        data_dir = os.path.join(self._get_plugin_dir(), "data")
        image_path = os.path.join(data_dir, chosen)
        basename = os.path.splitext(chosen)[0]
        text = text_map.get(chosen, "这只猪猪还没有被赋予故事呢~")
        img_b64 = self._image_to_base64(image_path)

        # 3. 保存缓存（仅当不跳过时）
        if not skip_cache:
            if original_card is None and group_id != "private":
                original_card = await self._get_user_current_card(group_id, target_user_id)

            cache[cache_key] = {
                "filename": chosen,
                "text": text,
                "date": today,
                "original_card": original_card or "",
            }
            self._save_user_cache(cache)

        # 4. 发送
        success = await self._send_via_napcat(
            img_b64, basename, text, target_user_id, group_id,
            at_user_name=target_user_name,
        )
        if success:
            await self._mock_store_reply(f"[@{target_user_id}] 今日猪猪：【{basename}】\n{text}")
            # 新抽取成功时同步修改群名片
            if group_id != "private":
                await self._set_user_card(group_id, target_user_id, basename)
            return True, f"发送了今日猪猪: {basename}", True
        else:
            await self.send_text("猪猪图片发送失败了，请检查 NapCat 连接配置。")
            return True, None, True


class TodayPigCommand(PigCommandBase):
    """今日猪猪 - 随机抽取一只猪猪"""

    command_name = "today_pig"
    command_description = "随机抽取一只今日猪猪"
    command_pattern = r"^(?:.*?\s)?/今日猪猪$"
    intercept_message = True

    def _resolve_target_user(self) -> Tuple[str, Optional[str]]:
        """解析目标用户：被@用户，@机器人则反弹给发送者
        返回 (user_id, name)，name 从 type="at" 的 Seg 字典中获取
        """
        sender_id = self.message.message_info.user_info.user_id
        sender_name = self.message.message_info.user_info.user_cardname or None

        at_users = self._extract_at_users()
        if at_users:
            target_id, target_name = at_users[0]
            bot_id = str(getattr(global_config.bot, "qq_account", ""))
            if target_id == bot_id:
                logger.info(f"[Pighub] @对象为机器人，反弹给发送者 {sender_id}")
                return sender_id, sender_name
            return target_id, target_name
        return sender_id, sender_name

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        try:
            target_user_id, target_user_name = self._resolve_target_user()
            group_id = self._get_group_id()
            return await self._draw_and_send(
                target_user_id, group_id, skip_cache=False,
                target_user_name=target_user_name,
            )
        except Exception as e:
            logger.error(f"今日猪猪出错: {e}", exc_info=True)
            await self.send_text("今日猪猪跑丢了，稍后再试吧")
            return False, str(e), True


class RefreshPigCommand(PigCommandBase):
    """刷新今日猪猪 - 清空缓存并重新抽取"""

    command_name = "refresh_today_pig"
    command_description = "清空今日猪猪缓存并重新抽取"
    # 支持 @其他用户 /刷新今日猪猪
    command_pattern = r"^(?:.*?\s)?/刷新今日猪猪"
    intercept_message = True

    def _resolve_target_user(self) -> Tuple[str, Optional[str]]:
        """解析目标用户：被@用户，@机器人则反弹给发送者
        返回 (user_id, name)，name 从 type="at" 的 Seg 字典中获取
        """
        sender_id = self.message.message_info.user_info.user_id
        sender_name = self.message.message_info.user_info.user_cardname or None

        at_users = self._extract_at_users()
        if at_users:
            target_id, target_name = at_users[0]
            bot_id = str(getattr(global_config.bot, "qq_account", ""))
            if target_id == bot_id:
                logger.info(f"[Pighub] @对象为机器人，反弹给发送者 {sender_id}")
                return sender_id, sender_name
            return target_id, target_name
        return sender_id, sender_name

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        try:
            target_user_id, target_user_name = self._resolve_target_user()
            group_id = self._get_group_id()
            today = datetime.now().strftime("%Y-%m-%d")
            cache = self._load_user_cache()
            cache_key = f"{group_id}:{target_user_id}"
            user_cache = cache.get(cache_key)

            # 检查今天是否有缓存
            if not user_cache or user_cache.get("date") != today:
                await self.send_text("今天还没有抽取猪猪呢，先发送 /今日猪猪 抽取吧~")
                return True, None, True

            # 保留初始名片，清除缓存后重新抽取
            original_card = user_cache.get("original_card", "")
            del cache[cache_key]
            self._save_user_cache(cache)
            logger.info(f"[Pighub] 已清除群 {group_id} 用户 {target_user_id} 的猪猪缓存")

            return await self._draw_and_send(
                target_user_id, group_id, skip_cache=False,
                original_card=original_card,
                target_user_name=target_user_name,
            )
        except Exception as e:
            logger.error(f"刷新今日猪猪出错: {e}", exc_info=True)
            await self.send_text("刷新今日猪猪失败了，稍后再试吧")
            return False, str(e), True
