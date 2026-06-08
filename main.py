"""
astrbot_plugin_sleep_sense
让 Bot 拥有真实睡眠感知的插件。
作者: 夕小柠  版本: 1.2.4
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

@register("sleep_sense", "夕小柠", "让 Bot 拥有真实睡眠感知", "1.2.4")
class SleepSensePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._ensure_dirs()
        self.cfg = self._load_config()
        self.prompts = self._load_prompts()
        self.state = self._load_state()
        self.alarms = self._load_alarms()
        self._lock = asyncio.Lock()
        
        # 启动后台任务
        asyncio.create_task(self._scheduler_loop())
        asyncio.create_task(self._alarm_loop())
        
        # 兼容性注册 WebUI
        if hasattr(self.context, "register_webui"):
            try:
                self.context.register_webui("sleep_sense_config", self._handle_webui_request)
                logger.info("[sleep_sense] WebUI 注册成功")
            except Exception as e:
                logger.warn(f"[sleep_sense] WebUI 注册失败: {e}")
        else:
            logger.warn("[sleep_sense] 当前 AstrBot 版本不支持 register_webui，请通过配置文件手动修改。")
            
        logger.info("[sleep_sense] 插件 v1.2.4 已成功加载")

    def _ensure_dirs(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "stats").mkdir(parents=True, exist_ok=True)

    def _load_config(self):
        if not CONFIG_PATH.exists(): self._write_default_config()
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except: return {}

    def _load_prompts(self):
        if not PROMPTS_PATH.exists(): self._write_default_prompts()
        try:
            with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except: return {}

    def _load_state(self):
        if not STATE_PATH.exists(): return {"sleep_state": SleepState.AWAKE}
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except: return {"sleep_state": SleepState.AWAKE}

    def _load_alarms(self):
        if not ALARMS_PATH.exists(): return []
        try:
            with open(ALARMS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except: return []

    async def _handle_webui_request(self, request):
        if request.method == "GET":
            html_path = Path(__file__).parent / "webui_config.html"
            with open(html_path, "r", encoding="utf-8") as f: html = f.read()
            init_data = {"config": self.cfg, "prompts": self.prompts, "state": self.state}
            return html.replace("const INITIAL_DATA = null;", f"const INITIAL_DATA = {json.dumps(init_data, ensure_ascii=False)};")
        elif request.method == "POST":
            try:
                data = await request.json()
                if data.get("action") == "save_config":
                    if "config" in data:
                        self.cfg = data["config"]
                        with open(CONFIG_PATH, "w", encoding="utf-8") as f: yaml.dump(self.cfg, f, allow_unicode=True)
                    if "prompts" in data:
                        self.prompts = data["prompts"]
                        with open(PROMPTS_PATH, "w", encoding="utf-8") as f: yaml.dump(self.prompts, f, allow_unicode=True)
                    return {"status": "success"}
            except Exception as e: return {"status": "error", "message": str(e)}
        return {"status": "error"}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # 完整核心逻辑...
        pass

    def _write_default_config(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("master: {enabled: true, qq: '1591793025', name: '熙熙'}")
    def _write_default_prompts(self):
        with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
            f.write("lazy: {inject: '晚安。'}")
    async def _scheduler_loop(self):
        while True: await asyncio.sleep(60)
    async def _alarm_loop(self):
        while True: await asyncio.sleep(30)
