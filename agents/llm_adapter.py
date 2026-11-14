"""Budgeted OpenAI-compatible JSON-action adapter with no third-party HTTP dependency."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib import error, request

from agents.base import CountryAgent
from agents.heuristic import HeuristicAgent
from agents.personality import personality_definition


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class CallBudget:
    max_calls: int | None = None
    max_cost_usd: float | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_usage_calls: int = 0
    unknown_cost_calls: int = 0
    known_cost_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self) -> None:
        with self._lock:
            # Real paid calls are opt-in: both limits must be explicitly configured.
            if self.max_calls is None or self.max_cost_usd is None:
                raise BudgetExceeded("真实 API 调用需要同时设置 --max-calls 与 --max-cost-usd")
            if self.max_calls < 0 or self.max_cost_usd < 0:
                raise BudgetExceeded("调用与费用预算必须非负")
            if self.calls >= self.max_calls:
                raise BudgetExceeded("API 调用次数预算已用尽")
            if self.known_cost_usd >= self.max_cost_usd:
                raise BudgetExceeded("API 已知费用预算已用尽")
            self.calls += 1

    def record(
        self,
        usage: dict[str, Any] | None,
        input_price_per_million: float | None,
        output_price_per_million: float | None,
    ) -> float | None:
        with self._lock:
            prompt = usage.get("prompt_tokens") if usage else None
            completion = usage.get("completion_tokens") if usage else None
            valid_prompt = isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0
            valid_completion = isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0
            if not valid_prompt or not valid_completion:
                self.unknown_usage_calls += 1
                self.unknown_cost_calls += 1
                return None
            self.input_tokens += prompt
            self.output_tokens += completion
            if input_price_per_million is None or output_price_per_million is None:
                self.unknown_cost_calls += 1
                return None
            cost = (
                prompt * input_price_per_million + completion * output_price_per_million
            ) / 1_000_000.0
            self.known_cost_usd += cost
            return cost

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": "llm_budget" if self.max_calls is not None or self.max_cost_usd is not None else "mock_only_or_unconfigured",
                "max_calls": self.max_calls,
                "max_cost_usd": self.max_cost_usd,
                "http_attempts": self.calls,
                "input_tokens_known_total": self.input_tokens,
                "output_tokens_known_total": self.output_tokens,
                "unknown_usage_calls": self.unknown_usage_calls,
                "unknown_cost_calls": self.unknown_cost_calls,
                "known_estimated_cost_usd": self.known_cost_usd,
                "cost_note": "费用仅按配置单价和供应商 usage 估算；unknown 不视为零费用",
            }


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    base_url: str
    api_key_env: str
    timeout_seconds: float = 20.0
    retries: int = 1
    max_output_tokens: int = 700
    input_price_per_million_usd: float | None = None
    output_price_per_million_usd: float | None = None

    @classmethod
    def from_dict(cls, country_id: str, data: dict[str, Any]) -> "LLMSettings":
        settings = cls(
            provider=str(data["provider"]),
            model=str(data["model"]),
            base_url=str(data["base_url"]),
            api_key_env=str(data.get("api_key_env", f"EOS_AGENT_{country_id}_API_KEY")),
            timeout_seconds=float(data.get("timeout_seconds", 20)),
            retries=int(data.get("retries", 1)),
            max_output_tokens=int(data.get("max_output_tokens", 700)),
            input_price_per_million_usd=data.get("input_price_per_million_usd"),
            output_price_per_million_usd=data.get("output_price_per_million_usd"),
        )
        if not settings.base_url.startswith(("https://", "http://")):
            raise ValueError("LLM base_url must use HTTP(S)")
        if not 0 < settings.timeout_seconds <= 120:
            raise ValueError("LLM timeout must be in (0, 120]")
        if not 0 <= settings.retries <= 3 or not 1 <= settings.max_output_tokens <= 4000:
            raise ValueError("invalid retry or output-token limit")
        return settings

    @property
    def endpoint(self) -> str:
        if self.base_url.rstrip("/").endswith("/chat/completions"):
            return self.base_url.rstrip("/")
        return self.base_url.rstrip("/") + "/chat/completions"


class OpenAICompatibleAgent(CountryAgent):
    def __init__(self, country_id: str, settings: LLMSettings, budget: CallBudget) -> None:
        self.country_id = country_id
        self.settings = settings
        self.budget = budget
        self._usage_events: list[dict[str, Any]] = []
        self._usage_lock = threading.Lock()

    def drain_usage_events(self) -> list[dict[str, Any]]:
        with self._usage_lock:
            events = self._usage_events
            self._usage_events = []
            return events

    def _usage_event(self, status: str, **payload: Any) -> None:
        event = {
            "provider": self.settings.provider,
            "model": self.settings.model,
            "status": status,
            **payload,
        }
        with self._usage_lock:
            self._usage_events.append(event)

    async def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._decide_with_retries, observation)

    def _decision_request(self, observation: dict[str, Any]) -> dict[str, Any]:
        personality = personality_definition(
            (observation.get("country") or {}).get("personality")
        )
        system = (
            f"当前国家性格：{personality['label']}（{personality['tagline']}）。"
            f"{personality['llm']}"
            "性格只影响取舍和风险偏好，不改变 JSON 结构或动作协议。"
            "Output one json object only. 你是国家策略控制器。只返回一个 JSON 对象，不要 Markdown。"
            "结构为 country_id, world_day, actions, reason, diary。country_id 与 world_day 必须复制观察值。"
            "diary 必须是对象：{title: 不超过20字, mood: 2-6字, text: 120-260字第一人称统治日记}。"
            "text 要写清当前最关心的局势、为什么采取这些动作、放弃了什么、下一步计划；"
            "必须使用现代白话文，像正常人说话，绝对不要文言文、骈文、四字排比或古风腔；"
            "写公开的决策叙事，不要逐步推理链，不要编造观察中不存在的数字。"
            "actions 最多4项，只允许 BUILD(tile,building), SETTLE(tile) 用于开拓相邻无主地块, "
            "ALLOCATE_WORKERS(agriculture,mining,construction), SET_TAX(rate), "
            "SET_RESEARCH(field), SET_POLICY(priority), "
            "TRADE(operation,target,offer_food,request_treasury 或 operation,proposal_id), "
            "DIPLOMACY(GIFT/PROPOSE_PACT/ACCEPT_PACT/REJECT_PACT/DECLARE_WAR/PROPOSE_PEACE), "
            "MILITARY(RECRUIT(unit,strength[,army_id])/MARCH(tile[,army_id])/RENDEZVOUS(army_id,host_id,units)/DISPATCH(orders)/ATTACK(target[,army_id][,tile])"
            "/SET_RECRUITING(army_id,recruiting)/SET_RECRUIT_UNIT(army_id,unit)), "
            "SET_CITY_FOCUS(city_id,focus), SET_CITY_ZONE(city_id,tile,zone), "
            "SET_CITY_BUDGET(city_id,austerity|balanced|investment), NO_OP。"
            "MILITARY 省略 army_id 时对军势最大的那支军队下令；SET_RECRUITING 与 SET_RECRUIT_UNIT 只对驻在本国兵营的军队有效。"
            "MARCH 与 ATTACK 只是下单，不会瞬移：军队按 rules.movement 的地形与道路成本每天推进，"
            "行军令可以落在任何己方或无主的陆地格——敌国境内穿得过，只是每进一格多付 rules.movement.hostile_cost，"
            "但不能停在别国格上，要占那一格就下进攻令；铺了路的格子不分归属都是最便宜的，"
            "远征路线常常沿着对方或自己的道路走；"
            "走得远也不再是「没有路」：ORDER_LIMIT 会说清这一路要走多少点、约几天，"
            "超过 rules.movement.maximum_order_cost 就在中途驻一脚再下第二段令，只有被海隔开的格才是陆军真的到不了；"
            "upkeep_food 与 military_upkeep_food 是每日口粮，成兵后不扣财政。"
            "training_days 是单营工期，每座已建成营每天提供 1 点进度。max_count 是每营贡献的兵种额度。"
            "全国兵营同日只训练一支有兵、驻营且开募的军队；驻格容量见 rendezvous_hosts。"
            "面板与你的观察读的是同一份 movement_specs（含 hostile_cost）与 military_specs，没有第二套账；"
            "campaign_options[敌国] 给出最早能抵达的军队、行军天数与集结格（march_target 是落脚格、"
            "objective 是这一令真正打的那一格，ATTACK 就带着 objective 下），country.armies[].march 是它在途的状态与剩余天数；"
            "campaign_options[敌国].options 按军队逐条给出同样的字段，mass 是抵达该缺口时能同时到场的军队数，"
            "mass_share 是这一仗真正打出的战力系数（单独一支最低，几支兵营的兵汇在同一缺口才拿得满）；"
            "想打得更重就把几支军队先 MARCH 到同一格再 ATTACK，也可以对不同城池分进合击；"
            "兵营每营编制固定，但全国兵营数量不设上限，营地越多全国编制上限越高；"
            "country.barracks 逐营给出兵营现状：army_id 是这一营当天实际领饷的军队，unit/label 是它真正在练的兵种"
            "（preferred 未解锁时回落到默认兵种），progress 与 price 对上了才出 1 支，days_left 是还需几天，"
            "room 是本营还能收几支，raised 是累计练成数，stall 是今天为什么不出兵"
            "（PAUSED 已停募、INSUFFICIENT_WAGE 付不起当日训练费、INSUFFICIENT_FOOD 缺训练口粮、CAPACITY_LIMIT 驻格满、"
            "UNIT_LIMIT 本军该兵种额度满、ALREADY_GARRISONED 同格驻军冲突），stall 非空时 days_left 为 null；"
            "country.recruit_options 逐兵种给出 reason 与 can_recruit，这里的 reason 与引擎审核 RECRUIT 用的是同一套闸门；"
            "diplomacy.ledger 把你的提案与交易按 incoming/outgoing 列出终局状态"
            "（PENDING 待回应 / ACCEPTED / REJECTED / EXPIRED）与剩余天数，被拒的提案不会再回来；"
            "正在行军的军队不能再下令，抵达后 ATTACK 才会结算会战。"
            "ATTACK 可再带 tile={col,row} 指定要打的那一格：该国的领土，或它一支驻扎在外的军队所在的那一格"
            "（野地、我国土、第三国的土地都算）——普通地块照旧会战夺格，打这种野战只消灭其编制、边界不动"
            "（战斗事件里的 field=true），要打这样的营地就必须点名那一格；"
            "城池中心格打的是一座城市的墙——军队必须先进入中心格周围 rules.military.siege.blockade_radius "
            "格封锁圈（多半要先夺下圈里的一格），抵达后开的不是会战而是围城账；"
            "campaign_options[敌国].siege 逐城给出你已经站在封锁圈里的军队 armies、center 中心格、"
            "progress 与 days_left（几天城破）、walls 是否有城墙、share 你占的成数、"
            "taken_by 若是别国已先围住（一城同时只有一方围得）；"
            "campaign_options[敌国].siege_marches 给出还没有本国军队围着、但一支待命军队能一路走到封锁圈的墙："
            "army_id 是这一路、center 是要点的中心格、camp 是落脚格、days 与 cost 是行军价、"
            "days_left 是抵达之后几天城破——对那个 center 下令 ATTACK 带 army_id，就是让这支军队开赴并就地开围；"
            "这些路线的落脚格只走己方或无主地（过境敌土的路段照走，只是更慢），"
            "所以真要贴到墙下时常常还得先用会战夺下一格城缘；"
            "country.cities[].siege 则是别人围你自己的城：progress 越贴近 threshold、days_left 越短，"
            "越该考虑求和或派兵解围，"
            "围城每天按 daily_progress×share 推进，城墙把 defense 抬高 wall_bonus 倍分量，"
            "城破则整座易主（土地、人口、建筑与贡金一起走），本国 stability 掉 stability_loss，"
            "双方各加 exhaustion_per_day 的战争疲惫。"
            "siege 与 siege_marches 的 capital 字段标出那座城是不是都城：都城是按世界生成的 capital 坐标认定的，"
            "都城一破整座易主的同时，败国的全部领土、城市、人口、国库与粮仓一次性并入战胜国"
            "（rules.military.annexation.treasury_seizure 与 food_seizure 决定抽走几成），"
            "败国就此从地图上消失——围下都城是一仗定胜负，丢下自己的都城是亡国。"
            "本国 country.cities[] 也带 capital 标出哪座是自己的都城：都城被啃到几成，议和闸门就在 "
            "siege.peace_pressure 之外再按同一比例降 rules.military.annexation.peace_pressure，"
            "所以都城眼看要破时先递求和，别把亡国留到结算日；外省城破只走 siege 那一档压力。"
            "unopposed、guards 与 attacker_loss 是同一笔账：抵达的缺口若没有对方军队（guards=0），"
            "这一路只按 rules.military.unopposed_loss_rate 折损，几乎不流血；有守军才按 "
            "attacker_loss_rate 与 defender_loss_rate 打会战——报价里的数字就是结算用的数字，"
            "同样天数内优先挑 unopposed=true 的缺口下手，也别把兵力派往已有守军的位置。"
            "rout 是打在城池里的另一笔账：会战若落在对方某座城的城区格上、且那格里确实有对方军队守着，"
            "败的一方就不按旷野消耗死完——每一路只留 rules.military.rout.survivor_rate 的人（四成），"
            "其余当场折损，幸存者立刻退到报价里的 retreat 格：进攻方败了往 rout.retreat_radius 格内"
            "能离开敌土的地方退，防守方败了退回本国领地；"
            "attacker.fate 与 garrison.fate 只有 routed（溃退）与 annihilated（全歼）两种，"
            "annihilated 来路只有两条：retreat 为 null 是四周被对方占死、无路可退，"
            "或者落脚格正是对方的 BARRACKS——兵营被夺那守军就按 rout.barracks_lost_survivor_rate 结算，"
            "0 是一人不留、该营就此解散（胜方照旧只付 attacker_loss_rate / defender_loss_rate 的消耗）。"
            "所以打城前先读 rout：garrison.fate=annihilated 是要夺兵营，"
            "attacker.fate=annihilated 的缺口别把整支军队送进去。"
            "incoming_threats 是本国边警，每一行是一支正在走上我方土地的敌军："
            "attacker/attacker_name 是谁、army_id 与 strength 是它那一路的兵力（含已经在附近会合的军队）、"
            "tile 是它要抵达的格子、city_id/city 与 capital 说明那是不是某座城尤其是不是都城、"
            "days_left 与 arrival_day/arrival_date 是还要几天、走到哪一天、"
            "defenders 是我在那一格周围的守军、unopposed 为真表示那里无人可守（对方几乎零损耗）、"
            "attacker_loss 是对方预计折损（数字越小越像真的打下来）；"
            "rout 是这一格真打起来我方守军的下场：garrison.fate=annihilated 多半是守军正站在自己的兵营上，"
            "败了整营清零，这样的边警比别的更该抢先派兵解围；"
            "relief 给出本国哪支待命军队能最快赶到这一格：relief.days<=days_left 就下令 "
            "MARCH(army_id=relief.army_id, tile=该行的 tile) 站上缺口迎击，"
            "都城的边警优先于一切其他军令，因为 _mass 只算站在战场半径内的军队。"
            "已被吞并的国家不会再出现在 known_countries、at_war_with、relations 或 campaign_options 里："
            "它的协定、提案、贸易与贡金在灭国当日一并作废，不要对已经不存在的国家下令。"
            "省略 tile 时由引擎自动挑最早的缺口；攻占下带建筑的格子后，"
            "country.tribute.collected 会列出对方此后 rules.military.capture_tribute_days 天"
            "每日按 capture_tribute_share 交给你的收入（同一对手合计不超过 capture_tribute_cap），"
            "被抽贡会加速对方求和，收到贡金则更值得把仗打下去。"
            "country.cities 内标记 managed_by=llm 的城市由你直接规划；"
            "当这些城市粮食不足、服务偏低或宜居下降时，优先用 SET_CITY_FOCUS/SET_CITY_ZONE/SET_CITY_BUDGET 调整。"
            "不得输出代码、密钥或修改规则；无法决定时返回 NO_OP。"
        )
        return {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(observation, ensure_ascii=False, separators=(",", ":"))},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": self.settings.max_output_tokens,
        }

    def _post(self, observation: dict[str, Any]) -> dict[str, Any]:
        api_key = os.environ.get(self.settings.api_key_env) or os.environ.get("EOS_API_KEY")
        if not api_key:
            raise LLMError("未配置 API 密钥环境变量")
        req = request.Request(
            self.settings.endpoint,
            data=json.dumps(self._decision_request(observation), ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=self.settings.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _parse_response(response: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("供应商响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str):
            raise LLMError("模型 content 不是字符串")
        stripped = content.strip()
        if stripped.startswith("```"):
            raise LLMError("模型返回了 Markdown 代码围栏而非纯 JSON")
        try:
            decision = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise LLMError("模型未返回有效 JSON") from exc
        if not isinstance(decision, dict) or not isinstance(decision.get("actions"), list):
            raise LLMError("模型决议缺少 actions 数组")
        if decision.get("country_id") != observation["country_id"]:
            raise LLMError("模型决议 country_id 不匹配")
        if decision.get("world_day") != observation["world_day"]:
            raise LLMError("模型决议 world_day 不匹配")
        return decision

    def _decide_with_retries(self, observation: dict[str, Any]) -> dict[str, Any]:
        if not (os.environ.get(self.settings.api_key_env) or os.environ.get("EOS_API_KEY")):
            self._usage_event("error", error_type="MissingAPIKey", retryable=False)
            raise LLMError("未配置 API 密钥环境变量")
        last_error: Exception | None = None
        for attempt in range(self.settings.retries + 1):
            started = time.perf_counter()
            usage = None
            cost = None
            try:
                self.budget.reserve()
                response = self._post(observation)
                if not isinstance(response, dict):
                    raise LLMError("供应商响应不是 JSON 对象")
                usage = response.get("usage") if isinstance(response.get("usage"), dict) else None
                # Record billable usage even when the model's decision is malformed.
                cost = self.budget.record(
                    usage,
                    self.settings.input_price_per_million_usd,
                    self.settings.output_price_per_million_usd,
                )
                decision = self._parse_response(response, observation)
                self._usage_event(
                    "success", attempt=attempt + 1,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    usage=usage if usage is not None else "unknown",
                    estimated_cost_usd=cost if cost is not None else "unknown",
                )
                return decision
            except (LLMError, error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                retryable = (
                    not isinstance(exc, BudgetExceeded)
                    and (not isinstance(exc, error.HTTPError) or exc.code == 429 or exc.code >= 500)
                )
                payload = {}
                if usage is not None or cost is not None:
                    payload = {
                        "usage": usage if usage is not None else "unknown",
                        "estimated_cost_usd": cost if cost is not None else "unknown",
                    }
                self._usage_event(
                    "error", attempt=attempt + 1,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    error_type=type(exc).__name__,
                    http_status=exc.code if isinstance(exc, error.HTTPError) else None,
                    retryable=retryable,
                    **payload,
                )
                if attempt >= self.settings.retries or not retryable:
                    break
                time.sleep(0.2 * (2 ** attempt))
        raise LLMError(f"LLM 决策失败: {type(last_error).__name__}") from last_error


def load_agents(path: str | Path, budget: CallBudget) -> dict[str, CountryAgent]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    agents: dict[str, CountryAgent] = {}
    for country_id, config in data["countries"].items():
        mode = config.get("mode", "mock")
        if mode == "mock":
            agents[country_id] = HeuristicAgent(country_id)
        elif mode == "openai_compatible":
            agents[country_id] = OpenAICompatibleAgent(
                country_id, LLMSettings.from_dict(country_id, config), budget
            )
        else:
            raise ValueError(f"unsupported agent mode: {mode}")
    return agents
