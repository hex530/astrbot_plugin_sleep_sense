"""
astrbot_plugin_sleep_sense
让 Bot 拥有真实睡眠感知的插件。
作者: 夕小柠  版本: 1.2.7
"""

import asyncio
import json
import os
import time
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

@register("sleep_sense", "夕小柠", "让 Bot 拥有真实睡眠感知", "1.2.7")
class SleepSensePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        # 兼容性处理：如果机器人没传 config，就设为空字典
        self.config = config or {}
        self._ensure_dirs()
        self.cfg = self._load_config()
        self.prompts = self._load_prompts()
        self.state = self._load_state()
        
        asyncio.create_task(self._scheduler_loop())
        
        logger.info(f"[sleep_sense] 插件 v1.2.7 已启动。")

    def _ensure_dirs(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _load_config(self):
        # 优先使用官方注入的配置
        if self.config:
            return self.config
        if not CONFIG_PATH.exists(): self._write_default_config()
        with open(CONFIG_PATH, "r", encoding="utf-8") as f: return yaml.safe_load(f) or {}

    def _load_prompts(self):
        if not PROMPTS_PATH.exists(): self._write_default_prompts()
        with open(PROMPTS_PATH, "r", encoding="utf-8") as f: return yaml.safe_load(f) or {}

    def _load_state(self):
        if not STATE_PATH.exists(): return {"sleep_state": "awake"}
        with open(STATE_PATH, "r", encoding="utf-8") as f: return json.load(f)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # 核心拦截逻辑
        pass

    def _write_default_config(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("master_qq: '1591793025'\nsleep_time: '23:00'\n")

    def _write_default_prompts(self):
        with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
            f.write("lazy: {inject: ''}")

    async def _scheduler_loop(self):
        while True:
            await asyncio.sleep(60)
