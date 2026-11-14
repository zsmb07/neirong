"""Campaign-level invariants for the march batch: order caps, peace terms, paving, columns.

`run_march_balance.py` measures this season; this file pins the parts that must never drift.
The long fixture reads the settlement's own event log, so a parameter that stops being
honoured fails here even while the unit tests around it keep passing.
"""
from __future__ import annotations

import pytest

import run_march_balance as harness
from agents.heuristic import HeuristicAgent
from agents.personality import PERSONALITIES
from core.diplomacy import MINIMUM_PACT_RELATION
from core.rules import RuleEngine
from core.state import GameState
from conftest import set_roster
from live_server import LiveSession

CAMPAIGN_DAYS = harness.DAYS


@pytest.fixture(scope="module")
def campaign():
    """Ten Mock-controlled years on one generated world, read back through the harness."""
    return harness.run_one(
        harness.SEEDS[0], harness.ASSIGNMENTS[0],
        {"name": "appetite", "days": CAMPAIGN_DAYS},
    )


def peace_at(reset: float) -> int:
    """Declare a war, sign the peace and report the relation the settlement left behind."""
    state = GameState.new(seed=2000)
    state.config["military"]["peace_relation_reset"] = reset
    engine = RuleEngine(
        state, planner=None, enable_city_manager=False, enable_frontier_ai=False
    )
    engine.diplomacy.declare_war("ELAR", "MARIS")
    proposal = engine.diplomacy.propose_peace("MARIS", "ELAR")
    engine.state.day_index = 1
    result = engine.diplomacy.respond_pact("ELAR", {
        "operation": "ACCEPT_PACT", "proposal_id": proposal["proposal_id"],
    })
    return result["relation"]


def test_every_order_in_the_campaign_fits_the_cap(campaign):
    assert campaign["marches"] >= 3, "the fixture stopped producing campaigns"
    assert campaign["over_cap_orders"] == 0
    assert campaign["march_days_max"] >= 1, "no order ever priced a real route"


def test_every_battle_fights_inside_the_mass_band(campaign):
    """A strike is never worth less than a lone army, nor more than the whole roster."""
    military = GameState.new(seed=2000).config["military"]
    floor = military["minimum_mass_share"]
    assert campaign["battles"] >= 3, "the fixture stopped producing battles"
    assert all(floor - 1e-9 <= value <= 1.0 for value in campaign["battle_shares"])



def test_the_decade_counts_the_seats_that_were_erased(campaign):
    """A capital's fall is the one way a realm leaves the board, so the two lists add up."""
    assert len(campaign["countries"]) + len(campaign["eliminated"]) == len(harness.COUNTRIES)
    for event in campaign["annexations"]:
        assert event["fallen"] in campaign["eliminated"], "an erased realm still has its tombstone"
        assert event["tiles"] + event["capital_tiles"] >= 1, (
            "a realm erased without ground moving: the sweep is allowed to be empty only "
            "when the capital that triggered it was already the last hex the realm held"
        )
        assert event["treasury"] >= 0.0 and event["food"] >= 0.0


def test_peace_closes_at_the_relation_the_content_table_asks_for(campaign):
    reset = GameState.new(seed=2000).config["military"]["peace_relation_reset"]
    assert campaign["peaces"] >= 1, "no peace was signed inside the fixture window"
    assert campaign["peace_relations"] == [reset]
    # The number must come from the table: the old settlement hardcoded a warmer term.
    assert peace_at(-45.0) == -45
    assert peace_at(30.0) == 30


def test_a_pact_proposal_is_never_asked_below_the_diplomatic_floor():
    for name, definition in PERSONALITIES.items():
        asked = definition["mock"]["pact_relation"]
        assert asked >= MINIMUM_PACT_RELATION, f"{name} proposes pacts the engine refuses"


def test_paving_appetite_is_a_profile_knob_and_stops_when_it_is_filled(monkeypatch):
    agent = HeuristicAgent("ELAR")
    rules = {"buildings": GameState.new(seed=2000).config["buildings"]}
    country = {
        "personality": "neutral", "technology": {}, "buildings": {}, "projects": [],
        "cities": [], "stability": 90.0, "population": 20000.0, "housing": 100.0,
        "food": 240.0, "food_capacity": 400.0, "treasury": 60.0,
    }
    profile = PERSONALITIES["neutral"]["mock"]
    monkeypatch.setitem(profile, "barracks", 0)
    monkeypatch.setitem(profile, "roads", 2)
    assert agent._building_choice(country, rules) == "ROAD"
    paved = {**country, "buildings": {"T1": "ROAD", "T2": "ROAD"}}
    assert agent._building_choice(paved, rules) != "ROAD"
    monkeypatch.setitem(profile, "roads", 0)
    assert agent._building_choice(country, rules) != "ROAD"


def column_option(army_id: str, days: int, objective: tuple[int, int],
                  mass: int = 1) -> dict:
    return {"army_id": army_id, "reachable": True, "can_attack": days == 0,
            "expedition": False, "days": days, "cost": float(days) * 2,
            "march_target": objective, "objective": objective,
            "mass": mass, "mass_share": 0.5 + 0.5 * mass}


def test_a_second_column_joins_only_when_it_marches_on_another_gate():
    gate = (10, 4)
    other = (12, 5)
    column = {
        "VAYR": dict(column_option("A2", 3, gate, mass=2), options=[
            column_option("A1", 3, gate), column_option("A2", 3, gate, mass=2),
            column_option("A3", 5, other),
        ]),
    }
    strike = HeuristicAgent._strike_choice(column, ["VAYR"])
    # Same days, two garrisons already camped at that gate: the heavier column is ordered.
    assert (strike["target"], strike["army_id"]) == ("VAYR", "A2")
    assert strike["companion"] == "A3"
    assert strike["rally"] == gate
    assert strike["reinforce"] == ["A1"], "the column still on the road is told to meet the lead"

    single = {"VAYR": dict(column_option("A1", 3, gate), options=[column_option("A1", 3, gate)])}
    solo = HeuristicAgent._strike_choice(single, ["VAYR"])
    assert solo["companion"] is None and solo["reinforce"] == []

    # An army already standing at the gate strikes now: nothing is massed behind it.
    arrived = {"VAYR": dict(column_option("A1", 0, gate), options=[
        column_option("A1", 0, gate), column_option("A2", 3, gate)])}
    assert HeuristicAgent._strike_choice(arrived, ["VAYR"])["reinforce"] == []


def test_columns_are_priced_per_army_and_capped_by_the_content_table(engine):
    state = engine.state
    columns = state.config["military"]["strike_columns"]
    elar = state.countries["ELAR"]
    elar.armies = []
    elar.army_serial = 0
    for tile in elar.tiles[:columns + 2]:
        elar.new_army(tile=tile, units={"MILITIA": 1})
    engine.diplomacy.declare_war("ELAR", "VAYR")
    priced = engine.military.strike_options("ELAR")["VAYR"]
    assert priced["options"], "armies standing on their own soil cannot be priced at all"
    assert len(priced["options"]) == columns, "the table must cap the columns, not the map"
    assert len({item["army_id"] for item in priced["options"]}) == len(priced["options"])
    assert priced["options"] == sorted(
        priced["options"], key=lambda item: (item["days"], -item["mass"], item["army_id"])
    )
    assert priced["army_id"] == priced["options"][0]["army_id"]


def test_a_tiny_order_cap_leaves_only_armies_that_already_stand_at_the_gate(engine):
    state = engine.state
    state.config["movement"]["maximum_order_cost"] = 0.5
    elar = state.countries["ELAR"]
    set_roster(elar, {"MILITIA": 2}, tile=elar.army_position)
    engine.diplomacy.declare_war("ELAR", "VAYR")
    priced = engine.military.strike_options("ELAR")["VAYR"]
    assert all(item["cost"] <= 0.5 for item in priced["options"])
    assert not priced["reachable"] or priced["days"] == 0, "a half-point cap buys no marching"


def test_the_panel_is_priced_from_the_commanded_realm_only():
    """`campaign_quotes` is the player's copy of the same table the AI reads."""
    session = LiveSession(speed=1.0, maximum_day=1, paused=True)
    try:
        assert session.campaign_quotes() == {}, "observer mode has no one to give orders"
        session.control("take_control", "ELAR")
        assert session.player_action("ELAR", {
            "type": "MILITARY", "operation": "RECRUIT", "unit": "MILITIA", "strength": 2,
        })["ok"]
        assert session.player_action("ELAR", {
            "type": "DIPLOMACY", "operation": "DECLARE_WAR", "target": "VAYR",
        })["ok"]
        column = session.campaign_quotes()["VAYR"]
        assert column["options"], "a commanded realm with a waiting army must see its columns"
        assert column["army_id"] == column["options"][0]["army_id"]
        cap = session.rules["movement"]["maximum_order_cost"]
        assert all(item["cost"] <= cap for item in column["options"])
        session.control("observe")
        assert session.campaign_quotes() == {}
    finally:
        session._stop.set()
        session.thread.join(timeout=2)
