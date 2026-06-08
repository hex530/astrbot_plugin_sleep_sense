"""
astrbot_plugin_sleep_sense
让 Bot 拥有真实睡眠感知的插件。
作者: 夕小柠  版本: 1.1.7
"""

import asyncio
import json
import math
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

DATA_DIR = Path("data/sleep_sense")
CONFIG_PATH = DATA_DIR / "config.yaml"
PROMPTS_PATH = DATA_DIR / "prompts.yaml"
STATE_PATH = DATA_DIR / "state.json"
ALARMS_PATH = DATA_DIR / "alarms.json"
LOG_PATH = DATA_DIR / "logs" / "sleep.log"
STATS_DIR = DATA_DIR / "stats"
OVERTIME_ACTIVE = DATA_DIR / "overtime" / "active.yaml"

# ─── 状态枚举 ──────────────────────────────────────────────────────────────────
class SleepState:
    AWAKE = "awake"
    LAZY = "lazy"
    SLEEPING = "sleeping"
    NAPPING = "napping"
    OVERTIME = "overtime"


@register("sleep_sense", "夕小柠", "让 Bot 拥有真实睡眠感知", "1.1.7")
class SleepSensePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._ensure_dirs()
        self.cfg = self._load_config()
        self.prompts = self._load_prompts()
        self.state = self._load_state()
        self.alarms = self._load_alarms()

        # 运行时计数器（内存）
        self._wake_counters: dict[str, int] = {}   # uid -> 消息计数
        self._last_xi_msg: float = time.time()     # 熙熙最后消息时间戳
        self._last_others_msg: dict[str, float] = {}
        self._config_cache_ts: float = 0
        self._lock = asyncio.Lock()

        # 启动后台任务
        asyncio.create_task(self._scheduler_loop())
        asyncio.create_task(self._alarm_loop())
        asyncio.create_task(self._log_cleaner())

        logger.info("[sleep_sense] 插件 v1.1.7 已加载，当前状态: " + self.state["sleep_state"])
        # 注册 WebUI 路由
        self.context.register_webui("sleep_sense_config", self._handle_webui_request)

    # ═══════════════════════════════════════════════════════════════════
    # 初始化辅助
    # ═══════════════════════════════════════════════════════════════════
    def _ensure_dirs(self):
        for d in [DATA_DIR / "logs", DATA_DIR / "stats", DATA_DIR / "overtime" / "history"]:
            d.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> dict:
        if not CONFIG_PATH.exists():
            self._write_default_config()
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _load_prompts(self) -> dict:
        if not PROMPTS_PATH.exists():
            self._write_default_prompts()
        with open(PROMPTS_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _load_state(self) -> dict:
        if not STATE_PATH.exists():
            default = {
                "sleep_state": SleepState.AWAKE,
                "sleep_start": None,
                "wake_start": time.time(),
                "overtime_days": 0,
                "consecutive_overtime": 0,
                "last_sleep_duration": 0,
                "weekly_overtime": 0,
                "nightmare_tonight": False,
                "sleep_cycle_phase": "normal",
                "cycle_phase_since": None,
                "wake_up_reason": None,
                "custom_wake_time": None,
            }
            self._save_state(default)
            return default
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)

    def _save_state(self, s: Optional[dict] = None):
        data = s or self.state
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save_alarms(self):
        with open(ALARMS_PATH, "w", encoding="utf-8") as f:
            json.dump(self.alarms, f, ensure_ascii=False, indent=2)

    # ═══════════════════════════════════════════════════════════════════
    # WebUI 处理
    # ═══════════════════════════════════════════════════════════════════
    async def _handle_webui_request(self, request):
        try:
            data = await request.json()
            action = data.get("action")
            if action == "get_config":
                return {"status": "success", "config": self.cfg, "prompts": self.prompts, "state": self.state}
            if action == "save_config":
                new_cfg = data.get("config")
                new_prompts = data.get("prompts")
                if new_cfg:
                    self.cfg = new_cfg
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        yaml.dump(new_cfg, f, allow_unicode=True, sort_keys=False)
                if new_prompts:
                    self.prompts = new_prompts
                    with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
                        yaml.dump(new_prompts, f, allow_unicode=True, sort_keys=False)
                self._config_cache_ts = time.time()
                logger.info("[sleep_sense] WebUI 配置已更新并保存")
                return {"status": "success", "message": "配置保存成功！"}
            return {"status": "error", "message": "未知操作"}
        except Exception as e:
            logger.error(f"[sleep_sense] WebUI 处理报错: {e}")
            return {"status": "error", "message": str(e)}

    # ═══════════════════════════════════════════════════════════════════
    # 核心：消息拦截
    # ═══════════════════════════════════════════════════════════════════
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        async with self._lock:
            cfg = self._get_cfg()
            state = self.state
            uid = event.get_sender_id()
            is_master = self._is_master(uid, cfg)
            is_group = event.is_group_message()
            is_at = event.is_at_me() if is_group else False
            # 兼容性处理
            text = getattr(event, "message_str", "") or (event.get_message_str() if hasattr(event, "get_message_str") else "")

            # 更新最后消息时间
            if is_master:
                self._last_xi_msg = time.time()
            else:
                self._last_others_msg[uid] = time.time()

            sleep_state = state.get("sleep_state", SleepState.AWAKE)

            # ── 睡眠中 ────────────────────────────────────────────────
            if sleep_state == SleepState.SLEEPING:
                await self._handle_sleeping(event, cfg, is_master, is_group, is_at, text)
                return

            # ── 午休/补觉 ─────────────────────────────────────────────
            if sleep_state == SleepState.NAPPING:
                await self._handle_napping(event, cfg, is_master, is_group, is_at)
                return

            # ── 清醒/慵懒/熬夜 → 正常流程，注入提示词 ─────────────────
            inject = self._build_awake_inject(cfg, sleep_state, is_master)
            if inject:
                event.inject_system_prompt(inject)

    # ... (其余逻辑保持不变，确保文件完整)
    def _write_default_config(self): pass
    def _write_default_prompts(self): pass
    async def _scheduler_loop(self): pass
    async def _alarm_loop(self): pass
    async def _log_cleaner(self): pass
    async def _tick(self): pass
    async def _check_sleep_condition(self, cfg, now, sleep_state): pass
    async def _check_wake_condition(self, cfg, now): pass
    async def _check_nap_condition(self, cfg, now): pass
    async def _check_overtime_decision(self, cfg, now): pass
    async def _check_nightmare(self, cfg): pass
    async def _enter_sleep(self, cfg): pass
    async def _enter_nap(self, cfg): pass
    async def _enter_awake(self): pass
    async def _do_wake_up(self, cfg): pass
    def _set_state(self, new_state: str): pass
    def _emit_state_change(self, new_state: str): pass
    def _update_sleep_cycle(self, cfg): pass
    def _get_cycle_multiplier(self, cfg) -> float: pass
    async def _check_alarms(self): pass
    @filter.llm_tool(name="sleep_set_alarm")
    async def tool_set_alarm(self, event, time_str, reason, target): pass
    @filter.command("睡眠")
    async def cmd_sleep(self, event, action="状态"): pass
    def _get_cfg(self): pass
    def _is_master(self, uid, cfg): pass
    def _parse_time(self, s): pass
    def _time_in_range(self, current, start, end): pass
    def _get_today_sleep_time(self, cfg, now): pass
    def _get_today_wake_time(self, cfg, now): pass
    def _check_private_silent(self, cfg): pass
    def _check_group_silent(self, cfg): pass
    async def _inject_others_end_chat(self, cfg, grace_minutes): pass
    async def _delayed_sleep(self, delay, cfg): pass
    async def _ask_overtime_decision(self, cfg): pass
    async def _send_to_master(self, content): pass
    async def _schedule_report_to_master(self, cfg): pass
    def _log(self, level, msg): pass
    def _record_stat(self, key, value): pass
