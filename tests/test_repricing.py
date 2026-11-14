from copy import deepcopy

import pytest

from core.content import ContentError, field_cap, validate_content
from core.state import DAYS_PER_YEAR, barracks_report, tile_army_capacity


def test_twenty_camps_can_be_built_and_enabled(engine):
    state = engine.state
    realm = state.countries["ELAR"]
    occupied = {tile for country in state.countries.values() for tile in country.tiles}
    terrain = state.config["buildings"]["BARRACKS"]["allowed_biomes"]
    candidates = [tile for tile, row in state.world.tiles.items()
                  if tile not in occupied and row["land"] and row["biome"] in terrain][:20]
    assert len(candidates) == 20
    realm.tiles.extend(candidates)
    realm.treasury = realm.food = 10000
    realm.technology["bronze_metallurgy"] = 2
    for tile in candidates:
        result = engine.apply_action("ELAR", {"type": "BUILD", "building": "BARRACKS",
                                     "tile": dict(zip(("col", "row"), tile))})
        assert result.accepted, (result.reason_code, result.reason_text)
    for _ in range(300):
        engine.construction.advance_all()
        if not realm.projects:
            break
    engine.military.sync_barracks()
    for tile in candidates:
        army = next(a for a in realm.armies if a.tile == tile)
        result = engine.apply_action("ELAR", {"type": "MILITARY", "operation": "SET_RECRUITING",
                                     "army_id": army.army_id, "recruiting": True})
        assert result.accepted, (result.reason_code, result.reason_text)
    assert field_cap(realm.buildings.values(), state.config) is None
    assert tile_army_capacity(realm, state.config, capital=False) == 20 * 14


def test_training_cost_and_speed_are_independent_and_use_one_pool(engine):
    state = engine.state
    realm = state.countries["ELAR"]
    realm.armies.clear()
    realm.treasury = realm.food = 10000
    tiles = realm.tiles[:2]
    realm.buildings.update({tile: "BARRACKS" for tile in tiles})
    a, b = [realm.new_army(tile, {"MILITIA": 1}, recruiting=True) for tile in tiles]
    unit = state.config["units"]["MILITIA"]
    assert barracks_report(realm, tiles[0], state.config)["days_left"] == 130
    unit["cost"]["treasury"] = 99
    a.progress = unit["training_days"] - 2
    treasury = realm.treasury
    engine.military.barracks_daily()
    assert a.strength == 2 and b.strength == 1
    assert realm.treasury == pytest.approx(treasury - .02)
    assert a.progress == b.progress == 0


def test_weak_slot_ratio_and_stronger_troops_take_longer(state):
    units = state.config["units"]
    order = ["MILITIA", "ARCHER", "CHARIOT", "HEAVY_INFANTRY", "CAVALRY", "SIEGE"]
    days = [units[unit]["training_days"] for unit in order]
    assert all(a < b for a,b in zip(days, days[1:]))
    per_camp = (10 * DAYS_PER_YEAR) // days[0]
    assert (units["MILITIA"]["max_count"], per_camp) == (8, 14)
    assert set(units["MILITIA"]["terrain"].values()) == {1}
    bad = deepcopy(state.config)
    key = next(iter(bad["movement"]["biome_cost"]))
    del bad["units"]["MILITIA"]["terrain"][key]
    with pytest.raises(ContentError, match=key):
        validate_content(bad)


def test_one_dispatch_orders_two_armies_and_refuses_double_stationing(engine):
    state = engine.state
    realm = state.countries["ELAR"]
    realm.armies.clear()
    start, start2, destination = realm.tiles[:3]
    first, second = [realm.new_army(tile, {"MILITIA": 2}) for tile in (start,start2)]
    orders = [{"type": "MILITARY", "operation": "MARCH", "army_id": a.army_id,
               "tile": dict(zip(("col", "row"), destination))} for a in (first, second)]
    result = engine.apply_action("ELAR", {"type": "MILITARY", "operation": "DISPATCH", "orders": orders})
    assert result.accepted and len(result.effects["orders"]) == 2
    assert len(state.action_log) == 1
    for _ in range(max(row["effects"]["days"] for row in result.effects["orders"] if row["accepted"])):
        engine.military.march_daily()
    assert len(engine.military._camps_of(realm, destination)) == 1
    assert realm.army_strength == 4
    early = first if first.tile == destination else second
    other = second if early is first else first
    assert other.tile != destination
    assert any(r["reason_code"] == "ALREADY_GARRISONED" for r in state.receipts["ELAR"])


def test_existing_overflow_keeps_its_troops_but_cannot_recruit_more(engine):
    state = engine.state
    realm = state.countries["ELAR"]
    realm.armies.clear()
    seat = state.capital_center("ELAR")
    realm.buildings[seat] = "BARRACKS"
    realm.treasury = realm.food = 10000
    army = realm.new_army(seat, {"MILITIA": 9}, recruiting=True)
    assert barracks_report(realm, seat, state.config)["stall"] == "UNIT_LIMIT"
    result = engine.apply_action("ELAR", {"type": "MILITARY", "operation": "RECRUIT",
                                 "army_id": army.army_id, "unit": "MILITIA", "strength": 1})
    assert result.reason_code == "UNIT_LIMIT"
    engine.military.barracks_daily()
    assert army.units == {"MILITIA": 9}


def test_both_sides_use_terrain_and_victory_chooses_the_loss_rate(engine):
    military = engine.military
    unit, spec = next((u, spec) for u, spec in military.units.items()
                      if len(set(spec["terrain"].values())) > 1)
    low = min(spec["terrain"], key=spec["terrain"].get)
    high = max(spec["terrain"], key=spec["terrain"].get)
    for statistic in ("power", "defense"):
        assert military._side_value({unit: 2}, statistic, {}, high, True) > (
            military._side_value({unit: 2}, statistic, {}, low, True))
    realm = engine.state.countries["ELAR"]
    a = realm.new_army(units={unit: 50})
    b = realm.new_army(units={unit: 50})
    assert military._loss_plan([a], [b], won=True) == (2, 21, False)
    assert military._loss_plan([a], [b], won=False) == (21, 2, False)
