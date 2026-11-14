"""Fail-fast validation for the content tables in config/rules.json.

The simulation reads these tables by direct key lookup, so a typo or a dangling
reference only surfaces mid-run. Everything that gates content is checked here
before day 0 is simulated.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

BIOME_KEYS_FALLBACK = frozenset({
    "grassland", "forest", "savanna", "steppe", "wetland",
    "desert", "alpine", "tundra", "ocean", "rainforest", "mountain",
})
POLICY_FACTORS = frozenset({"food", "mining", "construction", "research", "tax", "stability"})
WORKER_FIELDS = ("agriculture", "mining", "construction")
MILITARY_NUMERIC = (
    "maximum_strength", "recruit_batch_max",
    "attacker_loss_rate", "defender_loss_rate", "unopposed_loss_rate",
    "defense_bonus_flat",
    "expedition_factor", "metallurgy_power_per_level", "capture_stability_loss",
    "war_exhaustion_per_month", "war_stability_penalty_per_exhaustion",
    "war_exhaustion_relief", "peace_relation_reset", "strike_columns",
    "mass_radius", "minimum_mass_share",
    "capture_tribute_share", "capture_tribute_cap", "capture_tribute_resolve",
)
BARRACKS_KEYS = frozenset(
    {"daily_cost", "garrison_capacity", "default_unit", "progress_cap_ratio", "stall_notice_days"}
)
SIEGE_KEYS = frozenset({
    "daily_progress", "wall_bonus", "fall_threshold",
    "blockade_radius", "exhaustion_per_day", "stability_loss", "peace_pressure",
})
ANNEXATION_KEYS = frozenset({"treasury_seizure", "food_seizure", "peace_pressure"})
ROUT_KEYS = frozenset({"survivor_rate", "barracks_lost_survivor_rate", "retreat_radius"})
MOVEMENT_KEYS = frozenset(
    {"daily_budget", "road_cost", "hostile_cost", "maximum_order_cost", "biome_cost"}
)


class ContentError(ValueError):
    """A content table cannot be consumed by the simulation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContentError(message)


def _number(value: Any, where: str, low: float | None = None, high: float | None = None) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{where} 必须是数字")
    if low is not None:
        _require(value >= low, f"{where} 不得小于 {low}")
    if high is not None:
        _require(value <= high, f"{where} 不得大于 {high}")
    return float(value)


def _biomes(config: dict[str, Any]) -> frozenset[str]:
    table = config.get("biome_food_yield")
    if isinstance(table, dict) and table:
        return frozenset(table)
    return BIOME_KEYS_FALLBACK


def _technology(requirement: Any, fields: list[str], max_level: int, where: str) -> None:
    if requirement is None:
        return
    _require(isinstance(requirement, dict), f"{where} 的 required_technology 必须是对象")
    _require(
        set(requirement) == {"field", "level"},
        f"{where} 的 required_technology 只允许 field 与 level 两个键",
    )
    _require(requirement["field"] in fields, f"{where} 依赖未知科技线 {requirement['field']!r}")
    level = requirement["level"]
    _require(
        isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= max_level,
        f"{where} 的科技等级 {level!r} 超出 1-{max_level}",
    )


def _validate_buildings(config: dict[str, Any], fields: list[str], max_level: int) -> None:
    buildings = config["buildings"]
    _require(isinstance(buildings, dict) and buildings, "buildings 必须是非空对象")
    biomes = _biomes(config)
    for building_id, spec in buildings.items():
        where = f"建筑 {building_id}"
        _require(isinstance(building_id, str) and building_id == building_id.upper(), f"{where} 的 id 必须是大写字母")
        _require(isinstance(spec, dict), f"{where} 必须是对象")
        _require(isinstance(spec.get("label"), str) and spec["label"], f"{where} 缺少 label")
        _number(spec.get("cost"), f"{where} cost", low=0.0)
        _number(spec.get("days"), f"{where} days", low=1.0)
        allowed = spec.get("allowed_biomes")
        _require(isinstance(allowed, list) and allowed, f"{where} 必须列出 allowed_biomes")
        unknown = [biome for biome in allowed if biome not in biomes]
        _require(not unknown, f"{where} 引用未知地形 {unknown}")
        _require(len(set(allowed)) == len(allowed), f"{where} 的 allowed_biomes 有重复")
        _technology(spec.get("required_technology"), fields, max_level, where)
        _require(spec.get("requires_river", False) in (True, False), f"{where} 的 requires_river 必须是布尔值")
        _require(spec.get("on_city_center", False) in (True, False), f"{where} 的 on_city_center 必须是布尔值")
        limit = spec.get("max_per_country")
        if limit is not None:
            _require(isinstance(limit, int) and limit >= 1, f"{where} 的 max_per_country 必须是正整数")
        unknown_keys = set(spec) - {
            "label", "cost", "days", "allowed_biomes", "required_technology",
            "requires_river", "max_per_country", "on_city_center",
        }
        _require(not unknown_keys, f"{where} 含未使用的键 {sorted(unknown_keys)}")


def _validate_tech_tree(config: dict[str, Any], fields: list[str], max_level: int) -> None:
    tree = config["tech_tree"]
    _require(isinstance(tree, dict), "tech_tree 必须是对象")
    _require(set(tree) == set(fields), f"tech_tree 的分支必须与 research_fields 一致，实际 {sorted(tree)}")
    label_index = {spec["label"]: building_id for building_id, spec in config["buildings"].items()}
    for field_id in fields:
        branch = tree[field_id]
        _require(isinstance(branch, dict), f"科技线 {field_id} 必须是对象")
        _require(isinstance(branch.get("label"), str) and branch["label"], f"科技线 {field_id} 缺少 label")
        _require(isinstance(branch.get("color"), str) and branch["color"], f"科技线 {field_id} 缺少 color")
        levels = branch.get("levels")
        _require(isinstance(levels, list) and len(levels) == max_level,
                 f"科技线 {field_id} 必须恰好有 {max_level} 级")
        for index, node in enumerate(levels, start=1):
            where = f"科技线 {field_id} 第 {index} 级"
            _require(isinstance(node, dict) and set(node) == {"level", "name", "effect", "unlocks"},
                     f"{where} 只允许 level、name、effect、unlocks")
            _require(node["level"] == index, f"{where} 的 level 与顺序不符")
            _require(isinstance(node.get("name"), str) and node["name"], f"{where} 缺少 name")
            _require(isinstance(node.get("effect"), str) and node["effect"], f"{where} 缺少 effect")
            unlocks = node["unlocks"]
            _require(isinstance(unlocks, list) and all(isinstance(name, str) for name in unlocks),
                     f"{where} 的 unlocks 必须是名称列表")
            for name in unlocks:
                _require(name in label_index, f"{where} 解锁的 {name!r} 没有对应建筑")
        for building_id, spec in config["buildings"].items():
            requirement = spec.get("required_technology")
            if not requirement or requirement["field"] != field_id:
                continue
            node = levels[requirement["level"] - 1]
            _require(
                spec["label"] in node["unlocks"],
                f"建筑 {building_id} 前置 {field_id} L{requirement['level']}，但该节点 unlocks 未列出 {spec['label']!r}",
            )


def _validate_units(config: dict[str, Any], fields: list[str], max_level: int) -> None:
    units = config["units"]
    _require(isinstance(units, dict) and units, "units 必须是非空对象")
    biomes = _biomes(config)
    for unit_id, spec in units.items():
        where = f"兵种 {unit_id}"
        _require(isinstance(unit_id, str) and unit_id == unit_id.upper(), f"{where} 的 id 必须是大写字母")
        _require(isinstance(spec, dict), f"{where} 必须是对象")
        _require(isinstance(spec.get("label"), str) and spec["label"], f"{where} 缺少 label")
        _require(isinstance(spec.get("color"), str) and spec["color"], f"{where} 缺少 color")
        _number(spec.get("power"), f"{where} power", low=0.0)
        _number(spec.get("defense"), f"{where} defense", low=0.0)
        cap = spec.get("max_count")
        _require(isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1, f"{where} 的 max_count 必须是正整数")
        cost = spec.get("cost")
        _require(isinstance(cost, dict) and set(cost) == {"treasury", "food"}, f"{where} 的 cost 只允许 treasury 与 food")
        _number(cost.get("treasury"), f"{where} cost.treasury", low=0.0)
        _number(cost.get("food"), f"{where} cost.food", low=0.0)
        _number(spec.get("upkeep_per_day"), f"{where} upkeep_per_day", low=0.0)
        _technology(spec.get("required_technology"), fields, max_level, where)
        counters = spec.get("counters", {})
        _require(isinstance(counters, dict), f"{where} 的 counters 必须是对象")
        for rival in counters:
            _require(rival in units, f"{where} 克制了未知兵种 {rival!r}")
            modifier = _number(counters[rival], f"{where} 对 {rival} 的克制", low=-1.0, high=1.0)
            _require(modifier != 0.0, f"{where} 对 {rival} 的克制不得为 0")
        terrain = spec.get("terrain", {})
        _require(isinstance(terrain, dict), f"{where} 的 terrain 必须是对象")
        for biome in terrain:
            _require(biome in biomes, f"{where} 引用未知地形 {biome!r}")
            _number(terrain[biome], f"{where} 地形 {biome} 修正", low=0.0)
        unknown_keys = set(spec) - {
            "label", "color", "power", "defense", "max_count", "cost",
            "upkeep_per_day", "required_technology", "counters", "terrain",
        }
        _require(not unknown_keys, f"{where} 含未使用的键 {sorted(unknown_keys)}")
    military = config["military"]
    default_unit = military.get("default_unit")
    _require(default_unit in units, f"military.default_unit {default_unit!r} 不在 units 表中")
    _require(
        units[default_unit].get("required_technology") is None,
        f"military.default_unit {default_unit!r} 不能有科技前置",
    )
    _require(
        any(spec.get("required_technology") is None for spec in units.values()),
        "units 至少需要一个无科技前置的兵种",
    )


def _validate_military(config: dict[str, Any]) -> None:
    military = config["military"]
    _require(isinstance(military, dict), "military 必须是对象")
    extra = military.get("capital_extra_capacity", 4)
    _require(type(extra) is int and extra >= 0, "military.capital_extra_capacity 必须是非负整数")
    for key in MILITARY_NUMERIC:
        _require(key in military, f"military 缺少 {key}")
        _number(military[key], f"military.{key}")
    for key in ("attacker_loss_rate", "defender_loss_rate", "expedition_factor"):
        _require(military[key] > 0.0 and military[key] < 1.0, f"military.{key} 必须落在 0 与 1 之间")
    # Walking onto an undefended gate must be cheaper than a pitched battle, but not free:
    # a rate at or above attacker_loss_rate would make "no garrison" worthless as a target.
    unopposed = _number(military["unopposed_loss_rate"], "military.unopposed_loss_rate")
    _require(
        0.0 < unopposed < military["attacker_loss_rate"],
        "military.unopposed_loss_rate 必须落在 0 与 attacker_loss_rate 之间：无守军的目标要明显"
        "更便宜，但不能真的零损耗，否则侦察兵也能白拿一格",
    )
    for key in ("maximum_strength", "recruit_batch_max", "strike_columns", "mass_radius"):
        _require(isinstance(military[key], int) and military[key] >= 1, f"military.{key} 必须是正整数")
    share = _number(military["minimum_mass_share"], "military.minimum_mass_share")
    _require(
        0.0 < share <= 1.0,
        "military.minimum_mass_share 必须落在 0（不含）与 1（含）之间：单独一支军队也要打出部分战力，全军在场才不打折",
    )
    _number(military["peace_relation_reset"], "military.peace_relation_reset", low=-100.0, high=100.0)
    # Occupation tribute is a dividend on a war, not a stranglehold: the per-building cut
    # and the ceiling on a pair are both checked against the payer's gross daily income.
    tribute = _number(military["capture_tribute_share"], "military.capture_tribute_share")
    tribute_cap = _number(military["capture_tribute_cap"], "military.capture_tribute_cap")
    _require(
        0.0 < tribute <= tribute_cap <= 0.15,
        "military.capture_tribute_share 必须在 0 与 capture_tribute_cap 之间，"
        "且 capture_tribute_cap 不超过 0.15：被占一栋建筑只拿对方收入的小头，"
        "多栋叠起来也不能把对手经济抽干",
    )
    _require(
        _number(military["capture_tribute_resolve"], "military.capture_tribute_resolve") >= 0.0,
        "military.capture_tribute_resolve 不能是负数",
    )
    _require("capture_tribute_days" in military, "military 缺少 capture_tribute_days")
    days = military["capture_tribute_days"]
    _require(isinstance(days, int) and not isinstance(days, bool) and days >= 1,
             "military.capture_tribute_days 必须是正整数")
    _require("capture_tribute_exempt" in military, "military 缺少 capture_tribute_exempt")
    exempt = military["capture_tribute_exempt"]
    _require(isinstance(exempt, list) and all(isinstance(item, str) for item in exempt),
             "military.capture_tribute_exempt 必须是建筑 id 列表")
    _require(set(exempt) <= set(config["buildings"]),
             f"military.capture_tribute_exempt 引用未知建筑 {sorted(set(exempt) - set(config['buildings']))}")
    _require(len(set(exempt)) == len(exempt), "military.capture_tribute_exempt 有重复")
    roll = military.get("battle_random_range")
    _require(isinstance(roll, list) and len(roll) == 2, "military.battle_random_range 必须是 [低, 高]")
    low, high = (_number(value, f"military.battle_random_range {value}") for value in roll)
    _require(0.0 < low <= 1.0 <= high, "military.battle_random_range 必须以 1.0 为中心")
    barracks = military.get("barracks")
    _require(isinstance(barracks, dict) and barracks, "military.barracks 必须是非空对象")
    _require(set(barracks) <= BARRACKS_KEYS,
             f"military.barracks 含未使用的键 {sorted(set(barracks) - set(BARRACKS_KEYS))}")
    _require(set(barracks) == set(BARRACKS_KEYS),
             f"military.barracks 缺少键 {sorted(set(BARRACKS_KEYS) - set(barracks))}")
    if _number(barracks["daily_cost"], "military.barracks.daily_cost", low=0.0) <= 0.0:
        _require(False, "military.barracks.daily_cost 必须大于 0")
    capacity = barracks["garrison_capacity"]
    _require(isinstance(capacity, int) and not isinstance(capacity, bool) and capacity >= 1,
             "military.barracks.garrison_capacity 必须是正整数")
    _number(barracks["progress_cap_ratio"], "military.barracks.progress_cap_ratio", low=1.0)
    notices = barracks["stall_notice_days"]
    _require(isinstance(notices, int) and not isinstance(notices, bool) and notices >= 1,
             "military.barracks.stall_notice_days 必须是正整数")
    fallback = barracks["default_unit"]
    _require(fallback in config["units"], f"military.barracks.default_unit {fallback!r} 不在 units 表中")
    _require(config["units"][fallback].get("required_technology") is None,
             f"military.barracks.default_unit {fallback!r} 不能有科技前置，否则停募时没有兜底兵种")
    siege = military.get("siege")
    _require(isinstance(siege, dict) and siege, "military.siege 必须是非空对象")
    _require(set(siege) == set(SIEGE_KEYS),
             f"military.siege 的键必须是 {sorted(SIEGE_KEYS)}，实际 {sorted(siege)}")
    if _number(siege["daily_progress"], "military.siege.daily_progress", low=0.0) <= 0.0:
        _require(False, "military.siege.daily_progress 必须大于 0")
    _number(siege["fall_threshold"], "military.siege.fall_threshold", low=0.0)
    _require(siege["fall_threshold"] > 0.0, "military.siege.fall_threshold 必须大于 0")
    _number(siege["wall_bonus"], "military.siege.wall_bonus", low=1.0)
    radius = siege["blockade_radius"]
    _require(isinstance(radius, int) and not isinstance(radius, bool) and radius >= 1,
             "military.siege.blockade_radius 必须是正整数")
    _number(siege["exhaustion_per_day"], "military.siege.exhaustion_per_day", low=0.0)
    _number(siege["stability_loss"], "military.siege.stability_loss", low=0.0)
    # How much a fully starved city of its own moves a realm toward suing for peace.
    _number(siege["peace_pressure"], "military.siege.peace_pressure", low=0.0)
    # Taking a capital ends the realm: what the victor lifts out of the fallen treasury and
    # granaries, and how hard losing the capital pushes the survivor toward peace.
    annexation = military.get("annexation")
    _require(isinstance(annexation, dict) and annexation, "military.annexation 必须是非空对象")
    _require(set(annexation) == set(ANNEXATION_KEYS),
             f"military.annexation 的键必须是 {sorted(ANNEXATION_KEYS)}，实际 {sorted(annexation)}")
    for key in ("treasury_seizure", "food_seizure"):
        share = _number(annexation[key], f"military.annexation.{key}")
        _require(
            0.0 <= share <= 1.0,
            f"military.annexation.{key} 必须落在 0 与 1 之间：抄没的是死者的全部家底，"
            "超过 1 就是在凭空造钱",
        )
    _number(annexation["peace_pressure"], "military.annexation.peace_pressure", low=0.0)
    # Losing a fight inside a defended city is a rout, not a roll of the dice: the broken
    # column keeps `survivor_rate` of the men it brought and walks out of the ring, while a
    # garrison that also lost its barracks keeps nothing. Both are checked against plain
    # attrition so that walls, gates and camps still mean something to an attacker.
    rout = military.get("rout")
    _require(isinstance(rout, dict) and rout, "military.rout 必须是非空对象")
    _require(set(rout) == set(ROUT_KEYS),
             f"military.rout 的键必须是 {sorted(ROUT_KEYS)}，实际 {sorted(rout)}")
    survivors = _number(rout["survivor_rate"], "military.rout.survivor_rate")
    _require(
        0.0 <= survivors < 1.0,
        "military.rout.survivor_rate 必须落在 0（含）与 1（不含）之间：要能表达全歼，"
        "但留下 100% 等于说打败仗不付代价",
    )
    looted = _number(rout["barracks_lost_survivor_rate"], "military.rout.barracks_lost_survivor_rate")
    _require(
        0.0 <= looted <= survivors,
        "military.rout.barracks_lost_survivor_rate 必须落在 0 与 survivor_rate 之间："
        "连兵营一起丢的败局只能更惨，否则「先夺营再破城」这条命令没有意义",
    )
    radius = rout["retreat_radius"]
    _require(isinstance(radius, int) and not isinstance(radius, bool) and radius >= 1,
             "military.rout.retreat_radius 必须是正整数")
    attrition = max(military["attacker_loss_rate"], military["defender_loss_rate"])
    _require(
        1.0 - survivors >= attrition - 1e-9,
        "military.rout.survivor_rate 折出的战死比例必须不低于旷野消耗里最重的一档："
        f"当前消耗最高 {attrition}， survivors 高于 {1.0 - attrition} 就没人在攻城时怕输",
    )
    # A siege reads the wall on the city center, so the table must offer at least one
    # building that can actually stand there; otherwise fall_threshold is unreachable prose.
    centers = [
        building_id for building_id, spec in config["buildings"].items()
        if spec.get("on_city_center") is True
    ]
    _require(centers, "buildings 里没有任何 on_city_center 建筑，围城无墙可算")


def _validate_movement(config: dict[str, Any], world_biomes: set[str]) -> None:
    movement = config["movement"]
    _require(isinstance(movement, dict), "movement 必须是对象")
    _require(set(movement) == set(MOVEMENT_KEYS),
             f"movement 的键必须是 {sorted(MOVEMENT_KEYS)}，实际 {sorted(movement)}")
    budget = _number(movement["daily_budget"], "movement.daily_budget", low=0.0)
    _require(budget > 0.0, "movement.daily_budget 必须大于 0")
    order_cap = _number(movement["maximum_order_cost"], "movement.maximum_order_cost", low=0.0)
    costs = movement["biome_cost"]
    _require(isinstance(costs, dict) and costs, "movement.biome_cost 必须是非空对象")
    land = (_biomes(config) | set(world_biomes)) - {"ocean"}
    # A world can carry biomes the food table ignores (rainforest), and a missing cost
    # would surface as a KeyError mid-march, so the table is keyed on the terrain the
    # map actually contains plus everything the economy reads.
    known = BIOME_KEYS_FALLBACK - {"ocean"}
    _require(land <= set(costs), f"movement.biome_cost 缺少陆地群系成本 {sorted(land - set(costs))}")
    _require(set(costs) <= known,
             f"movement.biome_cost 含未知群系 {sorted(set(costs) - known)}")
    for name in sorted(costs):
        value = _number(costs[name], f"movement.biome_cost.{name}", low=0.0)
        _require(value > 0.0, f"movement.biome_cost.{name} 必须大于 0")
    road_cost = _number(movement["road_cost"], "movement.road_cost", low=0.0)
    _require(road_cost > 0.0, "movement.road_cost 必须大于 0")
    _require(road_cost < min(costs.values()),
             f"movement.road_cost 必须低于最快的陆地群系成本 {min(costs.values())}，"
             "否则道路无法无视地形限速")
    _require(order_cap > road_cost, "movement.maximum_order_cost 必须高于一格道路成本")
    hostile = _number(movement["hostile_cost"], "movement.hostile_cost", low=0.0)
    _require(hostile >= 0.0, "movement.hostile_cost 是踏入别国一格的减速惩罚，不能为负")
    # An army must be able to spend at least one order crawling across a hostile road; a cap
    # below that would make every foreign border an impassable wall again.
    _require(order_cap > road_cost + hostile,
             "movement.maximum_order_cost 必须高于一格敌土道路的成本 "
             f"{road_cost + hostile}")


def _validate_eras(config: dict[str, Any], max_level: int) -> None:
    eras = config["eras"]
    _require(isinstance(eras, list) and eras, "eras 必须是非空列表")
    indexes = []
    for era in eras:
        _require(isinstance(era, dict), "era 条目必须是对象")
        _require(set(era) == {"index", "name", "short", "min_tech_level", "max_astronomical_year", "baseline"},
                 f"era {era.get('index')!r} 的键不完整")
        _require(isinstance(era["name"], str) and era["name"], f"era {era.get('index')} 缺少 name")
        _require(isinstance(era["short"], str) and era["short"], f"era {era.get('index')} 缺少 short")
        for key in ("index", "min_tech_level"):
            _require(isinstance(era[key], int) and not isinstance(era[key], bool) and era[key] >= 1,
                     f"era 的 {key} 必须是正整数")
        _require(era["min_tech_level"] <= max_level, f"era {era['index']} 的科技门槛超过 research_max_level")
        baseline = era["baseline"]
        _require(isinstance(baseline, int) and not isinstance(baseline, bool)
                 and 0 <= baseline <= max_level, f"era {era['index']} 的 baseline 超出范围")
        year = era["max_astronomical_year"]
        _require(year is None or (isinstance(year, int) and not isinstance(year, bool)),
                 f"era {era['index']} 的 max_astronomical_year 必须是整数或 null")
        indexes.append(era["index"])
    _require(indexes == sorted(set(indexes)) and indexes[0] == 1, "eras 的 index 必须从 1 开始严格递增")
    _require(indexes[-1] == max_level, "最后一个时代的 index 必须等于 research_max_level")
    _require(eras[-1]["max_astronomical_year"] is None, "最后一个时代的 max_astronomical_year 必须是 null")


def _validate_city_tables(config: dict[str, Any]) -> None:
    zones = config["city_zones"]
    _require(isinstance(zones, dict) and zones, "city_zones 必须是非空对象")
    for zone_id, spec in zones.items():
        _require(set(spec) == {"label", "color", "food", "mining", "housing", "tax_bonus", "research_bonus"},
                 f"分区 {zone_id} 的键不完整")
        for key in ("food", "mining", "housing", "tax_bonus", "research_bonus"):
            _number(spec[key], f"分区 {zone_id} 的 {key}", low=0.0)
    for focus_id, spec in config["city_focuses"].items():
        _require(isinstance(spec.get("label"), str) and spec["label"], f"方针 {focus_id} 缺少 label")
        for key in spec:
            if key != "label":
                _number(spec[key], f"方针 {focus_id} 的 {key}", low=0.0)
    budgets = config["city_budgets"]
    for budget_id, spec in budgets.items():
        _require(set(spec) == {"label", "service_factor", "livability", "growth", "color"},
                 f"城市预算 {budget_id} 的键不完整")
        _number(spec["service_factor"], f"预算 {budget_id} 的 service_factor", low=0.0)
        _number(spec["growth"], f"预算 {budget_id} 的 growth", low=0.0)
    services = config["city_services"]
    for key in ("budget_security", "budget_health"):
        table = services.get(key)
        _require(isinstance(table, dict) and set(table) == set(budgets),
                 f"city_services.{key} 必须覆盖全部 city_budgets")


def _validate_policies_and_workers(config: dict[str, Any]) -> None:
    policies = config["policies"]
    _require(isinstance(policies, dict) and policies, "policies 必须是非空对象")
    for policy_id, spec in policies.items():
        _require(set(spec) <= POLICY_FACTORS, f"国策 {policy_id} 含未知因子键 {sorted(set(spec) - POLICY_FACTORS)}")
        for key, value in spec.items():
            # stability is an additive delta and may be negative; the rest are multipliers.
            _number(value, f"国策 {policy_id} 的 {key}", low=None if key == "stability" else 0.0)
    bonus = config["stability"].get("policy_bonus")
    _require(isinstance(bonus, dict) and set(bonus) == set(policies),
             "stability.policy_bonus 必须覆盖全部 policies")
    building_bonus = config["stability"].get("building_bonus", {})
    _require(set(building_bonus) <= set(config["buildings"]),
             f"stability.building_bonus 引用未知建筑 {sorted(set(building_bonus) - set(config['buildings']))}")
    workers = config["workers_baseline"]
    _require(set(workers) == set(WORKER_FIELDS), f"workers_baseline 必须恰好包含 {list(WORKER_FIELDS)}")
    total = sum(_number(value, f"workers_baseline.{key}") for key, value in workers.items())
    _require(abs(total - 1.0) <= 1e-9, f"workers_baseline 之和必须为 1.0，实际 {total}")


def _validate_victory(config: dict[str, Any]) -> None:
    conditions = config["victory_conditions"]
    _require(isinstance(conditions, dict) and conditions, "victory_conditions 必须是非空对象")
    for route, spec in conditions.items():
        _require(isinstance(spec, dict) and spec, f"胜利路线 {route} 必须是非空对象")
        for key, value in spec.items():
            _number(value, f"胜利路线 {route} 的 {key}")


def army_strength(units: dict[str, int]) -> int:
    """Total army size; derived so no caller can keep a stale scalar."""
    return sum(int(count) for count in units.values() if int(count) > 0)


def army_upkeep(units: dict[str, int], config: dict[str, Any]) -> float:
    table = config["units"]
    return sum(
        int(count) * float(table[unit_id]["upkeep_per_day"])
        for unit_id, count in units.items()
        if unit_id in table and int(count) > 0
    )


def field_cap(building_ids: Iterable[str], config: dict[str, Any]) -> int:
    """How many units a realm may keep under arms.

    One barracks trains a fixed garrison; how many barracks a realm finishes is its own
    choice, so the ceiling is the camps it actually holds. `maximum_strength` is the floor
    a realm with no barracks at all still commands.
    """
    military = config["military"]
    barracks = sum(building == "BARRACKS" for building in building_ids)
    return max(
        int(military["maximum_strength"]),
        barracks * int(military["barracks"]["garrison_capacity"]),
    )


def normalize_roster(units: Any, config: dict[str, Any]) -> dict[str, int]:
    """Drop unknown or empty entries and keep the roster in table order."""
    table = config["units"]
    counts = units if isinstance(units, dict) else {}
    return {
        unit_id: int(counts.get(unit_id, 0))
        for unit_id in table
        if int(counts.get(unit_id, 0)) > 0
    }


def roster_value(units: dict[str, int], config: dict[str, Any], field: str = "power") -> float:
    """Sum the per-type stat contribution before multipliers are applied."""
    table = config["units"]
    return sum(
        int(count) * float(table[unit_id][field])
        for unit_id, count in units.items()
        if unit_id in table and int(count) > 0
    )


def migrate_roster(data: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
    """v1 saves only carry a scalar army size; fold it into the default unit.

    max_count only gates recruitment, so a migrated roster may exceed a cap.
    """
    saved = data.get("army_units")
    if isinstance(saved, dict) and saved:
        return normalize_roster(saved, config)
    strength = int(data.get("army_strength", 0) or 0)
    if strength <= 0:
        return {}
    return {config["military"]["default_unit"]: strength}


def migrate_armies(data: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Saves written before the army entity existed carry one national roster.

    Fold it into a single army at the old army_position so a mid-game save keeps
    its troops instead of silently disbanding them.
    """
    if not data.get("country_id"):
        return []
    roster = migrate_roster(data, config)
    position = data.get("army_position")
    tile = [int(value) for value in position] if position else None
    if not roster and tile is None:
        return []
    return [{
        "army_id": f"{data['country_id']}-A1",
        "tile": tile,
        "units": roster,
        "recruiting": False,
        "progress": 0.0,
        "preferred_unit": None,
    }]


def validate_content(config: dict[str, Any], world_biomes: set[str] | None = None) -> None:
    """Raise ContentError unless every content table can drive real settlement.

    `world_biomes` is the terrain vocabulary of the map about to be simulated: march
    costs are priced per biome, so a table that ignores a biome the world contains would
    fail on the first route that crosses it.
    """
    _require(isinstance(config, dict), "rules 配置必须是对象")
    _require(isinstance(config.get("rules_version"), str) and config["rules_version"], "rules_version 不能为空")
    fields = config.get("research_fields")
    _require(isinstance(fields, list) and fields and all(isinstance(item, str) for item in fields),
             "research_fields 必须是非空字符串列表")
    _require(len(set(fields)) == len(fields), f"research_fields 有重复项 {fields}")
    max_level = config.get("research_max_level")
    _require(isinstance(max_level, int) and not isinstance(max_level, bool) and max_level >= 1,
             "research_max_level 必须是正整数")
    for key in ("buildings", "tech_tree", "eras", "policies", "military", "movement",
                "city_zones", "city_focuses", "city_budgets", "city_services",
                "stability", "workers_baseline", "victory_conditions", "units"):
        _require(key in config, f"rules.json 缺少内容表 {key}")
    _validate_buildings(config, fields, max_level)
    _validate_tech_tree(config, fields, max_level)
    _validate_units(config, fields, max_level)
    _validate_military(config)
    _validate_movement(config, world_biomes or set())
    _validate_eras(config, max_level)
    _validate_city_tables(config)
    _validate_policies_and_workers(config)
    _validate_victory(config)
