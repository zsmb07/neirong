from copy import deepcopy

from core.military import Military
from core.state import GameState


def setup_armies(state, *, camps=2):
    country = state.countries["ELAR"]
    capital = state.capital_center("ELAR")
    nearby = [tile for tile in state.world.neighbors(*capital) if tile in country.tiles]
    assert len(nearby) >= 2
    country.armies.clear()
    country.buildings.clear()
    country.buildings.update({tile: "BARRACKS" for tile in nearby[:camps]})
    host = country.new_army(capital, {"MILITIA": 2})
    source = country.new_army(nearby[0], {"MILITIA": 3, "ARCHER": 2}, recruiting=True)
    source.progress = 7
    return country, host, source, nearby


def dispatch(engine, source, host, units):
    return engine.apply_action("ELAR", {
        "type": "MILITARY", "operation": "RENDEZVOUS",
        "army_id": source.army_id, "host_id": host.army_id, "units": units,
    })


def settle(military, count=12):
    for _ in range(count):
        military.march_daily()


def test_additive_capital_and_independent_errors(state):
    country, host, source, nearby = setup_armies(state)
    military = Military(state)
    assert military.rendezvous_capacity(country, nearby[0])["capacity"] == 28
    assert military.rendezvous_capacity(country, host.tile)["capacity"] == 42
    country.buildings.clear()
    assert military.rendezvous_capacity(country, host.tile)["capacity"] == 14
    assert military.rendezvous_capacity(country, nearby[1])["capacity"] == 0
    country.buildings[host.tile] = "BARRACKS"
    engine_config = state.config["military"]
    engine_config["capital_extra_capacity"] = 0
    host.units = {"MILITIA": 8, "CHARIOT": 2}
    quote = military.rendezvous_quote("ELAR", source.army_id, host.army_id,
                                      {"MILITIA": 5, "ARCHER": 2})
    assert quote["source_units"] == {"MILITIA": 3, "ARCHER": 2}
    assert {e["code"] for e in quote["errors"]} == {
        "SOURCE_UNIT_LIMIT", "HOST_CAPACITY", "CAMP_UNIT_LIMIT",
    }
    assert any("还能装 4" in e["text"] for e in quote["errors"])
    assert quote["maximum"] == 2


def test_partial_dispatch_preserves_the_source_and_camp(engine):
    country, host, source, _ = setup_armies(engine.state)
    serial, tile, source_id = country.army_serial, source.tile, source.army_id
    result = dispatch(engine, source, host, {"MILITIA": 1, "ARCHER": 2})
    assert result.accepted, result.reason_text
    assert source.units == {"MILITIA": 2}
    assert source.recruiting and source.progress == 7 and source.tile == tile
    assert country.barracks_dispatches[tile]["remaining"] == 2
    settle(engine.military)
    assert country.army_serial == serial
    assert country.find_army(source_id) is source
    assert host.units == {"MILITIA": 3, "ARCHER": 2}
    assert len(country.armies) == 2


def test_empty_source_tombstone_and_march_survive_save(engine, tmp_path):
    country, host, source, _ = setup_armies(engine.state)
    source_id = source.army_id
    assert dispatch(engine, source, host, dict(source.units)).accepted
    assert country.find_army(source_id) is None
    assert country.army_tombstones[source_id]["progress"] == 7
    path = tmp_path / "save.json"
    engine.state.save(path)
    state = GameState.load(path)
    assert state.fingerprint() == engine.state.fingerprint()
    settle(Military(state))
    reloaded = state.countries["ELAR"]
    assert reloaded.find_army(host.army_id).strength == 7
    assert reloaded.army_tombstones[source_id]["host_id"] == host.army_id


def test_missing_host_camps_without_losing_units(engine):
    country, host, source, nearby = setup_armies(engine.state)
    destination = host.tile
    assert dispatch(engine, source, host, {"MILITIA": 1}).accepted
    host.tile = nearby[1]
    settle(engine.military)
    fallback = next(a for a in country.armies if a.tile == destination)
    assert fallback.units == {"MILITIA": 1}
    assert "~" not in fallback.army_id and fallback.march is None
    assert engine.state.receipts["ELAR"][-1]["reason_code"] == "RENDEZVOUS_CAMPED"


def test_same_day_arrivals_recheck_shared_capacity(engine):
    country, host, source, nearby = setup_armies(engine.state, camps=0)
    other = country.new_army(nearby[1], {"MILITIA": 2})
    engine.military.config["capital_extra_capacity"] = 4
    engine.military.movement_config["daily_budget"] = 100
    assert dispatch(engine, source, host, {"MILITIA": 2}).accepted
    assert dispatch(engine, other, host, {"MILITIA": 2}).accepted
    settle(engine.military)
    assert host.strength == 4
    assert country.army_strength == 9
    assert engine.military.rendezvous_capacity(country, host.tile)["room"] == 0
    codes = [r["reason_code"] for r in engine.state.receipts["ELAR"]]
    assert codes[-2:] == ["RENDEZVOUS_MERGED", "ALREADY_GARRISONED"]

def test_interception_accounts_for_every_detached_soldier(engine):
    state = engine.state
    country, host, source, _ = setup_armies(state)
    enemy = state.countries["VAYR"]
    enemy.armies.clear()
    enemy.new_army(host.tile, {"MILITIA": 4})
    country.at_war_with.append("VAYR")
    before = country.army_strength
    assert dispatch(engine, source, host, {"MILITIA": 2}).accepted
    settle(engine.military)
    battle = next(e for e in state.event_log if e["type"] == "battle")
    assert not battle["captured"] and host.tile in country.tiles
    assert country.army_strength == before - battle["attacker_loss"]
    assert not any(a.march for a in country.armies)
    assert state.receipts["ELAR"][-1]["reason_code"] in {
        "RENDEZVOUS_LANDING_REFUSED", "RENDEZVOUS_LOST",
    }


def test_foreign_landing_returns_detachment_with_rejection_receipt(engine):
    state = engine.state
    country, host, source, _ = setup_armies(state)
    destination = host.tile
    before = country.army_strength
    assert dispatch(engine, source, host, {"MILITIA": 1}).accepted
    country.tiles.remove(destination)
    state.countries["VAYR"].tiles.append(destination)
    settle(engine.military)
    last = state.receipts["ELAR"][-1]
    assert not last["accepted"] and last["reason_code"] == "RENDEZVOUS_LANDING_REFUSED"
    assert country.army_strength == before


def test_split_counts_source_at_its_old_tile_and_arrival_at_its_new_tile(engine):
    country, host, source, _ = setup_armies(engine.state)
    assert dispatch(engine, source, host, {"MILITIA": 2}).accepted
    part = next(a for a in country.armies if a.march)
    present, _ = engine.military._mass(country, host.tile)
    assert source in present and part not in present
    assert engine.military._bleed_order(country, present)[:len(present)] == present
    settle(engine.military)
    present, _ = engine.military._mass(country, host.tile)
    assert host in present and host.strength == 4 and source.strength == 3


def test_selected_full_armies_merge_into_one_garrison(engine):
    country, host, source, nearby = setup_armies(engine.state)
    other = country.new_army(nearby[1], {"MILITIA": 2})
    orders = [{"type": "MILITARY", "operation": "RENDEZVOUS",
               "army_id": a.army_id, "host_id": host.army_id, "units": dict(a.units)}
              for a in (source, other)]
    result = engine.apply_action("ELAR", {"type": "MILITARY", "operation": "DISPATCH", "orders": orders})
    assert result.accepted, result.reason_text
    settle(engine.military)
    assert country.armies == [host]
    assert host.units == {"MILITIA": 7, "ARCHER": 2}
    assert set(country.army_tombstones) == {source.army_id, other.army_id}


def test_batch_checks_combined_capacity_before_dispatching(engine):
    country, host, source, nearby = setup_armies(engine.state, camps=0)
    engine.military.config["capital_extra_capacity"] = 4
    other = country.new_army(nearby[1], {"MILITIA": 2})
    orders = [{"type": "MILITARY", "operation": "RENDEZVOUS",
               "army_id": a.army_id, "host_id": host.army_id, "units": {"MILITIA": 2}}
              for a in (source, other)]
    result = engine.apply_action("ELAR", {"type": "MILITARY", "operation": "DISPATCH", "orders": orders})
    assert result.reason_code == "HOST_CAPACITY"
    assert len(country.armies) == 3 and not any(a.march for a in country.armies)
