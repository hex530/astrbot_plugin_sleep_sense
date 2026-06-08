"""
astrbot_plugin_sleep_sense
让 Bot 拥有真实睡眠感知的插件。
作者: 夕小柠  版本: 1.2.0
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

@register("sleep_sense", "夕小柠", "让 Bot 拥有真实睡眠感知", "1.2.0")
class SleepSensePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._ensure_dirs()
        self.cfg = self._load_config()
        self.prompts = self._load_prompts()
        self.state = self._load_state()
        
        # 注册 WebUI
        self.context.register_webui("sleep_sense_config", self._handle_webui_request)
        logger.info("[sleep_sense] 插件 v1.2.0 已加载")

    def _ensure_dirs(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _load_config(self):
        if not CONFIG_PATH.exists(): return {}
        with open(CONFIG_PATH, encoding="utf-8") as f: return yaml.safe_load(f)

    def _load_prompts(self):
        if not PROMPTS_PATH.exists(): return {}
        with open(PROMPTS_PATH, encoding="utf-8") as f: return yaml.safe_load(f)

    def _load_state(self):
        if not STATE_PATH.exists(): return {"sleep_state": "awake"}
        with open(STATE_PATH, encoding="utf-8") as f: return json.load(f)

    async def _handle_webui_request(self, request):
        if request.method == "GET":
            # 读取 HTML 并注入初始数据
            html_path = Path(__file__).parent / "webui_config.html"
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
            
            # 注入数据到全局变量
            init_data = {
                "config": self.cfg,
                "prompts": self.prompts,
                "state": self.state
            }
            html = html.replace("const INITIAL_DATA = null;", f"const INITIAL_DATA = {json.dumps(init_data, ensure_ascii=False)};")
            return html

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
                    return {"status": "success", "message": "保存成功"}
            except Exception as e:
                return {"status": "error", "message": str(e)}
        return {"status": "error", "message": "Invalid request"}

    @filter.command("睡眠")
    async def cmd_sleep(self, event: AstrMessageEvent):
        yield event.plain_result("睡眠插件运行中，请通过 WebUI 配置。")

    async def _scheduler_loop(self): pass
    async def _alarm_loop(self): pass
    async def _log_cleaner(self): pass
