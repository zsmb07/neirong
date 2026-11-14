"""边警：敌人一下令，我国就要知道它是哪一支、走哪条路、哪一天踩上来，以及谁来迎它。

The player asked for a red warning the day an enemy puts an army on the road — not after the
battle report. One quote answers all of it: `Military.incoming_threats` reads the march orders
the daily settlement is already paying down and files them under the realm they land on, so
the panel's 天数, the AI's 回防 order and the battle's own `_arrive` are the same clock. These
tests pin the three ways that clock can lie: a day that is not the arrival day, a loss quoted
against a garrison that is not there, and a relief column that cannot get there in time.
"""
from __future__ import annotations

import asyncio

from agents.heuristic import HeuristicAgent
from agents.scheduler import build_observation
from core.military import Military
from core.rules import RuleEngine
from core.state import GameState
from live_server import LiveSession
from test_siege import (
    at_war, besieged, engine_of, events, march_ready, only_army, strike_tile,
)


def road_under_way(engine, actor="ELAR", rival="VAYR", units=60):
    """One of `actor`'s columns is on the road at a gate of `rival`, days still to walk."""
    state = engine.state
    at_war(engine, actor, rival)
    army = only_army(state.countries[actor], state.countries[actor].tiles[0], units=units)
    ordered = engine.apply_action(actor, {
        "type": "MILITARY", "operation": "ATTACK", "target": rival, "army_id": army.army_id,
    })
    assert ordered.accepted, ordered.reason_text
    assert army.march is not None, "an accepted attack from home is a column on the road"
    return army


def alerts(engine, **kwargs):
    """The quote read off an engine or off a bare state: it is a read, never an action."""
    return Military(getattr(engine, "state", engine)).incoming_threats(**kwargs)


# --- 边警本身就是行军账本：谁在走、走到哪、哪天到 ------------------------------------------

def test_a_column_on_the_road_is_filed_under_the_realm_it_lands_on():
    engine = engine_of()
    state = engine.state
    army = road_under_way(engine)
    grouped = alerts(engine)

    assert list(grouped) == ["VAYR"], "an invasion is an alert for the victim, not the mover"
    row = grouped["VAYR"][0]
    assert row["attacker"] == "ELAR" and row["army_id"] == army.army_id
    assert row["purpose"] == "ATTACK" and row["strength"] == army.strength
    assert row["tile"] == {"col": army.march.objective[0], "row": army.march.objective[1]}
    assert row["from"] == {"col": army.tile[0], "row": army.tile[1]}
    assert row["days_left"] >= 1
    assert row["arrival_day"] == state.day_index + row["days_left"] - 1, (
        "an order given today spends tonight's budget, so the landing is stamped one day earlier"
    )
    assert row["arrival_date"], "the panel prints a date, not a countdown alone"
    assert "relief" not in row, "the map paints from the cheap quote; the search is the AI's"


def test_the_alert_day_is_the_day_the_battle_is_fought():
    engine = engine_of()
    state = engine.state
    army = road_under_way(engine)
    row = alerts(engine)["VAYR"][0]

    for _ in range(row["days_left"] - 1):
        engine.step_day()
        waiting = alerts(engine)["VAYR"][0]
        assert waiting["arrival_day"] == row["arrival_day"], "the date does not drift"
        assert waiting["days_left"] == row["days_left"] - state.day_index
        assert not events(state, "battle"), "the column is still walking"
    engine.step_day()

    battles = events(state, "battle")
    assert battles and battles[-1]["day"] == row["arrival_day"]
    assert battles[-1]["date"] == row["arrival_date"], (
        "the date the panel promised is the date the column landed"
    )
    assert army.march is None, "a landed column has no order left to warn about"
    assert alerts(engine).get("VAYR", []) == [], "a landed column is a battle, not a warning"


def test_a_column_with_no_order_left_is_movement_not_invasion():
    engine = engine_of()
    state = engine.state
    army = road_under_way(engine)
    assert alerts(engine)["VAYR"]

    army.march = None
    army.tile = state.countries["VAYR"].tiles[0]
    assert alerts(engine).get("VAYR", []) == [], (
        "a column that turned around or reached its own camp drops off the list the same day"
    )


# --- 报价说的是抵达那一仗：守军为空就几乎不折兵 ------------------------------------------

def test_an_undefended_landing_costs_the_invader_a_fraction_of_a_battle():
    engine = engine_of()
    state = engine.state
    rate = state.config["military"]["unopposed_loss_rate"]
    guard_rate = state.config["military"]["victor_loss_rate"]
    army = road_under_way(engine, units=60)
    undefended = alerts(engine)["VAYR"][0]

    assert undefended["defenders"] == 0 and undefended["unopposed"] is True
    assert undefended["attacker_loss"] == int(60 * rate) < 60 * guard_rate

    defended_engine = engine_of()
    defended_state = defended_engine.state
    rival = defended_state.countries["VAYR"]
    column = road_under_way(defended_engine, units=60)
    gate = alerts(defended_engine)["VAYR"][0]["tile"]
    guard = only_army(rival, (gate["col"], gate["row"]), units=60)
    row = alerts(defended_engine)["VAYR"][0]

    assert row["defenders"] == 1 and row["unopposed"] is False
    assert row["attacker_loss"] == max(1, int(60 * guard_rate)) > undefended["attacker_loss"]
    assert row["strength"] == column.strength


# --- 都城：打的是中心格的那一列，说的是整座国 ----------------------------------------------

def test_a_column_walking_at_the_capital_ring_says_so():
    engine = engine_of()
    defender, city, army, camp, stand = march_ready(engine)
    ordered = engine.apply_action("ELAR", strike_tile("ELAR", "VAYR", army.army_id, city.center))
    assert ordered.accepted, ordered.reason_text

    row = alerts(engine)["VAYR"][0]
    assert row["capital"] is True and row["city_id"] == city.city_id
    assert row["tile"] == {"col": city.center[0], "row": city.center[1]}, (
        "the wall is what the column is walking at, so the warning names the center"
    )
    assert row["destination"] == {"col": camp[0], "row": camp[1]}


def test_a_gate_that_is_only_a_farm_hex_does_not_claim_to_be_a_capital():
    engine = engine_of()
    army = road_under_way(engine)
    row = alerts(engine)["VAYR"][0]
    assert row["capital"] is False and row["city_id"] is None


# --- 回防：能赶上的那一支才算答案 --------------------------------------------------------

def test_the_relief_names_the_column_that_can_stand_there_and_only_for_the_asker():
    engine = engine_of()
    state = engine.state
    rival = state.countries["VAYR"]
    army = road_under_way(engine)
    guard = only_army(rival, rival.cities[0].center, units=10)

    row = alerts(engine, with_relief=True, only_victim="VAYR")["VAYR"][0]
    assert row["relief"] == {
        "army_id": guard.army_id, "days": row["relief"]["days"], "cost": row["relief"]["cost"],
    }
    assert row["relief"]["days"] >= 1
    assert alerts(engine, with_relief=True, only_victim="MARIS") == {}, (
        "one realm's observation does not price the whole board's relief roads"
    )

    gate = row["tile"]
    guard.tile = (gate["col"], gate["row"])
    assert alerts(engine, with_relief=True, only_victim="VAYR")["VAYR"][0]["relief"] is None, (
        "a garrison already on the hex is the defender the alert counted, not a relief march"
    )


def threat_row(**overrides):
    """One alert row as the settlement would print it, for the planner's own arithmetic."""
    row = {
        "attacker": "ELAR", "army_id": "ELAR-A1", "strength": 40, "columns": 1,
        "purpose": "ATTACK", "expedition": False,
        "tile": {"col": 16, "row": 18}, "city_id": None, "city": None, "capital": False,
        "defenders": 0, "unopposed": True, "attacker_loss": 3,
        "days_left": 4, "arrival_day": 4, "arrival_date": "公元前2000年1月5日",
        "relief": {"army_id": "VAYR-A1", "days": 4, "cost": 2.0},
    }
    row.update(overrides)
    return row


def test_the_agent_meets_a_column_only_where_it_can_arrive_first():
    rows = [
        threat_row(),  # undefended gate, relief walks 4 days for a 4-day warning: go
        threat_row(army_id="ELAR-A2", tile={"col": 3, "row": 9}, days_left=4,
                   relief={"army_id": "VAYR-A2", "days": 5, "cost": 3.0}),  # too slow
        threat_row(army_id="ELAR-A3", tile={"col": 8, "row": 2}, defenders=2, unopposed=False,
                   relief={"army_id": "VAYR-A3", "days": 1, "cost": 1.0}),  # already manned
        threat_row(army_id="ELAR-A4", city_id="VAYR-C1", capital=True, defenders=1,
                   unopposed=False, strength=80, relief={"army_id": "VAYR-A4", "days": 2}),
    ]
    orders = HeuristicAgent._defense_orders(rows, limit=4)

    assert orders == [
        {"type": "MILITARY", "operation": "MARCH", "army_id": "VAYR-A4",
         "tile": rows[3]["tile"]},
        {"type": "MILITARY", "operation": "MARCH", "army_id": "VAYR-A1",
         "tile": rows[0]["tile"]},
    ], "the 都城 answers first, and a gate nobody can reach in time is left to its fate"
    assert HeuristicAgent._defense_orders(rows, limit=1) == orders[:1]
    assert HeuristicAgent._defense_orders([], limit=3) == []


def test_a_capital_alert_is_answered_before_a_gate_alert():
    rows = [
        threat_row(army_id="ELAR-A1", days_left=2, relief={"army_id": "VAYR-A1", "days": 2}),
        threat_row(army_id="ELAR-A2", capital=True, days_left=5, strength=20,
                   tile={"col": 17, "row": 20}, relief={"army_id": "VAYR-A2", "days": 5}),
    ]
    orders = HeuristicAgent._defense_orders(rows, limit=2)
    assert [order["army_id"] for order in orders] == ["VAYR-A2", "VAYR-A1"], (
        "losing the 都城 now costs the whole realm, so it is answered first"
    )


def test_one_column_is_not_ordered_twice_by_two_alerts():
    rows = [
        threat_row(tile={"col": 16, "row": 18}),
        threat_row(army_id="ELAR-A2", tile={"col": 15, "row": 17}),
    ]
    orders = HeuristicAgent._defense_orders(rows, limit=3)
    assert [order["army_id"] for order in orders] == ["VAYR-A1"], (
        "one relief column answers one gate; the second alert needs the second army"
    )


def test_the_defender_reads_the_alert_and_the_answer_in_its_own_observation():
    engine = engine_of()
    state = engine.state
    rival = state.countries["VAYR"]
    army = road_under_way(engine)
    guard = only_army(rival, rival.cities[0].center, units=20)

    observation = build_observation(state, "VAYR")
    rows = observation["incoming_threats"]
    assert [row["army_id"] for row in rows] == [army.army_id]
    assert rows[0]["relief"]["army_id"] == guard.army_id
    assert build_observation(state, "ELAR")["incoming_threats"] == []

    decision = asyncio.run(HeuristicAgent("VAYR").decide(observation))
    assert {
        "type": "MILITARY", "operation": "MARCH", "army_id": guard.army_id,
        "tile": rows[0]["tile"],
    } in decision["actions"], "the 回防 order is the first thing the threatened realm does"


# --- 面板与快照：同一个数字从引擎走到地图上 ----------------------------------------------

def test_the_snapshot_files_every_realm_s_alerts_for_the_panel():
    session = LiveSession(speed=1.0, maximum_day=1, paused=True)
    try:
        assert session.snapshot()["incoming_threats"] == {}
        session.control("take_control", "ELAR")
        assert session.player_action("ELAR", {
            "type": "DIPLOMACY", "operation": "DECLARE_WAR", "target": "VAYR",
        })["ok"]
        assert session.player_action("ELAR", {
            "type": "MILITARY", "operation": "RECRUIT", "unit": "MILITIA", "strength": 4,
        })["ok"]
        assert session.player_action("ELAR", {
            "type": "MILITARY", "operation": "ATTACK", "target": "VAYR",
        })["ok"]
        rows = session.snapshot()["incoming_threats"]["VAYR"]
        assert len(rows) == 1 and rows[0]["attacker"] == "ELAR"
        assert rows[0]["days_left"] >= 1 and rows[0]["arrival_date"]
        assert "relief" not in rows[0], "the snapshot answers the map, and the map does not search"
    finally:
        session._stop.set()
        session.thread.join(timeout=2)


def test_the_alert_repeats_itself_from_a_saved_game(tmp_path):
    engine = engine_of()
    state = engine.state
    road_under_way(engine)
    before = alerts(state)

    restored = GameState.load(state.save(tmp_path / "alerts.json"))
    assert alerts(restored) == before, (
        "the warning is the march order re-read, so a reloaded save warns the same day"
    )


# --- 警报叫醒议事：90 日的朝会赶不上 17 日的围城 ------------------------------------------

class Scribe:
    """A planner that only remembers who was called to the table."""

    def __init__(self):
        self.rounds = []

    def plan(self, engine, only=None):
        self.rounds.append(None if only is None else set(only))


def test_a_column_bound_for_a_wall_alarms_the_realm_that_owns_it():
    engine = engine_of()
    assert Military(engine.state).alarm_watch() == set()

    defender, city, army, _camp, _stand = march_ready(engine)
    assert engine.apply_action(
        "ELAR", strike_tile("ELAR", "VAYR", army.army_id, city.center)).accepted

    row = alerts(engine)["VAYR"][0]
    assert row["city_id"] == city.city_id, "this order opens a blockade when it lands"
    assert Military(engine.state).alarm_watch() == {"VAYR"}, (
        "a wall about to be starved is a council matter the day the order is given"
    )


def test_a_gate_raid_does_not_convene_anybody():
    engine = engine_of()
    road_under_way(engine)
    row = alerts(engine)["VAYR"][0]

    assert row["city_id"] is None, "a traded gate tile is a battle, not a siege"
    assert Military(engine.state).alarm_watch() == set(), (
        "waking every neighbour for every gate hex would only turn the council into a clock"
    )


def test_a_starving_wall_keeps_its_realm_under_watch():
    engine = engine_of()
    _defender, city, _army, _ordered = besieged(engine)

    assert city.siege is not None
    assert Military(engine.state).alarm_watch() == {"VAYR"}


def test_an_alarm_sits_the_besieged_realm_down_before_the_next_scheduled_council():
    scribe = Scribe()
    engine = RuleEngine(
        GameState.new(seed=2000), planner=scribe,
        enable_city_manager=False, enable_frontier_ai=False,
    )
    interval = engine.state.config["planning_interval_days"]
    assert interval > 30, "the point of the alarm is that the ordinary rhythm is too slow"

    engine.step_day()
    assert scribe.rounds == [None], "the scheduled day convenes the whole board"

    besieged(engine)
    engine.step_day()
    assert scribe.rounds[-1] == {"VAYR"}, (
        "a blockade runs ~17 days, so the realm whose wall is being starved answers today"
    )

    engine.step_day()
    assert scribe.rounds[-1] == {"VAYR"}, "the alarm keeps the realm at the table until it lifts"


def test_reaffirming_a_blockade_writes_no_second_entry():
    engine = engine_of()
    state = engine.state
    _defender, city, army, _ordered = besieged(engine)
    assert len(events(state, "siege_started")) == 1

    again = engine.apply_action("ELAR", strike_tile("ELAR", "VAYR", army.army_id, city.center))
    assert again.accepted, again.reason_text
    assert len(events(state, "siege_started")) == 1, (
        "an alarmed realm now meets every day of a siege; a ledger already carrying this "
        "column has nothing new to say"
    )
    assert city.siege["armies"] == [army.army_id]
