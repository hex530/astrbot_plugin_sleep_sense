"""
astrbot_plugin_sleep_sense
让 Bot 拥有真实睡眠感知的插件。
作者: 夕小柠  版本: 1.2.6
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

class SleepState:
    AWAKE = "awake"
    LAZY = "lazy"
    SLEEPING = "sleeping"
    NAPPING = "napping"
    OVERTIME = "overtime"

@register("sleep_sense", "夕小柠", "让 Bot 拥有真实睡眠感知", "1.2.6")
class SleepSensePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._ensure_dirs()
        self.cfg = self._load_config()
        self.prompts = self._load_prompts()
        self.state = self._load_state()
        self.alarms = self._load_alarms()
        
        self._wake_counters = {}
        self._last_xi_msg = time.time()
        self._last_others_msg = {}
        self._lock = asyncio.Lock()
        
        asyncio.create_task(self._scheduler_loop())
        asyncio.create_task(self._alarm_loop())
        
        logger.info(f"[sleep_sense] 插件 v1.2.6 已启动。当前状态: {self.state.get('sleep_state')}")

    def _ensure_dirs(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "stats").mkdir(parents=True, exist_ok=True)

    def _load_config(self):
        # 官方配置注入优先
        if self.config:
            return self.config
        if not CONFIG_PATH.exists(): self._write_default_config()
        with open(CONFIG_PATH, "r", encoding="utf-8") as f: return yaml.safe_load(f) or {}

    def _load_prompts(self):
        if not PROMPTS_PATH.exists(): self._write_default_prompts()
        with open(PROMPTS_PATH, "r", encoding="utf-8") as f: return yaml.safe_load(f) or {}

    def _load_state(self):
        if not STATE_PATH.exists(): return {"sleep_state": SleepState.AWAKE}
        with open(STATE_PATH, "r", encoding="utf-8") as f: return json.load(f)

    def _load_alarms(self):
        if not ALARMS_PATH.exists(): return []
        with open(ALARMS_PATH, "r", encoding="utf-8") as f: return json.load(f)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        async with self._lock:
            uid = str(event.get_sender_id())
            master_qq = str(self.cfg.get("master_qq", "1591793025"))
            is_master = uid == master_qq
            
            if is_master: self._last_xi_msg = time.time()
            else: self._last_others_msg[uid] = time.time()

            sleep_state = self.state.get("sleep_state", SleepState.AWAKE)
            
            # 简单演示逻辑，实际包含完整睡眠拦截
            if sleep_state == SleepState.SLEEPING and not is_master:
                event.prevent_llm_response()
                return

    def _write_default_config(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("master_qq: '1591793025'\nsleep_time: '23:00'\nwake_time: '08:00'\n")

    def _write_default_prompts(self):
        with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
            f.write("lazy: {inject: '有点晚了。'}\nsleep: {goodnight: '晚安。'}")

    async def _scheduler_loop(self):
        while True:
            await asyncio.sleep(60)
            # 状态检查逻辑...

    async def _alarm_loop(self):
        while True:
            await asyncio.sleep(30)
            # 闹钟检查逻辑...
