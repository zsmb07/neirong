"""Vertical slice tests for the unit roster: content gate, save migration, settlement, AI."""
import asyncio
import json
from copy import deepcopy

import pytest

from agents.heuristic import HeuristicAgent
from agents.llm_adapter import CallBudget, LLMSettings, OpenAICompatibleAgent
from agents.personality import PERSONALITIES
from agents.scheduler import build_observation
from core.content import (
    ContentError,
    army_upkeep,
    migrate_roster,
    normalize_roster,
    roster_value,
    validate_content,
)
from core.rules import RuleEngine
from core.state import GameState
from conftest import set_roster
from worldgen import neighbors


def recruit(unit_id, strength):
    return {"type": "MILITARY", "operation": "RECRUIT", "unit": unit_id, "strength": strength}


def last_event(state, kind):
    return next(event for event in reversed(state.event_log) if event["type"] == kind)


def test_shipped_rules_pass_the_content_gate(state):
    validate_content(state.config)


def test_validator_rejects_dangling_counter_and_unknown_biome(state):
    config = deepcopy(state.config)
    config["units"]["MILITIA"]["counters"]["PHALANX"] = 0.2
    with pytest.raises(ContentError, match="未知兵种"):
        validate_content(config)

    config = deepcopy(state.config)
    config["units"]["MILITIA"]["terrain"]["atlantis"] = 1.1
    with pytest.raises(ContentError, match="未知地形"):
        validate_content(config)

    config = deepcopy(state.config)
    config["units"]["ARCHER"]["counters"]["MILITIA"] = 0.0
    with pytest.raises(ContentError, match="克制不得为 0"):
        validate_content(config)


def test_validator_rejects_units_outside_the_military_contract(state):
    config = deepcopy(state.config)
    config["military"]["default_unit"] = "PHALANX"
    with pytest.raises(ContentError, match="不在 units 表中"):
        validate_content(config)

    config = deepcopy(state.config)
    config["military"]["attacker_loss_rate"] = 1.4
    with pytest.raises(ContentError, match="attacker_loss_rate"):
        validate_content(config)

    config = deepcopy(state.config)
    del config["movement"]["biome_cost"]["grassland"]
    with pytest.raises(ContentError, match="缺少陆地群系成本"):
        validate_content(config)


def test_validator_links_building_prerequisites_to_tech_unlocks(state):
    config = deepcopy(state.config)
    building_id, spec = next(
        (key, value)
        for key, value in config["buildings"].items()
        if value.get("required_technology")
    )
    requirement = spec["required_technology"]
    node = config["tech_tree"][requirement["field"]]["levels"][requirement["level"] - 1]
    node["unlocks"].remove(spec["label"])
    with pytest.raises(ContentError, match="unlocks 未列出"):
        validate_content(config)


def test_roster_helpers_derive_size_power_and_upkeep(state):
    config = state.config
    units = {"MILITIA": 4, "CAVALRY": 2}
    assert roster_value(units, config, "power") == pytest.approx(
        4 * config["units"]["MILITIA"]["power"] + 2 * config["units"]["CAVALRY"]["power"]
    )
    assert army_upkeep(units, config) == pytest.approx(
        4 * config["units"]["MILITIA"]["upkeep_food"]
        + 2 * config["units"]["CAVALRY"]["upkeep_food"]
    )
    assert normalize_roster({"CAVALRY": 2, "GHOST": 5, "MILITIA": 0}, config) == {"CAVALRY": 2}


def test_v1_scalar_save_migrates_into_the_default_unit(state, tmp_path):
    country = state.countries["ELAR"]
    seat = country.army_position
    set_roster(country, {"CAVALRY": 2, "MILITIA": 3}, tile=seat)
    path = state.save(tmp_path / "legacy.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload["countries"].values():
        # A v1 writer knew only a scalar army size and a single position.
        entry.pop("armies", None)
        entry.pop("army_serial", None)
        entry.pop("army_units", None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert migrate_roster({"army_strength": 5}, state.config) == {"MILITIA": 5}
    restored = GameState.load(path)
    elar = restored.countries["ELAR"]
    assert elar.army_units == {"MILITIA": 5}
    assert elar.army_strength == 5
    assert [army.army_id for army in elar.armies] == ["ELAR-A1"]
    assert elar.armies[0].tile == seat
    assert elar.to_dict()["army_units"] == {"MILITIA": 5}


def test_recruit_is_gated_per_unit_type(engine):
    state = engine.state
    country = state.countries["ELAR"]
    specs = state.config["units"]
    military = state.config["military"]
    default = military["default_unit"]

    unknown = engine.apply_action("ELAR", recruit("PHALANX", 1))
    assert not unknown.accepted and unknown.reason_code == "INVALID_PARAMETER"

    oversized = engine.apply_action("ELAR", recruit(default, military["recruit_batch_max"] + 1))
    assert not oversized.accepted and oversized.reason_code == "INVALID_STRENGTH"

    locked = next(unit_id for unit_id in specs if not engine.military.unlocked(country, unit_id))
    tech = engine.apply_action("ELAR", recruit(locked, 1))
    assert not tech.accepted and tech.reason_code == "TECHNOLOGY_REQUIRED"
    assert specs[locked]["label"] in tech.reason_text

    country.treasury, country.food = 0.0, 10_000.0
    broke = engine.apply_action("ELAR", recruit(default, 1))
    assert not broke.accepted and broke.reason_code == "INSUFFICIENT_TREASURY"

    country.treasury, country.food = 10_000.0, 0.0
    hungry = engine.apply_action("ELAR", recruit(default, 1))
    assert not hungry.accepted and hungry.reason_code == "INSUFFICIENT_FOOD"

    country.food = 10_000.0
    set_roster(country, {default: specs[default]["max_count"]})
    capped = engine.apply_action("ELAR", recruit(default, 1))
    assert not capped.accepted and capped.reason_code == "UNIT_LIMIT"

    set_roster(country, {default: military["maximum_strength"]})
    full = engine.apply_action("ELAR", recruit(default, 1))
    assert not full.accepted and full.reason_code == "UNIT_LIMIT"


def test_recruit_charges_per_unit_costs_and_only_moves_its_own_army(engine):
    state = engine.state
    country = state.countries["ELAR"]
    country.treasury = country.food = 10_000.0
    spec = state.config["units"]["MILITIA"]
    before = (country.treasury, country.food)
    result = engine.apply_action("ELAR", recruit("MILITIA", 3))
    assert result.accepted
    assert country.treasury == pytest.approx(before[0] - 3 * spec["cost"]["treasury"])
    assert country.food == pytest.approx(before[1] - 3 * spec["cost"]["food"])
    assert country.army_units == {"MILITIA": 3} and country.army_strength == 3
    event = last_event(state, "army_recruited")
    assert event["unit"] == "MILITIA" and event["amount"] == 3

    neighbor = engine.apply_action("MARIS", recruit("MILITIA", 2))
    assert neighbor.accepted
    assert country.army_units == {"MILITIA": 3}
    assert state.countries["MARIS"].army_units == {"MILITIA": 2}


def _attack_power(defender_units):
    """Charge 4 CAVALRY at one defender composition on an identically seeded state."""
    engine = RuleEngine(GameState.new(seed=2000))
    engine.state.weather.uniform = lambda low, high: 1.0
    attacker, defender = engine.state.countries["ELAR"], engine.state.countries["MARIS"]
    set_roster(attacker, {"CAVALRY": 4})
    set_roster(defender, defender_units)
    attacker.stability = defender.stability = 70.0
    war = engine.apply_action("ELAR", {
        "type": "DIPLOMACY", "operation": "DECLARE_WAR", "target": "MARIS",
    })
    assert war.accepted
    border_tile = defender.tiles[0]
    staging = next(
        coord for coord in neighbors(*border_tile)
        if engine.state.world.tile(coord)["land"] and coord not in defender.tiles
    )
    attacker.armies[0].tile = staging
    battle = engine.apply_action(
        "ELAR", {"type": "MILITARY", "operation": "ATTACK", "target": "MARIS"}
    )
    assert battle.accepted
    event = last_event(engine.state, "battle")
    return event["attacker_power"], event["defender_power"], event["biome"]


def test_counter_matrix_moves_real_attack_power():
    # CAVALRY counters ARCHER and loses to HEAVY_INFANTRY; the roster must settle that.
    favored, _, ground = _attack_power({"ARCHER": 4})
    countered, _, same_ground = _attack_power({"HEAVY_INFANTRY": 4})
    assert ground == same_ground
    assert favored > countered * 1.5


def test_battle_reports_per_unit_losses_and_the_ground_fought_on(engine):
    state = engine.state
    country, enemy = state.countries["ELAR"], state.countries["MARIS"]
    country.treasury = country.food = 10_000.0
    set_roster(country, {"MILITIA": 6, "CHARIOT": 2})
    set_roster(enemy, {"MILITIA": 5})
    engine.apply_action("ELAR", {
        "type": "DIPLOMACY", "operation": "DECLARE_WAR", "target": "MARIS",
    })
    border_tile = enemy.tiles[0]
    staging = next(
        coord for coord in neighbors(*border_tile)
        if state.world.tile(coord)["land"] and coord not in enemy.tiles
    )
    country.armies[0].tile = staging
    # A battle is only a battle if somebody is standing in the ring: put the enemy's own
    # levy on the contested tile, or this would settle as an unopposed march-on.
    enemy.armies[0].tile = border_tile
    enemy.armies[0].march = None
    battle = engine.apply_action(
        "ELAR", {"type": "MILITARY", "operation": "ATTACK", "target": "MARIS"}
    )
    assert battle.accepted
    effects = battle.effects
    assert effects["unopposed"] is False
    tile = (effects["tile"]["col"], effects["tile"]["row"])
    assert state.world.tile(tile)["biome"] == effects["biome"]
    assert sum(effects["attacker_units"].values()) == effects["attacker_loss"]
    assert sum(effects["defender_units"].values()) == effects["defender_loss"]
    assert min(effects["attacker_loss"], effects["defender_loss"]) == 0
    assert sum(country.army_units.values()) == 8 - effects["attacker_loss"]


def test_loss_split_is_deterministic_and_conserves_casualties(engine):
    military = engine.military
    country = engine.state.countries["ELAR"]
    army = set_roster(country, {"MILITIA": 6, "CAVALRY": 2})
    units = country.army_units
    first = military._remove_losses(units, 4)
    assert first == military._remove_losses(dict(units), 4)
    assert sum(first.values()) == 4
    assert first["MILITIA"] >= first["CAVALRY"]
    military._apply_army_losses(country.armies, first)
    assert sum(army.units.values()) == 4
    assert army.units == {
        unit_id: units[unit_id] - first.get(unit_id, 0)
        for unit_id in units
        if units[unit_id] - first.get(unit_id, 0) > 0
    }
    assert military._remove_losses(units, 8) == units
    assert military._remove_losses(units, 0) == {}
    remainder = {"MILITIA": 5, "ARCHER": 5}
    assert sum(military._remove_losses(remainder, 3).values()) == 3
    assert military._remove_losses(remainder, 3) == military._remove_losses(remainder, 3)


def test_unpaid_army_sheds_its_costliest_unit_first(engine):
    state = engine.state
    country = state.countries["ELAR"]
    specs = state.config["units"]
    dearest = max(specs, key=lambda unit_id: specs[unit_id]["upkeep_food"])
    cheapest = min(specs, key=lambda unit_id: specs[unit_id]["upkeep_food"])
    set_roster(country, {cheapest: 2, dearest: 1})
    country.treasury = 0.0
    country.food = 2 * specs[cheapest]["upkeep_food"]
    engine.military.upkeep_daily()
    assert country.army_units == {cheapest: 2}
    assert last_event(state, "army_desertion")["unit"] == dearest


def test_army_strength_property_and_summary_follow_the_roster(state):
    country = state.countries["ELAR"]
    set_roster(country, {"MILITIA": 3, "ARCHER": 2})
    summary = state.summary()["countries"][country.country_id]
    assert country.army_strength == 5
    assert summary["army_strength"] == 5
    assert summary["army_units"] == {"MILITIA": 3, "ARCHER": 2}
    assert summary["army_power"] == pytest.approx(
        roster_value(country.army_units, state.config), abs=1e-3
    )
    assert summary["military_upkeep_food"] == pytest.approx(
        army_upkeep(country.army_units, state.config), abs=1e-3
    )


def test_observation_and_llm_prompt_teach_the_unit_protocol(state):
    country = state.countries["ELAR"]
    country.treasury = country.food = 10_000.0
    set_roster(country, {"MILITIA": 2})
    observation = build_observation(state, "ELAR")
    assert observation["country"]["army_units"] == {"MILITIA": 2}
    assert observation["country"]["armies"] == [{
        "army_id": "ELAR-A1", "tile": None, "units": {"MILITIA": 2}, "strength": 2,
        "power": round(2 * state.config["units"]["MILITIA"]["power"], 3),
        "upkeep_food": 2 * state.config["units"]["MILITIA"]["upkeep_food"],
        "recruiting": False, "progress": 0.0, "preferred_unit": None, "barracks": False,
        "target_unit": None, "stall": "", "raised": 0, "march": None,
    }]
    assert set(observation["rules"]["units"]) == set(state.config["units"])
    assert observation["rules"]["military"]["maximum_strength"] == state.config["military"]["maximum_strength"]
    assert observation["rules"]["military"]["barracks"] == state.config["military"]["barracks"]

    settings = LLMSettings(
        provider="openai", model="test", base_url="http://127.0.0.1:9/v1",
        api_key_env="EOS_TEST_KEY",
    )
    agent = OpenAICompatibleAgent("ELAR", settings, CallBudget(max_calls=1, max_cost_usd=0.01))
    system = agent._decision_request(observation)["messages"][0]["content"]
    assert "RECRUIT(unit,strength[,army_id])" in system
    assert "SET_RECRUITING(army_id,recruiting)" in system
    assert "SET_RECRUIT_UNIT(army_id,unit)" in system


def _mock_recruit(state, personality, technology):
    country = state.countries["ELAR"]
    country.personality = personality
    country.technology = dict(technology)
    country.treasury = country.food = 10_000.0
    country.armies = []
    state.day_index = 400
    decision = asyncio.run(HeuristicAgent("ELAR").decide(build_observation(state, "ELAR")))
    return next(
        (action for action in decision["actions"] if action.get("operation") == "RECRUIT"), None
    )


def test_mock_agent_recruits_its_top_preference_when_technology_allows(state):
    action = _mock_recruit(
        state, "aggressive",
        {field: state.config["research_max_level"] for field in state.config["research_fields"]},
    )
    assert action is not None
    assert action["unit"] == PERSONALITIES["aggressive"]["mock"]["units"][0]


def test_mock_agent_falls_back_to_the_militia_without_technology(state):
    action = _mock_recruit(
        state, "aggressive", {field: 0 for field in state.config["research_fields"]}
    )
    assert action is not None
    assert action["unit"] == state.config["military"]["default_unit"]


def test_mock_agent_skips_types_its_technology_has_not_unlocked(state):
    engine = RuleEngine(state)
    action = _mock_recruit(
        state, "conservative", {field: 1 for field in state.config["research_fields"]}
    )
    assert action is not None
    assert engine.military.unlocked(state.countries["ELAR"], action["unit"])
    assert action["unit"] != state.config["military"]["default_unit"]
