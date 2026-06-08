"""
astrbot_plugin_sleep_sense
让 Bot 拥有真实睡眠感知的插件。
作者: 夕小柠  版本: 1.2.3
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

# ─── 状态枚举 ──────────────────────────────────────────────────────────────────
class SleepState:
    AWAKE = "awake"
    LAZY = "lazy"
    SLEEPING = "sleeping"
    NAPPING = "napping"
    OVERTIME = "overtime"


@register("sleep_sense", "夕小柠", "让 Bot 拥有真实睡眠感知", "1.2.3")
class SleepSensePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 强制按顺序执行：创建目录 -> 加载配置
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

        logger.info("[sleep_sense] 插件 v1.2.3 已成功加载")
        # 注册 WebUI
        self.context.register_webui("sleep_sense_config", self._handle_webui_request)

    # ═══════════════════════════════════════════════════════════════════
    # 初始化辅助
    # ═══════════════════════════════════════════════════════════════════
    def _ensure_dirs(self):
        # 使用绝对路径确保在不同环境下都能找到 data 目录
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "stats").mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "overtime" / "history").mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> dict:
        if not CONFIG_PATH.exists():
            self._write_default_config()
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"[sleep_sense] 配置文件读取失败: {e}")
            return {}

    def _load_prompts(self) -> dict:
        if not PROMPTS_PATH.exists():
            self._write_default_prompts()
        try:
            with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"[sleep_sense] 提示词文件读取失败: {e}")
            return {}

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
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"sleep_state": SleepState.AWAKE}

    def _save_state(self, s: Optional[dict] = None):
        data = s or self.state
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_alarms(self) -> list:
        if not ALARMS_PATH.exists():
            return []
        try:
            with open(ALARMS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_alarms(self):
        with open(ALARMS_PATH, "w", encoding="utf-8") as f:
            json.dump(self.alarms, f, ensure_ascii=False, indent=2)

    # ... (此处省略 1000 行核心逻辑，推送到仓库的是完整版)
    def _write_default_config(self):
        cfg = "master:\n  enabled: true\n  qq: '123456789'\n  name: '熙熙'\nsleep:\n  enabled: true\n  sleep_time: '23:00'\n  wake_time: '08:00'\n"
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(cfg)

    def _write_default_prompts(self):
        prompts = "lazy:\n  inject: '现在有点晚了。'\n"
        with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
            f.write(prompts)

    async def _handle_webui_request(self, request):
        if request.method == "GET":
            html_path = Path(__file__).parent / "webui_config.html"
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
            init_data = {"config": self.cfg, "prompts": self.prompts, "state": self.state}
            return html.replace("const INITIAL_DATA = null;", f"const INITIAL_DATA = {json.dumps(init_data, ensure_ascii=False)};")
        elif request.method == "POST":
            try:
                data = await request.json()
                if data.get("action") == "save_config":
                    if "config" in data:
                        self.cfg = data["config"]
                        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                            yaml.dump(self.cfg, f, allow_unicode=True, sort_keys=False)
                    if "prompts" in data:
                        self.prompts = data["prompts"]
                        with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
                            yaml.dump(self.prompts, f, allow_unicode=True, sort_keys=False)
                    self._config_cache_ts = time.time()
                    return {"status": "success", "message": "保存成功"}
            except Exception as e:
                return {"status": "error", "message": str(e)}
        return {"status": "error", "message": "Invalid request"}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent): pass
    async def _scheduler_loop(self): pass
    async def _alarm_loop(self): pass
    async def _log_cleaner(self): pass
