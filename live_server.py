"""Local-only real-time observer for the existing Project Eos simulation."""
from __future__ import annotations

import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timezone
from urllib import error, request
from urllib.parse import parse_qs, urlparse
import webbrowser

from agents.heuristic import HeuristicAgent
from agents.llm_adapter import (
    BudgetExceeded,
    CallBudget,
    LLMSettings,
    OpenAICompatibleAgent,
    load_agents,
)
from agents.personality import normalize_personality, personality_definition, personality_label
from agents.providers import provider_payload, resolve_simple_settings
from agents.scheduler import AgentScheduler
from core.messages import compose_ruler_messages, record_ruler_messages
from core.actions import ActionRejected, validate_action
from core.military import Military
from core.simulation import Simulation

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_HTML = BASE_DIR / "dashboard.html"
SAVE_DIR = BASE_DIR / "runs" / "saves"
ASSETS_DIR = BASE_DIR / "assets"
COUNTRY_COLORS = {
    "ELAR": "#e7b45f",
    "MARIS": "#63b1c9",
    "VAYR": "#d97f75",
    "SAHAR": "#b98bcb",
}
COUNTRY_NAMES = {
    "ELAR": "埃兰河谷城邦联盟",
    "MARIS": "玛瑞斯海湾城邦",
    "VAYR": "维尔山麓诸侯",
    "SAHAR": "萨赫尔河岸王国",
}
FALLBACK_PALETTE = [
    "#e7b45f", "#63b1c9", "#d97f75", "#b98bcb",
    "#8fbf7f", "#d6a36f", "#7f9ed6", "#c98fae",
    "#9fc9b4", "#c4b56f", "#aa8fd1", "#d68a8a",
]


def load_key_file(path: Path) -> None:
    """Inject one local key into this process without printing or persisting it."""
    text = path.read_text(encoding="utf-8-sig").strip()
    keys = re.findall(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}", text)
    if not keys and re.fullmatch(r"[A-Za-z0-9_-]{16,}", text):
        keys = [text]
    if len(keys) != 1:
        raise RuntimeError("API key file must contain exactly one key")
    os.environ.setdefault("EOS_API_KEY", keys[0])


def project_world(world_data: dict) -> dict:
    """Compact tile table the canvas draws, shared by the live map and the painter."""
    metadata = world_data["metadata"]
    polities = []
    for index, (country_id, polity) in enumerate(
            {polity["id"]: polity for polity in world_data["polities"]}.items()):
        polities.append({
            "id": country_id,
            "name": polity["name"],
            "color": (
                polity.get("color")
                or COUNTRY_COLORS.get(country_id)
                or FALLBACK_PALETTE[index % len(FALLBACK_PALETTE)]
            ),
            "archetype": polity.get("type"),
            "capital": polity["capital"],
        })
    tiles = [
        [
            tile["col"], tile["row"], 1 if tile["land"] else 0,
            tile["biome"], tile["owner_id"] or "", 1 if tile["river"] else 0,
            [tile["downstream"]["col"], tile["downstream"]["row"]]
            if tile.get("downstream") else None,
            tile["elevation_m"], tile["annual_precip_mm"],
            tile["annual_mean_temp_c"], round(tile.get("slope", 0), 4),
        ]
        for tile in world_data["tiles"]
    ]
    return {
        "metadata": {
            "grid_width": metadata["grid_width"],
            "grid_height": metadata["grid_height"],
            "cell_area_km2": metadata["cell_area_km2"],
            "hex_radius_km": metadata["hex_radius_km"],
            "schema_version": metadata.get("schema_version", 1),
            "generator": metadata.get("generator", "mapgen_v1"),
            "river_depth_model": metadata.get("river_depth_model"),
            "seed": metadata.get("seed"),
            "terrain_texture_url": (
                f"/api/terrain?seed={metadata.get('seed')}"
                if metadata.get("terrain_texture") else None
            ),
        },
        "polities": polities,
        "tiles": tiles,
        "river_cells": world_data.get("river_cells", []),
        "road_cells": world_data.get("road_cells", []),
        "rendering": world_data.get("rendering", {}),
    }


class LiveSession:
    def __init__(
        self,
        speed: float = 4.0,
        maximum_day: int = 365,
        paused: bool = False,
        scheduler: AgentScheduler | None = None,
        budget: CallBudget | None = None,
        agent_meta: dict | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.scheduler = scheduler
        self.budget = budget or CallBudget()
        self.agent_meta = agent_meta or {}
        from core.state import DEFAULT_RULES

        # The painter prices marches with the same rules table the engine will load, so the
        # draft never needs a running simulation to answer "how many days is that?".
        self.rules = json.loads(DEFAULT_RULES.read_text(encoding="utf-8"))
        self.player_country: str | None = None
        self.current_save_id: str | None = None
        self.current_save_name: str | None = None
        self.simulation = self._new_simulation()
        self._sync_personality_meta()
        self._message_cursor = len(self.simulation.state.event_log)
        self._last_message_days: dict[str, int] = {}
        self.speed = speed
        self.maximum_day = maximum_day
        self.paused = paused
        self.finished = False
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _new_simulation(self) -> Simulation:
        if self.scheduler is None:
            simulation = Simulation.new(use_mock_agents=True)
            self.scheduler = simulation.scheduler
        else:
            simulation = Simulation.new(use_mock_agents=True)
            simulation.scheduler = self.scheduler
            simulation.engine.planner = self.scheduler
        self.scheduler.set_player_country(self.player_country)
        return simulation

    def _emit_ruler_messages(self) -> None:
        state = self.simulation.state
        start = self._message_cursor
        self._message_cursor = len(state.event_log)
        if start >= len(state.event_log):
            return
        messages = compose_ruler_messages(
            state,
            state.event_log[start:],
            self._last_message_days,
            self.player_country,
        )
        record_ruler_messages(state, messages)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            should_save = False
            with self.lock:
                if not self.paused and self.simulation.state.day_index < self.maximum_day:
                    self.simulation.run(1, force=True)
                    self._emit_ruler_messages()
                    day = self.simulation.state.day_index
                    if not self.simulation.state.victory_announced():
                        victory = self.simulation.state.victory_status()
                        if victory["winner"]:
                            self.simulation.state.record(
                                "victory",
                                country_id=victory["winner"],
                                condition=victory["condition"],
                                label=victory["label"],
                            )
                            self.paused = True
                            self.finished = True
                            should_save = True
                    if day >= self.maximum_day:
                        self.paused = True
                        self.finished = True
                        should_save = True
                    elif day % 30 == 0:
                        should_save = True
                speed = self.speed
            if should_save:
                self.save_current()
            self._stop.wait(max(0.03, 1.0 / speed))

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(timeout=2)
        self.save_current()

    def _save_meta_path(self, slot_id: str) -> Path:
        return SAVE_DIR / f"{slot_id}.meta.json"

    def save_current(self, name: str | None = None) -> dict:
        with self.lock:
            SAVE_DIR.mkdir(parents=True, exist_ok=True)
            if self.current_save_id is None:
                self.current_save_id = f"save_{int(time.time() * 1000)}_{self.simulation.state.seed}"
            if name:
                self.current_save_name = name[:50]
            if not self.current_save_name:
                self.current_save_name = f"{self.simulation.state.date['label']} 的世界"
            path = SAVE_DIR / f"{self.current_save_id}.json"
            self.simulation.state.save(path)
            meta = {
                "id": self.current_save_id,
                "name": self.current_save_name,
                "day": self.simulation.state.day_index,
                "date": self.simulation.state.date["label"],
                "seed": self.simulation.state.seed,
                "start_year": self.simulation.state.start_astronomical_year,
                "era": self.simulation.state.summary()["era"]["name"],
                "countries": {
                    key: value.name for key, value in self.simulation.state.countries.items()
                },
                "player_country": self.player_country,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "file": path.name,
            }
            self._save_meta_path(self.current_save_id).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return meta

    def list_saves(self) -> list[dict]:
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        saves = []
        for meta_path in SAVE_DIR.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (SAVE_DIR / meta.get("file", "")).exists():
                saves.append(meta)
        saves.sort(key=lambda item: item.get("saved_at", ""), reverse=True)
        return saves

    def load_game(self, slot_id: str) -> dict:
        with self.lock:
            meta_path = self._save_meta_path(slot_id)
            if not meta_path.exists():
                return {"ok": False, "error": "存档不存在"}
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            save_path = SAVE_DIR / meta["file"]
            try:
                simulation = Simulation.load(save_path, use_mock_agents=True)
            except Exception as exc:
                return {"ok": False, "error": f"存档加载失败：{type(exc).__name__}"}
            preserved_agents = dict(self.scheduler.agents) if self.scheduler else {}
            preserved_meta = dict(self.agent_meta)
            self.simulation = simulation
            # Keep the configured LLM seats across loads; a save must not silently
            # downgrade a model-controlled country to the heuristic controller.
            simulation.scheduler = AgentScheduler({
                country_id: agent
                for country_id, agent in preserved_agents.items()
                if country_id in simulation.state.countries
            })
            simulation.engine.planner = simulation.scheduler
            self.scheduler = simulation.scheduler
            self.agent_meta = {
                country_id: preserved_meta.get(
                    country_id,
                    {"mode": "mock", "provider": None, "model": None, "api_key_env": None},
                )
                for country_id in simulation.state.countries
            }
            self._sync_personality_meta()
            restored_player = meta.get("player_country")
            self.player_country = (
                restored_player
                if restored_player in simulation.state.countries
                else None
            )
            self.scheduler.set_player_country(self.player_country)
            self.paused = True
            self.finished = False
            self.maximum_day = max(simulation.state.day_index + 365, simulation.state.day_index + 1)
            self.current_save_id = slot_id
            self.current_save_name = meta.get("name")
            return {"ok": True, "save": meta}

    def delete_save(self, slot_id: str) -> dict:
        with self.lock:
            meta_path = self._save_meta_path(slot_id)
            if not meta_path.exists():
                return {"ok": False, "error": "存档不存在"}
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            (SAVE_DIR / meta["file"]).unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            if slot_id == self.current_save_id:
                self.current_save_id = None
                self.current_save_name = None
            return {"ok": True}

    def control(self, action: str, value: object = None) -> dict:
        with self.lock:
            if action == "pause":
                self.paused = True
            elif action == "resume":
                if self.finished:
                    return {"ok": False, "error": "本轮已完成，请使用单步继续观察"}
                self.paused = False
            elif action == "step":
                self.simulation.step(1)
                self._emit_ruler_messages()
                if self.simulation.state.day_index >= self.maximum_day:
                    self.finished = True
                    self.paused = True
            elif action == "speed":
                speed = float(value)
                if not 0.25 <= speed <= 64:
                    return {"ok": False, "error": "速度必须在 0.25 到 64 日/秒"}
                self.speed = speed
            elif action == "take_control":
                if value not in self.simulation.state.countries:
                    return {"ok": False, "error": "unknown country"}
                self.player_country = str(value)
                self.scheduler.set_player_country(self.player_country)
                for city in self.simulation.state.countries[self.player_country].cities:
                    city.managed_by = "manual"
                self.paused = True
                self.simulation.state.record(
                    "control_changed", country_id=self.player_country, mode="player"
                )
            elif action == "observe":
                seat = self.simulation.state.countries.get(self.player_country or "")
                for city in (seat.cities if seat is not None else []):
                    if city.managed_by == "manual":
                        city.managed_by = "ai"
                self.player_country = None
                self.scheduler.set_player_country(None)
                self.simulation.state.record("control_changed", country_id=None, mode="observer")
            elif action == "extend_round":
                self.maximum_day += 365
                self.finished = False
                self.paused = True
            elif action == "reset":
                paused = self.paused
                self.simulation = self._new_simulation()
                self._message_cursor = len(self.simulation.state.event_log)
                self._last_message_days = {}
                self.paused = paused
                self.finished = False
            else:
                return {"ok": False, "error": "unknown control"}
            return {
                "ok": True,
                "paused": self.paused,
                "speed": self.speed,
                "player_country": self.player_country,
            }

    def start_new_game(
        self,
        countries: list[dict] | None,
        base_seed: int | None,
        save_name: str | None = None,
        start_astronomical_year: int = -799,
        draft: dict | None = None,
    ) -> dict:
        from mapgen import generate_fair_world, normalize_countries

        with self.lock:
            if draft is not None:
                from mapgen_custom import build_world_from_draft, write_draft_world

                world = build_world_from_draft(draft, seed=base_seed)
                path = write_draft_world(world)
            else:
                normalized = normalize_countries(countries)
                world, path = generate_fair_world(
                    normalized, base_seed=base_seed, generation_version="v2"
                )
            simulation = Simulation.new(world_path=path, seed=world["metadata"]["seed"])
            start_era = simulation.state.configure_starting_era(start_astronomical_year)
            self.simulation = simulation
            self._message_cursor = len(simulation.state.event_log)
            self._last_message_days = {}
            self.scheduler = simulation.scheduler
            self.agent_meta = {
                country_id: {
                    "mode": "mock", "provider": None, "model": None,
                    "api_key_env": None,
                    "personality": normalize_personality(
                        simulation.state.countries[country_id].personality
                    ),
                }
                for country_id in (p["id"] for p in world["polities"])
            }
            self.scheduler.set_player_country(None)
            self.player_country = None
            self.paused = True
            self.finished = False
            self.maximum_day = 365
            self._message_cursor = len(simulation.state.event_log)
            self._last_message_days = {}
            self.current_save_id = None
            self.current_save_name = None
            simulation.state.record(
                "world_generated",
                seed=world["metadata"]["seed"],
                fairness=world["metadata"]["fairness"]["score"],
            )
            draft_name = world["metadata"].get("draft_name")
            default_name = save_name or (
                f"手绘世界 {draft_name} {world['metadata']['seed']}" if draft_name
                else f"新世界 {world['metadata']['seed']}"
            )
            save = self.save_current(default_name)
            return {
                "ok": True,
                "seed": world["metadata"]["seed"],
                "generator": world["metadata"].get("generator"),
                "fairness": world["metadata"]["fairness"],
                "countries": {p["id"]: p["name"] for p in world["polities"]},
                "path": str(path),
                "save": save,
                "start_year": start_astronomical_year,
                "start_era": start_era,
            }

    def _sync_personality_meta(self) -> None:
        for country_id, country in self.simulation.state.countries.items():
            meta = self.agent_meta.setdefault(
                country_id,
                {"mode": "mock", "provider": None, "model": None, "api_key_env": None},
            )
            meta["personality"] = normalize_personality(
                getattr(country, "personality", meta.get("personality"))
            )
            country.personality = meta["personality"]

    def agent_payload(self) -> dict:
        with self.lock:
            countries = {}
            for country_id, meta in self.agent_meta.items():
                env_name = meta.get("api_key_env") or f"EOS_AGENT_{country_id}_API_KEY"
                if meta.get("provider_id"):
                    configured = bool(os.environ.get(env_name))
                else:
                    configured = bool(
                        os.environ.get(env_name) or os.environ.get("EOS_API_KEY")
                    )
                last_call = next(
                    (
                        event
                        for event in reversed(self.simulation.state.event_log)
                        if event["type"] == "llm_call"
                        and event.get("country_id") == country_id
                        and event.get("sequence", 0)
                        > meta.get("configured_sequence", 0)
                    ),
                    None,
                )
                realm = self.simulation.state.countries.get(country_id)
                personality = (
                    getattr(realm, "personality", None)
                    or meta.get("personality")
                    or "neutral"
                )
                countries[country_id] = {
                    **meta,
                    "eliminated": country_id in self.simulation.state.eliminated,
                    "personality": normalize_personality(personality),
                    "personality_label": personality_label(personality),
                    "api_key_env": env_name,
                    "key_configured": (
                        meta.get("mode") == "openai_compatible" and configured
                    ),
                    "last_call": (
                        {
                            "status": last_call.get("status"),
                            "error_type": last_call.get("error_type"),
                            "http_status": last_call.get("http_status"),
                            "day": last_call.get("day"),
                            "date": last_call.get("date"),
                        }
                        if last_call else None
                    ),
                }
            return {
                "countries": countries,
                "providers": provider_payload(),
                "budget": self.budget.summary(),
                "player_country": self.player_country,
            }

    def diary_payload(self, country_id: str | None = None, limit: int = 80) -> dict:
        with self.lock:
            entries = [
                deepcopy(event)
                for event in self.simulation.state.event_log
                if event["type"] in {"llm_diary", "ruler_message"}
                and (country_id is None or event.get("country_id") == country_id)
            ][-limit:]
            return {
                "entries": entries,
                "countries": {
                    key: {
                        "name": value.name,
                        "mode": self.agent_meta.get(key, {}).get("mode", "mock"),
                        "provider": self.agent_meta.get(key, {}).get("provider"),
                        "model": self.agent_meta.get(key, {}).get("model"),
                    }
                    for key, value in self.simulation.state.countries.items()
                },
            }

    def configure_agent(self, country_id: str, settings: dict) -> dict:
        with self.lock:
            if country_id not in self.simulation.state.countries:
                return {"ok": False, "error": "unknown country"}
            country = self.simulation.state.countries[country_id]
            raw_personality = settings.get("personality", getattr(country, "personality", "neutral"))
            requested_personality = normalize_personality(raw_personality)
            mode = settings.get("mode")
            if mode == "mock":
                agent = HeuristicAgent(country_id)
                meta = {"mode": "mock", "provider": None, "model": None, "api_key_env": None, "last_test": None, "personality": requested_personality}
            elif mode == "openai_compatible":
                provider_id = str(settings.get("provider_id") or "").strip()
                resolved = dict(settings)
                if provider_id:
                    try:
                        preset, env_name = resolve_simple_settings(country_id, provider_id)
                    except (ValueError, TypeError) as exc:
                        return {"ok": False, "error": f"unknown provider preset: {exc}"}
                    api_key = settings.get("api_key")
                    key_text = api_key.strip() if isinstance(api_key, str) else ""
                    if key_text:
                        # Simple setup: keep the key only in this process, never in
                        # saves, logs, /api/state, or the response payload.
                        os.environ[env_name] = key_text
                    previous_meta = self.agent_meta.get(country_id, {})
                    previous_provider = previous_meta.get("provider_id") or (
                        previous_meta.get("provider")
                        if previous_meta.get("mode") == "openai_compatible"
                        else None
                    )
                    if not key_text and previous_provider and previous_provider != provider_id:
                        return {"ok": False, "error": "切换厂商请重新填写 API Key"}
                    if not os.environ.get(env_name):
                        return {"ok": False, "error": "请填写 API Key"}
                    requested_model = str(
                        settings.get("model") or preset["model"]
                    ).strip()
                    allowed_models = {
                        item["id"] for item in preset.get("models", [])
                    }
                    if requested_model not in allowed_models:
                        return {"ok": False, "error": "该厂商预设不支持这个模型，请从模型列表选择"}
                    resolved = {
                        "provider": preset["label"],
                        "model": requested_model,
                        "base_url": preset["base_url"],
                        "api_key_env": env_name,
                        "timeout_seconds": preset["timeout_seconds"],
                        "retries": preset["retries"],
                        "max_output_tokens": preset["max_output_tokens"],
                    }
                try:
                    llm_settings = LLMSettings.from_dict(country_id, resolved)
                except (ValueError, KeyError, TypeError) as exc:
                    return {"ok": False, "error": f"invalid model settings: {exc}"}
                if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", llm_settings.api_key_env):
                    return {"ok": False, "error": "api_key_env must be an environment variable name"}
                agent = OpenAICompatibleAgent(country_id, llm_settings, self.budget)
                meta = {
                    "mode": "openai_compatible",
                    "provider": llm_settings.provider,
                    "provider_id": provider_id or None,
                    "model": llm_settings.model,
                    "base_url": llm_settings.base_url,
                    "api_key_env": llm_settings.api_key_env,
                    "last_test": None,
                    "personality": requested_personality,
                }
            else:
                return {"ok": False, "error": "mode must be mock or openai_compatible"}
            country.personality = requested_personality
            if mode == "openai_compatible":
                self._ensure_call_budget()
            self.scheduler.set_agent(country_id, agent)
            meta["configured_sequence"] = len(self.simulation.state.event_log)
            self.agent_meta[country_id] = meta
            self.simulation.state.record(
                "agent_configured",
                country_id=country_id,
                mode=meta["mode"],
                provider=meta["provider"],
                model=meta["model"],
                personality=requested_personality,
            )
            return {"ok": True, "country_id": country_id, **self.agent_payload()["countries"][country_id]}

    def set_personality(self, country_id: str, personality: object) -> dict:
        with self.lock:
            country = self.simulation.state.countries.get(country_id)
            if country is None:
                return {"ok": False, "error": "unknown country"}
            requested = str(personality or "").strip().lower()
            if requested not in {"aggressive", "neutral", "conservative"}:
                return {"ok": False, "error": "personality must be aggressive, neutral or conservative"}
            country.personality = requested
            meta = self.agent_meta.setdefault(
                country_id,
                {"mode": "mock", "provider": None, "model": None, "api_key_env": None},
            )
            meta["personality"] = requested
            self.simulation.state.record(
                "agent_personality_changed",
                country_id=country_id,
                personality=requested,
                label=personality_label(requested),
            )
            return {
                "ok": True,
                "country_id": country_id,
                "personality": requested,
                "personality_label": personality_label(requested),
                "tagline": personality_definition(requested)["tagline"],
            }

    def _ensure_call_budget(self) -> None:
        """A UI-configured LLM seat needs a real planning budget too.

        CLI budget flags remain authoritative when set; otherwise enable a
        conservative per-run budget so the seat does not pass its connection
        test and then fail every planning call with BudgetExceeded.
        """
        changed = False
        if self.budget.max_calls is None:
            self.budget.max_calls = 40
            changed = True
        if self.budget.max_cost_usd is None:
            self.budget.max_cost_usd = 1.0
            changed = True
        if changed:
            for meta in self.agent_meta.values():
                meta["configured_sequence"] = len(self.simulation.state.event_log)
            self.simulation.state.record(
                "budget_configured",
                max_calls=self.budget.max_calls,
                max_cost_usd=self.budget.max_cost_usd,
            )

    def configure_budget(self, max_calls: object, max_cost_usd: object) -> dict:
        with self.lock:
            try:
                calls = int(max_calls)
                cost = float(max_cost_usd)
            except (TypeError, ValueError):
                return {"ok": False, "error": "预算必须是数字"}
            if calls < 1 or calls < self.budget.calls:
                return {"ok": False, "error": "调用上限不能低于已用次数或 1"}
            if cost <= 0:
                return {"ok": False, "error": "费用上限必须大于 0"}
            self.budget.max_calls = calls
            self.budget.max_cost_usd = cost
            for meta in self.agent_meta.values():
                meta["configured_sequence"] = len(self.simulation.state.event_log)
            self.simulation.state.record(
                "budget_configured", max_calls=calls, max_cost_usd=cost
            )
            return {"ok": True, "budget": self.budget.summary()}

    def test_agent(self, country_id: str) -> dict:
        """Make one tiny chat call to verify the key/model/endpoint."""
        with self.lock:
            agent = self.scheduler.agents.get(country_id)
            if agent is None or not isinstance(agent, OpenAICompatibleAgent):
                return {"ok": False, "error": "当前席位不是 LLM，请先保存模型配置"}
            settings = agent.settings
            api_key = os.environ.get(settings.api_key_env) or os.environ.get("EOS_API_KEY")
            if not api_key:
                return {"ok": False, "error": "没有可用的 API Key"}
            configured_budget = (
                self.budget.max_calls is not None
                and self.budget.max_cost_usd is not None
            )
            probe_budget = self.budget if configured_budget else CallBudget(1, 0.10)
            try:
                probe_budget.reserve()
            except BudgetExceeded as exc:
                return {"ok": False, "error": str(exc), "budget": probe_budget.summary()}
            payload = {
                "model": settings.model,
                "messages": [{"role": "user", "content": "Reply with OK only."}],
                "max_tokens": 8,
                "temperature": 0,
                "stream": False,
            }
            req = request.Request(
                settings.endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            started = time.perf_counter()
            result: dict = {
                "ok": False,
                "provider": settings.provider,
                "model": settings.model,
                "base_url": settings.base_url,
                "tested_day": self.simulation.state.day_index,
                "tested_date": self.simulation.state.date["label"],
            }
            try:
                with request.urlopen(req, timeout=min(settings.timeout_seconds, 30)) as response:
                    status_code = int(response.status)
                    body = response.read().decode("utf-8", errors="replace")
                data = json.loads(body)
                if not isinstance(data, dict) or not data.get("choices"):
                    raise ValueError("响应缺少 choices")
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
                cost = probe_budget.record(
                    usage, settings.input_price_per_million_usd,
                    settings.output_price_per_million_usd,
                )
                result.update({
                    "ok": True,
                    "status_code": status_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "usage": usage if usage is not None else "unknown",
                    "estimated_cost_usd": cost if cost is not None else "unknown",
                    "message": "连接成功，Key、模型与端点可用",
                })
            except error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                message = raw[:240] if raw else str(exc.reason)
                result.update({
                    "ok": False,
                    "status_code": int(exc.code),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "error": f"HTTP {exc.code}: {message}",
                })
            except (error.URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                result.update({
                    "ok": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "error": f"网络错误: {str(reason)[:240]}",
                })
            except (ValueError, json.JSONDecodeError) as exc:
                result.update({
                    "ok": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "error": f"响应解析失败: {str(exc)[:240]}",
                })
            meta = self.agent_meta.setdefault(country_id, {})
            meta["last_test"] = result
            self.simulation.state.record(
                "agent_test",
                country_id=country_id,
                ok=result["ok"],
                status_code=result.get("status_code"),
                latency_ms=result.get("latency_ms"),
                provider=settings.provider,
                model=settings.model,
                error=result.get("error"),
            )
            return result

    def player_action(self, actor: str, action: object) -> dict:
        with self.lock:
            if self.player_country is None:
                return {"ok": False, "error": "当前为观战模式，请先介入一个国家"}
            if actor != self.player_country:
                return {"ok": False, "error": "只能为自己接管的国家下达动作"}
            if (
                isinstance(action, dict)
                and action.get("type") == "SET_CITY_GOVERNANCE"
                and action.get("managed_by") == "llm"
                and self.agent_meta.get(actor, {}).get("mode") != "openai_compatible"
            ):
                return {"ok": False, "error": "LLM 托管需要先把该国 AI 设为模型控制"}
            result = self.simulation.engine.apply_action(actor, action)
            return {"ok": result.accepted, "result": result.to_dict()}

    def map_preview(self, draft: dict) -> dict:
        """Playability report for a painted draft — pure, so hovering never blocks the sim."""
        from mapgen_custom import draft_report

        return {"ok": True, "report": draft_report(draft)}

    def march_preview(self, draft: dict, start, target) -> dict:
        """March timing for a draft, priced by core.movement.Movement over those tiles."""
        from mapgen_custom import march_preview

        return march_preview(draft, self.rules, start, target)

    def seat(self) -> str | None:
        """The realm the player still holds a chair at, or None once it has been annexed.

        `player_country` is kept as the historical seat so the panel can say whose throne
        was taken; everything that prices orders or draws tables reads this instead.
        """
        if self.player_country is None:
            return None
        return self.player_country if self.player_country in self.simulation.state.countries else None

    def campaign_quotes(self) -> dict:
        """March prices per army against this realm's current wars, for the military panel.

        Only the commanded country gets a quote: the observer view has no one to give orders,
        and `strike_options` answers empty while at peace, so an idle game pays nothing.
        """
        seat = self.seat()
        if seat is None:
            return {}
        return Military(self.simulation.state).strike_options(seat)

    def rendezvous_preview(self, payload: dict) -> dict:
        with self.lock:
            actor = self.seat()
            if actor is None:
                return {"ok": False, "error": "请先接管一个国家"}
            state = self.simulation.state
            raw = {"type": "MILITARY", "operation": "RENDEZVOUS",
                   "army_id": payload.get("army_id"), "host_id": payload.get("host_id"),
                   "units": payload.get("units", {})}
            try:
                action = validate_action(actor, raw, state.config)
                quote = Military(state).rendezvous_quote(
                    actor, action["army_id"], action["host_id"], action["units"]
                )
            except ActionRejected as error:
                return {"ok": False, "error": str(error)}
            return {"ok": True, "quote": quote}

    def tile_strikes(self, col: int, row: int) -> dict:
        """Who stands on one tile and what each of my armies would pay to take it.

        The panel asks per click rather than per frame: pricing a tile runs the same route
        search a march order runs, so keeping it off the snapshot keeps the clock smooth.
        The enemy columns walking *at* this tile are not priced here — the snapshot already
        ships every realm's alerts, and the panel filters that by tile.
        """
        with self.lock:
            state = self.simulation.state
            grid = state.world.data["metadata"]
            if not (0 <= col < int(grid["grid_width"]) and 0 <= row < int(grid["grid_height"])):
                return {"ok": False, "error": "坐标不在地图范围内"}
            military = Military(state)
            return {"ok": True, "tile": military.tile_strikes(self.seat(), (col, row))}

    def world_payload(self) -> dict:
        return project_world(self.simulation.state.world.data)

    def snapshot(self) -> dict:
        with self.lock:
            summary = self.simulation.state.summary()
            threats = Military(self.simulation.state).incoming_threats()
            recent = [
                deepcopy(event) for event in self.simulation.state.event_log
                if event["type"] not in {"economy_daily"}
            ][-35:]
            buildings = []
            projects = []
            for country_id, country in self.simulation.state.countries.items():
                for coord, building in country.buildings.items():
                    buildings.append([coord[0], coord[1], building, country_id])
                for project in country.projects:
                    total = max(float(project["total_days"]), 1.0)
                    projects.append([
                        project["tile"][0], project["tile"][1],
                        project["building"], country_id,
                        round(project["remaining_days"], 1),
                        round(1.0 - project["remaining_days"] / total, 4),
                        total,
                    ])
            llm_calls = [
                deepcopy(event) for event in self.simulation.state.event_log
                if event["type"] == "llm_call"
            ]
            llm_decisions = [
                deepcopy(event) for event in self.simulation.state.event_log
                if event["type"] == "llm_decision"
            ]
            return {
                "day": self.simulation.state.day_index,
                "date": summary["date"],
                "paused": self.paused,
                "finished": self.finished,
                "speed": self.speed,
                "maximum_day": self.maximum_day,
                "player_country": self.player_country,
                "mode": "player" if self.seat() else "observer",
                "eliminated": summary["eliminated"],
                "current_save_id": self.current_save_id,
                "current_save_name": self.current_save_name,
                "era": summary["era"],
                "building_specs": self.simulation.state.config["buildings"],
                "unit_specs": self.simulation.state.config["units"],
                "military_specs": self.simulation.state.config["military"],
                "rendezvous_hosts": (Military(self.simulation.state).rendezvous_hosts(self.seat())
                                     if self.seat() else []),
                "movement_specs": self.simulation.state.config["movement"],
                "campaign_quotes": self.campaign_quotes(),
                "incoming_threats": threats,
                "policy_options": sorted(self.simulation.state.config["policies"]),
                "research_fields": self.simulation.state.config["research_fields"],
                "tech_tree": self.simulation.state.config["tech_tree"],
                "eras": self.simulation.state.config["eras"],
                "research_max_level": self.simulation.state.config["research_max_level"],
                "research_base_threshold": self.simulation.state.config["research_base_threshold"],
                "city_focuses": self.simulation.state.config["city_focuses"],
                "city_zones": self.simulation.state.config["city_zones"],
                "city_budgets": self.simulation.state.config["city_budgets"],
                "maximum_tax_rate": self.simulation.state.config["maximum_tax_rate"],
                "expansion": self.simulation.state.config["expansion"],
                "countries": summary["countries"],
                "ranking": summary["ranking"],
                "leader": summary["leader"],
                "victory": summary["victory"],
                "recent_events": recent,
                "buildings": buildings,
                "projects": projects,
                "agents": self.agent_meta,
                "latest_llm_decision": llm_decisions[-1] if llm_decisions else None,
                "latest_llm_call": llm_calls[-1] if llm_calls else None,
                "llm_budget": self.budget.summary(),
                "fairness": self.simulation.state.world.data["metadata"].get("fairness"),
                "start_year": self.simulation.state.start_astronomical_year,
                "start_era_index": self.simulation.state.start_era_index,
                "action_counts": {
                    "accepted": sum(
                        entry["result"]["accepted"] for entry in self.simulation.state.action_log
                    ),
                    "rejected": sum(
                        not entry["result"]["accepted"] for entry in self.simulation.state.action_log
                    ),
                },
                "trades": summary["trades"],
            }


class Handler(BaseHTTPRequestHandler):
    session: LiveSession

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send(200, DASHBOARD_HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/world":
            self._json(self.session.world_payload())
        elif path == "/api/terrain":
            world_data = self.session.simulation.state.world.data
            reference = world_data.get("metadata", {}).get("terrain_texture")
            target = (BASE_DIR / reference).resolve() if reference else None
            if target and target.is_relative_to(BASE_DIR.resolve()) and target.is_file():
                self._send(200, target.read_bytes(), "image/png")
            else:
                self._json({"error": "terrain texture not found"}, 404)
        elif path == "/api/state":
            self._json(self.session.snapshot())
        elif path == "/api/agents":
            self._json(self.session.agent_payload())
        elif path == "/api/setup":
            from mapgen import ARCHETYPES, CORE_TILES, DEFAULT_COUNTRIES
            from mapgen_custom import DRAFT_HEIGHT, DRAFT_WIDTH, palette

            movement = self.session.rules["movement"]
            costs = movement["biome_cost"]
            self._json({
                "countries": DEFAULT_COUNTRIES,
                "archetypes": ARCHETYPES,
                "core_tiles": CORE_TILES,
                "draft": {
                    "width": DRAFT_WIDTH,
                    "height": DRAFT_HEIGHT,
                    "brushes": [
                        dict(brush, march_cost=costs.get(brush["biome"]))
                        for brush in palette()
                    ],
                    "daily_budget": movement["daily_budget"],
                    "road_cost": movement["road_cost"],
                    "maximum_order_cost": movement["maximum_order_cost"],
                    "biome_cost": costs,
                },
            })
        elif path == "/api/saves":
            self._json({
                "saves": self.session.list_saves(),
                "current": self.session.current_save_id,
            })
        elif path == "/api/diary":
            params = parse_qs(urlparse(self.path).query)
            country_id = params.get("country", [None])[0]
            try:
                limit = min(200, max(1, int(params.get("limit", ["80"])[0])))
            except ValueError:
                limit = 80
            self._json(self.session.diary_payload(country_id, limit))
        elif path == "/api/tile_strikes":
            params = parse_qs(urlparse(self.path).query)
            try:
                col = int(params.get("col", [""])[0])
                row = int(params.get("row", [""])[0])
            except ValueError:
                self._json({"ok": False, "error": "col、row 必须是整数"}, 400)
                return
            self._json(self.session.tile_strikes(col, row))
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif path.startswith("/assets/"):
            target = (ASSETS_DIR / path.removeprefix("/assets/")).resolve()
            if target.is_relative_to(ASSETS_DIR.resolve()) and target.is_file():
                content_types = {
                    ".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".webp": "image/webp",
                }
                self._send(200, target.read_bytes(), content_types.get(target.suffix.lower(), "application/octet-stream"))
            else:
                self._json({"error": "asset not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path not in {
            "/api/control", "/api/action", "/api/agents", "/api/agents/test",
            "/api/agents/budget", "/api/agents/personality", "/api/new_game", "/api/saves",
            "/api/map_preview", "/api/march_preview", "/api/rendezvous_preview",
        }:
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/agents/test":
                result = self.session.test_agent(str(payload.get("country_id", "")))
            elif self.path == "/api/agents/budget":
                result = self.session.configure_budget(
                    payload.get("max_calls"), payload.get("max_cost_usd")
                )
            elif self.path == "/api/agents/personality":
                result = self.session.set_personality(
                    str(payload.get("country_id", "")), payload.get("personality")
                )
            elif self.path == "/api/agents":
                result = self.session.configure_agent(
                    str(payload.get("country_id", "")), payload.get("settings", {})
                )
            elif self.path == "/api/saves":
                operation = payload.get("operation")
                if operation == "save":
                    result = {"ok": True, "save": self.session.save_current(payload.get("name"))}
                elif operation == "load":
                    result = self.session.load_game(str(payload.get("id", "")))
                elif operation == "delete":
                    result = self.session.delete_save(str(payload.get("id", "")))
                else:
                    result = {"ok": False, "error": "unknown save operation"}
            elif self.path == "/api/new_game":
                seed = payload.get("seed")
                start_year = payload.get("start_year", -799)
                result = self.session.start_new_game(
                    payload.get("countries"),
                    None if seed in (None, "") else int(seed),
                    payload.get("save_name"),
                    int(start_year),
                    payload.get("draft"),
                )
            elif self.path == "/api/map_preview":
                result = self.session.map_preview(payload.get("draft") or {})
            elif self.path == "/api/rendezvous_preview":
                result = self.session.rendezvous_preview(payload)
            elif self.path == "/api/march_preview":
                result = self.session.march_preview(
                    payload.get("draft") or {}, payload.get("from"), payload.get("to")
                )
            elif self.path == "/api/action":
                result = self.session.player_action(
                    str(payload.get("actor", "")), payload.get("action")
                )
            else:
                result = self.session.control(
                    str(payload.get("action", "")), payload.get("value")
                )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            result = {"ok": False, "error": str(exc)}
        self._json(result)

    def log_message(self, fmt: str, *args) -> None:
        if not self.path.startswith("/api/state"):
            print("live:", fmt % args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Project Eos local real-time observer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--speed", type=float, default=4.0, help="game days per real second")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--paused", action="store_true")
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    parser.add_argument("--models", type=Path, help="per-country Mock/LLM JSON configuration")
    parser.add_argument("--api-key-file", type=Path, help="local key file read once into process memory")
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Refusing non-loopback host: this dashboard has no authentication")

    budget = CallBudget(args.max_calls, args.max_cost_usd)
    scheduler = None
    agent_meta = {}
    if args.models:
        config_data = json.loads(args.models.read_text(encoding="utf-8"))
        agent_meta = {
            country_id: {
                "mode": settings.get("mode", "mock"),
                "provider": settings.get("provider"),
                "model": settings.get("model"),
            }
            for country_id, settings in config_data["countries"].items()
        }
        if args.api_key_file:
            load_key_file(args.api_key_file)
        scheduler = AgentScheduler(load_agents(args.models, budget))

    session = LiveSession(
        args.speed, args.days, args.paused, scheduler, budget, agent_meta
    )
    Handler.session = session
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print(f"Project Eos live dashboard: {url}")
    print(f"Real-time: {args.speed} game day(s) per second; stop at day {args.days}.")
    if agent_meta:
        controllers = ", ".join(
            f"{country}={meta['provider'] or 'mock'}:{meta['model'] or 'rules'}"
            for country, meta in agent_meta.items()
        )
        print(f"Controllers: {controllers}")
        print(
            f"API budget: {args.max_calls} call(s), "
            f"{args.max_cost_usd} USD known-cost limit"
        )
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping and saving...")
    finally:
        server.server_close()
        session.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
