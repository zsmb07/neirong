"""Marching is settled by the world itself: weighted routes, daily budget, roads, arrival battles."""
from copy import deepcopy

import pytest

from core.content import ContentError, validate_content
from core.movement import Movement, march_days
from core.rules import RuleEngine
from core.state import GameState
from conftest import set_roster


def march_action(tile, army_id=None):
    action = {
        "type": "MILITARY", "operation": "MARCH",
        "tile": {"col": tile[0], "row": tile[1]},
    }
    if army_id:
        action["army_id"] = army_id
    return action


def attack_action(target, army_id=None):
    action = {"type": "MILITARY", "operation": "ATTACK", "target": target}
    if army_id:
        action["army_id"] = army_id
    return action


def declare_war(target):
    return {"type": "DIPLOMACY", "operation": "DECLARE_WAR", "target": target}


def events_of(state, kind):
    return [event for event in state.event_log if event["type"] == kind]


def quiet_state(seed=2000):
    """A rule engine with no background planners, so only the test moves the world."""
    state = GameState.new(seed=seed)
    engine = RuleEngine(
        state, planner=None, enable_city_manager=False, enable_frontier_ai=False
    )
    return engine, state


def farthest_walkable_tile(engine, country_id):
    """The dearest hex a plain march order can be parked on.

    Two rules narrow the map down: a march only *ends* on own or unclaimed soil, and the
    route to it must not cross anybody's border — a column may do that, but then it is an
    expedition and the tests that time a march want the unpenalised road. Both are read
    from the settlement's own helpers, so the day counts below are the ones the engine
    quotes rather than a second arithmetic invented here.
    """
    state = engine.state
    military = engine.military
    seat = state.countries[country_id].army_position
    owners = military._owners()
    movement = military.movement(country_id)
    costs, previous = movement.search(seat)
    candidates = (
        coord for coord in costs
        if coord != seat and owners.get(coord) in (None, country_id)
    )
    # Dearest first, so the march has days to spare; ties settle on the same hex the
    # engine would pick.
    ordered = sorted(candidates,
                     key=lambda coord: (-round(costs[coord], 3), coord[1], -coord[0]))
    for coord in ordered:
        plan = military._road_plan(movement, previous, seat, coord)
        if not military._expedition(country_id, plan, owners):
            return coord
    raise AssertionError(f"{country_id} 找不到一格既合法落脚、又不过境敌土的远格")


def strip_movement(biomes, roads=(), **overrides):
    """A one-row hex strip: enough to price terrain, roads and sea without a world file."""
    table = {"movement": {
        "daily_budget": 3.0, "road_cost": 0.35, "maximum_order_cost": 24.0,
        "biome_cost": {
            "grassland": 1.0, "forest": 1.8, "rainforest": 2.2, "desert": 1.9,
            "alpine": 2.4, "savanna": 1.2, "steppe": 1.0, "tundra": 2.1, "wetland": 1.7,
        },
    }}
    table["movement"].update(overrides)
    paved = set(roads)

    def state_of(coord):
        col, row = coord
        if not 0 <= col < len(biomes) or row != 0:
            return None
        biome = biomes[col]
        if biome == "ocean":
            return {"land": False, "biome": biome, "road": False}
        return {"land": True, "biome": biome, "road": coord in paved}

    return Movement(
        table, state_of, lambda coord: [(coord[0] - 1, coord[1]), (coord[0] + 1, coord[1])]
    )


def test_march_days_rounds_any_part_of_a_day_up():
    assert march_days(0.0, 3.0) == 0
    assert march_days(1.0, 3.0) == 1
    assert march_days(6.0, 3.0) == 2
    assert march_days(6.1, 3.0) == 3


def test_movement_prices_terrain_and_turns_ocean_into_a_wall():
    strip = ["grassland"] * 4 + ["forest"] * 4 + ["grassland"] * 4
    movement = strip_movement(strip)
    plan = movement.plan((0, 0), (11, 0))
    assert plan["cost"] == pytest.approx(7 * 1.0 + 4 * 1.8)
    assert plan["days"] == 5
    assert plan["route"][0] == (0, 0) and plan["route"][-1] == (11, 0)
    assert plan["cumulative"][1] == pytest.approx(1.0)
    assert movement.plan((0, 0), (0, 0))["cost"] == 0.0
    assert movement.plan((0, 0), (12, 0)) is None
    assert movement.plan((0, 0), (0, 3)) is None


def test_a_road_beats_the_terrain_under_it_whatever_the_biome():
    strip = ["grassland"] * 4 + ["forest"] * 4 + ["grassland"] * 4
    paved = strip_movement(strip, roads=[(col, 0) for col in range(4, 8)])
    plan = paved.plan((0, 0), (11, 0))
    assert plan["cost"] == pytest.approx(7 * 1.0 + 4 * 0.35)
    assert plan["days"] == 3


def test_orders_are_refused_once_the_route_costs_more_than_the_cap():
    movement = strip_movement(["alpine"] * 12)
    assert movement.plan((0, 0), (9, 0))["cost"] == pytest.approx(9 * 2.4)
    assert movement.plan((0, 0), (11, 0)) is None


def test_validator_rejects_a_movement_table_the_settlement_cannot_use(state):
    config = deepcopy(state.config)
    del config["movement"]["daily_budget"]
    with pytest.raises(ContentError, match="movement 的键必须是"):
        validate_content(config)

    config = deepcopy(state.config)
    del config["movement"]["biome_cost"]["rainforest"]
    with pytest.raises(ContentError, match="缺少陆地群系成本"):
        validate_content(config, {"grassland", "rainforest"})

    config = deepcopy(state.config)
    config["movement"]["biome_cost"]["cloudpeak"] = 1.5
    with pytest.raises(ContentError, match="含未知群系"):
        validate_content(config)

    config = deepcopy(state.config)
    config["movement"]["biome_cost"]["grassland"] = 0
    with pytest.raises(ContentError, match="grassland 必须大于 0"):
        validate_content(config)

    config = deepcopy(state.config)
    config["movement"]["road_cost"] = min(config["movement"]["biome_cost"].values())
    with pytest.raises(ContentError, match="road_cost 必须低于"):
        validate_content(config)


def test_march_order_queues_and_walks_the_route_one_budget_per_day():
    engine, state = quiet_state()
    country = state.countries["ELAR"]
    seat = country.army_position
    army = set_roster(country, {"MILITIA": 3}, tile=seat)
    target = farthest_walkable_tile(engine, "ELAR")

    ordered = engine.apply_action("ELAR", march_action(target))
    assert ordered.accepted
    days = ordered.effects["days"]
    assert days >= 2 and army.tile == seat and army.march is not None
    assert ordered.effects["cost"] == pytest.approx(army.march.cost)
    assert ordered.effects["destination"] == {"col": target[0], "row": target[1]}

    refusal = engine.apply_action("ELAR", march_action(seat))
    assert not refusal.accepted and refusal.reason_code == "MARCH_IN_PROGRESS"

    engine.step_day(force=True)
    assert army.march.spent == state.config["movement"]["daily_budget"] and army.march.remaining > 0
    half_way = army.tile

    for _ in range(days):
        engine.step_day(force=True)
    assert army.tile == target and army.march is None
    assert half_way != target
    arrived = events_of(state, "army_arrived")[-1]
    assert arrived["tile"] == {"col": target[0], "row": target[1]}
    assert arrived["purpose"] == "MARCH" and arrived["fought"] is False
    assert not events_of(state, "battle")


def test_a_march_survives_the_corridor_turning_hostile():
    """A border moving under a column no longer revokes the order.

    The route keeps the price it was quoted with, so the hexes MARIS took mid-walk cost
    this column nothing extra and cost the *next* order the surcharge: crossing is priced
    when the order is placed, not renegotiated underneath it.
    """
    engine, state = quiet_state()
    country = state.countries["ELAR"]
    seat = country.army_position
    army = set_roster(country, {"MILITIA": 3}, tile=seat)
    ordered = engine.apply_action("ELAR", march_action(farthest_walkable_tile(engine, "ELAR")))
    assert ordered.accepted
    ahead = army.march.route[army.march.index + 1:]
    destination = army.march.route[-1]
    for coord in ahead:
        if coord in country.tiles:
            country.tiles.remove(coord)
    state.countries["MARIS"].tiles = [coord for coord in ahead if coord != destination]

    engine.step_day(force=True)
    assert army.march is not None and army.march.spent > 0
    assert not events_of(state, "march_cancelled")

    for _ in range(ordered.effects["days"]):
        engine.step_day(force=True)
    assert army.march is None and army.tile == destination
    assert events_of(state, "army_arrived")[-1]["purpose"] == "MARCH"


def test_an_attack_only_battles_when_the_army_arrives():
    engine, state = quiet_state()
    country = state.countries["ELAR"]
    seat = country.army_position
    army = set_roster(country, {"MILITIA": 4}, tile=seat)
    assert engine.apply_action("ELAR", declare_war("VAYR")).accepted

    priced = engine.military.strike_options("ELAR")["VAYR"]
    assert priced["reachable"] and priced["army_id"] == army.army_id
    assert priced["days"] >= 1 and priced["can_attack"] is False

    ordered = engine.apply_action("ELAR", attack_action("VAYR"))
    assert ordered.accepted
    assert ordered.effects["days"] == priced["days"]
    assert not events_of(state, "battle")
    assert army.tile == seat and army.march.purpose == "ATTACK"
    assert army.march.objective == (priced["objective"]["col"], priced["objective"]["row"])

    for _ in range(priced["days"] - 1):
        engine.step_day(force=True)
        assert not events_of(state, "battle")
    engine.step_day(force=True)

    battle = events_of(state, "battle")[-1]
    assert battle["attacker"] == "ELAR" and battle["defender"] == "VAYR"
    assert battle["expedition"] is priced["expedition"]
    assert army.march is None
    assert events_of(state, "army_arrived")[-1]["fought"] is True


def test_an_army_camped_on_the_border_still_fights_at_once():
    engine, state = quiet_state()
    country = state.countries["ELAR"]
    army = set_roster(country, {"MILITIA": 4}, tile=country.army_position)
    assert engine.apply_action("ELAR", declare_war("VAYR")).accepted
    staging = engine.military.strike_options("ELAR")["VAYR"]["march_target"]
    army.tile = (staging["col"], staging["row"])

    ordered = engine.apply_action("ELAR", attack_action("VAYR"))
    assert ordered.accepted and ordered.effects["battle"] is True
    assert army.march is None
    assert events_of(state, "battle")[-1]["expedition"] is False


def test_hostile_soil_is_priced_into_every_road_that_crosses_it():
    """`movement.hostile_cost` only rules the world if the quote actually moves with the border.

    One hex, one terrain, three callers: a realm marching over its own ground, a realm
    marching over somebody else's, and the map editor pricing pure geography. The middle
    pair differs by exactly the surcharge — the day the two sides of this boundary spoke
    different key names, every hostile road silently marched like home and nothing else
    in this file noticed.
    """
    engine, state = quiet_state()
    elar, maris, military = state.countries["ELAR"], state.countries["MARIS"], engine.military
    coord = elar.army_position
    assert coord in elar.tiles
    neutral = military.movement().cost(coord)
    assert military.movement("ELAR").cost(coord) == pytest.approx(neutral)

    surcharge = float(state.config["movement"]["hostile_cost"])
    elar.tiles = [tile for tile in elar.tiles if tile != coord]
    maris.tiles = list(maris.tiles) + [coord]
    assert military.movement("MARIS").cost(coord) == pytest.approx(neutral)
    assert military.movement("ELAR").cost(coord) == pytest.approx(neutral + surcharge)


def walled_world():
    """ELAR boxed in by SAHAR soil, with VAYR just outside that wall.

    With no friendly or unclaimed corridor left, the only route to VAYR crosses hostile
    ground: that is an expedition, and it is still priced by terrain and roads.
    """
    engine, state = quiet_state()
    elar, vayr, wall = state.countries["ELAR"], state.countries["VAYR"], state.countries["SAHAR"]
    seat = elar.army_position
    outside = set(elar.tiles) | set(vayr.tiles)
    state.countries["MARIS"].tiles = []
    wall.tiles = [
        coord for coord, tile in state.world.tiles.items()
        if tile["land"] and coord not in outside
    ]
    army = set_roster(elar, {"MILITIA": 4}, tile=seat)
    assert engine.apply_action("ELAR", declare_war("VAYR")).accepted
    return engine, state, army


def test_a_hostile_corridor_becomes_an_expedition_and_the_enemy_roads_help_it():
    engine, _state, army = walled_world()
    priced = engine.military.strike_options("ELAR")["VAYR"]
    assert priced["expedition"] is True

    ordered = engine.apply_action("ELAR", attack_action("VAYR"))
    assert ordered.accepted and ordered.effects["expedition"] is True
    assert army.march.expedition is True
    route = army.march.route

    paved_engine, paved_state, _paved_army = walled_world()
    wall = paved_state.countries["SAHAR"]
    for coord in route[1:-1]:
        wall.buildings[coord] = "ROAD"
    repaved = paved_engine.military.strike_options("ELAR")["VAYR"]
    assert repaved["cost"] < priced["cost"]
    assert repaved["days"] <= priced["days"]


def test_an_expedition_walks_its_quoted_road_and_fights_on_arrival():
    """The road the strike channel quotes is the road the settlement walks.

    This column crosses a continent of somebody else's soil, which used to be the case
    that halted at the border. It no longer does: the surcharge is paid hex by hex on the
    way out, and the price the expedition truly pays is the `expedition_factor` the battle
    itself applies when the column finally arrives.
    """
    engine, state, army = walled_world()
    priced = engine.military.strike_options("ELAR")["VAYR"]
    assert priced["expedition"] is True and priced["days"] > 1
    assert engine.apply_action("ELAR", attack_action("VAYR")).accepted
    assert army.march.expedition is True
    route = army.march.route

    for _ in range(priced["days"] - 1):
        engine.step_day(force=True)
        assert not events_of(state, "battle")

    engine.step_day(force=True)
    assert army.march is None and army.tile == route[-1]
    assert not events_of(state, "march_cancelled")
    arrived = events_of(state, "army_arrived")[-1]
    assert arrived["purpose"] == "ATTACK" and arrived["fought"] is True
    battle = events_of(state, "battle")[-1]
    assert battle["attacker"] == "ELAR" and battle["defender"] == "VAYR"
    assert battle["expedition"] is True


def test_a_march_in_progress_survives_a_save_and_resumes_where_it_stopped(tmp_path):
    engine, state = quiet_state()
    country = state.countries["ELAR"]
    seat = country.army_position
    army = set_roster(country, {"MILITIA": 3}, tile=seat)
    target = farthest_walkable_tile(engine, "ELAR")
    assert engine.apply_action("ELAR", march_action(target)).accepted
    engine.step_day(force=True)

    restored = GameState.load(state.save(tmp_path / "marching.json"))
    resumed_engine = RuleEngine(
        restored, planner=None, enable_city_manager=False, enable_frontier_ai=False
    )
    resumed = restored.countries["ELAR"].armies[0]
    assert resumed.march is not None
    assert resumed.march.route == army.march.route
    assert resumed.march.spent == pytest.approx(army.march.spent)
    assert resumed.march.index == army.march.index
    assert resumed.tile == army.tile

    for _ in range(march_days(army.march.remaining, state.config["movement"]["daily_budget"]) + 1):
        if army.march is None:
            break
        engine.step_day(force=True)
        resumed_engine.step_day(force=True)
    assert army.march is None and resumed.march is None
    assert resumed.tile == army.tile == target


def test_the_summary_publishes_what_the_march_panel_draws():
    engine, state = quiet_state()
    country = state.countries["ELAR"]
    army = set_roster(country, {"MILITIA": 3}, tile=country.army_position)
    target = farthest_walkable_tile(engine, "ELAR")
    assert engine.apply_action("ELAR", march_action(target)).accepted
    engine.step_day(force=True)

    row = state.summary()["countries"]["ELAR"]["armies"][0]
    assert {"army_id", "tile", "units", "strength", "power", "recruiting", "progress",
            "preferred_unit", "barracks", "march"} <= set(row)
    march = row["march"]
    assert march["destination"] == [target[0], target[1]]
    assert march["route"] == [list(coord) for coord in army.march.route]
    assert march["purpose"] == "MARCH" and march["index"] == army.march.index
    assert march["target"] is None and march["objective"] is None
    assert march["expedition"] is False
    assert march["remaining"] == pytest.approx(army.march.remaining, abs=0.002)
    assert march["cost"] == pytest.approx(army.march.cost, abs=0.002)
    assert march["days_left"] == march_days(army.march.remaining, state.config["movement"]["daily_budget"])


def test_an_attack_order_shows_its_objective_and_expedition_flag_to_the_dashboard():
    engine, state = quiet_state()
    country = state.countries["ELAR"]
    army = set_roster(country, {"MILITIA": 4}, tile=country.army_position)
    assert engine.apply_action("ELAR", declare_war("VAYR")).accepted
    priced = engine.military.strike_options("ELAR")["VAYR"]
    assert engine.apply_action("ELAR", attack_action("VAYR")).accepted

    march = state.summary()["countries"]["ELAR"]["armies"][0]["march"]
    assert march["purpose"] == "ATTACK" and march["target"] == "VAYR"
    assert march["objective"] == [priced["objective"]["col"], priced["objective"]["row"]]
    assert march["expedition"] is priced["expedition"]
    assert march["days_left"] == priced["days"]
