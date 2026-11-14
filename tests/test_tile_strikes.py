"""按格下令：点一格看驻军，点敌格就用指定那一支（或能赶到它周围的每一支）攻打。

The player asks for a tile-centric console, so every number the panel would print has to come
from the settlement itself: `Military.tile_strikes` prices the pointed tile with the same
`Movement` search and the same `_mass` rule that the resulting battle actually uses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.actions import ActionRejected, validate_action
from core.rules import RuleEngine
from core.state import GameState
from worldgen import hex_distance

ROOT = Path(__file__).resolve().parents[1]


def engine_of(seed=2000):
    """A bare settlement: no planners, so nothing but these actions touches the state."""
    return RuleEngine(
        GameState.new(seed=seed), planner=None,
        enable_city_manager=False, enable_frontier_ai=False,
    )


def clear_armies(country):
    country.armies = []
    country.army_serial = 0
    return country


def declare(engine, actor, rival):
    return engine.apply_action(actor, {
        "type": "DIPLOMACY", "operation": "DECLARE_WAR", "target": rival,
    })


def strike(actor, rival, army_id=None, tile=None):
    action = {"type": "MILITARY", "operation": "ATTACK", "target": rival, "army_id": army_id}
    if tile is not None:
        action["tile"] = {"col": tile[0], "row": tile[1]}
    return action


def last_event(state, kind):
    return next(event for event in reversed(state.event_log) if event["type"] == kind)


def gate_of(engine, actor, rival):
    """(staging tile, enemy gate) from the quote the engine would itself honour."""
    quote = engine.military.strike_options(actor, [rival])[rival]
    staging = (quote["march_target"]["col"], quote["march_target"]["row"])
    gate = (quote["objective"]["col"], quote["objective"]["row"])
    assert hex_distance(staging, gate) == 1
    return staging, gate


def army_parked(engine, country_id, coord, units=3):
    country = engine.state.countries[country_id]
    return country.new_army(tile=coord, units={"MILITIA": units})


def at_home(engine, actor, rival, units=3):
    """A realm with one army camped at home, plus the gate the engine would aim it at.

    `strike_options` answers empty while nobody is waiting to march, so the army has to
    exist before the quote can be read — which is also why the quote is the honest price.
    """
    home = engine.state.countries[actor].army_position or engine.state.countries[actor].tiles[0]
    army = army_parked(engine, actor, home, units)
    staging, gate = gate_of(engine, actor, rival)
    assert hex_distance(home, gate) > 1, "the home camp is not already a siege position"
    return home, army, (staging, gate)


def rows_by_army(payload):
    return {row["army_id"]: row for row in payload["strikes"]}


# --- 驻军：任何一格都能读到，观战视角也一样 --------------------------------------------

def test_a_tile_reports_every_army_standing_on_it_whether_or_not_someone_commands():
    engine = engine_of()
    state = engine.state
    elar = state.countries["ELAR"]
    home = elar.army_position or elar.tiles[0]
    clear_armies(elar)
    first = army_parked(engine, "ELAR", home, units=2)
    second = army_parked(engine, "ELAR", home, units=4)

    for actor in (None, "ELAR"):
        payload = engine.military.tile_strikes(actor, home)
        assert payload["tile"] == {"col": home[0], "row": home[1]}
        assert payload["owner"] == "ELAR"
        assert {row["army_id"] for row in payload["garrison"]} == {first.army_id, second.army_id}
        assert {row["country_id"] for row in payload["garrison"]} == {"ELAR"}

    # The army rows are the same payload the military panel already reads, fields included.
    held = {row["army_id"]: row for row in payload["garrison"]}
    assert held[first.army_id]["strength"] == 2 and held[second.army_id]["strength"] == 4
    assert held[first.army_id]["tile"] == [home[0], home[1]]
    assert held[first.army_id]["march"] is None

    # An observer gets the garrison and nothing else: there is nobody to give an order.
    watching = engine.military.tile_strikes(None, home)
    assert watching["reason"] == "NO_COMMAND" and watching["strikes"] == []


def test_an_unowned_tile_is_read_the_same_way_and_is_not_a_target():
    engine = engine_of()
    state = engine.state
    owners = engine.military._owners()
    neutral = next(
        coord for coord, tile in state.world.tiles.items()
        if tile["land"] and coord not in owners
    )
    payload = engine.military.tile_strikes("ELAR", neutral)
    assert payload["owner"] is None
    assert payload["reason"] == "NO_OWNER" and payload["strikes"] == []
    assert payload["garrison"] == []


def test_owning_the_tile_means_garrison_without_a_strike_list():
    engine = engine_of()
    elar = engine.state.countries["ELAR"]
    home = elar.army_position or elar.tiles[0]
    payload = engine.military.tile_strikes("ELAR", home)
    assert payload["reason"] == "OWN_TERRITORY"
    assert payload["strikes"] == [] and payload["at_war"] is False


# --- 敌格报价：周围六格的军队，按结算引擎的数字列出来 ----------------------------------

def test_a_peaceful_gate_shows_the_garrison_but_refuses_the_order():
    engine = engine_of()
    vayr = engine.state.countries["VAYR"]
    their_home = vayr.army_position or vayr.tiles[0]
    payload = engine.military.tile_strikes("ELAR", their_home)
    assert payload["owner"] == "VAYR" and payload["at_war"] is False
    assert payload["reason"] == "NOT_AT_WAR" and payload["strikes"] == []
    assert {row["country_id"] for row in payload["garrison"]} == {"VAYR"}


def test_a_city_center_is_quoted_as_a_blockade_rather_than_a_battle():
    engine = engine_of()
    state = engine.state
    elar, vayr = state.countries["ELAR"], state.countries["VAYR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    army = army_parked(engine, "ELAR", elar.army_position or elar.tiles[0], units=3)
    center = vayr.cities[0].center

    payload = engine.military.tile_strikes("ELAR", center)
    assert payload["city_center"] is True and payload["reason"] is None
    assert payload["siege_taken"] is None
    assert payload["siege"]["attacker"] == "ELAR" and payload["siege"]["defender"] == "VAYR"
    assert payload["siege"]["armies"] == [], "nobody lies here yet"
    assert payload["siege"]["days_left"] is None
    row = rows_by_army(payload)[army.army_id]
    assert row["can_attack"] is False, "the ring is still enemy soil"


def test_every_army_already_in_the_ring_is_offered_and_fights_at_the_real_mass():
    engine = engine_of()
    state = engine.state
    elar = state.countries["ELAR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    _home, reserve, (staging, gate) = at_home(engine, "ELAR", "VAYR", units=2)
    first = army_parked(engine, "ELAR", staging, units=3)
    second = army_parked(engine, "ELAR", staging, units=3)

    payload = engine.military.tile_strikes("ELAR", gate)
    assert payload["at_war"] is True and payload["reason"] is None
    rows = rows_by_army(payload)
    assert set(rows) == {first.army_id, second.army_id, reserve.army_id}
    for army in (first, second):
        row = rows[army.army_id]
        assert row["can_attack"] is True and row["days"] == 0 and row["cost"] == 0.0
        assert row["march_target"] == {"col": staging[0], "row": staging[1]}
    present, share = engine.military._mass(elar, gate, first)
    assert {army.army_id for army in present} == {first.army_id, second.army_id}
    assert rows[first.army_id]["mass"] == len(present) == 2
    assert rows[first.army_id]["mass_share"] == pytest.approx(round(share, 4))
    assert rows[reserve.army_id]["can_attack"] is False
    assert payload["strikes"][0]["days"] == 0, "the armies already on the ring come first"


def test_the_gate_price_matches_the_column_the_engine_would_order():
    engine = engine_of()
    elar = engine.state.countries["ELAR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    home = elar.army_position or elar.tiles[0]
    army = army_parked(engine, "ELAR", home, units=4)
    quote = engine.military.strike_options("ELAR", ["VAYR"])["VAYR"]

    row = rows_by_army(engine.military.tile_strikes("ELAR",
                                                    (quote["objective"]["col"],
                                                     quote["objective"]["row"])))[army.army_id]
    assert row["days"] == quote["days"] and row["cost"] == quote["cost"]
    assert row["expedition"] == quote["expedition"]
    assert row["march_target"] == quote["march_target"], "one price table, read two ways"


def test_a_named_gate_is_priced_to_that_gate_not_to_the_nearest_one():
    """The point of the request: the tile you click is the tile the army is charged for."""
    engine = engine_of()
    state = engine.state
    elar, vayr = state.countries["ELAR"], state.countries["VAYR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    army = army_parked(engine, "ELAR", elar.army_position or elar.tiles[0], units=4)
    gates = [
        coord for coord in sorted(vayr.tiles, key=lambda item: (item[1], item[0]))
        if coord not in {city.center for city in vayr.cities}
    ]
    priced = []
    for gate in gates[:6]:
        payload = engine.military.tile_strikes("ELAR", gate)
        row = rows_by_army(payload).get(army.army_id)
        if row is None or not row["reachable"]:
            continue
        hold = (row["march_target"]["col"], row["march_target"]["row"])
        assert hex_distance(hold, gate) == 1, "the order lands beside the tile that was clicked"
        assert hold not in vayr.tiles
        priced.append((gate, row["days"]))
    assert len({gate for gate, _days in priced}) >= 2, "several gates must be priceable"
    assert len({days for _gate, days in priced}) >= 1


def test_a_gate_no_single_order_can_reach_is_reported_instead_of_invented():
    engine = engine_of()
    state = engine.state
    elar, vayr = state.countries["ELAR"], state.countries["VAYR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    army = army_parked(engine, "ELAR", elar.army_position or elar.tiles[0], units=3)
    gate = next(coord for coord in vayr.tiles if coord not in {city.center for city in vayr.cities})
    state.config["movement"]["maximum_order_cost"] = 0.01

    row = rows_by_army(engine.military.tile_strikes("ELAR", gate))[army.army_id]
    assert row["reachable"] is False and row["can_attack"] is False
    assert row["days"] is None and row["cost"] is None and row["march_target"] is None
    # The army still arrives on the report, because the panel must not hide it.
    assert row["mass"] >= 1 and row["mass_share"] is not None


# --- 下令：邻近当场会战，远处下真正的行军单 --------------------------------------------

def test_clicking_a_gate_in_the_ring_fights_there_that_same_day():
    engine = engine_of()
    state = engine.state
    elar, vayr = state.countries["ELAR"], state.countries["VAYR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    _home, _lead, (staging, gate) = at_home(engine, "ELAR", "VAYR")
    army = army_parked(engine, "ELAR", staging, units=4)
    before = len(vayr.tiles)

    result = engine.apply_action("ELAR", strike("ELAR", "VAYR", army.army_id, gate))
    assert result.accepted, result.reason_code
    assert result.effects["battle"] is True
    assert result.effects["tile"] == {"col": gate[0], "row": gate[1]}
    assert army.march is None
    event = last_event(state, "battle")
    assert event["tile"] == {"col": gate[0], "row": gate[1]}, "the battle happened where it was pointed"
    assert event["attacker_army"] == army.army_id
    assert (len(vayr.tiles) in (before, before - 1))


def test_a_far_gate_becomes_the_objective_of_the_march_and_is_battle_on_arrival():
    engine = engine_of()
    state = engine.state
    elar, vayr = state.countries["ELAR"], state.countries["VAYR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    _home, army, (_staging, gate) = at_home(engine, "ELAR", "VAYR", units=4)

    result = engine.apply_action("ELAR", strike("ELAR", "VAYR", army.army_id, gate))
    assert result.accepted, result.reason_code
    order = army.march
    assert order is not None and order.purpose == "ATTACK" and order.target == "VAYR"
    assert tuple(order.objective) == gate, "the order carries the tile the player clicked"
    assert hex_distance(order.route[-1], gate) == 1
    assert order.route[-1] not in vayr.tiles

    for _ in range(200):
        engine.military.march_daily()
        if army.march is None:
            break
    assert army.march is None, "the route finishes"
    arrived = last_event(state, "army_arrived")
    assert arrived["objective"] == {"col": gate[0], "row": gate[1]}
    if arrived["fought"]:
        assert last_event(state, "battle")["tile"] == arrived["objective"]
    assert army.tile == order.route[-1] or hex_distance(army.tile, gate) <= 1


def test_the_pointed_gate_is_not_the_auto_pick():
    """Auto-pick takes the cheapest frontier; a clicked gate takes that gate even if pricier."""
    engine = engine_of()
    state = engine.state
    elar, vayr = state.countries["ELAR"], state.countries["VAYR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    _home, army, (_staging, auto) = at_home(engine, "ELAR", "VAYR", units=4)
    other = next(
        coord for coord in sorted(vayr.tiles, key=lambda item: (item[1], item[0]))
        if coord != auto and hex_distance(coord, army.tile) > 1
        and coord not in {city.center for city in vayr.cities}
        and rows_by_army(engine.military.tile_strikes("ELAR", coord)).get(
            army.army_id, {}).get("reachable")
    )
    assert tuple(other) != tuple(auto)

    engine.apply_action("ELAR", strike("ELAR", "VAYR", army.army_id, other))
    assert tuple(army.march.objective) == tuple(other)


def test_an_order_cannot_point_at_a_tile_the_target_does_not_hold():
    engine = engine_of()
    state = engine.state
    elar, vayr, sahar = (state.countries[key] for key in ("ELAR", "VAYR", "SAHAR"))
    clear_armies(elar)
    declare(engine, "ELAR", "SAHAR")
    army = army_parked(engine, "ELAR", elar.army_position or elar.tiles[0], units=3)
    borrowed = vayr.tiles[0]

    refused = engine.apply_action("ELAR", strike("ELAR", "SAHAR", army.army_id, borrowed))
    assert not refused.accepted and refused.reason_code == "NOT_ENEMY_TILE"


def test_a_center_far_from_every_army_is_refused_until_the_ring_is_reached():
    """Walls are not one order away: the ring has to be standing ground first."""
    engine = engine_of()
    state = engine.state
    elar, vayr = state.countries["ELAR"], state.countries["VAYR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    army = army_parked(engine, "ELAR", elar.army_position or elar.tiles[0], units=3)

    refused = engine.apply_action(
        "ELAR", strike("ELAR", "VAYR", army.army_id, vayr.cities[0].center)
    )
    assert not refused.accepted and refused.reason_code == "NO_PATH"
    assert army.march is None and vayr.cities[0].siege is None


def test_a_gate_outside_every_order_budget_is_refused_not_quietly_retargeted():
    engine = engine_of()
    state = engine.state
    elar, vayr = state.countries["ELAR"], state.countries["VAYR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    army = army_parked(engine, "ELAR", elar.army_position or elar.tiles[0], units=3)
    gate = next(coord for coord in vayr.tiles if coord not in {city.center for city in vayr.cities})
    state.config["movement"]["maximum_order_cost"] = 0.01

    refused = engine.apply_action("ELAR", strike("ELAR", "VAYR", army.army_id, gate))
    assert not refused.accepted and refused.reason_code == "NO_PATH"
    assert army.march is None, "a refused order leaves the army where it was"


def test_an_army_already_marching_cannot_be_repointed_at_a_gate():
    engine = engine_of()
    state = engine.state
    elar = state.countries["ELAR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    _home, army, (_staging, gate) = at_home(engine, "ELAR", "VAYR")
    assert engine.apply_action("ELAR", strike("ELAR", "VAYR", army.army_id, gate)).accepted

    again = engine.apply_action("ELAR", strike("ELAR", "VAYR", army.army_id, gate))
    assert not again.accepted and again.reason_code == "MARCH_IN_PROGRESS"
    assert tuple(army.march.objective) == gate


def test_the_plain_attack_order_still_works_and_chooses_the_cheapest_gate():
    """No tile, no behaviour change: the AI keeps issuing exactly this shape."""
    engine = engine_of()
    state = engine.state
    elar = state.countries["ELAR"]
    clear_armies(elar)
    declare(engine, "ELAR", "VAYR")
    _home, army, (_staging, gate) = at_home(engine, "ELAR", "VAYR")

    result = engine.apply_action("ELAR", strike("ELAR", "VAYR", army.army_id))
    assert result.accepted, result.reason_code
    assert tuple(army.march.objective) == gate


# --- 协议：tile 是可选参数，坏了就报出来 ------------------------------------------------

def config():
    return engine_of().state.config


def test_the_protocol_accepts_an_optional_gate_and_rejects_a_malformed_one():
    rules = config()

    def parsed(tile):
        raw = {"type": "MILITARY", "operation": "ATTACK", "target": "VAYR", "army_id": "ELAR-A1"}
        if tile is not None:
            raw["tile"] = tile
        return validate_action("ELAR", raw, rules)

    assert parsed(None)["tile"] is None
    assert parsed({"col": 12, "row": 7})["tile"] == {"col": 12, "row": 7}
    for broken in ({"col": "12", "row": 7}, {"col": 12}, {"col": 12, "row": 7, "extra": 1},
                   "12,7", [12, 7]):
        with pytest.raises(ActionRejected):
            parsed(broken)


def test_the_tile_survives_the_state_round_trip():
    """A march order priced for one gate has to still mean that gate after save/load."""
    from core.state import MarchOrder

    order = MarchOrder(
        purpose="ATTACK", route=[(1, 1), (1, 2)], cumulative=[0.0, 1.0],
        target="VAYR", objective=(3, 4), expedition=True,
    )
    restored = MarchOrder.from_dict(order.to_dict())
    assert tuple(restored.objective) == (3, 4)


# --- 界面与端点：面板不自造数字 ----------------------------------------------------------

def test_the_server_answers_for_one_tile_on_demand():
    live = (ROOT / "live_server.py").read_text(encoding="utf-8")
    assert 'elif path == "/api/tile_strikes":' in live
    assert "self._json(self.session.tile_strikes(col, row))" in live
    assert "military.tile_strikes(self.seat(), (col, row))" in live
    assert 'return {"ok": False, "error": "坐标不在地图范围内"}' in live


def test_the_tile_panel_reads_garrisons_and_prices_from_the_engine():
    dashboard = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    assert "/api/tile_strikes?col=" in dashboard
    assert ",onTile=price?price.garrison:tileGarrisonAt(tile.col,tile.row)" in dashboard
    assert "function tilePrice(tile){" in dashboard
    assert "operation:'ATTACK',target:tile.owner,army_id:btn.dataset.strike" in dashboard
    assert "tile:{col:tile.col,row:tile.row}" in dashboard
    assert "NOT_AT_WAR:'" in dashboard and "CITY_CENTER" not in dashboard, (
        "a city center is no longer refused: it opens a blockade"
    )
    assert 'data-tile-intent="attack"' in dashboard and 'id="tileDispatchDialog"' in dashboard
    assert "if(tileStrikeKey!==key)return;" in dashboard, "a late answer must not paint a new tile"
