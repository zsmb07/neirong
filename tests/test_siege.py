"""围城与整城易主：城墙不是一下午能打完的，封锁圈里的军队每天啃一口，啃穿就整座换手。

The player asked for 攻城 to end with 整城易主 rather than hex-by-hex drift, so one ledger on
the city is the whole truth: `siege_ledger` is what the daily settlement advances, what the
tile panel quotes, and what the city card prints. These tests pin the three things that can
rot independently — the share the ring is worth, the day the wall gives out, and everything
that moves with the city when it does.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agents.heuristic import HeuristicAgent
from agents.llm_adapter import CallBudget, LLMSettings, OpenAICompatibleAgent
from agents.personality import personality_definition
from agents.scheduler import build_observation
from core.content import SIEGE_KEYS, validate_content
from core.messages import compose_ruler_messages
from core.rules import RuleEngine
from core.state import CityState, GameState, MarchOrder, siege_besiegers, siege_ledger
from worldgen import hex_distance

ROOT = Path(__file__).resolve().parents[1]


def engine_of(seed=2000):
    """A bare settlement: no planners, so nothing but these orders touches the state."""
    return RuleEngine(
        GameState.new(seed=seed), planner=None,
        enable_city_manager=False, enable_frontier_ai=False,
    )


def at_war(engine, actor, rival):
    declared = engine.apply_action(actor, {
        "type": "DIPLOMACY", "operation": "DECLARE_WAR", "target": rival,
    })
    assert declared.accepted, declared.reason_text
    return engine.state.countries[rival]


def strike_tile(actor, rival, army_id, tile):
    return {"type": "MILITARY", "operation": "ATTACK", "target": rival, "army_id": army_id,
            "tile": {"col": tile[0], "row": tile[1]}}


def only_army(country, tile, units=8):
    country.armies = []
    country.army_serial = 0
    return country.new_army(tile=tile, units={"MILITIA": units})


def ring_of(engine, city, defender):
    """The six hexes a blockade stands in, with any non-enemy hex first."""
    return sorted(
        engine.state.world.neighbors(*city.center),
        key=lambda coord: (coord in defender.tiles, coord[1], coord[0]),
    )


def besieged(engine, actor="ELAR", rival="VAYR", units=8):
    """An enemy capital with one of `actor`'s armies already standing in its ring."""
    state = engine.state
    defender = at_war(engine, actor, rival)
    city = defender.cities[0]
    army = only_army(state.countries[actor], ring_of(engine, city, defender)[0], units=units)
    ordered = engine.apply_action(actor, strike_tile(actor, rival, army.army_id, city.center))
    assert ordered.accepted, ordered.reason_text
    return defender, city, army, ordered


def events(state, kind):
    return [event for event in state.event_log if event["type"] == kind]


def days_until_fall(engine, city, limit=200):
    """Step the world until this city has changed hands, and say how long the wall held."""
    before = len(events(engine.state, "city_fallen"))
    for day in range(1, limit + 1):
        engine.step_day()
        if len(events(engine.state, "city_fallen")) > before:
            return day
    raise AssertionError(f"{city.name} 在 {limit} 天内没有陷落")


# --- 下令：中心格开的是封锁账，不是一场会战 -------------------------------------------------

def test_ordering_a_center_opens_a_blockade_instead_of_a_battle():
    engine = engine_of()
    state = engine.state
    defender, city, army, ordered = besieged(engine)
    ledger = ordered.effects["siege"]

    assert city.siege == {
        "attacker": "ELAR", "armies": [army.army_id], "progress": 0.0, "started_day": 0,
    }
    assert ledger["defender"] == "VAYR" and ledger["armies"] == [army.army_id]
    assert ledger["garrison"] == [] and ledger["walls"] is False
    assert events(state, "battle") == [], "walls are not taken by one afternoon of fighting"
    assert events(state, "siege_started")[0]["city_id"] == "VAYR-C1"
    assert engine.military.tile_strikes("ELAR", city.center)["siege"] == siege_ledger(
        state.countries, defender, city, state.config
    ), "the panel and the settlement answer from one ledger"


def test_a_center_order_needs_the_ring_and_the_right_army():
    engine = engine_of()
    state = engine.state
    elar, defender = state.countries["ELAR"], at_war(engine, "ELAR", "VAYR")
    city = defender.cities[0]
    army = only_army(elar, elar.tiles[0])

    refused = engine.apply_action("ELAR", strike_tile("ELAR", "VAYR", army.army_id, city.center))
    assert not refused.accepted and refused.reason_code == "NO_PATH"
    assert army.march is None and city.siege is None, "a refused order leaves nothing behind"

    stranger = engine.apply_action("ELAR", strike_tile("ELAR", "VAYR", "VAYR-A9", city.center))
    assert not stranger.accepted and stranger.reason_code in {"UNKNOWN_ARMY", "NOT_OWNED"}

    army.tile = ring_of(engine, city, defender)[0]
    opened = engine.apply_action("ELAR", strike_tile("ELAR", "VAYR", army.army_id, city.center))
    assert opened.accepted and opened.effects["siege"]["armies"] == [army.army_id]


def test_the_ring_is_what_a_blockade_counts_not_the_hex_a_battle_needs():
    engine = engine_of()
    state = engine.state
    elar, defender = state.countries["ELAR"], at_war(engine, "ELAR", "VAYR")
    city = defender.cities[0]
    army = only_army(elar, elar.tiles[0])

    quote = engine.military.tile_strikes("ELAR", city.center)
    assert quote["city_center"] is True and quote["reason"] is None
    assert quote["siege"]["armies"] == [] and quote["siege"]["days_left"] is None
    row = quote["strikes"][0]
    assert row["can_attack"] is False, "the ring is still enemy soil"


# --- 每日：账上啃多少，面板就报几天 --------------------------------------------------------

def test_an_unopposed_ring_takes_exactly_the_days_the_ledger_promised():
    engine = engine_of()
    _defender, city, _army, ordered = besieged(engine)
    promised = ordered.effects["siege"]["days_left"]
    assert promised == 17

    assert days_until_fall(engine, city) == promised
    assert events(engine.state, "siege_lifted")[-1]["reason"] == "captured"


def test_each_day_advances_the_ledger_by_the_quoted_share():
    engine = engine_of()
    defender, city, army, ordered = besieged(engine)
    daily = ordered.effects["siege"]["daily"]
    elar = engine.state.countries["ELAR"]

    engine.step_day()
    assert city.siege["progress"] == pytest.approx(daily)
    assert siege_ledger(engine.state.countries, defender, city, engine.state.config)[
        "days_left"
    ] == 16
    assert defender.war_exhaustion == pytest.approx(0.05)
    assert elar.war_exhaustion == pytest.approx(0.05)

    engine.step_day()
    assert city.siege["progress"] == pytest.approx(2 * daily)
    assert army.march is None, "a blockading army stays where it was quoted"


def test_walls_and_a_garrison_both_slow_the_fall_by_the_same_arithmetic():
    quotes = {}
    for label, wall, garrison in (("open", False, 0), ("walled", True, 0), ("manned", False, 8)):
        engine = engine_of()
        state = engine.state
        defender = at_war(engine, "ELAR", "VAYR")
        city = defender.cities[0]
        if wall:
            defender.buildings[city.center] = "WALL"
        if garrison:
            only_army(defender, city.center, units=garrison)
        army = only_army(state.countries["ELAR"], ring_of(engine, city, defender)[0])
        quotes[label] = engine.apply_action(
            "ELAR", strike_tile("ELAR", "VAYR", army.army_id, city.center)
        ).effects["siege"]

    assert quotes["open"]["days_left"] == 17
    assert quotes["walled"]["walls"] is True and quotes["walled"]["days_left"] == 20
    assert quotes["walled"]["share"] == pytest.approx(8 / 9.6, abs=1e-4)
    assert quotes["manned"]["garrison"] and quotes["manned"]["share"] == pytest.approx(0.5)
    assert quotes["manned"]["days_left"] == 34


def test_the_panel_quotes_the_fall_date_before_any_order_exists():
    engine = engine_of()
    state = engine.state
    elar, defender = state.countries["ELAR"], at_war(engine, "ELAR", "VAYR")
    city = defender.cities[0]
    army = only_army(elar, ring_of(engine, city, defender)[0])

    projection = engine.military.tile_strikes("ELAR", city.center)["siege"]
    assert projection["attacker"] == "ELAR" and projection["armies"] == [army.army_id]
    assert projection["progress"] == 0.0 and projection["days_left"] == 17

    _defender, _city, _army, ordered = besieged(engine_of())
    assert ordered.effects["siege"]["days_left"] == projection["days_left"], (
        "the date a panel prints is the date the settlement reaches"
    )


# --- 解围：圈里没人，或者不再打仗 ------------------------------------------------------------

def test_a_blockade_lifts_when_the_ring_is_empty():
    engine = engine_of()
    defender, city, army, _ordered = besieged(engine)
    engine.step_day()

    army.tile = defender.tiles[0]
    army.march = MarchOrder(
        purpose="MARCH", route=[defender.tiles[0], defender.tiles[0]], cumulative=[0.0, 50.0],
    )
    engine.step_day()

    assert city.siege is None
    lifted = events(engine.state, "siege_lifted")[-1]
    assert lifted["reason"] == "ring_empty" and lifted["progress"] == pytest.approx(0.06)


def test_a_marching_or_bled_out_army_counts_for_nothing():
    engine = engine_of()
    state = engine.state
    elar, defender = state.countries["ELAR"], at_war(engine, "ELAR", "VAYR")
    city = defender.cities[0]
    army = only_army(elar, ring_of(engine, city, defender)[0])
    listed = [army.army_id]

    army.march = MarchOrder(purpose="MARCH", route=[army.tile, army.tile], cumulative=[0.0, 50.0])
    assert siege_besiegers(elar, city, state.config, listed=listed) == []
    army.march = None
    army.units = {}
    assert siege_besiegers(elar, city, state.config, listed=listed) == []


def test_peace_lifts_a_blockade_the_day_after_it_is_signed():
    engine = engine_of()
    state = engine.state
    defender, city, _army, _ordered = besieged(engine)
    elar = state.countries["ELAR"]
    engine.step_day()
    assert city.siege["progress"] == pytest.approx(0.06)

    proposal = engine.apply_action("ELAR", {
        "type": "DIPLOMACY", "operation": "PROPOSE_PEACE", "target": "VAYR",
    })
    assert proposal.accepted, proposal.reason_text
    signed = engine.apply_action("VAYR", {
        "type": "DIPLOMACY", "operation": "ACCEPT_PACT",
        "proposal_id": proposal.effects["proposal_id"],
    })
    assert signed.accepted, signed.reason_text
    assert "VAYR" not in elar.at_war_with

    engine.step_day()
    assert city.siege is None
    assert events(state, "siege_lifted")[-1]["reason"] == "peace"


# --- 一城一主：第二个围城者被指着去看别人 ---------------------------------------------------

def test_one_wall_lies_to_one_realm():
    engine = engine_of()
    state = engine.state
    defender, city, first, _ordered = besieged(engine)
    rival = only_army(state.countries["SAHAR"], ring_of(engine, city, defender)[1])
    at_war(engine, "SAHAR", "VAYR")

    refused = engine.apply_action("SAHAR", strike_tile("SAHAR", "VAYR", rival.army_id, city.center))
    assert not refused.accepted and refused.reason_code == "SIEGE_TAKEN"
    assert city.siege["attacker"] == "ELAR" and city.siege["armies"] == [first.army_id]

    quote = engine.military.tile_strikes("SAHAR", city.center)
    assert quote["siege_taken"]["attacker"] == "ELAR"
    assert quote["siege"] is None, "a rival ledger is not this realm's to quote"
    assert [row["can_attack"] for row in quote["strikes"]] == [False]


def test_a_second_army_of_the_same_realm_joins_the_same_ledger():
    engine = engine_of()
    state = engine.state
    elar, defender = state.countries["ELAR"], at_war(engine, "ELAR", "VAYR")
    city = defender.cities[0]
    ring = ring_of(engine, city, defender)
    first = elar.new_army(tile=ring[0], units={"MILITIA": 4})
    assert engine.apply_action(
        "ELAR", strike_tile("ELAR", "VAYR", first.army_id, city.center)
    ).accepted
    second = elar.new_army(tile=ring[1], units={"MILITIA": 4})

    joined = engine.apply_action("ELAR", strike_tile("ELAR", "VAYR", second.army_id, city.center))
    assert joined.accepted and joined.effects["joined"] is True
    assert joined.effects["siege"]["armies"] == [first.army_id, second.army_id]
    assert len(events(state, "siege_joined")) == 1

    again = engine.apply_action("ELAR", strike_tile("ELAR", "VAYR", second.army_id, city.center))
    assert again.accepted and again.effects["joined"] is False
    assert len(events(state, "siege_joined")) == 1


# --- AI：读同一本账、围同一面墙、城破之前先求和 -------------------------------------------------

def test_the_rows_an_agent_reads_are_the_settlement_ledger():
    engine = engine_of()
    state = engine.state
    defender, city, army, _ordered = besieged(engine)
    row = engine.military.siege_opportunities("ELAR")["VAYR"][0]
    ledger = siege_ledger(state.countries, defender, city, state.config)

    assert row["city_id"] == "VAYR-C1"
    assert row["center"] == {"col": city.center[0], "row": city.center[1]}
    assert row["armies"] == [army.army_id] and row["open"] is True
    assert row["taken_by"] is None
    for key in ("days_left", "progress", "walls", "share"):
        assert row[key] == ledger[key], "an agent plans with the numbers the day will run on"

    campaigns = build_observation(state, "ELAR")["campaign_options"]
    assert campaigns["VAYR"]["siege"] == [row]


def test_a_wall_nobody_has_ordered_yet_is_quoted_as_an_open_blockade():
    engine = engine_of()
    state = engine.state
    elar, defender = state.countries["ELAR"], at_war(engine, "ELAR", "VAYR")
    city = defender.cities[0]
    army = only_army(elar, ring_of(engine, city, defender)[0])

    row = engine.military.siege_opportunities("ELAR")["VAYR"][0]
    assert row["open"] is False and row["taken_by"] is None
    assert row["armies"] == [army.army_id]
    assert row["days_left"] == 17 and row["progress"] == 0.0
    assert city.siege is None, "standing near the wall is not yet a blockade on the books"

    assert engine.military.tile_strikes("ELAR", city.center)["siege"]["open"] is False
    assert engine.apply_action(
        "ELAR", strike_tile("ELAR", "VAYR", army.army_id, city.center)
    ).accepted
    assert engine.military.tile_strikes("ELAR", city.center)["siege"]["open"] is True


def test_own_walls_answer_their_blockade_from_the_tile_they_are_clicked_on():
    engine = engine_of()
    state = engine.state
    elar = state.countries["ELAR"]
    at_war(engine, "VAYR", "ELAR")
    city = elar.cities[0]
    invader = only_army(state.countries["VAYR"], ring_of(engine, city, elar)[0])
    assert engine.apply_action(
        "VAYR", strike_tile("VAYR", "ELAR", invader.army_id, city.center)
    ).accepted
    engine.step_day()

    quote = engine.military.tile_strikes("ELAR", city.center)
    assert quote["city_center"] is True and quote["reason"] == "OWN_TERRITORY"
    assert quote["strikes"] == [], "a realm is never asked to attack its own gate"
    ledger = quote["siege"]
    assert ledger["attacker"] == "VAYR" and ledger["open"] is True
    assert ledger["armies"] == [invader.army_id] and ledger["days_left"] == 16
    assert engine.military.tile_strikes("VAYR", city.center)["siege"] == ledger, (
        "both sides of one wall read one ledger"
    )

    quiet = state.countries["SAHAR"].cities[0]
    assert engine.military.tile_strikes("SAHAR", quiet.center)["siege"] is None


def test_the_agent_blockades_walls_instead_of_marching_the_ring_off():
    engine = engine_of()
    state = engine.state
    _defender, city, army, _ordered = besieged(engine)
    decision = asyncio.run(HeuristicAgent("ELAR").decide(build_observation(state, "ELAR")))
    strikes = [action for action in decision["actions"] if action.get("operation") == "ATTACK"]

    assert [action["army_id"] for action in strikes] == [army.army_id]
    assert strikes[0]["tile"] == {"col": city.center[0], "row": city.center[1]}
    assert not [
        action for action in decision["actions"]
        if action.get("operation") == "MARCH" and action.get("army_id") == army.army_id
    ], "an army lying around a wall is not sent to a gate battle"
    for action in strikes:
        assert engine.apply_action("ELAR", action).accepted


def test_a_second_realm_is_not_ordered_onto_a_wall_another_already_holds():
    engine = engine_of()
    state = engine.state
    defender, city, _first, _ordered = besieged(engine)
    rival = only_army(state.countries["SAHAR"], ring_of(engine, city, defender)[1])
    at_war(engine, "SAHAR", "VAYR")

    row = engine.military.siege_opportunities("SAHAR")["VAYR"][0]
    assert row["armies"] == [rival.army_id] and row["taken_by"] == "ELAR"
    campaigns = build_observation(state, "SAHAR")["campaign_options"]
    assert campaigns["VAYR"]["siege"] == [row]
    assert HeuristicAgent._siege_orders(campaigns, ["VAYR"], 4) == [], (
        "one wall lies to one realm: a second ledger on the same city is only refused"
    )


def test_starving_walls_make_the_besieged_realm_sue_for_peace_before_they_fall():
    engine = engine_of()
    state = engine.state
    elar = state.countries["ELAR"]
    at_war(engine, "VAYR", "ELAR")
    city = elar.cities[0]
    invader = only_army(state.countries["VAYR"], ring_of(engine, city, elar)[0])
    assert engine.apply_action(
        "VAYR", strike_tile("VAYR", "ELAR", invader.army_id, city.center)
    ).accepted

    observation = build_observation(state, "ELAR")
    own = next(row for row in observation["country"]["cities"] if row["siege"])
    assert own["city_id"] == "ELAR-C1"
    assert own["siege"]["attacker"] == "VAYR" and own["siege"]["armies"] == [invader.army_id]

    profile = personality_definition(elar.personality)["mock"]
    rules = observation["rules"]
    pressure = float(rules["military"]["siege"]["peace_pressure"])
    annex = float(rules["military"]["annexation"]["peace_pressure"])
    idle = HeuristicAgent._peace_gate(profile, {"cities": [], "tribute": {}}, rules)
    assert idle == pytest.approx(profile["peace_exhaustion"])

    for _ in range(5):
        engine.step_day()
    starving = build_observation(state, "ELAR")
    row = starving["country"]["cities"][0]
    progress = row["siege"]["progress"]
    assert row["capital"] is True, "a one-city realm's town is its seat"
    gate = HeuristicAgent._peace_gate(profile, starving["country"], rules)
    assert gate == pytest.approx(idle - (pressure + annex) * progress), (
        "the fall is priced before it happens, in the share of the wall already eaten"
    )
    town = dict(starving["country"], cities=[dict(row, capital=False)])
    assert HeuristicAgent._peace_gate(profile, town, rules) == pytest.approx(
        idle - pressure * progress
    ), "only the seat carries the annexation premium"

    day = 5
    while day < 17:
        engine.step_day()
        day += 1
        actions = asyncio.run(
            HeuristicAgent("ELAR").decide(build_observation(state, "ELAR"))
        )["actions"]
        if any(action.get("operation") == "PROPOSE_PEACE" for action in actions):
            break
    assert day < 17, f"the realm waited {day} days, past the fall the ledger promised"
    assert city.siege is not None, "peace was offered while the wall still stood"


def test_the_llm_prompt_teaches_the_blockade():
    engine = engine_of()
    _defender, city, _army, _ordered = besieged(engine)
    settings = LLMSettings(
        provider="test", model="mock", base_url="https://example.test",
        api_key_env="EOS_TEST_KEY",
    )
    request = OpenAICompatibleAgent(
        "ELAR", settings, CallBudget()
    )._decision_request(build_observation(engine.state, "ELAR"))
    system = request["messages"][0]["content"]

    for needle in ("campaign_options[敌国].siege", "country.cities[].siege", "blockade_radius",
                   "taken_by", "wall_bonus", "daily_progress", "城破则整座易主",
                   "annexation.peace_pressure"):
        assert needle in system
    assert "不是会战而是围城账" in system
    assert city.city_id not in system, "the prompt teaches the rule, not this season's answer"


# --- 城破：整座换手，地、人、建筑与贡金一起走 -------------------------------------------------

def test_a_fallen_city_moves_whole_with_its_ground_and_people():
    engine = engine_of()
    state = engine.state
    elar = state.countries["ELAR"]
    defender, city, _army, _ordered = besieged(engine)
    ground, zones = list(city.tiles), dict(city.zones)
    farm = next(coord for coord in ground if coord != city.center)
    defender.buildings[farm] = "FARM"
    defender.projects.append({
        "project_id": "P000001", "tile": farm, "building": "MINES",
        "started_day": state.day_index, "remaining_days": 20.0, "cost": 12.0,
    })
    population, stability = city.population, defender.stability
    elar_pop, defender_pop = elar.population, defender.population

    assert days_until_fall(engine, city) == 17

    assert city in elar.cities and city not in defender.cities
    assert city.city_id == "VAYR-C1" and city.name == "维尔山麓诸侯都城"
    assert city.tiles == ground and city.zones == zones, "the city moves as one object"
    assert city.managed_by == "ai" and city.siege is None
    for coord in ground:
        assert coord in elar.tiles and coord not in defender.tiles
    assert elar.buildings[farm] == "FARM" and farm not in defender.buildings
    assert defender.projects == [], "work in progress changed masters with the ground"
    assert defender.population == defender_pop - population
    assert elar.population == elar_pop + population
    assert defender.stability == pytest.approx(max(0.0, stability - 8.0))
    assert [levy["building"] for levy in defender.levies if levy["to"] == "ELAR"] == ["FARM"]
    assert sum(levy["share"] for levy in defender.levies) == pytest.approx(0.02)

    fallen = events(state, "city_fallen")[-1]
    assert fallen["city_id"] == "VAYR-C1" and fallen["tiles"] == len(ground)
    assert fallen["buildings"] == ["FARM"] and fallen["population"] == population


def test_a_realm_that_lost_its_capital_never_comes_back_on():
    """A starved capital is the end of the realm, not the start of a landless rump.

    Before v0.40 a conquered court survived on an empty ledger and its frontier planner
    spent the next season re-founding a capital out of nowhere, so the map kept painting a
    realm that owned no soil. The capital now carries the whole realm with it, so the only
    honest assertion left is that sixty more days change nothing.
    """
    engine = engine_of()
    state = engine.state
    elar = state.countries["ELAR"]
    defender, city, _army, _ordered = besieged(engine)
    ground = list(defender.tiles)
    days_until_fall(engine, city)
    tombstone = dict(state.eliminated)
    assert defender.cities == [] and defender.tiles == []
    annexed = events(state, "realm_annexed")[-1]
    assert tombstone == {"VAYR": annexed["day"]}, "the tombstone dates the day the court fell"

    for _ in range(60):
        engine.step_day()

    assert "VAYR" not in state.countries, "the annexed court is off the board for good"
    assert city in elar.cities, "the conquered capital stays where the victor put it"
    assert all(coord in elar.tiles for coord in ground)
    summary = state.summary()
    assert "VAYR" not in summary["countries"]
    assert summary["eliminated"] == tombstone
    captured = next(row for row in summary["countries"]["ELAR"]["cities"]
                    if row["city_id"] == "VAYR-C1")
    assert captured["siege"] is None, "nobody blockades a wall that is already ours"


# --- 城墙建筑：只能立在中心格 ---------------------------------------------------------------

def build(actor, building, tile):
    return {"type": "BUILD", "building": building,
            "tile": {"col": tile[0], "row": tile[1]}}


def test_a_wall_can_only_be_laid_on_a_city_center():
    engine = engine_of()
    elar = engine.state.countries["ELAR"]
    elar.treasury = 1_000.0
    elar.technology["bronze_metallurgy"] = 3
    city = elar.cities[0]
    ground = next(coord for coord in city.tiles if coord != city.center)

    refused = engine.apply_action("ELAR", build("ELAR", "WALL", ground))
    assert not refused.accepted and refused.reason_code == "NOT_CITY_CENTER"

    accepted = engine.apply_action("ELAR", build("ELAR", "WALL", city.center))
    assert accepted.accepted, accepted.reason_text
    assert any(
        project["building"] == "WALL" and tuple(project["tile"]) == city.center
        for project in elar.projects
    )


def test_a_wall_is_a_technology_gate_before_it_is_a_number():
    engine = engine_of()
    elar = engine.state.countries["ELAR"]
    elar.treasury = 1_000.0
    city = elar.cities[0]

    refused = engine.apply_action("ELAR", build("ELAR", "WALL", city.center))
    assert not refused.accepted and refused.reason_code == "TECHNOLOGY_REQUIRED"


# --- 面板与短讯：城墙上的数字只有一个来源 ------------------------------------------------------

def test_the_panel_prints_the_blockade_straight_from_the_ledger():
    dashboard = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    for needle in ("function tileSiegeCard(price){", "price.siege_taken", "held=price.siege_taken",
                   "rows=(quotes[id]||{}).siege||[]", "围城战线", 'data-siege-city=',
                   "siegeBadge(city)", "${siegeBar(ledger)}${siegeArmies(ledger)}",
                   "case 'siege_started'", "case 'city_fallen'", "围城封锁圈"):
        assert needle in dashboard
    assert "function tileSiegeCard" in dashboard and "tileSiegeCard(price)" in dashboard
    assert "CITY_CENTER" not in dashboard, "中心格不再被拒：它开的是一本围城账"
    assert "整座易主" in dashboard and "封锁圈" in dashboard


def test_the_rules_page_prints_the_siege_table_from_config():
    """规则页那张常量卡只能抄 `military.siege`，不许自己写一份数字。

    A player reading the constants is the only way to sanity-check a siege without opening
    `rules.json`, so every one of the seven keys has to be printed from the snapshot and no
    number may be typed into the panel — a hardcoded 0.06 there would survive a rebalance and
    then teach the wrong fall date.
    """
    dashboard = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    assert "围城与整城易主常量" in dashboard
    assert "const sg=rules.siege||{},wall=buildings.WALL||{};" in dashboard
    block = dashboard.split("const siegeStats=[", 1)[1].split("].map(([label,value])=>stat(label,value))", 1)[0]
    for key in sorted(SIEGE_KEYS):
        assert f"sg.{key}" in block, key
    assert "wall.cost" in block and "wall.days" in block and "wall.required_technology" in block
    for hardcoded in ("0.06", "1.60", "8.0", "10.0", "0.05"):
        assert hardcoded not in block, f"{hardcoded} 是抄来的数字，不是读来的"


def test_a_blockade_and_a_fall_both_reach_the_ruler_messages():
    engine = engine_of()
    state = engine.state
    defender, city, _army, _ordered = besieged(engine)
    opening = {
        message["country_id"]: message
        for message in compose_ruler_messages(state, [events(state, "siege_started")[0]], {})
    }
    assert opening["ELAR"]["title"] == "围城开局" and "封锁圈" in opening["ELAR"]["text"]
    assert "解围" in opening["VAYR"]["text"]

    days_until_fall(engine, city)
    fallen = {
        message["country_id"]: message
        for message in compose_ruler_messages(state, [events(state, "city_fallen")[0]], {})
    }
    assert fallen["ELAR"]["title"] == "敌城陷落" and "格城域" in fallen["ELAR"]["text"]
    assert "VAYR" not in fallen, "a court annexed at its capital has no ruler left to mail"

    annexed = {
        message["country_id"]: message
        for message in compose_ruler_messages(state, [events(state, "realm_annexed")[0]], {})
    }
    assert annexed["ELAR"]["title"] == "敌国覆灭" and "不在这张地图上" in annexed["ELAR"]["text"]
    assert set(annexed) == {"ELAR"}, "only the victor is told a realm just left the board"


def test_a_plain_city_fall_still_mails_both_courts():
    """The defender keeps its mail only while it still has a capital to sit in.

    Feeding the builder a fall of a non-capital town is the one way to prove the surviving
    side of `city_fallen` still answers both courts — every realm in a seed world owns
    exactly one city, so no settlement path reaches that branch without annexing first.
    """
    engine = engine_of()
    state = engine.state
    messages = {
        message["country_id"]: message
        for message in compose_ruler_messages(state, [{
            "type": "city_fallen", "attacker": "ELAR", "defender": "VAYR",
            "city_id": "VAYR-C2", "city": "边镇", "tiles": 5, "population": 900,
            "buildings": [], "tribute": [], "day": state.day_index,
        }], {})
    }
    assert messages["VAYR"]["title"] == "城池易主" and "落到" in messages["VAYR"]["text"]
    assert messages["ELAR"]["title"] == "敌城陷落"


# --- 存档：账本跟着城走，旧档没有这一栏也不炸 --------------------------------------------------

def test_the_blockade_survives_a_save_round_trip(tmp_path):
    engine = engine_of()
    state = engine.state
    defender, city, army, _ordered = besieged(engine)
    for _ in range(3):
        engine.step_day()
    expected = dict(city.siege)

    restored = GameState.load(state.save(tmp_path / "siege.json"))
    same = restored.countries["VAYR"].cities[0]
    assert same.siege == expected
    assert siege_ledger(restored.countries, defender, same, restored.config)[
        "armies"
    ] == [army.army_id]
    assert restored.countries["VAYR"].cities[0].siege["progress"] == pytest.approx(0.18)


def test_an_older_save_without_a_siege_column_reads_as_no_blockade():
    raw = json.loads(json.dumps(GameState.new(seed=2000).countries["VAYR"].cities[0].to_dict()))
    assert "siege" in raw
    del raw["siege"]
    assert CityState.from_dict(raw).siege is None


# --- 闸门：围城数字只有一份，缺一个就报错 ------------------------------------------------------

def test_the_siege_table_is_validated_content():
    config = json.loads((ROOT / "config" / "rules.json").read_text(encoding="utf-8"))
    validate_content(config)

    for key in ("daily_progress", "fall_threshold", "blockade_radius", "wall_bonus",
                "peace_pressure", "exhaustion_per_day", "stability_loss"):
        broken = json.loads(json.dumps(config))
        del broken["military"]["siege"][key]
        with pytest.raises(ValueError, match="siege"):
            validate_content(broken)

    stalled = json.loads(json.dumps(config))
    stalled["military"]["siege"]["daily_progress"] = 0.0
    with pytest.raises(ValueError, match="siege"):
        validate_content(stalled)

    unbuildable = json.loads(json.dumps(config))
    del unbuildable["buildings"]["WALL"]["on_city_center"]
    with pytest.raises(ValueError, match="on_city_center"):
        validate_content(unbuildable)


def test_the_city_card_carries_the_same_ledger_as_the_settlement():
    engine = engine_of()
    state = engine.state
    defender, city, army, _ordered = besieged(engine)
    engine.step_day()

    row = next(item for item in state.summary()["countries"]["VAYR"]["cities"]
               if item["city_id"] == city.city_id)
    assert row["siege"]["armies"] == [army.army_id]
    assert row["siege"]["progress"] == pytest.approx(city.siege["progress"])
    assert row["siege"]["days_left"] == 16
    assert next(item for item in state.summary()["countries"]["ELAR"]["cities"]
                if item["city_id"] == "ELAR-C1")["siege"] is None


# --- 开赴：还没有本国军队围着的那面墙，也有一路军队能走到封锁圈里 -------------------------------

def walled_edge(engine, actor_id, defender, city):
    """The two hexes a battle would have had to take to lie to a city.

    `camp` is the ring hex a blockade stands on, `stand` the hex behind it a column camps on
    first. Both are handed to `actor_id` the way a won battle hands them over, so the road to the
    wall runs down our own ground: `march_daily` halts a column the day its next hex belongs to
    somebody else, so a wall whose ring is all enemy soil is one no single order can reach, and
    the march table must not promise it.
    """
    actor = engine.state.countries[actor_id]
    for camp in ring_of(engine, city, defender):
        stand = next(
            (coord for coord in engine.state.world.neighbors(*camp)
             if coord in city.tiles and hex_distance(coord, city.center) > 1),
            None,
        )
        if stand is None:
            continue
        for coord in (camp, stand):
            defender.tiles.remove(coord)
            city.tiles.remove(coord)
            actor.tiles.append(coord)
        return camp, stand
    raise AssertionError("城缘没有第二环的落脚格")


def march_ready(engine, actor="ELAR", rival="VAYR", units=8):
    """An enemy wall nobody lies to, with one of our columns one day from its ring."""
    defender = at_war(engine, actor, rival)
    city = defender.cities[0]
    camp, stand = walled_edge(engine, actor, defender, city)
    army = only_army(engine.state.countries[actor], stand, units=units)
    return defender, city, army, camp, stand


def test_a_wall_nobody_lies_to_is_priced_with_the_march_that_would_reach_it():
    engine = engine_of()
    state = engine.state
    defender, city, army, camp, stand = march_ready(engine)
    row = engine.military.siege_marches("ELAR")["VAYR"][0]

    assert row["city_id"] == city.city_id and row["name"] == city.name
    assert row["center"] == {"col": city.center[0], "row": city.center[1]}
    assert row["camp"] == {"col": camp[0], "row": camp[1]}
    assert row["army_id"] == army.army_id
    assert row["days"] == engine.military.movement().days(row["cost"]) and row["cost"] == pytest.approx(
        engine.military.movement().cost(camp)), "our own ground, one day's march"
    assert row["walls"] is False and row["share"] == 1.0 and row["days_left"] == 17
    assert tuple(army.tile) == stand and city.siege is None

    booked = {"attacker": "ELAR", "armies": [army.army_id], "progress": 0.0,
              "started_day": state.day_index}
    ledger = siege_ledger(state.countries, defender, city, state.config, siege=booked,
                          at={army.army_id: camp})
    assert (row["days_left"], row["share"], row["walls"]) == (
        ledger["days_left"], ledger["share"], ledger["walls"]
    ), "the table prices a wall with the ledger the day it is lain to will run"
    at_home = siege_ledger(state.countries, defender, city, state.config, siege=booked)
    assert at_home["share"] == 0.0 and at_home["days_left"] is None, (
        "a column still standing at home is not already starving a city"
    )


def test_a_column_still_at_home_is_not_counted_as_lying_to_the_wall():
    engine = engine_of()
    elar = engine.state.countries["ELAR"]
    defender, city, army, camp, stand = march_ready(engine)

    army.tile = camp
    assert engine.military.siege_marches("ELAR") == {}, (
        "an army already in the ring is an order given for free, not a march"
    )
    assert [row["armies"] for row in engine.military.siege_opportunities("ELAR")["VAYR"]] == [
        [army.army_id]
    ]

    army.tile = stand
    defender.tiles.append(camp)
    city.tiles.append(camp)
    elar.tiles.remove(camp)
    assert engine.military.siege_marches("ELAR") == {}, (
        "hand the camp hex back and no wall in this realm has a camp tile left"
    )


def test_only_a_march_the_settlement_can_deliver_is_offered():
    engine = engine_of()
    elar = engine.state.countries["ELAR"]
    defender = at_war(engine, "ELAR", "VAYR")
    city = defender.cities[0]
    seat = min(elar.tiles, key=lambda coord: (hex_distance(coord, city.center), coord[1], coord[0]))
    army = only_army(elar, seat)

    assert engine.military.siege_marches("ELAR") == {}, (
        "every ring hex of this wall is enemy soil: there is nothing to camp on"
    )
    assert engine.military.siege_opportunities("ELAR") == {}
    assert engine.military._approach_to("ELAR", army, defender, city.center) is None

    refused = engine.apply_action("ELAR", strike_tile("ELAR", "VAYR", army.army_id, city.center))
    assert not refused.accepted and refused.reason_code == "NO_PATH"
    assert army.march is None and city.siege is None, "a wall the table skips is a wall refused"


def test_the_column_arrives_and_books_the_blockade_the_day_it_gets_there():
    engine = engine_of()
    state = engine.state
    defender, city, army, camp, _stand = march_ready(engine)
    row = engine.military.siege_marches("ELAR")["VAYR"][0]

    ordered = engine.apply_action("ELAR", strike_tile("ELAR", "VAYR", row["army_id"], city.center))
    assert ordered.accepted, ordered.reason_text
    assert army.march is not None and army.march.expedition is False
    assert army.march.route[-1] == camp and army.march.objective == city.center

    for _ in range(row["days"]):
        engine.step_day()
    assert tuple(army.tile) == camp and army.march is None
    assert city.siege["attacker"] == "ELAR" and city.siege["armies"] == [army.army_id]
    assert not events(state, "battle"), "walls are not taken by the afternoon a column walks into"
    started = events(state, "siege_started")[0]
    assert started["days_left"] == row["days_left"] == 17, (
        "the fall date promised on the road is the one the blockade opens with"
    )
    ledger = siege_ledger(state.countries, defender, city, state.config)
    assert ledger["progress"] == pytest.approx(0.06) and ledger["days_left"] == 16


def test_the_agent_sends_a_column_to_a_wall_nobody_lies_to():
    engine = engine_of()
    state = engine.state
    defender, city, army, _camp, _stand = march_ready(engine)
    campaigns = build_observation(state, "ELAR")["campaign_options"]

    assert campaigns["VAYR"]["siege_marches"] == engine.military.siege_marches("ELAR")["VAYR"]
    assert HeuristicAgent._siege_orders(campaigns, ["VAYR"], 4) == [{
        "type": "MILITARY", "operation": "ATTACK", "target": "VAYR",
        "army_id": army.army_id, "tile": {"col": city.center[0], "row": city.center[1]},
    }]


def test_lying_to_a_wall_beats_marching_to_one_and_no_army_is_ordered_twice():
    center = {"col": 17, "row": 20}
    campaigns = {"VAYR": {
        "siege": [{"city_id": "VAYR-C1", "name": "围着的城", "center": center,
                   "armies": ["ELAR-A1"], "open": False, "taken_by": None, "days_left": 30,
                   "progress": 0.0, "walls": False, "share": 1.0}],
        "siege_marches": [
            {"city_id": "VAYR-C1", "name": "开赴的城", "center": center,
             "camp": {"col": 18, "row": 20}, "army_id": "ELAR-A2", "days": 2, "cost": 3.0,
             "walls": False, "share": 1.0, "days_left": 15},
            {"city_id": "VAYR-C2", "name": "啃不动的城", "center": {"col": 21, "row": 24},
             "camp": {"col": 22, "row": 24}, "army_id": "ELAR-A3", "days": 5, "cost": 6.0,
             "walls": True, "share": 0.2, "days_left": None},
        ],
    }}

    assert [order["army_id"] for order in HeuristicAgent._siege_orders(campaigns, ["VAYR"], 1)] == [
        "ELAR-A1"
    ], "a wall already lain to starves today; a column still walking is a promise for later"
    assert [order["army_id"] for order in HeuristicAgent._siege_orders(campaigns, ["VAYR"], 2)] == [
        "ELAR-A1", "ELAR-A2"
    ]
    every = HeuristicAgent._siege_orders(campaigns, ["VAYR"], 4)
    armies = [order["army_id"] for order in every]
    assert armies == ["ELAR-A1", "ELAR-A2", "ELAR-A3"], "one army, one order"
    expected = {"ELAR-A1": center, "ELAR-A2": center, "ELAR-A3": {"col": 21, "row": 24}}
    assert all(order["operation"] == "ATTACK" and order["tile"] == expected[order["army_id"]]
               for order in every)


def test_the_prompt_and_the_panel_both_offer_the_march():
    dashboard = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    for needle in ("(quotes[id]||{}).siege_marches||[]", 'data-siege-march="', "可开赴的城墙",
                   "开赴并开围", "抵达当日自动开围", "落脚格只要不在对方领土里"):
        assert needle in dashboard, needle

    engine = engine_of()
    _defender, city, _army, _camp, _stand = march_ready(engine)
    settings = LLMSettings(
        provider="test", model="mock", base_url="https://example.test",
        api_key_env="EOS_TEST_KEY",
    )
    system = OpenAICompatibleAgent(
        "ELAR", settings, CallBudget()
    )._decision_request(build_observation(engine.state, "ELAR"))["messages"][0]["content"]
    for needle in ("campaign_options[敌国].siege_marches", "camp", "开赴并就地开围",
                   "抵达之后几天城破", "只走己方或无主地"):
        assert needle in system, needle
    assert city.city_id not in system, "the prompt teaches the rule, not this season's answer"
