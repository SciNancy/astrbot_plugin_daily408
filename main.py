"""
考研408每日真题推送插件
版本: 1.0.0

整体架构说明：
- 插件通过 AstrBot 的 Star 框架注册，利用 filter.command 响应用户指令
- 题目数据存储在本地 JSON 文件（data/questions.json）中，用户可按格式自行扩充
- 采用异步轮询（asyncio.sleep）实现定时推送，简化部署依赖
- 支持两种抽题模式：随机抽奖（/408抽奖）和顺序学习（/408顺序）
"""

import asyncio
import json
import os
import random
import re
from datetime import datetime
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
import astrbot.core.message.components as Comp

from ._version import __version__, __plugin_name__, __author__, __plugin_desc__


# 全局管理员列表，用于在命令处理函数中快速判断权限
ADMIN_USERS: List[str] = []

# 科目代码到中文名称的映射，用于命令参数解析和消息展示
SUBJECT_MAP = {
    "ds": "数据结构",
    "co": "计算机组成原理",
    "os": "操作系统",
    "cn": "计算机网络"
}


def _resolve_subject(subject_arg: str) -> Optional[str]:
    """
    将用户输入的科目参数解析为标准科目代码。

    支持多种写法：
    - 标准代码：ds / co / os / cn
    - 中文名称：数据结构 / 计组 / 组成原理 / 操作系统 / 计算机网络 / 网络
    - 大小写不敏感

    返回 None 表示未匹配到有效科目。
    """
    arg = subject_arg.strip().lower()
    if arg in SUBJECT_MAP:
        return arg

    # 中文别名映射
    alias_map = {
        "数据结构": "ds",
        "计算机组成原理": "co",
        "计组": "co",
        "组成原理": "co",
        "操作系统": "os",
        "计算机网络": "cn",
        "网络": "cn"
    }
    if arg in alias_map:
        return alias_map[arg]

    return None


@register(__plugin_name__, __author__, __plugin_desc__, __version__)
class Daily408Plugin(Star):
    """考研408每日真题推送插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        """
        构造函数：初始化插件运行所需的目录、配置、题库和锁。

        关键设计：
        - 数据目录通过 StarTools.get_data_dir 获取，确保 AstrBot 升级时用户数据不被覆盖
        - 使用 asyncio.Lock 保护文件写操作，防止并发命令导致 json 文件损坏
        - 题库加载后常驻内存，抽题时直接读取，响应迅速
        - group_last_question 用于缓存每个群最近抽到的题目，支持 /408答案 命令
        """
        super().__init__(context)
        self.config = config
        self.context = context

        # 数据目录
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_daily408"))
        os.makedirs(self.data_dir, exist_ok=True)

        # 配置文件路径
        self.config_file = os.path.join(self.data_dir, "config.json")
        self.subscription_file = os.path.join(self.data_dir, "subscription.json")
        self.progress_file = os.path.join(self.data_dir, "progress.json")

        # 群的 unified_msg_origin，定时推送时需要用到
        self.group_origins: Dict[str, str] = {}

        # 加载配置
        self._load_config()

        # 加载题库
        self.questions: List[Dict] = []
        self._load_questions()

        # 文件写入锁
        self._file_lock = asyncio.Lock()

        # 后台监控任务句柄
        self._monitor_task: Optional[asyncio.Task] = None

        global ADMIN_USERS
        ADMIN_USERS = self.admin_users.copy()

        logger.info(f"考研408每日真题插件已加载，共加载 {len(self.questions)} 道题目")

    def _load_config(self):
        """
        加载并合并配置文件。

        配置优先级（从高到低）：
        1. AstrBot 全局配置
        2. 本地 subscription.json（动态订阅数据）
        3. 本地 config.json（旧版静态配置）
        4. 代码内 default_config（兜底默认值）
        """
        default_config = {
            "check_interval_seconds": 30,
            "inform_hour": 9,
            "inform_minute": 0,
            "admin_users": [],
            "group_origins": {},
            "subscribed_groups": []
        }

        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    default_config.update(loaded_config)
            except json.JSONDecodeError as e:
                logger.error(f"配置文件JSON格式错误: {e}")
            except Exception as e:
                logger.error(f"加载配置文件失败: {e}")

        if self.config:
            default_config["inform_hour"] = self.config.get(
                "daily408_inform_hour", default_config["inform_hour"]
            )
            default_config["inform_minute"] = self.config.get(
                "daily408_inform_minute", default_config["inform_minute"]
            )
            admin_from_config = self.config.get("daily408_admin_users", [])
            if admin_from_config:
                default_config["admin_users"] = [str(u) for u in admin_from_config]

        if os.path.exists(self.subscription_file):
            try:
                with open(self.subscription_file, 'r', encoding='utf-8') as f:
                    sub_data = json.load(f)
                    if "subscribed_groups" in sub_data:
                        default_config["subscribed_groups"] = sub_data["subscribed_groups"]
                    if "group_origins" in sub_data:
                        self.group_origins = sub_data["group_origins"]
            except json.JSONDecodeError as e:
                logger.error(f"订阅配置JSON格式错误: {e}")
            except Exception as e:
                logger.error(f"加载订阅配置失败: {e}")

        self.inform_hour = default_config["inform_hour"]
        self.inform_minute = default_config["inform_minute"]
        self.admin_users = default_config["admin_users"]
        self.subscribed_groups = default_config["subscribed_groups"]

    def _load_questions(self):
        """
        从插件包内的 data/questions.json 加载题库。

        设计说明：
        - 题目文件与代码打包在一起，方便用户直接编辑 JSON 来扩充题库
        - 兼容 AstrBot 可能的 .pyc 加载方式，会尝试多个候选路径
        - 加载失败时记录错误，但插件仍能正常运行（只是没有题目可抽）
        """
        candidate_paths = []

        # 候选1：根据 __file__ 推导（可能是 .py 或 .pyc）
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.basename(plugin_dir) == '__pycache__':
            plugin_dir = os.path.dirname(plugin_dir)
        candidate_paths.append(os.path.join(plugin_dir, "data", "questions.json"))

        # 候选2：AstrBot 数据目录
        candidate_paths.append(os.path.join(self.data_dir, "questions.json"))

        # 候选3：尝试从已安装的包路径找（如 pip install 后）
        try:
            import importlib.util
            spec = importlib.util.find_spec("astrbot_plugin_daily408")
            if spec and spec.origin:
                pkg_dir = os.path.dirname(spec.origin)
                candidate_paths.append(os.path.join(pkg_dir, "data", "questions.json"))
        except Exception:
            pass

        loaded = False
        for questions_path in candidate_paths:
            logger.info(f"尝试加载题库: {questions_path}")
            if os.path.exists(questions_path):
                try:
                    with open(questions_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.questions = data.get("questions", [])
                        # 按数组顺序自动编号，支持 /408-<编号> 调取
                        for i, q in enumerate(self.questions, 1):
                            q["no"] = i
                    logger.info(f"题库加载成功: {questions_path}，共 {len(self.questions)} 道题")
                    loaded = True
                    break
                except json.JSONDecodeError as e:
                    logger.error(f"题库JSON格式错误 [{questions_path}]: {e}")
                except Exception as e:
                    logger.error(f"加载题库失败 [{questions_path}]: {e}")

        if not loaded:
            logger.warning(f"未在任何候选路径找到有效题库文件: {candidate_paths}")

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        """
        获取群组 ID 并统一转为字符串。
        """
        group_id = event.get_group_id()
        if group_id:
            return str(group_id)
        return None

    def _save_group_origin(self, event: AstrMessageEvent):
        """
        保存群的统一会话标识（unified_msg_origin）。
        定时推送后台没有 event 对象，必须在用户首次发命令时记录下来。
        """
        group_id = self._get_group_id(event)
        if group_id and hasattr(event, 'unified_msg_origin'):
            self.group_origins[group_id] = event.unified_msg_origin

    def _get_session_for_group(self, group_id: str) -> str:
        """获取用于发送消息的群会话标识。"""
        if group_id in self.group_origins:
            return self.group_origins[group_id]
        return group_id

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """
        检查用户是否为管理员。
        兼顾平台原生权限和插件自定义权限。
        """
        if event.is_admin():
            return True
        sender_id = str(event.get_sender_id())
        return sender_id in ADMIN_USERS

    async def _save_subscription(self):
        """异步保存订阅配置到 subscription.json，加锁防止并发写损坏。"""
        async with self._file_lock:
            try:
                data = {
                    "subscribed_groups": self.subscribed_groups,
                    "group_origins": self.group_origins
                }
                with open(self.subscription_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"保存订阅配置失败: {e}")

    async def initialize(self):
        """插件初始化时启动后台监控任务。"""
        self._monitor_task = asyncio.create_task(self._async_monitor())

    async def terminate(self):
        """插件卸载时取消后台监控任务。"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _async_monitor(self):
        """
        异步监控任务：每30秒检查一次是否需要推送每日真题。
        last_inform_date 防止同一分钟内重复推送。
        """
        logger.info("考研408每日真题监控任务已启动")
        last_inform_date = ""

        try:
            while True:
                try:
                    now = datetime.now()
                    today_date = now.strftime("%Y-%m-%d")

                    if (now.hour == self.inform_hour and
                        now.minute == self.inform_minute and
                        today_date != last_inform_date):

                        logger.info(f"开始推送考研408每日真题: {today_date}")
                        question = self._draw_random_question()
                        if question:
                            await self._send_question_to_subscribers(question)
                            last_inform_date = today_date
                            logger.info(f"考研408每日真题已推送: {question.get('id', '未知')}")

                except Exception as e:
                    logger.error(f"考研408监控任务出错: {e}")

                await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("考研408每日真题监控任务已停止")

    def _filter_questions(self, subject_code: Optional[str] = None) -> List[Dict]:
        """根据科目代码筛选题目，不传则返回全部。"""
        if not subject_code:
            return self.questions
        return [q for q in self.questions if q.get("subject_code") == subject_code]

    def _draw_random_question(self, subject_code: Optional[str] = None) -> Optional[Dict]:
        """随机抽取一道题。"""
        pool = self._filter_questions(subject_code)
        if not pool:
            return None
        return random.choice(pool)

    def _build_question_message(self, question: Dict, show_answer: bool = True) -> List:
        """
        将题目字典转换为 AstrBot 消息链。

        参数：
        - show_answer: 为 True 时同时输出答案和解析
        """
        chain = []

        subject_emoji = {
            "ds": "🌲",
            "co": "⚙️",
            "os": "💻",
            "cn": "🌐"
        }

        emoji = subject_emoji.get(question.get("subject_code", ""), "📚")
        year = question.get("year", "未知年份")
        subject = question.get("subject", "未知科目")
        q_type = question.get("type", "单选题")
        q_text = question.get("question", "")
        options = question.get("options", [])

        # 标题行
        chain.append(Comp.Plain(f"{emoji} 【{year}年·{subject}】\n"))
        chain.append(Comp.Plain(f"题型: {q_type}\n"))
        chain.append(Comp.Plain("=" * 30 + "\n"))

        # 题目内容
        chain.append(Comp.Plain(f"{q_text}\n\n"))

        # 选项
        if options:
            for opt in options:
                chain.append(Comp.Plain(f"{opt}\n"))

        # 答案和解析
        if show_answer:
            chain.append(Comp.Plain("\n" + "=" * 30 + "\n"))
            answer = question.get("answer", "")
            analysis = question.get("analysis", "")
            chain.append(Comp.Plain(f"✅ 答案: {answer}\n\n"))
            chain.append(Comp.Plain(f"💡 解析:\n{analysis}"))

        return chain

    async def _send_question_to_subscribers(self, question: Dict):
        """将题目推送到所有已订阅的群，群发间隔1秒防止风控。"""
        chain = self._build_question_message(question)

        for group_id in self.subscribed_groups:
            try:
                await self.context.send_message(
                    self._get_session_for_group(group_id),
                    MessageChain(chain)
                )
                logger.info(f"考研408每日真题已发送到群 {group_id}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"发送题目到群 {group_id} 失败: {e}")

    async def _send_plain_text(self, group_id: str, text: str):
        """向指定群发送纯文本消息。"""
        try:
            chain = [Comp.Plain(text)]
            await self.context.send_message(self._get_session_for_group(group_id), MessageChain(chain))
        except Exception as e:
            logger.error(f"发送消息到群 {group_id} 失败: {e}")

    # ========== 命令处理 ==========

    @filter.command("408菜单")
    async def cmd_menu(self, event: AstrMessageEvent):
        """命令：/408菜单 - 显示主菜单（私聊/群聊均可使用）"""
        self._save_group_origin(event)

        msg = """🎓 考研408每日真题 - 主菜单

【抽题命令】
🎲 /408抽奖 [科目] - 随机抽取一道真题
🔢 /408-<编号> - 调取指定编号题目（如 /408-3）

【科目缩写】
ds=数据结构  co=计组  os=操作系统  cn=计算机网络

【管理命令】
➕ /408订阅 - 在当前群订阅每日推送
➖ /408退订 - 在当前群取消订阅
📋 /408列表 - 查看当前群订阅状态
📋 /408全部订阅 - 查看所有订阅群
📖 /408帮助 - 查看详细帮助"""

        yield event.plain_result(msg)

    @filter.command("408帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """命令：/408帮助 - 显示详细帮助（私聊/群聊均可使用）"""
        self._save_group_origin(event)

        msg = """📖 考研408每日真题 - 详细使用说明

【抽题命令】
1️⃣ /408抽奖 [科目]
   随机从题库抽一道题。不传科目则从全库抽取。
   示例: /408抽奖 ds

2️⃣ /408-<编号>
   调取指定编号的题目，编号即题库中的顺序号。
   示例: /408-3  /408-122

【管理命令】
3️⃣ /408订阅 - 订阅每日真题推送（默认09:00，仅限群聊）
4️⃣ /408退订 - 取消每日推送（仅限群聊）
5️⃣ /408列表 - 查看本群订阅状态
6️⃣ /408全部订阅 - 查看所有已订阅群（管理员）

【配置】
- 默认每日 09:00 推送
- 可在 AstrBot 插件配置中修改推送时间

【提示】
- 管理命令仅限管理员使用
- 抽题命令支持私聊和群聊
- 题库文件为 data/questions.json，可自行扩充"""

        yield event.plain_result(msg)

    @filter.command("408抽奖")
    async def cmd_draw(self, event: AstrMessageEvent, subject: str = ""):
        """
        命令：/408抽奖 [科目]
        功能：随机抽取一道408真题。支持私聊和群聊。
        """
        self._save_group_origin(event)

        # 懒加载兜底：如果题库为空，尝试重新加载
        if not self.questions:
            self._load_questions()

        subject_code = None
        if subject:
            subject_code = _resolve_subject(subject)
            if not subject_code:
                yield event.plain_result("❌ 未识别的科目，请使用 ds/co/os/cn 或中文名称")
                return

        question = self._draw_random_question(subject_code)
        if not question:
            yield event.plain_result("❌ 题库为空或该科目暂无题目")
            return

        chain = self._build_question_message(question)
        yield event.chain_result(chain)

    @filter.command("408编号")
    async def cmd_draw_by_no(self, event: AstrMessageEvent, no_str: str = ""):
        """
        命令：/408编号 <编号>
        功能：调取指定编号的408真题。支持私聊和群聊。
        """
        self._save_group_origin(event)

        # 懒加载兜底：如果题库为空，尝试重新加载
        if not self.questions:
            self._load_questions()

        logger.info(f"编号取题被调用, no_str={no_str}, questions_len={len(self.questions)}")

        if not no_str:
            yield event.plain_result("❌ 请输入题目编号，例如 /408编号 1")
            return

        try:
            no = int(no_str.strip())
        except ValueError:
            yield event.plain_result("❌ 编号必须是数字")
            return

        if no < 1 or no > len(self.questions):
            yield event.plain_result(f"❌ 编号超出范围，当前题库共 {len(self.questions)} 道题")
            return

        question = self.questions[no - 1]
        chain = self._build_question_message(question)
        yield event.chain_result(chain)


    @filter.command("408订阅")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """命令：/408订阅 - 将当前群加入订阅列表"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群聊中使用")
            return

        if group_id in self.subscribed_groups:
            yield event.plain_result("❌ 本群已经订阅了考研408每日真题")
            return

        self.subscribed_groups.append(group_id)
        await self._save_subscription()

        yield event.plain_result(
            f"✅ 本群已成功订阅考研408每日真题\n每日 {self.inform_hour:02d}:{self.inform_minute:02d} 推送"
        )

    @filter.command("408退订")
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """命令：/408退订 - 将当前群从订阅列表移除"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群聊中使用")
            return

        if group_id not in self.subscribed_groups:
            yield event.plain_result("❌ 本群没有订阅考研408每日真题")
            return

        self.subscribed_groups.remove(group_id)
        await self._save_subscription()

        yield event.plain_result("✅ 本群已取消订阅考研408每日真题")

    @filter.command("408列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """命令：/408列表 - 查看当前群订阅状态"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群聊中使用")
            return

        if group_id in self.subscribed_groups:
            yield event.plain_result(
                f"✅ 本群已订阅考研408每日真题\n每日 {self.inform_hour:02d}:{self.inform_minute:02d} 推送"
            )
        else:
            yield event.plain_result("❌ 本群未订阅考研408每日真题")

    @filter.command("408全部订阅")
    async def cmd_all_subscriptions(self, event: AstrMessageEvent):
        """命令：/408全部订阅 - 列出所有已订阅的群"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        if not self.subscribed_groups:
            yield event.plain_result("📋 暂无能订阅考研408每日真题的群")
            return

        lines = ["📋 已订阅考研408每日真题的群:"]
        lines.append("=" * 30)
        for i, group_id in enumerate(self.subscribed_groups, 1):
            lines.append(f"{i}. {group_id}")

        yield event.plain_result("\n".join(lines))
