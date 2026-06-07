"""
astrbot_plugin_sleep_sense v1.1.0
让 Bot 拥有真实睡眠感知：睡觉、慵懒、熬夜、补觉、吵醒、睡眠周期、做梦
作者: 夕小柠
"""

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api import logger, AstrBotConfig, llm_tool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


# ── 数据目录 ───────────────────────────────────────────────────────────────
DATA_DIR = Path("data/sleep_sense")
STATE_PATH = DATA_DIR / "state.json"
ALARMS_PATH = DATA_DIR / "alarms.json"
LOG_PATH = DATA_DIR / "logs" / "sleep.log"
STATS_DIR = DATA_DIR / "stats"
DREAMS_PATH = DATA_DIR / "dreams.json"   # 梦境存档


# ── 状态常量 ────────────────────────────────────────────────────────────────
class S:
    AWAKE = "awake"
    LAZY = "lazy"
    SLEEPING = "sleeping"
    NAPPING = "napping"
    OVERTIME = "overtime"


class SleepSensePlugin(Star):
    """让 Bot 拥有真实睡眠感知的插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config

        # 确保目录
        for d in [DATA_DIR / "logs", STATS_DIR, DATA_DIR / "overtime" / "history"]:
            d.mkdir(parents=True, exist_ok=True)

        # 运行时状态
        self.state = self._load_state()
        self.alarms: list = self._load_json(ALARMS_PATH, [])
        self.dreams: list = self._load_json(DREAMS_PATH, [])  # 梦境存档

        # 做梦：收集入睡前对话关键词（内存缓冲，最近N条消息摘要）
        self._pre_sleep_ctx: list[str] = []   # 入睡前上下文素材
        self._dream_generated_tonight = False  # 今晚是否已生成梦

        # 内存计数器（不持久化，重启即清）
        self._msg_counters: dict[str, int] = {}   # f"{uid}_{is_group}" -> 消息计数
        self._woken_flag: dict[str, bool] = {}    # 是否已经完成第一波唤醒
        self._last_admin_ts: float = time.time()
        self._last_others_ts: dict[str, float] = {}
        self._lock = asyncio.Lock()

        # 启动后台任务
        asyncio.create_task(self._scheduler_loop())
        asyncio.create_task(self._alarm_loop())

        logger.info(f"[sleep_sense] 插件已启动，当前状态: {self.state['sleep_state']}")

    # ═══════════════════════════════════════════════════════════════════════
    # 消息主入口
    # ═══════════════════════════════════════════════════════════════════════
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        async with self._lock:
            uid = str(event.get_sender_id())
            is_admin = self._is_admin(uid)
            is_group = event.get_group_id() is not None
            is_at = getattr(event, "is_at_me", False) if is_group else True
            if callable(is_at): is_at = is_at()
            text = event.message_str or ""

            # 更新最后消息时间戳
            if is_admin:
                self._last_admin_ts = time.time()
            else:
                self._last_others_ts[uid] = time.time()

            # ── 收集入睡前上下文素材（仅清醒/慵懒/熬夜状态时收集）──────────
            cur = self.state["sleep_state"]
            if cur in (S.AWAKE, S.LAZY, S.OVERTIME) and text:
                self._collect_dream_ctx(text)

            cur = self.state["sleep_state"]

            if cur == S.SLEEPING:
                result = await self._handle_sleeping(event, uid, is_admin, is_group, is_at, text)
                if not result:
                    # 阻止 LLM 响应：返回 None 即可（不 yield 就不回复）
                    return
            elif cur == S.NAPPING:
                result = await self._handle_napping(event, uid, is_admin, is_group, is_at)
                if not result:
                    return
            else:
                # 清醒/慵懒/熬夜：注入提示词
                inject = self._build_inject(is_admin)
                if inject:
                    event.set_extra("sleep_sense_inject", inject)
                    # 通过 AstrBot 系统提示词注入机制
                    try:
                        ctx_sys = event.get_extra("system_prompt") or ""
                        event.set_extra("system_prompt", (ctx_sys + "\n\n" + inject).strip())
                    except Exception:
                        pass

    # ═══════════════════════════════════════════════════════════════════════
    # 睡觉中处理
    # ═══════════════════════════════════════════════════════════════════════
    async def _handle_sleeping(self, event, uid, is_admin, is_group, is_at, text) -> bool:
        """返回 True = 允许 LLM 回复（已注入提示词），False = 阻断"""
        cfg = self.cfg
        if not cfg.get("wake_trigger_enabled", True):
            return False

        # 场景过滤：群聊只响应艾特
        if is_group and not is_at:
            return False

        # 场景开关
        if not is_admin and not is_group and not cfg.get("scene_private_others", True):
            return False
        if not is_admin and is_group and not cfg.get("scene_group_others", True):
            return False
        if is_admin and not is_group and not cfg.get("scene_private_admin", True):
            return False
        if is_admin and is_group and not cfg.get("scene_group_admin", True):
            return False

        # 警戒词：立刻唤醒
        if is_admin:
            alert_raw = cfg.get("wake_alert_words", "醒醒,有大事,紧急,快起来")
            alerts = [w.strip() for w in alert_raw.split(",") if w.strip()]
            group_alert = is_group and cfg.get("wake_alert_group_enabled", True)
            if not is_group or group_alert:
                if any(w in text for w in alerts):
                    await self._do_wake(event, uid, is_admin, is_group, force=True)
                    return True

        # 睡眠周期乘数
        multiplier = self._get_cycle_multiplier()
        key = f"{uid}_{is_group}"
        cnt = self._msg_counters.get(key, 0) + 1
        self._msg_counters[key] = cnt

        # 需要的消息条数
        if is_admin:
            needed = cfg.get("wake_count_private_admin", 3) if not is_group else cfg.get("wake_count_group_admin", 3)
        else:
            needed = cfg.get("wake_count_private_others", 10) if not is_group else cfg.get("wake_count_group_others", 6)

        needed_adj = max(1, round(needed * multiplier))

        if cnt >= needed_adj:
            self._msg_counters[key] = 0
            await self._do_wake(event, uid, is_admin, is_group)
            return True
        return False

    async def _do_wake(self, event, uid, is_admin, is_group, force=False):
        woken_key = f"woken_{uid}_{is_group}"
        already = self._woken_flag.get(woken_key, False)

        cycle_prefix = self._get_cycle_prompt_prefix()

        if not already:
            # 第一波：懵
            if is_admin and not is_group:
                p = self.cfg.get("prompts_wake_private_admin_1", "这是你的管理员，你刚才被吵醒了，很懵很困，可以单发一个「？」或者「嗯？怎么了」。")
            elif is_admin and is_group:
                p = self.cfg.get("prompts_wake_group_admin_1", "管理员在群里艾特你了，把你吵醒了，很懵很困，可以短回复一下。")
            elif not is_group:
                p = self.cfg.get("prompts_wake_private_others_1", "你被他吵醒了，很懵，可以发个问号或者「嗯？」之类的短回复。")
            else:
                p = self.cfg.get("prompts_wake_group_others_1", "你被群里的人艾特吵醒了，很懵，可以简短问一下怎么了。")

            self._woken_flag[woken_key] = True
            self._set_state(S.AWAKE)
            self._emit_state("awake")
            self._log("info", f"被唤醒 uid={uid} group={is_group}")

        else:
            # 多次唤醒
            multi_cfg = self.cfg.get("multi_wake_enabled", True)
            if not multi_cfg:
                return
            if not is_admin and not self.cfg.get("multi_wake_others", False):
                return

            if is_admin and not is_group:
                p = self.cfg.get("prompts_multi_wake_private_admin", "这是管理员再次发消息吵醒你了，你不生气，只是有点疑惑比较困，回复简短。")
            elif is_admin and is_group:
                p = "这是管理员在群里再次吵醒你，你不生气，有点困，回复简短，可以问问为什么还没睡。"
            else:
                p = self.cfg.get("prompts_wake_private_others_2", "你刚刚被吵醒，慢慢清醒，语气还是困，如果不是大事回复简短。")

        prompt = (cycle_prefix + p).strip()
        self._inject_prompt(event, prompt)

        # 被别人吵醒后汇报
        if not is_admin and self.cfg.get("wake_report_to_admin", True):
            asyncio.create_task(self._report_to_admin_later())

    # ═══════════════════════════════════════════════════════════════════════
    # 午休/补觉处理
    # ═══════════════════════════════════════════════════════════════════════
    async def _handle_napping(self, event, uid, is_admin, is_group, is_at) -> bool:
        if is_group and not is_at:
            return False

        key = f"nap_{uid}_{is_group}"
        cnt = self._msg_counters.get(key, 0) + 1
        self._msg_counters[key] = cnt

        if is_admin:
            needed = self.cfg.get("nap_wake_private_admin", 3) if not is_group else self.cfg.get("nap_wake_group_admin", 2)
        else:
            needed = self.cfg.get("nap_wake_private_others", 4) if not is_group else self.cfg.get("nap_wake_group_others", 3)

        if cnt >= needed:
            self._msg_counters[key] = 0
            self._set_state(S.AWAKE)
            self._emit_state("awake")

            if is_admin and not is_group:
                p = "刚才是在午休，被管理员吵醒了，回复一下吧。"
            elif is_admin and is_group:
                p = "刚才在午休，管理员在群里艾特了，睡着了，回复一下。"
            elif not is_group:
                p = "你刚才在午休，被对方发消息吵醒了。"
            else:
                p = "你刚才在补觉，被群聊里有人艾特吵醒了。如果问你去哪了，可以回复刚才睡着了。"

            self._inject_prompt(event, p)
            return True
        return False

    # ═══════════════════════════════════════════════════════════════════════
    # 清醒状态：注入提示词
    # ═══════════════════════════════════════════════════════════════════════
    def _build_inject(self, is_admin: bool) -> str:
        parts = []
        cur = self.state["sleep_state"]

        if cur == S.LAZY:
            p = self.cfg.get("lazy_prompt", "")
            if p:
                parts.append(p)

        if cur == S.AWAKE:
            consec = self.state.get("consecutive_overtime", 0)
            fatigue_start = self.cfg.get("overtime_fatigue_start_day", 2)
            if consec >= fatigue_start:
                day = min(consec, 4)
                keys = {1: "prompts_fatigue_day1", 2: "prompts_fatigue_day2",
                        3: "prompts_fatigue_day3", 4: "prompts_fatigue_day4"}
                fp = self.cfg.get(keys.get(day, "prompts_fatigue_day4"), "")
                if fp:
                    parts.append(fp)

        if cur == S.OVERTIME:
            scope = self.cfg.get("overtime_night_scope", "admin_only")
            if scope == "global" or is_admin:
                p = self.cfg.get("overtime_night_mood_prompt", "")
                if p:
                    parts.append(p)

        return " ".join(p for p in parts if p)

    # ═══════════════════════════════════════════════════════════════════════
    # 指令：/睡眠
    # ═══════════════════════════════════════════════════════════════════════
    @filter.command("睡眠")
    async def cmd_sleep(self, event: AstrMessageEvent):
        """睡眠插件管理指令。用法: /睡眠 状态|清醒|慵懒|睡觉|熬夜|重载|日志on|日志off"""
        text = (event.message_str or "").strip()
        # 去掉指令前缀
        action = text.replace("/睡眠", "").strip() or "状态"

        # 权限：仅管理员 QQ 或 AstrBot 管理员可用
        uid = str(event.get_sender_id())
        is_admin = self._is_admin(uid)
        try:
            is_op = await self.context.is_admin(uid)
        except Exception:
            is_op = False
        if not (is_admin or is_op):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return

        s = self.state
        if action == "状态":
            dur = s.get("last_sleep_duration", 0)
            yield event.plain_result(
                f"🌙 状态：{s.get('sleep_state','?')}\n"
                f"💤 周期：{s.get('sleep_cycle_phase','-')}\n"
                f"⏰ 上次睡眠：{dur/3600:.1f}h\n"
                f"🌃 连续熬夜：{s.get('consecutive_overtime',0)}天"
            )
        elif action == "清醒":
            self._set_state(S.AWAKE)
            self._emit_state("awake")
            yield event.plain_result("✅ 已切换：清醒")
        elif action == "慵懒":
            self._set_state(S.LAZY)
            yield event.plain_result("✅ 已切换：慵懒")
        elif action == "睡觉":
            await self._enter_sleep()
            yield event.plain_result("✅ 已切换：睡眠")
        elif action == "熬夜":
            self._set_state(S.OVERTIME)
            self.state["consecutive_overtime"] = self.state.get("consecutive_overtime", 0) + 1
            self._save_state()
            yield event.plain_result("✅ 已切换：熬夜")
        elif action == "重载":
            # AstrBotConfig 是实时的，直接重新读即可
            yield event.plain_result("✅ 配置已是最新（AstrBot 配置实时生效）")
        elif action == "日志on":
            self._log_level = "debug"
            yield event.plain_result("✅ debug 日志已开启")
        elif action == "日志off":
            self._log_level = "info"
            yield event.plain_result("✅ debug 日志已关闭")
        else:
            yield event.plain_result(
                "可用: 状态 / 清醒 / 慵懒 / 睡觉 / 熬夜 / 重载 / 日志on / 日志off"
            )

    # ═══════════════════════════════════════════════════════════════════════
    # AI 工具：设置临时闹钟
    # ═══════════════════════════════════════════════════════════════════════
    @llm_tool(name="sleep_set_alarm")
    async def tool_set_alarm(self, event: AstrMessageEvent, time_str: str, reason: str, target: str = ""):
        """设置临时闹钟，让Bot在指定时间自动唤醒。

        Args:
            time_str(string): 时间，格式 HH:MM，如 06:30
            reason(string): 闹钟原因说明
            target(string): 目标用户QQ号（可留空）
        """
        if not self.cfg.get("alarm_enabled", True):
            yield event.plain_result("临时闹钟功能已关闭。")
            return
        max_cnt = self.cfg.get("alarm_max_count", 3)
        if len(self.alarms) >= max_cnt:
            yield event.plain_result(f"闹钟已满（最多 {max_cnt} 个）。")
            return

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        try:
            alarm_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            if alarm_dt <= now:
                alarm_dt += timedelta(days=1)
                date_str = alarm_dt.strftime("%Y-%m-%d")
        except ValueError:
            yield event.plain_result(f"时间格式错误，请用 HH:MM，如 06:30")
            return

        self.alarms.append({
            "time": time_str, "date": date_str,
            "reason": reason, "target": target,
            "created_at": int(time.time()),
        })
        self._save_json(ALARMS_PATH, self.alarms)
        yield event.plain_result(f"✅ 闹钟已设置：{date_str} {time_str}，原因：{reason}")

    # ═══════════════════════════════════════════════════════════════════════
    # 后台定时任务
    # ═══════════════════════════════════════════════════════════════════════
    async def _scheduler_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"[sleep_sense] scheduler error: {e}")

    async def _tick(self):
        now = datetime.now()
        cur = self.state["sleep_state"]

        # 慵懒检测
        if cur == S.AWAKE and self.cfg.get("lazy_enabled", True):
            ls = self._parse_time(self.cfg.get("lazy_start", "22:00"))
            le = self._parse_time(self.cfg.get("lazy_end", "23:00"))
            if self._in_range(now.time(), ls, le):
                self._set_state(S.LAZY)

        # 慵懒→睡觉时间到了切回清醒判断（下面睡觉检测会接管）
        if cur == S.LAZY:
            le = self._parse_time(self.cfg.get("lazy_end", "23:00"))
            if now.time() >= le:
                pass  # 直接让睡觉检测接管

        # 睡觉检测
        if cur in (S.AWAKE, S.LAZY, S.OVERTIME):
            await self._check_sleep(now)

        # 起床检测
        if cur == S.SLEEPING:
            await self._check_wake(now)
            self._update_cycle()
            await self._check_nightmare()

        # 午休检测
        if cur == S.AWAKE:
            await self._check_nap(now)

        # 熬夜决策
        if cur in (S.AWAKE, S.LAZY):
            await self._check_overtime_decision(now)

    async def _check_sleep(self, now: datetime):
        if not self.cfg.get("sleep_enabled", True):
            return

        target_str = self._get_day_sleep_time(now)
        variance = self.cfg.get("sleep_variance", 30)
        offset = random.randint(-variance, variance)
        target = self._parse_time(target_str)
        target_dt = now.replace(hour=target.hour, minute=target.minute, second=0) + timedelta(minutes=offset)

        if now < target_dt:
            return

        # 条件①到点 ②管理员空闲 ③私聊无消息
        admin_idle = (time.time() - self._last_admin_ts) >= self.cfg.get("admin_idle_minutes", 30) * 60
        private_silent = self._is_private_silent()

        if admin_idle and private_silent:
            await self._enter_sleep()
        elif admin_idle and not private_silent:
            # 别人还在聊，等一会再强制睡
            grace = self.cfg.get("others_grace_minutes", 3) * 60
            asyncio.create_task(self._delayed_sleep(grace))

    async def _check_wake(self, now: datetime):
        custom_wake = self.state.get("custom_wake_time")
        wake_str = custom_wake or self._get_day_wake_time(now)
        variance = self.cfg.get("wake_variance", 20)
        offset = random.randint(0, variance)
        target = self._parse_time(wake_str)
        target_dt = now.replace(hour=target.hour, minute=target.minute, second=0) + timedelta(minutes=offset)

        if now >= target_dt:
            await self._do_wake_up()

    async def _check_nap(self, now: datetime):
        if not self.cfg.get("nap_enabled", True):
            return

        last_dur = self.state.get("last_sleep_duration", 8 * 3600)
        critical = self.cfg.get("nap_critical_hours", 2) * 3600
        min_hours = self.cfg.get("nap_min_sleep_hours", 6) * 3600
        is_critical = last_dur < critical

        in_window = is_critical  # 严重不足时忽略时间窗
        if not is_critical:
            ws = self._parse_time(self.cfg.get("nap_window_start", "13:00"))
            we = self._parse_time(self.cfg.get("nap_window_end", "16:00"))
            in_window = self._in_range(now.time(), ws, we)

        prob = self.cfg.get("nap_probability", 0.8)
        siesta = self.cfg.get("siesta_enabled", True)

        if in_window and (siesta or last_dur < min_hours):
            if random.random() < prob:
                if self._is_private_silent():
                    await self._enter_nap()

    async def _check_overtime_decision(self, now: datetime):
        if not self.cfg.get("overtime_enabled", True):
            return
        if self.state.get("overtime_decided_today"):
            return

        sleep_str = self._get_day_sleep_time(now)
        t = self._parse_time(sleep_str)
        target_dt = now.replace(hour=t.hour, minute=t.minute, second=0)
        window_start = target_dt - timedelta(minutes=30)
        if not (window_start <= now <= target_dt):
            return

        mode = self.cfg.get("overtime_mode", "probability")
        should = False
        if mode == "probability":
            should = random.random() < self.cfg.get("overtime_probability", 0.1)
        elif mode == "weekly_limit":
            should = self.state.get("weekly_overtime", 0) < self.cfg.get("overtime_weekly_limit", 2)

        if should:
            self.state["overtime_decided_today"] = True
            self._save_state()
            prompt = self.cfg.get("overtime_ask_prompt", "今天晚上，你想熬夜吗？由你自己来决定，只回复是或否。")
            await self._send_to_admin(prompt)

    async def _check_nightmare(self):
        if not self.cfg.get("nightmare_enabled", True):
            return
        if self.state.get("nightmare_tonight"):
            return
        base = self.cfg.get("nightmare_probability", 0.02)
        if random.random() < base / 60:  # 每分钟触发
            self.state["nightmare_tonight"] = True
            self._save_state()
            p = self.cfg.get("nightmare_prompt", "你刚才做噩梦惊醒了，现在心里有点难受，可以发消息给管理员寻求安慰。")
            await self._send_to_admin(p)
            self._set_state(S.AWAKE)
            self._emit_state("awake")

    async def _alarm_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_alarms()
            except Exception as e:
                logger.error(f"[sleep_sense] alarm error: {e}")

    async def _check_alarms(self):
        now = datetime.now()
        done = []
        for a in self.alarms:
            try:
                alarm_dt = datetime.strptime(f"{a['date']} {a['time']}", "%Y-%m-%d %H:%M")
            except Exception:
                done.append(a)
                continue
            if now >= alarm_dt:
                done.append(a)
                p = f"[闹钟] {a.get('reason','')} 你被唤醒了，自行判断现在的时间，正常回复。"
                await self._send_to_admin(p)
                self._log("info", f"闹钟触发: {a}")
        self.alarms = [x for x in self.alarms if x not in done]
        if done:
            self._save_json(ALARMS_PATH, self.alarms)

    # ═══════════════════════════════════════════════════════════════════════
    # 状态转换
    # ═══════════════════════════════════════════════════════════════════════
    async def _enter_sleep(self):
        self._set_state(S.SLEEPING)
        self.state["sleep_start"] = time.time()
        self.state["sleep_cycle_phase"] = "light"
        self.state["nightmare_tonight"] = False
        self.state["overtime_decided_today"] = False
        self._msg_counters.clear()
        self._woken_flag.clear()
        self._dream_generated_tonight = False
        self._save_state()
        self._emit_state("sleep")
        self._log("info", "进入睡眠")
        self._record_stat("sleep_start", datetime.now().isoformat())
        # 入睡后，异步调度做梦
        asyncio.create_task(self._dream_scheduler())

    async def _enter_nap(self):
        self._set_state(S.NAPPING)
        self._save_state()
        self._emit_state("nap")
        self._log("info", "进入午休/补觉")

    async def _do_wake_up(self):
        start = self.state.get("sleep_start")
        if start:
            self.state["last_sleep_duration"] = time.time() - start
        self.state["custom_wake_time"] = None
        self.state["consecutive_overtime"] = 0
        self._set_state(S.AWAKE)
        self._emit_state("awake")
        self._save_state()
        self._log("info", "正常起床")
        self._record_stat("wake_time", datetime.now().isoformat())
        # 起床时尝试浮现梦境记忆
        await self._dream_recall_on_wake()

    def _set_state(self, s: str):
        self.state["sleep_state"] = s
        self._save_state()

    def _emit_state(self, s: str):
        try:
            self.context.emit_event("sleep_plugin_state_change", {"state": s})
        except Exception:
            pass

    async def _delayed_sleep(self, delay: float):
        await asyncio.sleep(delay)
        if self.state["sleep_state"] not in (S.SLEEPING, S.NAPPING):
            await self._enter_sleep()

    # ═══════════════════════════════════════════════════════════════════════
    # 做梦模块（Dream Engine）
    # ═══════════════════════════════════════════════════════════════════════

    def _collect_dream_ctx(self, text: str):
        """收集入睡前的对话片段作为做梦素材，保留最近 N 条"""
        max_ctx = self.cfg.get("dream_ctx_max", 30)
        # 只取有意义的片段（过滤太短的）
        stripped = text.strip()
        if len(stripped) >= 4:
            self._pre_sleep_ctx.append(stripped)
            if len(self._pre_sleep_ctx) > max_ctx:
                self._pre_sleep_ctx = self._pre_sleep_ctx[-max_ctx:]

    async def _dream_scheduler(self):
        """入睡后定时触发做梦。在深睡期开始后的随机时间点生成一次梦。"""
        if not self.cfg.get("dream_enabled", True):
            return

        # 等到深睡期才做梦（入睡后 deep_minutes 分钟）
        deep_min = self.cfg.get("cycle_deep_minutes", 90)
        # 在深睡期内随机一个时间点触发
        trigger_offset = random.randint(0, self.cfg.get("dream_window_minutes", 60)) * 60
        await asyncio.sleep(deep_min * 60 + trigger_offset)

        # 确认还在睡眠中
        if self.state["sleep_state"] != S.SLEEPING:
            return
        if self._dream_generated_tonight:
            return

        # 做梦概率检查
        prob = self.cfg.get("dream_probability", 0.7)
        if random.random() > prob:
            self._log("debug", "今晚随机跳过做梦")
            return

        await self._generate_dream()

    async def _generate_dream(self):
        """调用 LLM 生成梦境内容，存档，标记是否记得。"""
        if self._dream_generated_tonight:
            return
        self._dream_generated_tonight = True

        cfg = self.cfg
        ctx_lines = self._pre_sleep_ctx[-cfg.get("dream_ctx_use", 15):]
        ctx_summary = "、".join(ctx_lines) if ctx_lines else "（没有特别的内容）"

        # 梦的类型：随机或加权
        dream_type = self._pick_dream_type()

        # 构造生成提示词
        generate_prompt = cfg.get("dream_generate_prompt", "").strip()
        if not generate_prompt:
            generate_prompt = (
                "你现在正在睡觉，进入了做梦状态。\n"
                "今天睡前聊到的内容大致是：{ctx}\n"
                "梦的类型是：{type}\n"
                "请用第一人称，生成一段梦境。要求：\n"
                "- 意象自由联想，不需要逻辑连贯，允许跳跃和隐喻\n"
                "- 有些细节可以和今天的内容有关，也可以完全无关\n"
                "- 100~200字，像碎片一样，不要分析梦的含义\n"
                "- 直接输出梦境内容，不要有任何前缀或解释"
            )
        generate_prompt = generate_prompt.replace("{ctx}", ctx_summary).replace("{type}", dream_type)

        # 用 context 的 LLM 能力生成（不消耗用户对话）
        dream_content = await self._llm_generate(generate_prompt)
        if not dream_content:
            self._log("warn", "梦境生成失败，跳过")
            return

        # 记忆概率：随机决定醒来还记不记得
        recall_prob = cfg.get("dream_recall_probability", 0.6)
        will_recall = random.random() < recall_prob

        # 模糊程度：记得清晰 / 模糊 / 只剩感觉
        if will_recall:
            clarity = random.choices(
                ["clear", "blurry", "feeling_only"],
                weights=[
                    cfg.get("dream_clarity_clear_weight", 40),
                    cfg.get("dream_clarity_blurry_weight", 40),
                    cfg.get("dream_clarity_feeling_weight", 20),
                ]
            )[0]
        else:
            clarity = "forgotten"

        # 存入档案
        record = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "sleep_start": self.state.get("sleep_start"),
            "type": dream_type,
            "content": dream_content,
            "clarity": clarity,
            "recalled": will_recall,
            "ts": time.time(),
        }
        self.dreams.append(record)
        # 只保留最近 N 条
        max_archive = self.cfg.get("dream_archive_max", 30)
        if len(self.dreams) > max_archive:
            self.dreams = self.dreams[-max_archive:]
        self._save_json(DREAMS_PATH, self.dreams)
        self._log("info", f"梦境已生成 type={dream_type} clarity={clarity} recall={will_recall}")

        # 把梦境内容存到今晚的 state，供起床时浮现
        self.state["tonight_dream"] = {
            "content": dream_content,
            "type": dream_type,
            "clarity": clarity,
            "recalled": will_recall,
        }
        self._save_state()

    def _pick_dream_type(self) -> str:
        """根据权重随机选择梦的类型"""
        cfg = self.cfg
        types = [
            ("日常碎片", cfg.get("dream_type_w_daily", 35)),
            ("奇异梦境", cfg.get("dream_type_w_strange", 25)),
            ("美梦", cfg.get("dream_type_w_sweet", 20)),
            ("噩梦", cfg.get("dream_type_w_nightmare", 10)),
            ("情感梦", cfg.get("dream_type_w_emotion", 10)),
        ]
        # 也支持用户自定义额外类型
        extra_types = cfg.get("dream_extra_types", "")
        if extra_types:
            for t in extra_types.split(","):
                t = t.strip()
                if t:
                    types.append((t, cfg.get("dream_extra_types_weight", 5)))

        labels = [t[0] for t in types]
        weights = [t[1] for t in types]
        return random.choices(labels, weights=weights)[0]

    async def _dream_recall_on_wake(self):
        """起床时，根据 clarity 决定是否把梦境浮现给管理员。"""
        if not self.cfg.get("dream_enabled", True):
            return
        if not self.cfg.get("dream_recall_on_wake", True):
            return

        dream = self.state.get("tonight_dream")
        if not dream:
            return

        # 清空今晚梦境（避免重复浮现）
        self.state.pop("tonight_dream", None)
        self._save_state()

        clarity = dream.get("clarity", "forgotten")
        content = dream.get("content", "")
        dtype = dream.get("type", "")

        if clarity == "forgotten":
            # 完全不记得，静默，不发消息
            return

        # 根据清晰度构造起床时的浮现提示词
        if clarity == "clear":
            recall_prompt = self.cfg.get("dream_recall_clear_prompt", "").strip()
            if not recall_prompt:
                recall_prompt = (
                    "你刚刚睡醒，还记得昨晚做的梦。"
                    "梦的内容是：{dream}\n"
                    "你可以结合这个梦境，自然地跟管理员提一下，"
                    "就像真人早上会说「我昨晚做了个奇怪的梦」一样，不用完整复述，"
                    "可以只说印象最深的片段，语气自然随意。"
                )
        elif clarity == "blurry":
            recall_prompt = self.cfg.get("dream_recall_blurry_prompt", "").strip()
            if not recall_prompt:
                recall_prompt = (
                    "你刚刚睡醒，隐约记得昨晚做了梦，但细节模糊了。"
                    "梦大概是关于：{dream}\n"
                    "你可以提一句，但不用说太详细，就说「好像梦到了什么，但记不太清了」之类的，"
                    "语气随意，可以有点困。"
                )
        else:  # feeling_only
            recall_prompt = self.cfg.get("dream_recall_feeling_prompt", "").strip()
            if not recall_prompt:
                recall_prompt = (
                    "你刚刚睡醒，梦已经记不住了，只剩下一点模糊的感觉。"
                    "梦的类型是{type}。"
                    "你可以说一句「做了个梦但完全记不住了」，或者说说醒来的感觉，不需要描述内容。"
                )

        recall_prompt = (
            recall_prompt
            .replace("{dream}", content)
            .replace("{type}", dtype)
        )
        await self._send_to_admin(recall_prompt)
        self._log("info", f"梦境浮现 clarity={clarity}")

    async def _llm_generate(self, prompt: str) -> str:
        """用 AstrBot 内置 LLM 能力生成文本（不进入用户对话流）"""
        try:
            # AstrBot context.get_llm_tool_use_handler() 或直接请求默认 provider
            resp = await self.context.llm_tools.text_chat(
                prompt=prompt,
                session_id="sleep_sense_dream_internal",
                image_urls=[],
            )
            return resp.completion_text.strip() if resp else ""
        except Exception:
            pass
        # 回退：用 context.get_using_provider
        try:
            provider = self.context.get_using_provider()
            if provider:
                resp = await provider.text_chat(
                    prompt=prompt,
                    session_id="sleep_sense_dream_internal",
                )
                return resp.completion_text.strip() if resp else ""
        except Exception as e:
            self._log("warn", f"LLM 生成梦境失败: {e}")
        return ""

    @filter.command("梦境")
    async def cmd_dream(self, event: AstrMessageEvent):
        """查看梦境记录。用法: /梦境 列表|今晚|清除"""
        uid = str(event.get_sender_id())
        is_admin = self._is_admin(uid)
        try:
            is_op = await self.context.is_admin(uid)
        except Exception:
            is_op = False
        if not (is_admin or is_op):
            yield event.plain_result("❌ 权限不足")
            return

        text = (event.message_str or "").strip()
        action = text.replace("/梦境", "").strip() or "列表"

        if action == "列表":
            if not self.dreams:
                yield event.plain_result("📖 暂无梦境记录")
                return
            lines = []
            for d in self.dreams[-5:]:
                c = {"clear": "记得清楚", "blurry": "有点模糊", "feeling_only": "只剩感觉", "forgotten": "完全忘了"}.get(d.get("clarity",""), "?")
                lines.append(f"📅 {d.get('date','')} [{d.get('type','')}] {c}")
            yield event.plain_result("最近5条梦境记录：\n" + "\n".join(lines))

        elif action == "今晚":
            dream = self.state.get("tonight_dream")
            if not dream:
                # 查最近一条
                if self.dreams:
                    d = self.dreams[-1]
                    if d.get("clarity") != "forgotten":
                        yield event.plain_result(
                            f"最近一次梦境（{d.get('date','')}）\n"
                            f"类型：{d.get('type','')}\n"
                            f"清晰度：{d.get('clarity','')}\n\n"
                            f"{d.get('content','')}"
                        )
                        return
                yield event.plain_result("今晚还没有梦境记录，或已经忘记了。")
            else:
                yield event.plain_result(
                    f"今晚的梦 [{dream.get('type','')}]\n"
                    f"清晰度：{dream.get('clarity','')}\n\n"
                    f"{dream.get('content','')}"
                )

        elif action == "清除":
            self.dreams = []
            self._save_json(DREAMS_PATH, self.dreams)
            self.state.pop("tonight_dream", None)
            self._save_state()
            yield event.plain_result("✅ 梦境记录已清除")

        else:
            yield event.plain_result("可用: 列表 / 今晚 / 清除")

    # ═══════════════════════════════════════════════════════════════════════
    # 睡眠周期
    # ═══════════════════════════════════════════════════════════════════════
    def _update_cycle(self):
        if not self.cfg.get("sleep_cycle_enabled", True):
            return
        start = self.state.get("sleep_start") or time.time()
        elapsed_min = (time.time() - start) / 60
        light = self.cfg.get("cycle_light_minutes", 30)
        deep = self.cfg.get("cycle_deep_minutes", 90)
        if elapsed_min < light:
            self.state["sleep_cycle_phase"] = "light"
        elif elapsed_min < deep:
            self.state["sleep_cycle_phase"] = "deep"
        else:
            self.state["sleep_cycle_phase"] = "normal"

    def _get_cycle_multiplier(self) -> float:
        if not self.cfg.get("sleep_cycle_enabled", True):
            return 1.0
        phase = self.state.get("sleep_cycle_phase", "normal")
        if phase == "light":
            return self.cfg.get("cycle_light_multiplier", 0.5)
        if phase == "deep":
            return self.cfg.get("cycle_deep_multiplier", 1.5)
        return 1.0

    def _get_cycle_prompt_prefix(self) -> str:
        if not self.cfg.get("sleep_cycle_enabled", True):
            return ""
        phase = self.state.get("sleep_cycle_phase", "normal")
        if phase == "light":
            return self.cfg.get("prompts_cycle_light", "") + " "
        if phase == "deep":
            return self.cfg.get("prompts_cycle_deep", "") + " "
        return ""

    # ═══════════════════════════════════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════════════════════════════════
    def _is_admin(self, uid: str) -> bool:
        if not self.cfg.get("admin_enabled", True):
            return False
        return str(uid) == str(self.cfg.get("admin_qq", ""))

    def _is_private_silent(self, thresh_sec: int = 300) -> bool:
        now = time.time()
        return all(now - ts >= thresh_sec for ts in self._last_others_ts.values())

    def _parse_time(self, s: str):
        from datetime import time as dtime
        try:
            h, m = map(int, s.split(":"))
            return dtime(h, m)
        except Exception:
            return dtime(23, 0)

    def _in_range(self, cur, start, end) -> bool:
        if start <= end:
            return start <= cur <= end
        return cur >= start or cur <= end

    def _get_day_sleep_time(self, now: datetime) -> str:
        if not self.cfg.get("schedule_enabled", True):
            return self.cfg.get("sleep_time", "23:00")
        keys = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        key = "schedule_" + keys[now.weekday()]
        val = self.cfg.get(key, "23:00,08:00")
        return val.split(",")[0].strip() if "," in val else self.cfg.get("sleep_time", "23:00")

    def _get_day_wake_time(self, now: datetime) -> str:
        if not self.cfg.get("schedule_enabled", True):
            return self.cfg.get("wake_time", "08:00")
        keys = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        key = "schedule_" + keys[now.weekday()]
        val = self.cfg.get(key, "23:00,08:00")
        parts = val.split(",")
        return parts[1].strip() if len(parts) > 1 else self.cfg.get("wake_time", "08:00")

    def _inject_prompt(self, event: AstrMessageEvent, prompt: str):
        """把提示词写入 event 的 system_prompt extra"""
        try:
            existing = event.get_extra("system_prompt") or ""
            event.set_extra("system_prompt", (existing + "\n\n" + prompt).strip())
        except Exception:
            pass

    async def _send_to_admin(self, content: str):
        admin_qq = self.cfg.get("admin_qq", "")
        if not admin_qq:
            return
        try:
            await self.context.send_message(f"aiocqhttp:FriendMessage:{admin_qq}", [
                {"type": "text", "data": {"text": content}}
            ])
        except Exception as e:
            self._log("warn", f"发送给管理员失败: {e}")

    async def _report_to_admin_later(self):
        await asyncio.sleep(30)
        p = "你根据现在对话历史，考虑要不要跟管理员说自己被吵醒了。可以问一下管理员睡了吗？结合吵醒原因自行决定。"
        await self._send_to_admin(p)

    # ── 持久化 ──────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        default = {
            "sleep_state": S.AWAKE,
            "sleep_start": None,
            "last_sleep_duration": 28800,
            "consecutive_overtime": 0,
            "weekly_overtime": 0,
            "nightmare_tonight": False,
            "sleep_cycle_phase": "normal",
            "custom_wake_time": None,
            "overtime_decided_today": False,
        }
        return self._load_json(STATE_PATH, default)

    def _save_state(self):
        self._save_json(STATE_PATH, self.state)

    def _load_json(self, path: Path, default):
        try:
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return default

    def _save_json(self, path: Path, data):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(path)  # 原子写入，避免死锁
        except Exception as e:
            logger.error(f"[sleep_sense] save_json error {path}: {e}")

    # ── 日志 ────────────────────────────────────────────────────────────────
    _log_level = "info"

    def _log(self, level: str, msg: str):
        levels = {"debug": 0, "info": 1, "warn": 2}
        if levels.get(level, 1) < levels.get(self._log_level, 1):
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}][{level.upper()}] {msg}\n"
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
        if level == "warn":
            logger.warning(f"[sleep_sense] {msg}")
        else:
            logger.info(f"[sleep_sense] {msg}")

    def _record_stat(self, key: str, val):
        week = datetime.now().strftime("%Y-W%W")
        p = STATS_DIR / f"{week}.json"
        data = self._load_json(p, {"records": []})
        data["records"].append({"key": key, "val": val, "ts": time.time()})
        self._save_json(p, data)

    # ── 插件卸载 ─────────────────────────────────────────────────────────────
    async def terminate(self):
        self._save_state()
        self._save_json(ALARMS_PATH, self.alarms)
        self._save_json(DREAMS_PATH, self.dreams)
        logger.info("[sleep_sense] 插件已卸载，状态已保存")
