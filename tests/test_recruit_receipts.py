"""募兵与外交回执：为什么没出兵、命令成没成，面板都要能直接读回来。

这一批补的是「结算侧知道答案、界面侧只能猜」的那段距离。兵营今天的停摆原因、每个兵种的
招募闸门、命令回执与提案的终局状态，都由 core/state.py 单源算出，面板与 AI 观测照搬。
所以测试也只读这些字段，不另写一套公式：它验的是「面板说能募，引擎就真能募」这条不变量，
以及「说得出原因」这件事本身——每种停摆各有一条事件，每道闸门各有一个回执码。
"""
from __future__ import annotations

import math

import pytest

from agents.llm_adapter import CallBudget, LLMSettings, OpenAICompatibleAgent
from agents.scheduler import build_observation
from core.rules import RuleEngine
from core.state import (
    RECEIPT_LIMIT,
    GameState,
    barracks_report,
    barracks_reports,
    camp_garrison,
    diplomacy_ledger,
    recruit_options,
    recruit_stall,
)

# 面板上的 reason 对应引擎拒绝同一个订单时的 reason_code；
# 「需「科技」N 级」这类写法带变量，因此按 TECHNOLOGY_REQUIRED 单独处理。
GATE_CODES = {
    "驻格容量不足": "CAPACITY_LIMIT",
    "该兵种名额已满": "UNIT_LIMIT",
    "财政不足": "INSUFFICIENT_TREASURY",
    "粮食不足": "INSUFFICIENT_FOOD",
}


def recruit(unit_id: str, amount: int, army_id: str | None = None) -> dict:
    order = {"type": "MILITARY", "operation": "RECRUIT", "unit": unit_id, "strength": amount}
    if army_id:
        order["army_id"] = army_id
    return order


def engine_of() -> RuleEngine:
    """A bare settlement: no planners, so only these orders and the daily wage move the state."""
    return RuleEngine(
        GameState.new(seed=2000), planner=None,
        enable_city_manager=False, enable_frontier_ai=False,
    )


def pay_up(country, treasury: float = 100_000.0, food: float = 100_000.0):
    """A realm that can afford anything, with every troop type already researched."""
    country.treasury, country.food = treasury, food
    country.technology = {field: 5 for field in country.technology}
    return country


def camp_on(engine: RuleEngine, country_id: str = "ELAR", units: dict | None = None):
    """One finished camp with one recruiting garrison standing on it."""
    country = pay_up(engine.state.countries[country_id])
    tile = next(coord for coord in country.tiles if coord not in country.buildings)
    country.buildings[tile] = "BARRACKS"
    army = country.new_army(tile=tile, units=dict({"MILITIA": 1} if units is None else units), recruiting=True)
    return tile, army


def report_at(engine: RuleEngine, tile, country_id: str = "ELAR") -> dict:
    return barracks_report(engine.state.countries[country_id], tile, engine.state.config)


def events_of(state, kind: str) -> list:
    return [event for event in state.event_log if event["type"] == kind]


def stalled_events(state, code: str | None = None, army_id: str | None = None) -> list:
    return [
        event for event in events_of(state, "barracks_stalled")
        if (code is None or event["code"] == code)
        and (army_id is None or event["army_id"] == army_id)
    ]


def option_rows(country, config) -> dict:
    """The panel's own view, keyed by troop type."""
    return {row["unit"]: row for row in recruit_options(country, config)}


def ledger_row(state, country_id: str, proposal_id: str, direction: str) -> dict:
    ledger = diplomacy_ledger(country_id, state.diplomatic_proposals, state.trades, state.day_index)
    return next(row for row in ledger[direction] if row["proposal_id"] == proposal_id)


def propose_pact(engine: RuleEngine, actor: str, target: str) -> str:
    result = engine.apply_action(actor, {
        "type": "DIPLOMACY", "operation": "PROPOSE_PACT", "target": target, "pact": "TRADE",
    })
    assert result.accepted, result.reason_text
    return result.effects["proposal_id"]


def test_camp_report_counts_the_soldier_it_handed_over():
    """进度条就是结算的那一条：走完的那天，`raised` 与编成表同时 +1。"""
    engine = engine_of()
    config = engine.state.config
    tile, army = camp_on(engine)
    wait = config["units"]["MILITIA"]["training_days"]
    army.progress = wait - 1
    almost = report_at(engine, tile)
    assert almost["army_id"] == army.army_id
    assert almost["unit"] == "MILITIA" and almost["stall"] == ""
    assert almost["raised"] == 0 and almost["strength"] == 1
    assert almost["days_left"] == 1

    engine.step_day()
    done = report_at(engine, tile)
    assert done["raised"] == 1 == army.raised
    assert done["strength"] == 2 and army.units["MILITIA"] == 2
    assert done["progress"] == 0.0
    assert done["days_left"] == wait
    assert done["room"] == config["units"]["MILITIA"]["max_count"] - 2
    assert events_of(engine.state, "barracks_recruit")[-1]["unit"] == "MILITIA"


def test_camp_report_is_live_not_the_last_settlement_answer():
    """两次结算之间打开面板，看到的也得是今天的事实，不是昨天的存档。"""
    engine = engine_of()
    state = engine.state
    tile, army = camp_on(engine)
    for _ in range(5):
        engine.step_day()
    assert report_at(engine, tile)["stall"] == ""

    army.recruiting = False
    paused = report_at(engine, tile)
    assert paused["stall"] == "PAUSED" and paused["stall_label"] == "已停募"
    assert paused["days_left"] is None

    army.recruiting = True
    army.progress = 0.0
    state.countries["ELAR"].treasury = 0.0
    broke = report_at(engine, tile)
    assert broke["stall"] == "INSUFFICIENT_WAGE"
    assert recruit_stall(state.countries["ELAR"], army, "MILITIA", state.config) == "INSUFFICIENT_WAGE"


def test_an_involuntary_stop_is_announced_once_and_pausing_is_quiet():
    """付不起军饷要说一次；玩家自己按下的暂停不该天天占新闻。"""
    engine = engine_of()
    state = engine.state
    tile, army = camp_on(engine)
    for _ in range(6):
        engine.step_day()
    state.countries["ELAR"].treasury = 0.0
    for _ in range(4):
        engine.military.barracks_daily()

    assert report_at(engine, tile)["stall"] == "INSUFFICIENT_WAGE"
    assert stalled_events(state, "INSUFFICIENT_WAGE", army.army_id) == stalled_events(state)

    before = len(stalled_events(state))
    assert engine.apply_action("ELAR", {
        "type": "MILITARY", "operation": "SET_RECRUITING",
        "army_id": army.army_id, "recruiting": False,
    }).accepted
    engine.step_day()
    assert report_at(engine, tile)["stall"] == "PAUSED"
    assert len(stalled_events(state)) == before
    assert len(events_of(state, "recruiting_changed")) == 1


def test_one_camp_complains_about_the_same_reason_once_per_cooldown():
    """国库在日饷上下反复横跳：停摆原因天天翻转，公告却按 stall_notice_days 限速。"""
    engine = engine_of()
    state = engine.state
    tile, army = camp_on(engine)
    country = state.countries["ELAR"]
    cooldown = 20
    state.config["military"]["barracks"]["stall_notice_days"] = cooldown

    country.treasury = 0.0
    engine.military.barracks_daily()
    state.day_index += 1
    assert stalled_events(state, "INSUFFICIENT_WAGE", army.army_id), "第一次付不起要说一次"

    # 五轮「付得起 / 付不起」= 10 日，全在冷却里：面板照样读得到原因，新闻不再重复。
    for _ in range(5):
        country.treasury = 5.0
        engine.military.barracks_daily()
        state.day_index += 1
        country.treasury = 0.0
        engine.military.barracks_daily()
        state.day_index += 1
    announced = len(stalled_events(state, "INSUFFICIENT_WAGE", army.army_id))
    assert announced == 1
    assert report_at(engine, tile)["stall"] == "INSUFFICIENT_WAGE"

    # 冷却过后同一条理由可以再说一遍。
    for _ in range(6):
        country.treasury = 5.0
        engine.military.barracks_daily()
        state.day_index += 1
        country.treasury = 0.0
        engine.military.barracks_daily()
        state.day_index += 1
    assert len(stalled_events(state, "INSUFFICIENT_WAGE", army.army_id)) == announced + 1


def test_recruit_option_reason_is_the_gate_the_engine_will_use():
    """面板说能募，订单就真能募；说不行，给出的必须是引擎自己会用的那个码。"""
    engine = engine_of()
    state = engine.state
    country = pay_up(state.countries["ELAR"])
    assert set(option_rows(country, state.config)) == set(state.config["units"])

    # 逐行重算：下面这一笔订单本身会推动全国编制，写在循环外的旧快照会自相矛盾
    for unit_id in state.config["units"]:
        row = option_rows(country, state.config)[unit_id]
        result = engine.apply_action("ELAR", recruit(unit_id, max(1, row["batch"])))
        if row["reason"] == "可招募":
            assert result.accepted, (row, result.reason_code)
        else:
            assert not result.accepted, (row, result.reason_code)
            assert result.reason_code == GATE_CODES.get(row["reason"], "TECHNOLOGY_REQUIRED"), row

    country.armies = []
    country.treasury = 1.0
    poor = option_rows(country, state.config)["MILITIA"]
    assert poor["reason"] == "财政不足" and poor["can_recruit"] is False
    assert engine.apply_action("ELAR", recruit("MILITIA", 1)).reason_code == "INSUFFICIENT_TREASURY"

    country.treasury, country.food = 100_000.0, 2.0
    hungry = option_rows(country, state.config)["MILITIA"]
    assert hungry["reason"] == "粮食不足"
    assert engine.apply_action("ELAR", recruit("MILITIA", 1)).reason_code == "INSUFFICIENT_FOOD"

    pay_up(country)
    maxed = int(state.config["units"]["MILITIA"]["max_count"])
    country.new_army(units={"MILITIA": maxed})
    full = option_rows(country, state.config)["MILITIA"]
    assert full["reason"] == "该兵种名额已满" and full["batch"] == 0
    assert engine.apply_action("ELAR", recruit("MILITIA", 1)).reason_code == "UNIT_LIMIT"

    country.armies = []
    country.new_army(units={"MILITIA": maxed, "ARCHER": 4})
    row = option_rows(country, state.config)["ARCHER"]
    assert row["batch"] == 2
    assert engine.apply_action("ELAR", recruit("ARCHER", 2)).accepted


def test_every_order_leaves_a_receipt_and_the_ledger_stays_bounded():
    engine = engine_of()
    state = engine.state
    pay_up(state.countries["ELAR"])
    for _ in range(RECEIPT_LIMIT + 4):
        engine.apply_action("ELAR", recruit("MILITIA", 1))
        engine.apply_action("ELAR", {"type": "SET_TAX", "rate": 0.1})

    rows = state.receipts["ELAR"]
    assert len(rows) == RECEIPT_LIMIT
    assert [row["action_id"] for row in rows] == [
        entry["result"]["action_id"] for entry in state.action_log
    ][-RECEIPT_LIMIT:]
    assert rows[-1]["action_type"] == "SET_TAX" and rows[-1]["accepted"] is True
    assert rows[-1]["operation"] is None and rows[-1]["action"]["rate"] == pytest.approx(0.1)
    ordered = next(row for row in rows if row["action_type"] == "MILITARY")
    assert ordered["operation"] == "RECRUIT" and ordered["action"]["unit"] == "MILITIA"

    refused = engine.apply_action("VAYR", recruit("WAGON", 1))
    # 兵种不在内容表里，协议闸门先于结算引擎回绝；未通过审核的参数不入账，
    # 面板靠 reason_text 说明回绝的是哪一个兵种。
    assert not refused.accepted and refused.reason_code == "INVALID_PARAMETER"
    assert "WAGON" in refused.reason_text
    assert state.receipts["VAYR"][-1]["reason_code"] == "INVALID_PARAMETER"
    assert state.receipts["VAYR"][-1]["action"] == {"type": "MILITARY"}


def test_receipts_survive_the_save_and_do_not_move_the_fingerprint(tmp_path):
    """回执是给面板看的账，不参与指纹：多记一条回执不该让存档变成另一个局面。"""
    engine = engine_of()
    state = engine.state
    pay_up(state.countries["ELAR"])
    assert engine.apply_action("ELAR", recruit("MILITIA", 2)).accepted
    state.countries["ELAR"].treasury = 4.0
    assert not engine.apply_action("ELAR", recruit("SIEGE", 1)).accepted
    fingerprint = state.fingerprint()

    restored = GameState.load(state.save(tmp_path / "receipts.json"))
    rows = restored.receipts["ELAR"]
    assert [row["accepted"] for row in rows] == [True, False]
    assert rows[-1]["reason_code"] == "INSUFFICIENT_TREASURY"
    assert restored.fingerprint() == fingerprint


def test_the_ledger_keeps_the_whole_life_of_a_proposal():
    """成与败都留在账上：只剩待回应的那一半，面板就答不出「我的外交成了没有」。"""
    engine = engine_of()
    state = engine.state
    engine.diplomacy.set_relation("ELAR", "MARIS", 60)
    engine.diplomacy.set_relation("ELAR", "VAYR", 60)

    signed = propose_pact(engine, "ELAR", "MARIS")
    pending = ledger_row(state, "ELAR", signed, "outgoing")
    assert pending["status"] == "PENDING" and pending["status_label"] == "待回应"
    assert pending["counterparty"] == "MARIS" and pending["direction"] == "outgoing"
    assert pending["days_left"] == state.config["trade_expiry_days"]
    assert pending["resolved_day"] is None

    assert engine.apply_action("MARIS", {
        "type": "DIPLOMACY", "operation": "ACCEPT_PACT", "proposal_id": signed,
    }).accepted
    active = ledger_row(state, "ELAR", signed, "outgoing")
    assert active["status"] == "ACTIVE" and active["status_label"] == "生效中"
    assert active["resolved_day"] == state.day_index and active["days_left"] is None
    assert state.pacts[signed]["status"] == "ACTIVE"

    refused = propose_pact(engine, "ELAR", "VAYR")
    assert engine.apply_action("VAYR", {
        "type": "DIPLOMACY", "operation": "REJECT_PACT", "proposal_id": refused,
    }).accepted
    rejected = ledger_row(state, "ELAR", refused, "outgoing")
    assert rejected["status"] == "REJECTED" and rejected["status_label"] == "已拒绝"

    ledger = diplomacy_ledger("ELAR", state.diplomatic_proposals, state.trades, state.day_index)
    assert {row["proposal_id"] for row in ledger["outgoing"]} == {signed, refused}
    assert ledger["pending"] == 0 and ledger["decided"] == 2
    assert ledger["incoming"] == []


def test_the_ledger_names_the_other_side_not_the_reader():
    """同一张纸从收发两端读，counterparty 都指向对面那一方。"""
    engine = engine_of()
    state = engine.state
    engine.diplomacy.set_relation("MARIS", "ELAR", 60)
    proposal_id = propose_pact(engine, "MARIS", "ELAR")

    ours = ledger_row(state, "ELAR", proposal_id, "incoming")
    assert ours["counterparty"] == "MARIS" and ours["direction"] == "incoming"
    assert ledger_row(state, "MARIS", proposal_id, "outgoing")["counterparty"] == "ELAR"

    assert engine.apply_action("ELAR", {
        "type": "DIPLOMACY", "operation": "REJECT_PACT", "proposal_id": proposal_id,
    }).accepted
    assert ledger_row(state, "ELAR", proposal_id, "incoming")["status"] == "REJECTED"


def test_an_unanswered_proposal_expires_into_the_ledger():
    engine = engine_of()
    state = engine.state
    engine.diplomacy.set_relation("ELAR", "MARIS", 60)
    proposal_id = propose_pact(engine, "ELAR", "MARIS")

    for _ in range(int(state.config["trade_expiry_days"])):
        engine.step_day()
    # 到期日当天仍挂在账上：过期是那天开头的一次结算宣布的
    assert ledger_row(state, "ELAR", proposal_id, "outgoing")["days_left"] == 0
    assert ledger_row(state, "ELAR", proposal_id, "outgoing")["status"] == "PENDING"

    engine.step_day()
    expired = ledger_row(state, "ELAR", proposal_id, "outgoing")
    assert expired["status"] == "EXPIRED" and expired["status_label"] == "已过期"
    assert expired["days_left"] is None
    # 到期没人回应，不等于哪天定了案——面板因此走「到期未回应」这一支
    assert expired["resolved_day"] is None
    assert events_of(state, "pact_expired")[-1]["proposal_id"] == proposal_id


def test_trades_join_the_same_ledger_as_pacts():
    engine = engine_of()
    state = engine.state
    pay_up(state.countries["ELAR"])
    offered = engine.apply_action("ELAR", {
        "type": "TRADE", "operation": "PROPOSE", "target": "MARIS",
        "offer_food": 5.0, "request_treasury": 3.0,
    })
    assert offered.accepted, offered.reason_text
    proposal_id = offered.effects["proposal_id"]

    row = ledger_row(state, "ELAR", proposal_id, "outgoing")
    assert row["kind"] == "TRADE" and row["status"] == "PENDING"
    assert row["counterparty"] == "MARIS"
    assert row["offer_food"] == 5.0 and row["request_treasury"] == 3.0

    assert engine.apply_action("MARIS", {
        "type": "TRADE", "operation": "REJECT", "proposal_id": proposal_id,
    }).accepted
    rejected = ledger_row(state, "ELAR", proposal_id, "outgoing")
    assert rejected["status"] == "REJECTED" and rejected["resolved_day"] == state.day_index
    assert state.countries["ELAR"].food == pytest.approx(100_000.0)
    assert any(item["action_type"] == "TRADE" for item in state.receipts["MARIS"])


def test_barracks_reports_share_one_staffed_training_pool():
    engine = engine_of()
    state = engine.state
    country = pay_up(state.countries["ELAR"])
    country.armies = []
    tiles = country.tiles[:3]
    country.buildings.update({tile: "BARRACKS" for tile in tiles})
    country.new_army(tile=tiles[0], units={}, recruiting=True)
    target = country.new_army(tile=tiles[1], units={"MILITIA": 1}, recruiting=True)
    queued = country.new_army(tile=tiles[2], units={"MILITIA": 1}, recruiting=True)
    engine.military.barracks_daily()
    reports = barracks_reports(country, state.config)
    assert {report["training_army_id"] for report in reports} == {target.army_id}
    assert {report["progress_per_day"] for report in reports} == {3}
    assert target.progress == 3 and queued.progress == 0


def test_the_observation_and_the_prompt_read_the_same_fields():
    """AI 与面板读的是同一批字段，提示词也得把这些字段的读法讲清楚。"""
    engine = engine_of()
    state = engine.state
    tile, army = camp_on(engine)
    engine.step_day()

    observation = build_observation(state, "ELAR")
    assert [row["tile"] for row in observation["country"]["barracks"]] == [[tile[0], tile[1]]]
    assert observation["country"]["barracks"][0]["army_id"] == army.army_id
    assert {row["unit"] for row in observation["country"]["recruit_options"]} == set(state.config["units"])
    assert "ledger" in observation["diplomacy"]

    settings = LLMSettings(
        provider="openai", model="test", base_url="http://127.0.0.1:9/v1",
        api_key_env="EOS_TEST_KEY",
    )
    agent = OpenAICompatibleAgent("ELAR", settings, CallBudget(max_calls=1, max_cost_usd=0.01))
    prompt = agent._decision_request(observation)["messages"][0]["content"]
    for needle in ("country.barracks", "country.recruit_options", "diplomacy.ledger",
                   "stall 是今天为什么不出兵"):
        assert needle in prompt
