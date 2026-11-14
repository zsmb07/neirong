"""Ten-year march-balance experiment: paving programs, order caps, peace terms, columns.

Every scenario runs the deterministic Mock controller over the same generated worlds, so the
only difference between two rows is the parameter named in the scenario. March days, peace
relations and order caps are read back out of the event log instead of recomputed here: the
report prints what the settlement actually did.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime
import argparse
import json
from pathlib import Path
import statistics

from agents.personality import PERSONALITIES
from core.simulation import Simulation
from core.rules import RuleEngine
from core.state import DEFAULT_RULES, GameState
from mapgen import generate_fair_world

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "runs" / "march_balance"
REPORT = ROOT / "MARCH_BALANCE_REPORT.md"

SEEDS = [31001, 42002, 51003]
DAYS = 3650
START_ERA = -799
PERSONALITY_ORDER = ("aggressive", "neutral", "conservative")
LABELS = {
    "rendezvous_control": "同配置不派兵",
    "rendezvous": "都城会师派兵",
    "appetite": "默认筑路偏好",
    "no_paving": "无人筑路",
    "everyone_paves": "全国筑路",
    "tight_cap": "行军上限 12",
    "wide_cap": "行军上限 48",
    "open_camps": "兵营当饭吃",
    "no_camps": "无人修兵营",
}
ASSIGNMENTS = [
    ["aggressive", "neutral", "conservative", "aggressive", "neutral", "conservative"],
    ["neutral", "conservative", "aggressive", "neutral", "conservative", "aggressive"],
    ["conservative", "aggressive", "neutral", "conservative", "aggressive", "neutral"],
]
COUNTRIES = [
    {"name": "国家A", "color": "#e7b45f", "archetype": "river_valley", "population": 20000},
    {"name": "国家B", "color": "#63b1c9", "archetype": "maritime", "population": 20000},
    {"name": "国家C", "color": "#d97f75", "archetype": "highland", "population": 20000},
    {"name": "国家D", "color": "#b98bcb", "archetype": "maritime", "population": 20000},
    {"name": "国家E", "color": "#8fbf7f", "archetype": "highland", "population": 20000},
    {"name": "国家F", "color": "#d6a36f", "archetype": "river_valley", "population": 20000},
]
SCENARIOS = [
    {"name": "rendezvous_control", "rendezvous_setup": True},
    {"name": "rendezvous", "rendezvous_setup": True, "dispatch": True},
    {"name": "appetite"},
    {"name": "no_paving", "roads": {"aggressive": 0, "neutral": 0, "conservative": 0}},
    {"name": "everyone_paves",
     "roads": {"aggressive": 6, "neutral": 6, "conservative": 6}},
    {"name": "tight_cap", "movement": {"maximum_order_cost": 12.0}},
    {"name": "wide_cap", "movement": {"maximum_order_cost": 48.0}},
    # Barracks are the lever the content table no longer caps: one side leans on it hard,
    # the other refuses to build any, and the difference should show up as army size,
    # massed battles and wages -- not as a rejected BUILD action.
    {"name": "open_camps",
     "barracks": {"aggressive": 6, "neutral": 6, "conservative": 6}},
    {"name": "no_camps", "barracks": {"aggressive": 0, "neutral": 0, "conservative": 0}},
]


@contextmanager
def mock_appetite(key: str, values: dict[str, int] | None):
    """Rewrite one personality appetite knob for the duration of one scenario.

    Both `roads` and `barracks` are personality numbers, so a scenario can pin them
    without touching the content table; whatever it sets is restored afterwards.
    """
    if values is None:
        yield
        return
    previous = {name: PERSONALITIES[name]["mock"].get(key) for name in values}
    for name, target in values.items():
        PERSONALITIES[name]["mock"][key] = target
    try:
        yield
    finally:
        for name, target in previous.items():
            if target is None:
                PERSONALITIES[name]["mock"].pop(key, None)
            else:
                PERSONALITIES[name]["mock"][key] = target


def march_spans(events: list[dict]) -> list[dict]:
    """Pair every march order with the day it closed, so overlap can be counted per day."""
    waiting: dict[tuple[str, str], dict] = {}
    spans: list[dict] = []
    for event in events:
        if event["type"] == "march_ordered":
            waiting[(event["country_id"], event["army_id"])] = event
        elif event["type"] in {"army_arrived", "march_cancelled"}:
            order = waiting.pop((event.get("country_id"), event.get("army_id")), None)
            if order is not None:
                spans.append({
                    "country_id": order["country_id"], "army_id": order["army_id"],
                    "day": order["day"], "start": order["day"], "end": event["day"],
                    "closed": event["type"] == "army_arrived",
                })
    for order in waiting.values():
        spans.append({"country_id": order["country_id"], "army_id": order["army_id"],
                      "day": order["day"], "start": order["day"], "end": None,
                      "closed": False})
    return spans


def column_sizes(spans: list[dict]) -> Counter:
    """March orders issued the same day by the same realm are one campaign, not two."""
    return Counter(
        Counter((span["country_id"], span["day"]) for span in spans).values()
    )


def two_column_days(spans: list[dict], days: int) -> int:
    """Game days on which at least one realm had two or more armies on the road.

    Both endpoints count: a one-day march was on the road on the day it was ordered and on
    the day it closed, and two armies that leave together are a column pair even if one of
    them arrives the next morning.
    """
    busy = 0
    for day in range(1, days + 1):
        on_the_road = {
            (span["country_id"], span["army_id"]) for span in spans
            if span["start"] <= day <= (span["end"] if span["end"] is not None else days)
        }
        # Keyed by army, so an army re-ordered the day it arrives is not counted twice.
        counts = Counter(country for country, _army in on_the_road)
        if any(count >= 2 for count in counts.values()):
            busy += 1
    return busy


def peace_gaps(events: list[dict]) -> list[int]:
    """Days from a signed peace to the next declaration of war against the same realm."""
    declared = defaultdict(list)
    for event in events:
        if event["type"] == "war_declared":
            declared[tuple(sorted((event["attacker"], event["defender"])))].append(event["day"])
    gaps = []
    for event in events:
        if event["type"] != "peace_accepted":
            continue
        pair = tuple(sorted((event["sender"], event["target"])))
        later = [day for day in declared[pair] if day > event["day"]]
        if later:
            gaps.append(min(later) - event["day"])
    return gaps


def seed_rendezvous_trial(state, country_ids, dispatch):
    """Give both arms of the comparison identical camps and starting rosters."""
    engine = RuleEngine(state)
    for country_id in country_ids:
        country = state.countries[country_id]
        capital = state.capital_center(country_id)
        camps = [tile for tile in state.world.neighbors(*capital) if tile in country.tiles][:2]
        if len(camps) < 2:
            continue
        country.buildings.update({tile: "BARRACKS" for tile in camps})
        country.armies.clear()
        host = country.new_army(capital, {"MILITIA": 2})
        source = country.new_army(camps[0], {"MILITIA": 3, "ARCHER": 2})
        if dispatch:
            result = engine.apply_action(country_id, {
                "type": "MILITARY", "operation": "RENDEZVOUS",
                "army_id": source.army_id, "host_id": host.army_id,
                "units": {"MILITIA": 2, "ARCHER": 1},
            })
            if not result.accepted:
                raise RuntimeError(result.reason_text)


def run_one(seed: int, assignment: list[str], scenario: dict) -> dict:
    """One world, one parameter set, one Mock-controlled campaign."""
    world, world_path = generate_fair_world(COUNTRIES, base_seed=seed, attempts=10)
    polities = {polity["id"]: polity for polity in world["polities"]}
    config = deepcopy(json.loads(DEFAULT_RULES.read_text(encoding="utf-8")))
    config["movement"].update(scenario.get("movement", {}))
    cap = float(config["movement"]["maximum_order_cost"])
    appetite = scenario.get("roads")
    camp_appetite = scenario.get("barracks")
    targets = (
        appetite if appetite is not None
        else {name: profile["mock"].get("roads", 0) for name, profile in PERSONALITIES.items()}
    )
    camp_targets = (
        camp_appetite if camp_appetite is not None
        else {name: profile["mock"].get("barracks", 0) for name, profile in PERSONALITIES.items()}
    )
    with ExitStack() as stack:
        stack.enter_context(mock_appetite("roads", appetite))
        stack.enter_context(mock_appetite("barracks", camp_appetite))
        state = GameState.new(world_path, config, world["metadata"]["seed"])
        simulation = Simulation(state, use_mock_agents=True)
        simulation.state.configure_starting_era(START_ERA)
        country_ids = sorted(simulation.state.countries)
        mapping = dict(zip(country_ids, assignment))
        for country_id, personality in mapping.items():
            simulation.state.countries[country_id].personality = personality
        if scenario.get("rendezvous_setup"):
            seed_rendezvous_trial(state, country_ids, scenario.get("dispatch", False))
        completed = simulation.run(scenario.get("days", DAYS), force=True)
        events = simulation.state.event_log
        summary = simulation.state.summary()
        spans = march_spans(events)
        marches = [event for event in events if event["type"] == "march_ordered"]
        battles = [event for event in events if event["type"] == "battle"]
        recruits = [event for event in events if event["type"] == "barracks_recruit"]
        countries = {}
        for country_id in country_ids:
            if country_id not in summary["countries"]:
                # Taken at its capital: the realm has no books left to read, only a tombstone.
                continue
            country = summary["countries"][country_id]
            own_marches = [event for event in marches if event["country_id"] == country_id]
            own_strikes = [event for event in battles if event["attacker"] == country_id]
            countries[country_id] = {
                "name": polities.get(country_id, {}).get("name", country_id),
                "personality": mapping[country_id],
                "road_target": targets.get(mapping[country_id], 0),
                "roads": sum(
                    1 for building in simulation.state.countries[country_id].buildings.values()
                    if building == "ROAD"
                ),
                "barracks_target": camp_targets.get(mapping[country_id], 0),
                "territory": len(country["tiles"]),
                "cities": len(country.get("cities", [])),
                "army": country["army_strength"],
                "capital_capacity": next((c["military_capacity"] for c in country["cities"] if c["capital"]), 0),
                "army_food_daily": country["military_upkeep_food"],
                "treasury": round(country["treasury"], 1),
                "score": round(country.get("total", 0.0), 1),
                "marches": len(own_marches),
                "march_days": statistics.median([event["days"] for event in own_marches])
                if own_marches else 0,
                "march_cost_mean": round(
                    statistics.mean([event["cost"] for event in own_marches]), 2
                ) if own_marches else 0.0,
                "cost_max": max((event["cost"] for event in own_marches), default=0.0),
                "battles": sum(
                    1 for event in battles
                    if country_id in (event["attacker"], event["defender"])
                ),
                "captured": sum(
                    1 for event in battles
                    if event["captured"] and event["attacker"] == country_id
                ),
                "battles_massed": sum(1 for event in own_strikes if len(event["attacker_mass"]) >= 2),
                "battle_share_mean": round(
                    statistics.mean([event["attacker_share"] for event in own_strikes]), 3
                ) if own_strikes else 0.0,
                "barracks_built": sum(
                    1 for building in simulation.state.countries[country_id].buildings.values()
                    if building == "BARRACKS"
                ),
                "barracks_recruited": sum(
                    1 for event in recruits if event["country_id"] == country_id
                ),
                "wars_declared": sum(
                    1 for event in events
                    if event["type"] == "war_declared" and event["attacker"] == country_id
                ),
            }
        fingerprint = simulation.state.fingerprint()
        annexations = [
            {"day": event["day"], "attacker": event["attacker"], "fallen": event["fallen"],
             "tiles": event["tiles"], "capital_tiles": event["capital_tiles"],
             "treasury": event["treasury"], "food": event["food"]}
            for event in events if event["type"] == "realm_annexed"
        ]
        eliminated = dict(simulation.state.eliminated)
        simulation.close()
    return {
        "scenario": scenario["name"],
        "seed": seed,
        "world_seed": world["metadata"]["seed"],
        "fairness": round(world["metadata"]["fairness"]["score"] * 100, 1),
        "completed_days": completed,
        "order_cap": cap,
        "over_cap_orders": sum(1 for event in marches if event["cost"] > cap + 1e-9),
        "marches": len(marches),
        "march_days_median": statistics.median([event["days"] for event in marches])
        if marches else 0,
        "march_days_max": max((event["days"] for event in marches), default=0),
        "expedition_marches": sum(1 for event in marches if event["expedition"]),
        "cancelled_marches": sum(1 for span in spans if not span["closed"]),
        "column_sizes": {str(key): value for key, value in column_sizes(spans).items()},
        "two_column_days": two_column_days(spans, completed),
        "battles": len(battles),
        "captures": sum(1 for event in battles if event["captured"]),
        "massed_battles": sum(1 for event in battles if len(event["attacker_mass"]) >= 2),
        "battle_shares": [event["attacker_share"] for event in battles],
        "barracks": sum(country["barracks_built"] for country in countries.values()),
        "barracks_recruited": sum(
            country["barracks_recruited"] for country in countries.values()),
        "wars": sum(1 for event in events if event["type"] == "war_declared"),
        "peaces": sum(1 for event in events if event["type"] == "peace_accepted"),
        "peace_relations": sorted({
            event["relation"] for event in events if event["type"] == "peace_accepted"
        }),
        "peace_gaps": peace_gaps(events),
        "countries": countries,
        "annexations": annexations,
        "eliminated": eliminated,
        "fingerprint": fingerprint,
    }


def aggregate(runs: list[dict]) -> dict:
    """Pool the runs by scenario, and split by personality where the scenario moved appetite."""
    result = {}
    for scenario in sorted({run["scenario"] for run in runs}):
        rows = [run for run in runs if run["scenario"] == scenario]
        by_personality = {}
        for personality in PERSONALITY_ORDER:
            countries = [
                country for run in rows for country in run["countries"].values()
                if country["personality"] == personality
            ]
            if not countries:
                continue
            # Average the march price only over realms that actually marched: a nation at
            # peace all decade has no route to price, and a zero would drag the mean down.
            priced = [country["march_cost_mean"] for country in countries if country["marches"]]
            struck = [country["battle_share_mean"] for country in countries
                      if country["battle_share_mean"]]
            by_personality[personality] = {
                "label": PERSONALITIES[personality]["label"],
                "count": len(countries),
                "roads": round(statistics.mean(country["roads"] for country in countries), 2),
                "territory": round(statistics.mean(
                    country["territory"] for country in countries), 1),
                "cities": round(statistics.mean(country["cities"] for country in countries), 2),
                "army": round(statistics.mean(country["army"] for country in countries), 2),
                "score": round(statistics.mean(country["score"] for country in countries), 1),
                "marches": round(statistics.mean(country["marches"] for country in countries), 2),
                "march_cost": round(statistics.mean(priced), 2) if priced else 0.0,
                "captured": round(statistics.mean(
                    country["captured"] for country in countries), 2),
                "barracks": round(statistics.mean(
                    country["barracks_built"] for country in countries), 2),
                "barracks_recruited": round(statistics.mean(
                    country["barracks_recruited"] for country in countries), 1),
                "massed_battles": round(statistics.mean(
                    country["battles_massed"] for country in countries), 2),
                "battle_share": round(statistics.mean(struck), 3) if struck else 0.0,
                "wars": round(statistics.mean(
                    country["wars_declared"] for country in countries), 2),
                "survived": sum(1 for country in countries if country["territory"] >= 20),
            }
        gaps = sorted(gap for run in rows for gap in run["peace_gaps"])
        result[scenario] = {
            "runs": len(rows),
            "marches": sum(run["marches"] for run in rows),
            "march_days_median": statistics.median(
                [run["march_days_median"] for run in rows]),
            "march_days_max": max(run["march_days_max"] for run in rows),
            "expedition_marches": sum(run["expedition_marches"] for run in rows),
            "cancelled_marches": sum(run["cancelled_marches"] for run in rows),
            "two_column_days": sum(run["two_column_days"] for run in rows),
            "column_sizes": {
                key: sum(run["column_sizes"].get(key, 0) for run in rows)
                for key in sorted({key for run in rows for key in run["column_sizes"]})
            },
            "over_cap_orders": sum(run["over_cap_orders"] for run in rows),
            "battles": sum(run["battles"] for run in rows),
            "captures": sum(run["captures"] for run in rows),
            "massed_battles": sum(run["massed_battles"] for run in rows),
            "battle_shares": sorted(value for run in rows for value in run["battle_shares"]),
            "barracks": round(statistics.mean([
                country["barracks_built"]
                for run in rows for country in run["countries"].values()
            ]), 2),
            "barracks_recruited": sum(run["barracks_recruited"] for run in rows),
            "wars": sum(run["wars"] for run in rows),
            "peaces": sum(run["peaces"] for run in rows),
            "peace_relations": sorted({
                value for run in rows for value in run["peace_relations"]
            }),
            "peace_gaps": gaps,
            "by_personality": by_personality,
            # A capital's fall erases a realm, so the board itself is the readout: how many
            # seats were taken, when the first went, and how many realms are left to hold it.
            "annexations": sum(len(run["annexations"]) for run in rows),
            "annex_by_attacker": dict(sorted(Counter(
                event["attacker"] for run in rows for event in run["annexations"]
            ).items())),
            "annex_days": sorted(event["day"] for run in rows for event in run["annexations"]),
            "seized_treasury": round(sum(
                event["treasury"] for run in rows for event in run["annexations"]), 1),
            "seized_food": round(sum(
                event["food"] for run in rows for event in run["annexations"]), 1),
            "survivors": sorted(len(run["countries"]) for run in rows),
            "boards": [
                {"seed": run["seed"], "left": len(run["countries"]),
                 "fallen": sorted(run["eliminated"])}
                for run in rows
            ],
        }
    return result


def scenario_rows(agg: dict) -> list[str]:
    rows = []
    for name in LABELS:
        data = agg.get(name)
        if data is None:
            continue
        gaps = data["peace_gaps"]
        gap_text = "— / —" if not gaps else f"{min(gaps)} / {statistics.mean(gaps):.0f}"
        rows.append(
            f"| {LABELS[name]} | {data['runs']} | {data['marches']} | "
            f"{data['march_days_median']} | {data['march_days_max']} | {data['battles']} | "
            f"{data['captures']} | {data['wars']} | {data['peaces']} | "
            f"{data['two_column_days']} | {data['over_cap_orders']} | {gap_text} |"
        )
    return rows


def appetite_rows(agg: dict) -> list[str]:
    rows = []
    for scenario in ("no_paving", "appetite", "everyone_paves"):
        data = agg.get(scenario)
        if data is None:
            continue
        for personality in PERSONALITY_ORDER:
            entry = data["by_personality"].get(personality)
            if entry is None:
                continue
            rows.append(
                f"| {LABELS[scenario]} | {entry['label']} | {entry['roads']} | "
                f"{entry['territory']} | {entry['cities']} | {entry['army']} | "
                f"{entry['score']} | {entry['marches']} | {entry['march_cost']} | "
                f"{entry['captured']} | {entry['wars']} | {entry['survived']}/{entry['count']} |"
            )
    return rows


def barracks_rows(agg: dict) -> list[str]:
    """Per-personality barracks scaling: how many camps a realm finishes and what they buy."""
    rows = []
    for scenario in ("no_camps", "appetite", "open_camps"):
        data = agg.get(scenario)
        if data is None:
            continue
        for personality in PERSONALITY_ORDER:
            entry = data["by_personality"].get(personality)
            if entry is None:
                continue
            rows.append(
                f"| {LABELS[scenario]} | {entry['label']} | {entry['barracks']} | "
                f"{entry['barracks_recruited']} | {entry['army']} | "
                f"{entry['massed_battles']} | {entry['battle_share']} | "
                f"{entry['captured']} | {entry['score']} |"
            )
    return rows


def annex_rows(agg: dict) -> list[str]:
    """Per scenario: how many seats were taken, and how many realms are left to hold the board."""
    rows = []
    for name in LABELS:
        data = agg.get(name)
        if data is None:
            continue
        days, survivors = data["annex_days"], data["survivors"]
        rows.append(
            f"| {LABELS[name]} | {data['annexations']} | {min(survivors)}–{max(survivors)} | "
            f"{days[0] if days else '—'} | {days[-1] if days else '—'} | "
            f"{data['seized_treasury']} | {data['seized_food']} |"
        )
    return rows


def detail_rows(runs: list[dict]) -> list[str]:
    rows = []
    for run in runs:
        for country_id in sorted(run["countries"]):
            country = run["countries"][country_id]
            rows.append(
                f"| {LABELS[run['scenario']]} | {run['seed']} | {country['name']} | "
                f"{country['personality']} | {country['road_target']} | {country['roads']} | "
                f"{country['territory']} | {country['cities']} | {country['army']} | "
                f"{country['marches']} | {country['march_days']} | {country['cost_max']:.1f} | "
                f"{country['battles']} | {country['captured']} | {country['wars_declared']} | "
                f"{country['barracks_built']} | {country['barracks_recruited']} | "
                f"{country['battles_massed']} | {country['score']} |"
            )
    return rows


def summary_lines(runs: list[dict], agg: dict) -> list[str]:
    """The headline numbers, phrased so a reader can check them against the tables."""
    base = agg.get("appetite", {})
    years = max(run["completed_days"] for run in runs) / 365
    lines = []
    paved = base.get("by_personality", {}).get("aggressive")
    if paved and len(base.get("by_personality", {})) == 3:
        lines.append(
            f"- 默认筑路偏好下，{years:.0f} 年后激进国平均 {paved['roads']} 条道路、"
            f"行军均价 {paved['march_cost']}，中立国 "
            f"{base['by_personality']['neutral']['roads']} 条 / "
            f"{base['by_personality']['neutral']['march_cost']}，保守国 "
            f"{base['by_personality']['conservative']['roads']} 条 / "
            f"{base['by_personality']['conservative']['march_cost']}。"
        )
    over = sum(data["over_cap_orders"] for data in agg.values())
    lines.append(
        f"- {len(runs)} 局共 {sum(data['marches'] for data in agg.values())} 道行军令，"
        f"其中超过 `maximum_order_cost` 的 {over} 道。"
    )
    relations = sorted({value for data in agg.values() for value in data["peace_relations"]})
    gaps = sorted(gap for data in agg.values() for gap in data["peace_gaps"])
    lines.append(
        f"- 议和后双边关系一律为 {relations}；再次宣战的最短间隔 "
        f"{min(gaps) if gaps else '—'} 日，平均 {statistics.mean(gaps):.0f} 日。"
        if gaps else
        f"- 议和后双边关系一律为 {relations}；本轮没有出现同一对国家和议后再战。"
    )
    two = sum(data["two_column_days"] for data in agg.values())
    paired = sum(
        count for data in agg.values()
        for key, count in data["column_sizes"].items() if int(key) >= 2
    )
    lines.append(
        f"- 两支以上军队同时在途的游戏日合计 {two} 日，同日两路以上行军令 {paired} 次。"
    )
    tempo = {name: agg.get(name) for name in ("no_paving", "appetite", "everyone_paves")}
    if all(tempo.values()):
        lines.append(
            "- 筑路与战争频率的取舍：" + "、".join(
                f"{LABELS[name]} {tempo[name]['wars']} 次宣战 / "
                f"{tempo[name]['battles']} 场会战 / {tempo[name]['captures']} 格易手"
                for name in ("no_paving", "appetite", "everyone_paves")
            ) + f"（各 {tempo['appetite']['runs']} 局）。"
        )
    camps = {name: agg.get(name) for name in ("no_camps", "appetite", "open_camps")}
    if all(camps.values()):
        lines.append(
            "- 兵营数量不再封顶之后：" + "、".join(
                f"{LABELS[name]} 平均每国 {camps[name]['barracks']} 座 / "
                f"兵营募兵 {camps[name]['barracks_recruited']} 支 / "
                f"两支以上在场的会战 {camps[name]['massed_battles']} 场"
                for name in ("no_camps", "appetite", "open_camps")
            ) + "。"
        )
    annexed = agg.get("appetite")
    if annexed:
        days, survivors = annexed["annex_days"], annexed["survivors"]
        lines.append(
            f"- 都城吞并（{LABELS['appetite']} {annexed['runs']} 局）：{annexed['annexations']} 次灭国，"
            f"终局剩余 {min(survivors)}–{max(survivors)} 国（开局 {len(COUNTRIES)} 国），"
            + (f"最早第 {days[0]} 日、最晚第 {days[-1]} 日，抄走财政 {annexed['seized_treasury']}、"
               f"粮草 {annexed['seized_food']}。" if days else "十年里没有一座都城被围到账满。")
        )
    shares = sorted(value for data in agg.values() for value in data["battle_shares"])
    if shares:
        floor = json.loads(DEFAULT_RULES.read_text(encoding="utf-8"))["military"]["minimum_mass_share"]
        lines.append(
            f"- 全部 {len(shares)} 场会战的进攻方战力系数："
            f"最低 {min(shares):.3f}、中位 {statistics.median(shares):.3f}、最高 {max(shares):.3f}"
            f"（单独一支军队应贴着保底 {floor}，全军汇在一格才逼近 1.0）。"
        )
    return lines


def write_report(runs: list[dict], agg: dict, generated_at: str) -> None:
    seeds = sorted({run["seed"] for run in runs})
    days = max(run["completed_days"] for run in runs)
    reset = json.loads(DEFAULT_RULES.read_text(encoding="utf-8"))["military"]["peace_relation_reset"]
    thresholds = "、".join(
        f"{profile['label']}档 {profile['mock']['war_relation']}"
        for profile in PERSONALITIES.values()
    )
    text = f"""# Project Eos · 行军批次平衡报告

> 生成时间：{generated_at}
> 实验代码：`run_march_balance.py`
> 控制器：全部 Mock 规则 AI，无 API 调用
> 时长：每局 {days} 游戏日（{days / 365:.0f} 个游戏年），开局公元前 799 年
> 世界：`mapgen.generate_fair_world` 生成的 6 国公平世界 × {len(seeds)} 个种子，性格槽位轮换
> 原始数据：`runs/march_balance/results.json`

## 0. 结论摘要

{chr(10).join(summary_lines(runs, agg))}

## 1. 场景对照

| 场景 | 局数 | 行军令 | 中位天数 | 最长天数 | 会战 | 夺城格 | 宣战 | 议和 | 双路日 | 超限令 | 再战最短间隔 / 平均 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(scenario_rows(agg))}

## 2. 筑路偏好对照

> 每格是该性格在 {len(seeds)} 局 × 每局 2 国共 {2 * len(seeds)} 个国家上的平均值；
> “道路”是十年末仍在本国名下的道路格，“存活”是终局领土 ≥ 20 格的国家数。
> “无人筑路”关掉的是性格层面的筑路欲望：以商贸道路为立国偏好（`building: ROAD`）的国家
> 仍会按经济偏好修到 2 条，所以那一列不是全 0。

| 场景 | 性格 | 道路 | 领土 | 城市 | 军队 | 评分 | 行军令 | 行军均价 | 夺城 | 宣战 | 存活 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(appetite_rows(agg))}

## 3. 行军命令上限

`movement.maximum_order_cost` 是单条命令能报价的最大行军成本；超限的路线不是“走不到”，
而是“一次命令下不了”，界面与 AI 都会看到分段提示。上限 12 与 48 两组用于确认这个闸门
真的在削减战争频率，而不是把军队钉死。

| 场景 | 行军令 | 中位天数 | 最长天数 | 会战 | 夺城格 | 宣战 | 超限令 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(cap_rows(agg))}

## 4. 议和条款回归

`military.peace_relation_reset` = {reset} 在议和结算里真正生效：本节统计的所有
`peace_accepted` 事件关系值都等于它，旧的硬编码 +20 已不存在。再战间隔见第 1 节最后一列。

这个值同时是再战的闸门：`agents/heuristic.py` 只在 `relation <= mock.war_relation` 时宣战
（{thresholds}），引擎本身不设停战期。{reset} 这样的负值一落下来，三档性格在下一个 90 天
决策点就重新满足开战条件——第 1 节 90 日的最短间隔就是这么来的。想让和平真的顶住，
得把这个值抬到中立档阈值之上，光靠“打完变冷”不够。

## 5. 多路机动

同一天由同一国家下达的行军令数记为一次“列”：

{chr(10).join(column_lines(agg))}

## 6. 兵营规模与集结

兵营每座每日提供 1 点训练进度，集中给同一支军队。`buildings.BARRACKS`
不再写 `max_per_country`：全国修几座是国家的抉择，全国编制上限随之抬升。会战则按
`military.mass_radius` 格内到场的军队折算，`minimum_mass_share` 是单独一支军队的保底系数。
两个新场景把兵营愿望分别抬到 6 座与压到 0 座，用来确认放开上限改变的是规模与集结，
而不是被拒绝的建设命令。

| 场景 | 性格 | 兵营 | 兵营募兵 | 军队 | 两支以上在场的会战 | 战力系数 | 夺城 | 评分 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(barracks_rows(agg))}

## 7. 都城吞并与连锁灭国

`military.annexation` 把围城的赌注抬到整国：都城中心格账满的那天，败国的全部领土、城市、
人口、国库与粮仓一次性归入胜国，败国从 `state.countries` 里消失，只在 `state.eliminated`
留一枚墓碑。这一节看的是赌注变大之后棋盘有没有塌方——如果灭国是连锁的，终局剩余国数会
贴着 1，且最早一次灭国会发生在开局不久。

| 场景 | 灭国次数 | 终局剩余国数 | 最早灭国日 | 最晚灭国日 | 抄走财政 | 抄走粮草 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(annex_rows(agg))}

> 开局每局 {len(COUNTRIES)} 国。“灭国次数”是 `realm_annexed` 事件数，“终局剩余国数”是
> 该局结束时还在 `state.countries` 里的国家数（{len(SEEDS)} 局的最小值–最大值）。

## 8. 逐国明细

| 场景 | 种子 | 国家 | 性格 | 筑路目标 | 道路 | 领土 | 城市 | 军队 | 行军令 | 中位天数 | 最大成本 | 会战 | 夺城 | 宣战 | 兵营 | 兵营募兵 | 集结会战 | 评分 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(detail_rows(runs))}

## 9. 限制

- 单个种子的小参数改动会沿确定性轨迹放大，因此第 2 节比较的是 {len(seeds)} 局的平均，
  不是单局的因果；要下更强的结论需要更多种子。
- Mock AI 每 90 日议事一次；只有 `Military.alarm_watch()` 点名的国家（自家城墙正被啃，
  或有一支军队的命令抵达时会开围）会在非议事日被叫醒，其余国家在两次决策之间只推进、
  不换目标。
- 停战没有冷却：议和之后双边关系按 `peace_relation_reset` 落成负值，法律上立刻可以再宣战。
  议事变密之后「全国筑路」里出现了议和次日再开战（第 1 节「再战最短间隔」= 1 日），
  默认筑路偏好仍是 89 日；要不要加一条停战冷却天数属于下一批的内容决定。
- 攻城仍是整城易主：一次会战只夺一格，且夺格时防守方该格建筑被拆，道路随领土易手消失。
- 集结只看结算当场站住缺口环内的军队，在途、未编成与零兵的军队不计；战力系数是兵力份额
  的线性插值，不是士气或补给模型。
"""
    REPORT.write_text(text, encoding="utf-8")


def cap_rows(agg: dict) -> list[str]:
    rows = []
    for name in ("tight_cap", "appetite", "wide_cap"):
        data = agg.get(name)
        if data is None:
            continue
        rows.append(
            f"| {LABELS[name]} | {data['marches']} | {data['march_days_median']} | "
            f"{data['march_days_max']} | {data['battles']} | {data['captures']} | "
            f"{data['wars']} | {data['over_cap_orders']} |"
        )
    return rows


def column_lines(agg: dict) -> list[str]:
    lines = []
    for name in LABELS:
        data = agg.get(name)
        if data is None:
            continue
        sizes = data["column_sizes"]
        lines.append(
            f"- {LABELS[name]}：同日同行军令分布 "
            + "、".join(f"{key} 列 ×{value} 次"
                       for key, value in sorted(sizes.items(), key=lambda item: int(item[0])))
            + f"；双列以上在途的游戏日 {data['two_column_days']} 日。"
        )
    return lines


def main() -> int:
    global OUT, REPORT
    parser = argparse.ArgumentParser(description="Run the march balance experiment")
    parser.add_argument("--seeds", type=int, nargs="*", default=SEEDS)
    parser.add_argument("--scenarios", nargs="*", default=None,
                        help="scenario names to run; defaults to all")
    parser.add_argument("--output", type=Path, help="write report and results to this directory")
    parser.add_argument("--days", type=int, default=DAYS)
    arguments = parser.parse_args()
    if arguments.output:
        OUT = arguments.output
        REPORT = OUT / "MARCH_BALANCE_REPORT.md"
    scenarios = SCENARIOS
    if arguments.scenarios:
        wanted = set(arguments.scenarios)
        scenarios = [item for item in SCENARIOS if item["name"] in wanted]
    OUT.mkdir(parents=True, exist_ok=True)
    runs = []
    for scenario in scenarios:
        scenario = {**scenario, "days": arguments.days}
        for index, seed in enumerate(arguments.seeds):
            run = run_one(seed, ASSIGNMENTS[index % len(ASSIGNMENTS)], scenario)
            runs.append(run)
            print(
                f"{scenario['name']:<14} seed {seed}: marches={run['marches']} "
                f"battles={run['battles']} wars={run['wars']} "
                f"peaces={run['peaces']} two-column={run['two_column_days']} "
                f"over-cap={run['over_cap_orders']}",
                flush=True,
            )
    agg = aggregate(runs)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    (OUT / "results.json").write_text(
        json.dumps({
            "generated_at": generated_at, "days": arguments.days, "seeds": arguments.seeds,
            "scenarios": scenarios, "runs": runs, "aggregate": agg,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_report(runs, agg, generated_at)
    print(f"report: {REPORT}")
    print(f"results: {OUT / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
