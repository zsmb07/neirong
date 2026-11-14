"""Immutable geographic input, mutable game state, and portable JSON saves."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

from core.content import (
    army_strength as roster_strength,
    army_upkeep,
    field_cap,
    migrate_armies,
    normalize_roster,
    roster_value,
    validate_content,
)
from core.movement import march_days

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_WORLD = BASE_DIR / "output" / "world_2000BCE.json"
DEFAULT_RULES = BASE_DIR / "config" / "rules.json"
MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relation_key(left: str, right: str) -> str:
    return "|".join(sorted((left, right)))


def tile_key(coord: tuple[int, int]) -> str:
    return f"{coord[0]},{coord[1]}"


def parse_tile_key(key: str) -> tuple[int, int]:
    col, row = key.split(",", 1)
    return int(col), int(row)


def _merge_defaults(defaults: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in saved.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def calendar_date(day_index: int, start_astronomical_year: int = -1999) -> dict[str, Any]:
    if day_index < 0:
        raise ValueError("day_index must be non-negative")
    year = start_astronomical_year + day_index // 365
    day_of_year = day_index % 365
    month = 0
    while day_of_year >= MONTH_DAYS[month]:
        day_of_year -= MONTH_DAYS[month]
        month += 1
    month += 1
    day = day_of_year + 1
    if year <= 0:
        label = f"公元前{1 - year}年{month}月{day}日"
    else:
        label = f"公元{year}年{month}月{day}日"
    return {
        "astronomical_year": year,
        "month": month,
        "day": day,
        "day_of_year": day_index % 365 + 1,
        "label": label,
    }


@dataclass(frozen=True)
class WorldStatic:
    path: Path
    sha256: str
    data: dict[str, Any]
    tiles: dict[tuple[int, int], dict[str, Any]]
    polities: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_WORLD) -> "WorldStatic":
        world_path = Path(path).resolve()
        data = json.loads(world_path.read_text(encoding="utf-8"))
        width = data["metadata"]["grid_width"]
        height = data["metadata"]["grid_height"]
        if len(data["tiles"]) != width * height:
            raise ValueError("world tile count does not match metadata")
        tiles = {(tile["col"], tile["row"]): tile for tile in data["tiles"]}
        if len(tiles) != len(data["tiles"]):
            raise ValueError("duplicate tile coordinates")
        polities = {polity["id"]: polity for polity in data["polities"]}
        if not polities:
            raise ValueError("world must contain at least one polity")
        return cls(world_path, file_sha256(world_path), data, tiles, polities)

    def tile(self, coord: tuple[int, int]) -> dict[str, Any]:
        try:
            return self.tiles[coord]
        except KeyError as exc:
            raise ValueError(f"unknown tile {coord}") from exc

    def neighbors(self, x: int, y: int):
        from worldgen import neighbors

        return neighbors(
            x, y,
            self.data["metadata"]["grid_width"],
            self.data["metadata"]["grid_height"],
        )


@dataclass
class CityState:
    city_id: str
    name: str
    center: tuple[int, int]
    population: int
    focus: str
    tiles: list[tuple[int, int]]
    founded_day: int = 0
    zones: dict[tuple[int, int], str] = field(default_factory=dict)
    managed_by: str = "ai"
    livability: float = 60.0
    food_output: float = 0.0
    food_need: float = 0.0
    food_balance: float = 0.0
    food_loss: float = 0.0
    budget: str = "balanced"
    local_revenue: float = 0.0
    local_upkeep: float = 0.0
    local_net: float = 0.0
    security: float = 55.0
    health: float = 55.0
    siege: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "city_id": self.city_id,
            "name": self.name,
            "center": list(self.center),
            "population": self.population,
            "focus": self.focus,
            "tiles": [list(coord) for coord in self.tiles],
            "founded_day": self.founded_day,
            "zones": {tile_key(coord): zone for coord, zone in self.zones.items()},
            "managed_by": self.managed_by,
            "livability": self.livability,
            "food_output": self.food_output,
            "food_need": self.food_need,
            "food_balance": self.food_balance,
            "food_loss": self.food_loss,
            "budget": self.budget,
            "local_revenue": self.local_revenue,
            "local_upkeep": self.local_upkeep,
            "local_net": self.local_net,
            "security": self.security,
            "health": self.health,
            "siege": dict(self.siege) if self.siege else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CityState":
        return cls(
            city_id=data["city_id"],
            name=data["name"],
            center=tuple(data["center"]),
            population=int(data["population"]),
            focus=data.get("focus", "balanced"),
            tiles=[tuple(coord) for coord in data["tiles"]],
            founded_day=int(data.get("founded_day", 0)),
            zones={parse_tile_key(key): zone for key, zone in data.get("zones", {}).items()},
            managed_by=data.get("managed_by", "ai"),
            livability=float(data.get("livability", 60.0)),
            food_output=float(data.get("food_output", 0.0)),
            food_need=float(data.get("food_need", 0.0)),
            food_balance=float(data.get("food_balance", 0.0)),
            food_loss=float(data.get("food_loss", 0.0)),
            budget=data.get("budget", "balanced"),
            local_revenue=float(data.get("local_revenue", 0.0)),
            local_upkeep=float(data.get("local_upkeep", 0.0)),
            local_net=float(data.get("local_net", 0.0)),
            security=float(data.get("security", 55.0)),
            health=float(data.get("health", 55.0)),
            siege=data.get("siege"),
        )


@dataclass
class MarchOrder:
    """A queued march: the route priced when the order was given, plus the budget spent."""

    purpose: str
    route: list[tuple[int, int]]
    cumulative: list[float]
    target: str | None = None
    objective: tuple[int, int] | None = None
    expedition: bool = False
    spent: float = 0.0
    rendezvous: dict[str, Any] | None = None
    issued_sequence: int = 0

    @property
    def destination(self) -> tuple[int, int] | None:
        return self.route[-1] if self.route else None

    @property
    def cost(self) -> float:
        return float(self.cumulative[-1]) if self.cumulative else 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.cost - self.spent)

    @property
    def index(self) -> int:
        """Which route tile the army currently stands on."""
        step = 0
        while step + 1 < len(self.cumulative) and self.cumulative[step + 1] <= self.spent + 1e-9:
            step += 1
        return step

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "target": self.target,
            "objective": list(self.objective) if self.objective else None,
            "expedition": self.expedition,
            "route": [list(coord) for coord in self.route],
            "cumulative": [round(value, 4) for value in self.cumulative],
            "spent": round(self.spent, 4),
            **({"issued_sequence": self.issued_sequence} if self.issued_sequence else {}),
            **({"rendezvous": dict(self.rendezvous)} if self.rendezvous else {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarchOrder":
        route = [(int(coord[0]), int(coord[1])) for coord in data.get("route") or []]
        objective = data.get("objective")
        return cls(
            purpose=str(data.get("purpose") or "MARCH"),
            route=route,
            cumulative=[float(value) for value in data.get("cumulative") or []],
            target=data.get("target") or None,
            objective=(int(objective[0]), int(objective[1])) if objective else None,
            expedition=bool(data.get("expedition", False)),
            spent=max(0.0, float(data.get("spent", 0.0) or 0.0)),
            rendezvous=data.get("rendezvous"),
            issued_sequence=int(data.get("issued_sequence", 0)),
        )


@dataclass
class ArmyState:
    """One field army: where it stands, what it holds, its barracks state and its march order."""

    army_id: str
    tile: tuple[int, int] | None = None
    units: dict[str, int] = field(default_factory=dict)
    recruiting: bool = False
    progress: float = 0.0
    preferred_unit: str | None = None
    march: MarchOrder | None = None
    stall: str = ""
    raised: int = 0
    stall_notices: dict[str, int] = field(default_factory=dict)
    arrival_day: int = -1
    arrival_sequence: int = 0

    @property
    def strength(self) -> int:
        return roster_strength(self.units)

    def to_dict(self) -> dict[str, Any]:
        return {
            "army_id": self.army_id,
            "tile": list(self.tile) if self.tile else None,
            "units": dict(self.units),
            "recruiting": self.recruiting,
            "progress": round(self.progress, 4),
            "preferred_unit": self.preferred_unit,
            "march": self.march.to_dict() if self.march else None,
            "stall": self.stall,
            "raised": self.raised,
            "stall_notices": dict(self.stall_notices),
            **({"arrival_day": self.arrival_day, "arrival_sequence": self.arrival_sequence}
               if self.arrival_day >= 0 else {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], config: dict[str, Any]) -> "ArmyState":
        payload = data.get("march")
        return cls(
            army_id=str(data["army_id"]),
            tile=tuple(int(value) for value in data["tile"]) if data.get("tile") else None,
            units=normalize_roster(data.get("units"), config),
            recruiting=bool(data.get("recruiting", False)),
            progress=max(0.0, float(data.get("progress", 0.0) or 0.0)),
            preferred_unit=data.get("preferred_unit") or None,
            march=MarchOrder.from_dict(payload) if payload else None,
            stall=str(data.get("stall", "") or ""),
            raised=max(0, int(data.get("raised", 0) or 0)),
            arrival_day=int(data.get("arrival_day", -1)),
            arrival_sequence=int(data.get("arrival_sequence", 0)),
            stall_notices={str(code): int(day)
                           for code, day in (data.get("stall_notices") or {}).items()},
        )


def march_payload(order: MarchOrder | None, config: dict[str, Any]) -> dict[str, Any] | None:
    """The march order as the AI and the dashboard read it: destination, price, days left."""
    if order is None:
        return None
    budget = float(config["movement"]["daily_budget"])
    return {
        "destination": list(order.destination),
        "purpose": order.purpose,
        "target": order.target,
        "objective": list(order.objective) if order.objective else None,
        "expedition": order.expedition,
        "cost": round(order.cost, 3),
        "remaining": round(order.remaining, 3),
        "days_left": march_days(order.remaining, budget),
        "route": [list(coord) for coord in order.route],
        "index": order.index,
    }


def tile_army_capacity(country: "CountryState", config: dict[str, Any], *, capital: bool) -> int:
    rules = config["military"]
    camps = sum(building == "BARRACKS" for building in country.buildings.values())
    per_camp = int(rules["barracks"]["garrison_capacity"])
    return camps * per_camp + (int(rules.get("capital_extra_capacity", 4)) if capital else 0)


def army_payload(army: ArmyState, country: "CountryState",
                 config: dict[str, Any]) -> dict[str, Any]:
    """One army the way the AI and the dashboard read it: where, what, and what it raises."""
    on_camp = army.tile is not None and country.buildings.get(army.tile) == "BARRACKS"
    served = on_camp and camp_garrison(country, army.tile, config) is army
    unit_id = barracks_target(country, army, config) if served else None
    return {
        "army_id": army.army_id,
        "tile": list(army.tile) if army.tile else None,
        "units": dict(army.units),
        "strength": army.strength,
        "power": round(roster_value(army.units, config), 3),
        "recruiting": army.recruiting,
        "progress": round(army.progress, 3),
        "preferred_unit": army.preferred_unit,
        "barracks": on_camp,
        "target_unit": unit_id,
        "stall": recruit_stall(country, army, unit_id, config) if served else "",
        "raised": army.raised,
        "march": march_payload(army.march, config),
    }


# Why a camp hands over no soldier today. The settlement writes the same code the panel prints,
# so "why didn't I get a unit" never has to be guessed from a progress bar.
RECRUIT_STALL_LABELS = {
    "PAUSED": "已停募",
    "NO_GARRISON": "营中无兵，明日重新编成",
    "INSUFFICIENT_WAGE": "国库付不起今天的军饷",
    "INSUFFICIENT_FOOD": "粮仓不够新兵的口粮",
    "ARMY_LIMIT": "全国编制已满，先修兵营才能扩军",
    "UNIT_LIMIT": "该兵种已达编成上限",
    "GARRISON_LIMIT": "这一营的定额已满",
}

# Stalls the player chose are not news; only an involuntary stop is worth an event line.
RECRUIT_STALL_QUIET = {"", "PAUSED"}


def stall_label(code: str) -> str:
    return RECRUIT_STALL_LABELS.get(code, "")


def unit_unlocked(country: "CountryState", unit_id: str, config: dict[str, Any]) -> bool:
    requirement = config["units"][unit_id].get("required_technology")
    if requirement is None:
        return True
    return country.technology.get(requirement["field"], 0) >= requirement["level"]


def barracks_target(country: "CountryState", army: ArmyState, config: dict[str, Any]) -> str:
    """What this camp is raising: the garrison's own choice, else the fallback type."""
    preferred = army.preferred_unit
    if preferred in config["units"] and unit_unlocked(country, preferred, config):
        return preferred
    return config["military"]["barracks"]["default_unit"]


def recruit_block(country: "CountryState", army: ArmyState, unit_id: str,
                  config: dict[str, Any]) -> str:
    """The cap or shortage that keeps a camp from delivering a unit; '' means it can."""
    barracks = config["military"]["barracks"]
    spec = config["units"][unit_id]
    if country.army_strength + 1 > field_cap(country.buildings.values(), config):
        return "ARMY_LIMIT"
    if country.army_units.get(unit_id, 0) + 1 > int(spec["max_count"]):
        return "UNIT_LIMIT"
    if army.strength + 1 > int(barracks["garrison_capacity"]):
        return "GARRISON_LIMIT"
    if country.food + 1e-9 < float(spec["cost"]["food"]):
        return "INSUFFICIENT_FOOD"
    return ""


def recruit_stall(country: "CountryState", army: ArmyState, unit_id: str,
                  config: dict[str, Any]) -> str:
    """Why nothing comes out of this camp today, in settlement order; '' means it trains."""
    if not army.recruiting:
        return "PAUSED"
    if country.treasury + 1e-9 < float(config["military"]["barracks"]["daily_cost"]):
        return "INSUFFICIENT_WAGE"
    return recruit_block(country, army, unit_id, config)


def camp_garrison(country: "CountryState", tile: tuple[int, int],
                  config: dict[str, Any]) -> ArmyState | None:
    """The one army a camp draws its wage for: the first here that is recruiting."""
    standing = [army for army in country.armies if army.tile == tile and not (army.march and army.march.rendezvous)]
    for army in standing:
        if army.recruiting:
            return army
    return standing[0] if standing else None


def barracks_report(country: "CountryState", tile: tuple[int, int],
                    config: dict[str, Any]) -> dict[str, Any]:
    """One camp: who trains here, what comes out of it, and how long until the next soldier."""
    barracks = config["military"]["barracks"]
    wage = float(barracks["daily_cost"])
    army = camp_garrison(country, tile, config)
    unit_id = barracks_target(country, army, config) if army else barracks["default_unit"]
    spec = config["units"][unit_id]
    price = float(spec["cost"]["treasury"])
    progress = 0.0 if army is None else army.progress
    stall = ("NO_GARRISON" if army is None or (not army.strength and tile in country.barracks_dispatches)
             else recruit_stall(country, army, unit_id, config))
    waiting = max(0.0, price - progress)
    days_left = None if stall else (0 if waiting <= 1e-9 else (
        math.ceil(waiting / wage) if wage > 0 else None))
    last_dispatch = country.barracks_dispatches.get(tile)
    return {
        "tile": [tile[0], tile[1]],
        **({"last_dispatch": last_dispatch} if last_dispatch else {}),
        "army_id": army.army_id if army else None,
        "recruiting": bool(army and army.recruiting),
        "unit": unit_id,
        "label": spec["label"],
        "preferred": army.preferred_unit if army else None,
        "price": round(price, 3),
        "food_cost": round(float(spec["cost"]["food"]), 3),
        "upkeep": round(float(spec["upkeep_per_day"]), 4),
        "power": round(float(spec["power"]), 2),
        "wage": round(wage, 3),
        "progress": round(progress, 3),
        "days_left": days_left,
        "strength": army.strength if army else 0,
        "capacity": int(barracks["garrison_capacity"]),
        "room": 0 if army is None else max(0, int(barracks["garrison_capacity"]) - army.strength),
        "raised": army.raised if army else 0,
        "stall": stall,
        "stall_label": stall_label(stall),
    }


def barracks_reports(country: "CountryState", config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every camp this realm owns, newest ledger first, in a stable order."""
    return [
        barracks_report(country, coord, config)
        for coord, building in sorted(country.buildings.items())
        if building == "BARRACKS"
    ]


def recruit_options(country: "CountryState", config: dict[str, Any]) -> list[dict[str, Any]]:
    """What each troop type costs to buy today and, when it cannot be bought, the reason.

    The same gates reject a RECRUIT order, so a panel built on this list cannot promise
    a soldier the settlement will refuse.
    """
    military = config["military"]
    batch_max = int(military["recruit_batch_max"])
    cap = field_cap(country.buildings.values(), config)
    rows: list[dict[str, Any]] = []
    for unit_id, spec in config["units"].items():
        have = country.army_units.get(unit_id, 0)
        unit_room = max(0, int(spec["max_count"]) - have)
        room = max(0, min(unit_room, cap - country.army_strength))
        batch = min(batch_max, room)
        treasury_cost = float(spec["cost"]["treasury"])
        food_cost = float(spec["cost"]["food"])
        requirement = spec.get("required_technology")
        # Stated in the order Military.recruit checks its gates, so the reason a row gives
        # is the reason an order of `batch` units would actually be refused for.
        if not unit_unlocked(country, unit_id, config):
            branch = config["tech_tree"][requirement["field"]]["label"]
            reason = f"需「{branch}」{requirement['level']} 级"
        elif cap - country.army_strength <= 0:
            reason = "全国编制已满"
        elif unit_room <= 0:
            reason = "已达编成上限"
        elif country.treasury + 1e-9 < batch * treasury_cost:
            reason = "财政不足"
        elif country.food + 1e-9 < batch * food_cost:
            reason = "粮食不足"
        else:
            reason = "可招募"
        rows.append({
            "unit": unit_id,
            "label": spec["label"],
            "color": spec.get("color", ""),
            "power": round(float(spec["power"]), 2),
            "defense": round(float(spec["defense"]), 2),
            "upkeep": round(float(spec["upkeep_per_day"]), 4),
            "cost_treasury": treasury_cost,
            "cost_food": food_cost,
            "have": have,
            "max_count": int(spec["max_count"]),
            "room": room,
            "batch": batch,
            "unlocked": unit_unlocked(country, unit_id, config),
            "required_technology": requirement,
            "reason": reason,
            "can_recruit": reason == "可招募",
        })
    return rows


def siege_table(config: dict[str, Any]) -> dict[str, Any]:
    return config["military"]["siege"]


def siege_besiegers(country: "CountryState", city: "CityState", config: dict[str, Any],
                    listed: list[str] | None = None,
                    at: dict[str, Any] | None = None) -> list["ArmyState"]:
    """The armies a siege counts: named, paid, whole, and standing in the ring.

    `listed` lets a panel quote a blockade that has not been written down yet — the armies a
    player could bring to bear on those walls today — without a second copy of the filter.
    `at` prices one of those armies from a tile it has not reached yet, so a column still on
    the road can be quoted at the ring tile its order would deliver it to.
    """
    from worldgen import hex_distance

    siege = city.siege
    names = set(siege["armies"] if siege else []) if listed is None else set(listed)
    if not names:
        return []
    radius = int(siege_table(config)["blockade_radius"])
    armies = []
    for army in sorted(country.armies, key=lambda item: item.army_id):
        if army.army_id not in names or army.march is not None or army.strength <= 0:
            continue
        stand = army.tile if at is None else at.get(army.army_id, army.tile)
        if stand is None or hex_distance(stand, city.center) > radius:
            continue
        armies.append(army)
    return armies


def siege_defenders(defender: "CountryState", city: "CityState",
                    config: dict[str, Any]) -> list["ArmyState"]:
    """Whose backs hold the wall: the defender's armies standing in the same ring."""
    from worldgen import hex_distance

    radius = int(siege_table(config)["blockade_radius"])
    return [
        army for army in sorted(defender.armies, key=lambda item: item.army_id)
        if army.strength > 0 and army.march is None and army.tile is not None
        and hex_distance(army.tile, city.center) <= radius
    ]


def siege_ledger(countries: dict[str, "CountryState"], defender: "CountryState",
                 city: "CityState", config: dict[str, Any],
                 siege: dict[str, Any] | None = None,
                 at: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The one set of numbers a blockade runs on, so panels and settlement never diverge.

    Siege strength is the arms around the wall, not the morale behind them: stability still
    decides what a fallen city costs, but folding it into the daily count would make a panel
    quote a fall date the settlement can only reach by re-deriving the factor itself.
    `siege` projects a blockade nobody has written down yet, so a panel can quote the fall
    date of a wall before anybody has ordered the first army to stand around it; `at` places
    one of those projected armies on a tile it only reaches on a later day.
    """
    siege = city.siege if siege is None else siege
    if not siege:
        return None
    # `open` separates a wall actually being starved on the books from one a panel is only
    # quoting as a possibility, so a card can say "还没开账" without re-deriving it.
    booked = siege is city.siege
    attacker = countries.get(siege["attacker"])
    if attacker is None:
        return None
    table = siege_table(config)
    besieging = siege_besiegers(attacker, city, config, listed=list(siege["armies"]), at=at)
    garrisoned = siege_defenders(defender, city, config)
    walls = defender.buildings.get(city.center) == "WALL"
    attack_value = sum(roster_value(army.units, config, "power") for army in besieging)
    defense_value = sum(roster_value(army.units, config, "defense") for army in garrisoned)
    defense_value += float(table["wall_bonus"]) if walls else 0.0
    total = attack_value + defense_value
    # Nobody around the wall is not an unopposed blockade: the ledger quotes a stalled
    # siege, and the settlement that reads it lifts the blockade instead of advancing it.
    share = 0.0 if total <= 0.0 else attack_value / total
    progress = float(siege["progress"])
    threshold = float(table["fall_threshold"])
    daily = float(table["daily_progress"]) * share
    remaining = threshold - progress
    return {
        "city_id": city.city_id,
        "name": city.name,
        "center": [city.center[0], city.center[1]],
        "attacker": siege["attacker"],
        "defender": defender.country_id,
        "open": booked,
        "armies": [army.army_id for army in besieging],
        "garrison": [army.army_id for army in garrisoned],
        "walls": walls,
        "attack_power": round(attack_value, 3),
        "defense_power": round(defense_value, 3),
        "share": round(share, 4),
        "progress": round(progress, 4),
        "threshold": round(threshold, 4),
        "daily": round(daily, 5),
        "days_left": None if daily <= 1e-9 else (0 if remaining <= 1e-9 else math.ceil(remaining / daily)),
        "started_day": int(siege["started_day"]),
        "exhaustion": round(float(table["exhaustion_per_day"]), 4),
    }


def tribute_base(country: "CountryState") -> float:
    """The money a levy is measured against: the realm's gross income for the day."""
    economy = country.last_economy
    return max(0.0, economy.get("tax_income", 0.0) + economy.get("mine_income", 0.0))


def tribute_transfers(payer: "CountryState", countries: dict[str, "CountryState"],
                      config: dict[str, Any]) -> list[dict[str, Any]]:
    """What one realm owes its collectors today, grouped by collector.

    Every captured building adds `capture_tribute_share`, and the stack per collector stops
    at `capture_tribute_cap` — five farms still leave the payer 92% of its income. The daily
    cut is priced off the payer's live income, so a ruined realm pays little and a growing
    one pays more; settlement and every panel read the shares from here.
    """
    cap = float(config["military"]["capture_tribute_cap"])
    base = tribute_base(payer)
    grouped: dict[str, dict[str, Any]] = {}
    for levy in payer.levies:
        if levy["to"] not in countries:
            continue
        entry = grouped.setdefault(levy["to"], {"share": 0.0, "days_left": 0, "buildings": []})
        entry["share"] += float(levy["share"])
        entry["days_left"] = max(entry["days_left"], int(levy["days_left"]))
        entry["buildings"].append(levy["building"])
    transfers = []
    for collector, entry in sorted(grouped.items()):
        share = min(cap, entry["share"])
        transfers.append({
            "to": collector,
            "share": round(share, 4),
            "capped": round(entry["share"] - share, 4),
            "days_left": entry["days_left"],
            "buildings": entry["buildings"],
            "daily": round(base * share, 4),
        })
    return transfers


def tribute_payload(country: "CountryState", countries: dict[str, "CountryState"],
                    config: dict[str, Any]) -> dict[str, Any]:
    """Both sides of the ledger for one realm, the way the AI and the dashboard read it."""
    paid = tribute_transfers(country, countries, config)
    collected = []
    for payer in countries.values():
        for transfer in tribute_transfers(payer, countries, config):
            if transfer["to"] == country.country_id:
                collected.append({"from": payer.country_id, **transfer})
    collected.sort(key=lambda line: (-line["daily"], line["from"]))
    return {
        "paid": paid,
        "collected": collected,
        "daily_paid": round(sum(line["daily"] for line in paid), 4),
        "daily_collected": round(sum(line["daily"] for line in collected), 4),
        "base_income": round(tribute_base(country), 4),
    }


PROPOSAL_STATUS_LABELS = {
    "PENDING": "待回应",
    "ACCEPTED": "已接受",
    "ACTIVE": "生效中",
    "REJECTED": "已拒绝",
    "EXPIRED": "已过期",
    "BROKEN": "已作废",
}

# How many orders and proposals one panel row-set remembers; the full log stays in the save.
RECEIPT_LIMIT = 12
LEDGER_LIMIT = 12


def proposal_row(proposal: dict[str, Any], country_id: str, day: int, kind: str) -> dict[str, Any]:
    """One proposal seen from one side: who asked, what it is, and whatever came of it."""
    incoming = proposal["target"] == country_id
    status = str(proposal["status"])
    return {
        "proposal_id": proposal["proposal_id"],
        "kind": kind,
        "pact": proposal.get("pact") or kind,
        "direction": "incoming" if incoming else "outgoing",
        # The other side of the paper: who asked us, or who we asked.
        "counterparty": proposal["sender"] if incoming else proposal["target"],
        "sender": proposal["sender"],
        "target": proposal["target"],
        "status": status,
        "status_label": PROPOSAL_STATUS_LABELS.get(status, status),
        "created_day": int(proposal.get("created_day", 0)),
        "resolved_day": proposal.get("resolved_day"),
        "days_left": max(0, int(proposal["expires_day"]) - day) if status == "PENDING" else None,
        "offer_food": proposal.get("offer_food"),
        "request_treasury": proposal.get("request_treasury"),
    }


def diplomacy_ledger(country_id: str, proposals: dict[str, Any], trades: dict[str, Any],
                     day: int, limit: int = LEDGER_LIMIT) -> dict[str, Any]:
    """What this realm asked of others and what others asked of it, pending first, newest next.

    Only the live half used to reach the panel: a refused alliance vanished into the event log,
    so "did my diplomacy work" had no answer. Every proposal keeps its terminal status here.
    """
    rows = [
        proposal_row(item, country_id, day, "PACT")
        for item in proposals.values() if country_id in (item["sender"], item["target"])
    ]
    rows += [
        proposal_row(item, country_id, day, "TRADE")
        for item in trades.values() if country_id in (item["sender"], item["target"])
    ]
    rows.sort(key=lambda row: (row["status"] != "PENDING", -row["created_day"], row["proposal_id"]))

    def side(direction: str) -> list[dict[str, Any]]:
        return [row for row in rows if row["direction"] == direction][:limit]

    return {
        "incoming": side("incoming"),
        "outgoing": side("outgoing"),
        "pending": sum(1 for row in rows if row["status"] == "PENDING"),
        "decided": sum(1 for row in rows if row["status"] != "PENDING"),
    }


@dataclass
class CountryState:
    country_id: str
    name: str
    population: int
    food: float
    treasury: float
    tiles: list[tuple[int, int]]
    technology: dict[str, int]
    tax_rate: float
    workers: dict[str, float] = field(default_factory=dict)
    research_field: str = "agriculture"
    research_progress: dict[str, float] = field(default_factory=dict)
    policy: str = "BALANCED"
    buildings: dict[tuple[int, int], str] = field(default_factory=dict)
    projects: list[dict[str, Any]] = field(default_factory=list)
    contacts: list[str] = field(default_factory=list)
    outposts: list[tuple[int, int]] = field(default_factory=list)
    last_economy: dict[str, float] = field(default_factory=dict)
    shortage_days_total: int = 0
    days_without_shortage: int = 0
    stability: float = 60.0
    armies: list[ArmyState] = field(default_factory=list)
    army_serial: int = 0
    army_tombstones: dict[str, Any] = field(default_factory=dict)
    barracks_dispatches: dict[tuple[int, int], dict] = field(default_factory=dict)
    at_war_with: list[str] = field(default_factory=list)
    war_exhaustion: float = 0.0
    levies: list[dict[str, Any]] = field(default_factory=list)
    cities: list[CityState] = field(default_factory=list)
    personality: str = "neutral"

    @property
    def army_strength(self) -> int:
        return sum(army.strength for army in self.armies)

    @property
    def army_units(self) -> dict[str, int]:
        """Merged national roster; battles still resolve the whole realm's strength."""
        merged: dict[str, int] = {}
        for army in self.armies:
            for unit_id, count in army.units.items():
                merged[unit_id] = merged.get(unit_id, 0) + count
        return merged

    @property
    def main_army(self) -> ArmyState | None:
        """The strike army: the largest roster, ties going to the older army."""
        if not self.armies:
            return None
        return max(self.armies, key=lambda army: army.strength)

    @property
    def army_position(self) -> tuple[int, int] | None:
        main = self.main_army
        return main.tile if main else None

    def find_army(self, army_id: str) -> ArmyState | None:
        return next((army for army in self.armies if army.army_id == army_id), None)

    def new_army(self, tile: tuple[int, int] | None = None,
                 units: dict[str, int] | None = None, recruiting: bool = False,
                 preferred_unit: str | None = None) -> ArmyState:
        self.army_serial += 1
        army = ArmyState(
            army_id=f"{self.country_id}-A{self.army_serial}",
            tile=tile,
            units=dict(units or {}),
            recruiting=recruiting,
            preferred_unit=preferred_unit,
        )
        self.armies.append(army)
        return army

    def to_dict(self) -> dict[str, Any]:
        return {
            "country_id": self.country_id,
            "name": self.name,
            "population": self.population,
            "food": self.food,
            "treasury": self.treasury,
            "tiles": [list(coord) for coord in self.tiles],
            "technology": self.technology,
            "tax_rate": self.tax_rate,
            "workers": self.workers,
            "research_field": self.research_field,
            "research_progress": self.research_progress,
            "policy": self.policy,
            "buildings": {tile_key(coord): building for coord, building in self.buildings.items()},
            "projects": self.projects,
            "contacts": self.contacts,
            "outposts": [list(coord) for coord in self.outposts],
            "last_economy": self.last_economy,
            "shortage_days_total": self.shortage_days_total,
            "days_without_shortage": self.days_without_shortage,
            "stability": self.stability,
            "armies": [army.to_dict() for army in self.armies],
            "army_serial": self.army_serial,
            **({"barracks_dispatches": [[list(tile), row] for tile, row in self.barracks_dispatches.items()]}
               if self.barracks_dispatches else {}),
            **({"army_tombstones": self.army_tombstones} if self.army_tombstones else {}),
            "army_units": self.army_units,
            "army_strength": self.army_strength,
            "army_position": list(self.army_position) if self.army_position else None,
            "at_war_with": self.at_war_with,
            "war_exhaustion": self.war_exhaustion,
            "levies": [dict(levy) for levy in self.levies],
            "cities": [city.to_dict() for city in self.cities],
            "personality": self.personality,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], config: dict[str, Any]) -> "CountryState":
        payload = data.get("armies")
        armies = [ArmyState.from_dict(army, config) for army in payload] if payload else [
            ArmyState.from_dict(army, config) for army in migrate_armies(data, config)
        ]
        restored = cls(
            country_id=data["country_id"],
            name=data["name"],
            population=data["population"],
            food=data["food"],
            treasury=data["treasury"],
            tiles=[tuple(coord) for coord in data["tiles"]],
            technology=data["technology"],
            tax_rate=data["tax_rate"],
            workers=data["workers"],
            research_field=data["research_field"],
            research_progress=data["research_progress"],
            policy=data["policy"],
            buildings={parse_tile_key(key): value for key, value in data["buildings"].items()},
            projects=data["projects"],
            contacts=data["contacts"],
            outposts=[tuple(coord) for coord in data.get("outposts", [])],
            last_economy=data["last_economy"],
            shortage_days_total=data["shortage_days_total"],
            days_without_shortage=data["days_without_shortage"],
            stability=float(data.get("stability", 60.0)),
            armies=armies,
            army_serial=int(data.get("army_serial", len(armies)) or 0),
            army_tombstones=dict(data.get("army_tombstones") or {}),
            barracks_dispatches={tuple(tile): row for tile, row in data.get("barracks_dispatches", [])},
            at_war_with=list(data.get("at_war_with", [])),
            war_exhaustion=float(data.get("war_exhaustion", 0.0)),
            levies=[dict(levy) for levy in data.get("levies", [])],
            cities=[CityState.from_dict(city) for city in data.get("cities", [])],
            personality=str(data.get("personality", "neutral")),
        )
        for project in restored.projects:
            project["tile"] = tuple(project["tile"])
        return restored


class GameState:
    def __init__(self, world: WorldStatic, config: dict[str, Any], seed: int) -> None:
        from core.weather import WeatherModel

        self.world = world
        self.config = deepcopy(config)
        self.seed = seed
        self.start_astronomical_year = int(
            world.data["metadata"].get("astronomical_year", -1999)
        )
        from core.society import starting_era_for_year
        self.start_era_index = starting_era_for_year(
            self.config, self.start_astronomical_year
        )["index"]
        self.day_index = 0
        self.countries: dict[str, CountryState] = {}
        # {country_id: day_index} — a realm taken at its capital leaves the board, and the
        # tombstone is the only record that survives the purge.
        self.eliminated: dict[str, int] = {}
        self.trades: dict[str, dict[str, Any]] = {}
        self.relations: dict[str, int] = {}
        country_ids = sorted(world.polities)
        for index, left in enumerate(country_ids):
            for right in country_ids[index + 1:]:
                self.relations[f"{left}|{right}"] = 0
        self.pacts: dict[str, dict[str, Any]] = {}
        self.diplomatic_proposals: dict[str, dict[str, Any]] = {}
        self.next_diplomacy_number = 1
        self.action_log: list[dict[str, Any]] = []
        self.receipts: dict[str, list[dict[str, Any]]] = {}
        self.event_log: list[dict[str, Any]] = []
        self.next_action_number = 1
        self.next_project_number = 1
        self.next_trade_number = 1
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.weather = WeatherModel(world, seed)

    @classmethod
    def new(
        cls,
        world_path: str | Path = DEFAULT_WORLD,
        config_path: str | Path | dict[str, Any] = DEFAULT_RULES,
        seed: int = 2000,
    ) -> "GameState":
        world = WorldStatic.load(world_path)
        config = (
            deepcopy(config_path)
            if isinstance(config_path, dict)
            else json.loads(Path(config_path).read_text(encoding="utf-8"))
        )
        from core.society import default_zone

        validate_content(config, {tile["biome"] for tile in world.tiles.values()})
        state = cls(world, config, seed)
        for country_id, polity in world.polities.items():
            capital = (int(polity["capital"]["col"]), int(polity["capital"]["row"]))
            state.countries[country_id] = CountryState(
                country_id=country_id,
                name=polity["name"],
                cities=[CityState(
                    city_id=f"{country_id}-C1",
                    name=f"{polity['name']}都城",
                    center=capital,
                    population=int(polity["population"]),
                    focus="balanced",
                    tiles=[tuple(coord) for coord in polity["initial_tiles"]],
                    founded_day=0,
                    zones={
                        (int(coord[0]), int(coord[1])): default_zone(
                            capital,
                            (int(coord[0]), int(coord[1])),
                            world.tiles[(int(coord[0]), int(coord[1]))],
                        )
                        for coord in polity["initial_tiles"]
                    },
                )],
                population=int(polity["population"]),
                food=float(polity["food_storage_units"]),
                treasury=float(polity["treasury_units"]),
                tiles=[tuple(coord) for coord in polity["initial_tiles"]],
                technology=deepcopy(polity["technology"]),
                tax_rate=float(config["default_tax_rate"]),
                workers=deepcopy(config["workers_baseline"]),
                research_progress={field: 0.0 for field in config["research_fields"]},
                stability=float(config["stability"]["initial"]),
                personality=str(polity.get("personality", "neutral")),
            )
            state.countries[country_id].new_army(tile=capital)
        state.record("simulation_initialized", seed=seed, rules_version=config["rules_version"])
        return state

    @property
    def date(self) -> dict[str, Any]:
        return calendar_date(self.day_index, self.start_astronomical_year)

    def sync_city_population(self, country: CountryState) -> None:
        """Keep national population equal to the sum of city populations."""
        if not country.cities:
            if not country.tiles:
                # Conquered to the last hex: there is no soil left to stand a capital on, and
                # a city whose center belongs to another realm would only falsify the books.
                return
            from core.society import default_zone
            capital = self.world.polities[country.country_id]["capital"]
            center = (int(capital["col"]), int(capital["row"]))
            country.cities = [CityState(
                city_id=f"{country.country_id}-C1",
                name=f"{country.name}都城",
                center=center,
                population=country.population,
                focus="balanced",
                tiles=list(country.tiles),
                zones={
                    coord: default_zone(center, coord, self.world.tiles[coord])
                    for coord in country.tiles
                },
            )]
            return
        difference = country.population - sum(city.population for city in country.cities)
        if difference == 0:
            return
        target = max(country.cities, key=lambda city: city.population)
        target.population = max(0, target.population + difference)

    def capital_center(self, country_id: str) -> tuple[int, int] | None:
        """Where the world set this realm's capital, whoever holds that hex today."""
        polity = self.world.polities.get(country_id)
        if polity is None:
            return None
        return (int(polity["capital"]["col"]), int(polity["capital"]["row"]))

    def is_capital(self, country: "CountryState", city: "CityState") -> bool:
        """Whether losing this city costs the realm its seat rather than one more town.

        The capital is the city standing on the hex the world generated it at, so a realm
        that re-founded a 都城 on its own soil is recognised the same way as on day 0.
        """
        center = self.capital_center(country.country_id)
        return center is not None and city.center == center

    def eliminate(self, country_id: str, heir: str | None = None) -> dict[str, Any]:
        """Take a conquered realm off the board, leaving no ledger that still names it.

        Only a capital's fall calls this: the loser keeps no soil to hold treaties, debts or
        tributaries on, so every cross-reference is dropped here instead of being guarded
        downstream. `heir` inherits the tribute the dead court was collecting, because the
        provinces that paid it now answer to whoever took the capital. A column still
        marching at the fallen realm is left alone: `march_daily` stops it on foreign
        ground and `_arrive` finds no war to fight when it gets there.
        """
        country = self.countries.pop(country_id)
        for other_id, other in self.countries.items():
            other.at_war_with = [item for item in other.at_war_with if item != country_id]
            other.contacts = [item for item in other.contacts if item != country_id]
            kept = []
            for levy in other.levies:
                if levy["to"] != country_id:
                    kept.append(levy)
                elif heir is not None and other_id != heir:
                    levy["to"] = heir
                    kept.append(levy)
            other.levies = kept
            for city in other.cities:
                if city.siege and city.siege["attacker"] == country_id:
                    city.siege = None
        self.relations = {
            key: value for key, value in self.relations.items()
            if country_id not in key.split("|")
        }
        self.pacts = {
            pact_id: pact for pact_id, pact in self.pacts.items()
            if country_id not in (pact["sender"], pact["target"])
        }
        self.diplomatic_proposals = {
            proposal_id: proposal
            for proposal_id, proposal in self.diplomatic_proposals.items()
            if country_id not in (proposal["sender"], proposal["target"])
        }
        for trade_id, trade in list(self.trades.items()):
            if country_id not in (trade["sender"], trade["target"]):
                continue
            buyer = trade["sender"]
            if trade["status"] == "PENDING" and buyer in self.countries and buyer != country_id:
                # The food this realm already handed over now buys nothing: put it back.
                self.countries[buyer].food += float(trade["offer_food"])
            del self.trades[trade_id]
        self.receipts.pop(country_id, None)
        self.weather.forget(country_id)
        self.eliminated[country_id] = self.day_index
        return {
            "country_id": country_id, "name": country.name, "day": self.day_index,
            "heir": heir, "armies": len(country.armies),
        }

    def configure_starting_era(self, astronomical_year: int) -> dict[str, Any]:
        from core.society import starting_era_for_year

        if not -799 <= astronomical_year <= 500:
            raise ValueError("start year must be between 800 BCE and 500 CE")
        era = starting_era_for_year(self.config, astronomical_year)
        baseline = int(era["baseline"])
        bonuses = {
            "river_valley": "agriculture",
            "semiarid": "agriculture",
            "highland": "bronze_metallurgy",
            "maritime": "writing",
        }
        maximum = self.config["research_max_level"]
        for country in self.countries.values():
            polity_type = self.world.polities[country.country_id].get("type")
            for field in self.config["research_fields"]:
                country.technology[field] = baseline
                country.research_progress[field] = 0.0
            bonus_field = bonuses.get(polity_type)
            if bonus_field:
                country.technology[bonus_field] = min(
                    maximum, country.technology[bonus_field] + 1
                )
        self.start_astronomical_year = astronomical_year
        self.start_era_index = int(era["index"])
        self.record(
            "era_configured",
            start_year=astronomical_year,
            era=era,
            baseline_technology=baseline,
        )
        return era

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def record(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {
            "sequence": len(self.event_log) + 1,
            "day": self.day_index,
            "date": self.date["label"],
            "type": event_type,
            **payload,
        }
        self.event_log.append(event)
        for listener in self._listeners:
            listener(event)
        return event

    def add_receipt(self, result: "Any", action: dict[str, Any]) -> dict[str, Any]:
        """Keep the last few orders a realm gave, accepted or refused, on the realm itself.

        The full log stays in `action_log`; this is the short answer to "did my order work",
        which the panel needs without scanning centuries of history on every poll.
        """
        row = {
            "action_id": result.action_id,
            "day": result.applied_day,
            "action_type": result.action_type,
            "operation": action.get("operation"),
            "target": action.get("target"),
            "accepted": result.accepted,
            "reason_code": result.reason_code,
            "reason_text": result.reason_text,
            "action": action,
        }
        rows = self.receipts.setdefault(result.actor, [])
        rows.append(row)
        del rows[:-RECEIPT_LIMIT]
        return row

    def victory_announced(self) -> bool:
        for event in reversed(self.event_log):
            if event["type"] == "victory":
                return True
        return False

    def victory_status(
        self, scores: dict[str, dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        from core.society import score_country, victory_progress

        progress = {
            country_id: victory_progress(country, self)
            for country_id, country in self.countries.items()
        }
        achievers = [
            country_id for country_id, value in progress.items() if value["any"]
        ]
        if scores is None:
            scores = {
                country_id: score_country(country, self.config)
                for country_id, country in self.countries.items()
            }
        winner = (
            max(achievers, key=lambda country_id: scores[country_id]["total"])
            if achievers else None
        )
        return {
            "winner": winner,
            "condition": progress[winner]["primary"] if winner else None,
            "label": progress[winner]["label"] if winner else "",
            "countries": progress,
        }

    def summary(self) -> dict[str, Any]:
        from core.society import (
            city_static_metrics,
            city_tier,
            era_info,
            food_capacity,
            housing_capacity,
            score_country,
            specialization,
            stability_factor,
        )

        country_eras = {
            country_id: era_info(self.config, min(country.technology.values()), self.start_era_index)
            for country_id, country in self.countries.items()
        }
        world_era = max(country_eras.values(), key=lambda era: era["index"])
        scores = {
            country_id: score_country(country, self.config)
            for country_id, country in self.countries.items()
        }
        victory = self.victory_status(scores)
        ranking = sorted(
            (
                {"country_id": country_id, **score}
                for country_id, score in scores.items()
            ),
            key=lambda entry: entry["total"],
            reverse=True,
        )
        return {
            "day": self.day_index,
            "date": self.date["label"],
            "era": world_era,
            "start_year": self.start_astronomical_year,
            "countries": {
                country_id: {
                    "population": country.population,
                    "food": round(country.food, 4),
                    "treasury": round(country.treasury, 4),
                    "tax_rate": country.tax_rate,
                    "workers": country.workers,
                    "policy": country.policy,
                    "research_field": country.research_field,
                    "research_progress": {
                        key: round(value, 4) for key, value in country.research_progress.items()
                    },
                    "technology": country.technology,
                    "buildings": {
                        tile_key(coord): building for coord, building in country.buildings.items()
                    },
                    "projects": len(country.projects),
                    "contacts": country.contacts,
                    "diplomacy": {
                        "relations": {
                            other: self.relations.get(relation_key(country_id, other), 0)
                            for other in self.countries if other != country_id
                        },
                        "active_pacts": [
                            pact for pact in self.pacts.values()
                            if pact["status"] == "ACTIVE"
                            and country_id in (pact["sender"], pact["target"])
                        ],
                        "incoming_proposals": [
                            proposal for proposal in self.diplomatic_proposals.values()
                            if proposal["target"] == country_id and proposal["status"] == "PENDING"
                        ],
                        "outgoing_proposals": [
                            proposal for proposal in self.diplomatic_proposals.values()
                            if proposal["sender"] == country_id and proposal["status"] == "PENDING"
                        ],
                        "ledger": diplomacy_ledger(
                            country_id, self.diplomatic_proposals, self.trades, self.day_index
                        ),
                    },
                    "tiles": [list(coord) for coord in country.tiles],
                    "outposts": [list(coord) for coord in country.outposts],
                    "cities": [
                        {
                            **city_static_metrics(city, country, self.world, self.config),
                            # The same ledger the daily settlement advances, so a city card and
                            # a march panel quote one fall date rather than two guesses.
                            "capital": self.is_capital(country, city),
                            "military_capacity": tile_army_capacity(
                                country, self.config, capital=self.is_capital(country, city)
                            ),
                            "siege": siege_ledger(self.countries, country, city, self.config),
                        }
                        for city in country.cities
                    ],
                    "shortage_days_total": country.shortage_days_total,
                    "stability": round(country.stability, 2),
                    "stability_factor": round(stability_factor(country.stability, self.config), 4),
                    "army_strength": country.army_strength,
                    "army_cap": field_cap(country.buildings.values(), self.config),
                    "army_tombstones": dict(country.army_tombstones),
                    "army_units": dict(country.army_units),
                    "army_power": round(roster_value(country.army_units, self.config), 3),
                    "army_position": list(country.army_position) if country.army_position else None,
                    "armies": [army_payload(army, country, self.config) for army in country.armies],
                    "barracks": barracks_reports(country, self.config),
                    "recruit_options": recruit_options(country, self.config),
                    "receipts": [dict(row) for row in self.receipts.get(country_id, [])],
                    "at_war_with": country.at_war_with,
                    "war_exhaustion": round(country.war_exhaustion, 2),
                    "tribute": tribute_payload(country, self.countries, self.config),
                    "personality": country.personality,
                    "military_upkeep": round(army_upkeep(country.army_units, self.config), 3),
                    "housing": housing_capacity(country, self.config),
                    "food_capacity": food_capacity(country, self.config),
                    "city_tier": city_tier(country.population),
                    "specialization": specialization(country),
                    "era": country_eras[country_id],
                    "last_economy": {
                        key: round(value, 6) for key, value in country.last_economy.items()
                    },
                    "victory": victory["countries"][country_id],
                    **scores[country_id],
                }
                for country_id, country in self.countries.items()
            },
            "victory": {
                "winner": victory["winner"],
                "condition": victory["condition"],
                "label": victory["label"],
            },
            "ranking": ranking,
            "leader": ranking[0]["country_id"] if ranking else None,
            "eliminated": dict(self.eliminated),
            "trades": {
                status: sum(trade["status"] == status for trade in self.trades.values())
                for status in ("PENDING", "ACCEPTED", "REJECTED", "EXPIRED")
            },
        }

    def fingerprint(self) -> str:
        dynamic = {
            "day_index": self.day_index,
            "countries": {key: value.to_dict() for key, value in self.countries.items()},
            "eliminated": self.eliminated,
            "trades": self.trades,
            "relations": self.relations,
            "pacts": self.pacts,
            "diplomatic_proposals": self.diplomatic_proposals,
            "weather": self.weather.to_dict(),
            "counters": [
                self.next_action_number, self.next_project_number,
                self.next_trade_number, self.next_diplomacy_number,
            ],
        }
        blob = json.dumps(dynamic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        try:
            world_reference = str(self.world.path.relative_to(BASE_DIR))
        except ValueError:
            world_reference = str(self.world.path)
        return {
            "format": "project-eos-dynamic-save",
            "format_version": 1,
            "rules_version": self.config["rules_version"],
            "world_file": world_reference,
            "world_sha256": self.world.sha256,
            "seed": self.seed,
            "day_index": self.day_index,
            "start_astronomical_year": self.start_astronomical_year,
            "start_era_index": self.start_era_index,
            "rules": self.config,
            "countries": {key: value.to_dict() for key, value in self.countries.items()},
            "eliminated": self.eliminated,
            "trades": self.trades,
            "relations": self.relations,
            "pacts": self.pacts,
            "diplomatic_proposals": self.diplomatic_proposals,
            "weather": self.weather.to_dict(),
            "counters": {
                "action": self.next_action_number,
                "project": self.next_project_number,
                "trade": self.next_trade_number,
                "diplomacy": self.next_diplomacy_number,
            },
            "action_log": self.action_log,
            "receipts": {key: list(value) for key, value in self.receipts.items()},
            "event_log": self.event_log,
        }

    def save(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(destination)
        return destination

    @classmethod
    def load(
        cls,
        path: str | Path,
        world_path: str | Path | None = None,
    ) -> "GameState":
        from core.weather import WeatherModel

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("format") != "project-eos-dynamic-save" or data.get("format_version") != 1:
            raise ValueError("unsupported save format")
        if world_path is None:
            candidate = Path(data["world_file"])
            world_path = candidate if candidate.is_absolute() else BASE_DIR / candidate
        world = WorldStatic.load(world_path)
        if world.sha256 != data["world_sha256"]:
            raise ValueError("static world SHA-256 differs from the saved baseline")
        state = cls.__new__(cls)
        state.world = world
        # Merge newly introduced rule fields into older saves without discarding saved tuning.
        default_config = json.loads(DEFAULT_RULES.read_text(encoding="utf-8"))
        state.config = _merge_defaults(default_config, data["rules"])
        # Content/schema updates (tech names, eras, building prerequisites) must come
        # from the current rules so old saves do not keep a superseded tech tree.
        for content_key in ("tech_tree", "eras", "research_fields", "buildings", "units"):
            if content_key in default_config:
                state.config[content_key] = deepcopy(default_config[content_key])
        validate_content(state.config, {tile["biome"] for tile in world.tiles.values()})
        state.seed = data["seed"]
        state.start_astronomical_year = int(
            data.get("start_astronomical_year", world.data["metadata"]["astronomical_year"])
        )
        from core.society import starting_era_for_year
        state.start_era_index = int(
            data.get(
                "start_era_index",
                starting_era_for_year(state.config, state.start_astronomical_year)["index"],
            )
        )
        state.day_index = data["day_index"]
        state.countries = {
            key: CountryState.from_dict(value, state.config)
            for key, value in data["countries"].items()
        }
        state.trades = data["trades"]
        state.relations = data.get("relations", {})
        for index, left in enumerate(sorted(state.countries)):
            for right in sorted(state.countries)[index + 1:]:
                state.relations.setdefault(relation_key(left, right), 0)
        state.pacts = data.get("pacts", {})
        state.diplomatic_proposals = data.get("diplomatic_proposals", {})
        state.action_log = data["action_log"]
        state.receipts = {
            key: list(value)[-RECEIPT_LIMIT:] for key, value in data.get("receipts", {}).items()
        }
        state.event_log = data["event_log"]
        counters = data["counters"]
        state.next_action_number = counters["action"]
        state.next_project_number = counters["project"]
        state.next_trade_number = counters["trade"]
        state.next_diplomacy_number = counters.get("diplomacy", 1)
        state._listeners = []
        state.weather = WeatherModel(world, state.seed, data["weather"])
        state.eliminated = dict(data.get("eliminated", {}))
        # Weather still samples the whole grid, so a dead realm only comes back through the
        # tombstone: forget its capital readings again the way the settlement did.
        for country_id in state.eliminated:
            state.weather.forget(country_id)
        from core.society import default_zone
        for country in state.countries.values():
            for city in country.cities:
                if not city.zones or all(
                    city.zones.get(coord, "unzoned") == "unzoned" for coord in city.tiles
                ):
                    for coord in city.tiles:
                        city.zones[coord] = default_zone(
                            city.center, coord, world.tiles[coord]
                        )
            state.sync_city_population(country)
        return state
