"""Deterministic, API-free country baselines with distinct strategic preferences."""
from __future__ import annotations

from typing import Any

from agents.base import CountryAgent
from agents.personality import personality_definition

PREFERENCES = {
    "ELAR": {"policy": "AGRICULTURE", "research": "agriculture", "building": "FARM"},
    "MARIS": {"policy": "TRADE", "research": "bronze_metallurgy", "building": "ROAD"},
    "VAYR": {"policy": "INDUSTRY", "research": "bronze_metallurgy", "building": "MINE"},
    "SAHAR": {"policy": "AGRICULTURE", "research": "agriculture", "building": "FARM"},
}
DEFAULT_PROFILES = [
    {"policy": "AGRICULTURE", "research": "agriculture", "building": "FARM"},
    {"policy": "TRADE", "research": "bronze_metallurgy", "building": "ROAD"},
    {"policy": "INDUSTRY", "research": "bronze_metallurgy", "building": "MINE"},
    {"policy": "BALANCED", "research": "writing", "building": "LIBRARY"},
]
PROFILE_ALIASES = {"A": "ELAR", "B": "MARIS", "C": "VAYR", "D": "SAHAR"}


class HeuristicAgent(CountryAgent):
    def __init__(self, country_id: str) -> None:
        self.country_id = country_id
        # New/custom worlds may contain any number of countries. Keep the four
        # tuned profiles as special cases, but never fail for an unknown ID.
        profile_id = PROFILE_ALIASES.get(country_id, country_id)
        profile_index = sum(ord(char) for char in country_id) % len(DEFAULT_PROFILES)
        self.preference = dict(
            PREFERENCES.get(profile_id, DEFAULT_PROFILES[profile_index])
        )

    def _recruit_choice(self, observation: dict[str, Any], profile: dict[str, Any], size: int) -> str | None:
        """Preferred type that is unlocked, below its cap and actually affordable."""
        rules = observation["rules"]
        units = rules.get("units", {})
        military = rules.get("military", {})
        country = observation["country"]
        technology = country.get("technology", {})
        options = {o["unit"]: o for o in country.get("recruit_options", [])}
        order = [unit_id for unit_id in profile.get("units", []) if unit_id in units]
        default = military.get("default_unit")
        if default in units and default not in order:
            order.append(default)
        for unit_id in order:
            spec = units[unit_id]
            requirement = spec.get("required_technology")
            if requirement and technology.get(requirement["field"], 0) < requirement["level"]:
                continue
            if unit_id in options and options[unit_id]["room"] < size:
                continue
            if country["treasury"] < size * spec["cost"]["treasury"]:
                continue
            if country["food"] < size * spec["cost"]["food"]:
                continue
            return unit_id
        return None

    @staticmethod
    def _peace_gate(profile: dict[str, Any], country: dict[str, Any],
                    rules: dict[str, Any]) -> float:
        """Where this realm sues for peace, given what the same war pays it.

        Tribute is read as a share of the realm's own daily income, so 2% weighs the same
        for a large and a poor economy: collecting it buys another season of fighting,
        paying it asks for peace sooner. `capture_tribute_cap` bounds how far either goes.
        A city of its own whose walls are already starved counts the other way: the fall is
        priced before it happens, so a realm does not stand by and wait for one. The seat
        counts twice over, because a capital's fall is not one more town lost but the realm
        struck off the board — `annexation.peace_pressure` is that premium on top of the
        ordinary wall pressure.
        """
        siege = rules["military"]["siege"]
        starved = [
            (float(row["siege"]["progress"]) / max(float(row["siege"]["threshold"]), 1e-9),
             bool(row.get("capital")))
            for row in country.get("cities", []) if row.get("siege")
        ]
        threat = max((share for share, _seat in starved), default=0.0)
        seat = max((share for share, capital in starved if capital), default=0.0)
        gate = float(profile["peace_exhaustion"]) - min(1.0, threat) * float(siege["peace_pressure"])
        gate -= min(1.0, seat) * float(rules["military"]["annexation"]["peace_pressure"])
        tribute = country.get("tribute") or {}
        base = max(0.0, float(tribute.get("base_income", 0.0)))
        if base <= 0.0:
            return gate
        net = float(tribute.get("daily_collected", 0.0)) - float(tribute.get("daily_paid", 0.0))
        ratio = max(-1.0, min(1.0, net / base))
        return gate + ratio * float(rules["military"]["capture_tribute_resolve"])

    @staticmethod
    def _siege_orders(campaigns: dict[str, Any], wars: list[str], limit: int,
                      skip: tuple = ()) -> list[dict[str, Any]]:
        """Blockade the walls in reach: the enemy capital first, then the ones worth marching to.

        `siege` rows are the ledger the daily settlement advances, so a realm lying around a
        city is on the shortest road to owning it whole — worth an action slot before any
        gate battle. A blockade another realm opened first is left alone, because one wall
        lies to one realm and a second ledger on the same city is only refused.

        `siege_marches` rows are the same book one step earlier: a column that is not beside
        those walls yet but gets there in one order. The engine books the blockade the day it
        sleeps against them, so the slot spent sending it is the only way a fresh war turns
        into a captured city instead of a hundred traded gate tiles. Lying to a wall already
        reached beats marching to a new one, and an army that has an order is not given a
        second one.

        A 都城 outranks every other wall by intent, not by arithmetic: v0.40 settled that a
        capital starved whole annexes the realm with it, so the siege that ends the war is
        worth two slots more than the one that trades a gate tile. `skip` carries the armies
        already sent to meet an invasion, because pulling a column off a relief march to
        blockade a wall would leave the hex it was walking to undefended.
        """
        skip_all = set(skip)
        choices = sorted(
            (0 if row.get("capital") else 1,
             row["days_left"] if row["days_left"] is not None else 9_999,
             target, index, row)
            for target, column in campaigns.items() if target in wars
            for index, row in enumerate(column.get("siege", []))
            if row["taken_by"] is None and any(
                army_id not in skip_all for army_id in row["armies"])
        )
        orders: list[dict[str, Any]] = []
        committed = set(skip_all)
        for _capital, _days, target, _index, row in choices:
            for army_id in row["armies"]:
                if len(orders) >= limit:
                    return orders
                if army_id in committed:
                    continue
                committed.add(army_id)
                orders.append({
                    "type": "MILITARY", "operation": "ATTACK", "target": target,
                    "army_id": army_id, "tile": row["center"],
                })
        marches = sorted(
            (0 if row.get("capital") else 1,
             row["days"] + row["days_left"] if row["days_left"] is not None else 9_999,
             target, index, row)
            for target, column in campaigns.items() if target in wars
            for index, row in enumerate(column.get("siege_marches", []))
            if row["army_id"] not in committed
        )
        for _capital, _fall, target, _index, row in marches:
            if len(orders) >= limit:
                break
            if row["army_id"] in committed:
                continue
            committed.add(row["army_id"])
            orders.append({
                "type": "MILITARY", "operation": "ATTACK", "target": target,
                "army_id": row["army_id"], "tile": row["center"],
            })
        return orders

    @staticmethod
    def _defense_orders(threats: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        """Meet the column on the gate: stand a waiting army on the hex it is walking to.

        The alert and the relief are both priced by the settlement, so an army is only pulled
        off its post when it can actually arrive first — and the hex it is sent to is the one
        the battle will be fought on, which is the only ground where `_mass` counts it. A
        defended gate is left alone: two garrisons on one hex add nothing to a battle the
        first one already holds. A column walking at the 都城 is answered before anything
        else, because losing that hex now costs the whole realm, not one more town. Second
        come the hexes whose garrison would be *destroyed* rather than routed — a column that
        lands on one of our own barracks is answered even with soldiers already standing
        there, because losing that fight spends the camp and the army with it.
        """
        wanted = sorted(
            (0 if row["capital"] else 1, 0 if HeuristicAgent._garrison_doomed(row) else 1,
             row["days_left"], -int(row["strength"]), row["army_id"], row)
            for row in threats if row.get("relief")
            and int(row["relief"]["days"]) <= int(row["days_left"])
            and (row["capital"] or int(row["defenders"]) == 0
                 or HeuristicAgent._garrison_doomed(row))
        )
        orders: list[dict[str, Any]] = []
        sent: set[str] = set()
        for _capital, _doomed, _days, _strength, _invader, row in wanted:
            if len(orders) >= limit:
                break
            army_id = row["relief"]["army_id"]
            if army_id in sent:
                continue
            sent.add(army_id)
            orders.append({
                "type": "MILITARY", "operation": "MARCH",
                "army_id": army_id, "tile": row["tile"],
            })
        return orders

    @staticmethod
    def _garrison_doomed(row: dict[str, Any]) -> bool:
        """Whether this inbound column would destroy the garrison instead of routing it."""
        rout = row.get("rout") or {}
        return bool(rout) and rout.get("garrison", {}).get("fate") == "annihilated"

    @staticmethod
    def _column_doomed(option: dict[str, Any]) -> bool:
        """Whether the settlement would find no way out for the column being ordered."""
        rout = option.get("rout") or {}
        return bool(rout) and rout.get("attacker", {}).get("fate") == "annihilated"

    @staticmethod
    def _strike_choice(campaigns: dict[str, Any], wars: list[str],
                       held: tuple = ()) -> dict[str, Any] | None:
        """The invaded realm this realm can reach soonest, with the armies that would march.

        Armies already marching report no option, so a waiting general is not asked to
        reorder a strike that is already on its way. Ties go to the gate that already holds
        the most garrisons: converging the camps is what raises the weight a battle is
        fought at, and a strike priced at the same days with more mass is the better buy.
        A column still on the road is met rather than joined: the armies that would strike
        the same gate are ordered onto its rally tile and camp there, so the gate is met by
        the whole field army at one weight instead of one garrison at a time. A second
        column earns its place only against a different gate: two armies at one gate
        still fight one battle. Armies in `held` are already blockading a wall or marching
        to a threatened hex and are not pulled off either. Between two gates reachable in
        the same days, the undefended one is the order to give: the settlement prices it at
        `unopposed_loss_rate` instead of a pitched battle, so the same march buys a hex and
        keeps its soldiers. Two more of the settlement's own numbers break the next ties: a
        gate inside a defended city is a rout rather than an attrition, so a column the quote
        says would have no way out is not sent there while an escapable gate stands; and of
        two escapable gates, the one whose hex carries the defender's barracks is the better
        buy, because a garrison that loses its camp loses the army with it.
        The hex the quote priced travels with the choice: `objective` is a gate on enemy soil
        or the field a column has pitched elsewhere, and only naming it asks for that battle.
        """
        options = sorted(
            (value["days"], 0 if value.get("unopposed") else 1,
             0 if not HeuristicAgent._column_doomed(value) else 1,
             0 if (value.get("rout") or {}).get("garrison", {}).get("fate") == "annihilated"
             else 1,
             -int(value.get("mass") or 0), target, value["army_id"], value)
            for target, value in campaigns.items()
            if target in wars and value.get("reachable") and value["army_id"] is not None
            and value["army_id"] not in held
        )
        if not options:
            return None
        _days, _cheap, _doomed, _burn, _mass, target, army_id, column = options[0]
        lead = next(
            (option for option in column.get("options", [])
             if option["army_id"] == army_id), column,
        )
        others = [
            option for option in column.get("options", [])
            if option["army_id"] != army_id and option.get("reachable")
            and option["army_id"] not in held
        ]
        rally = lead.get("march_target")
        reinforce = (
            [option["army_id"] for option in others
             if option["objective"] == lead["objective"] and option["days"] > 0]
            if lead["days"] > 0 and rally else []
        )
        companion = next(
            (option for option in others
             if option["objective"] != lead["objective"]),
            None,
        )
        return {
            "target": target, "army_id": army_id,
            "companion": None if companion is None else companion["army_id"],
            "companion_objective": None if companion is None else companion.get("objective"),
            "reinforce": reinforce, "rally": rally,
            "objective": lead.get("objective"),
        }

    @staticmethod
    def _strike_action(target: str, army_id: str,
                       objective: dict[str, int] | None) -> dict[str, Any]:
        """An ATTACK aimed at the very hex the quote priced.

        Naming the tile matters twice: the frontier the engine would pick for a bare ATTACK is
        whatever is cheapest the day the order lands, not the gap the days were counted to, and
        a column standing on ground its own realm does not hold is no frontier at all — only an
        explicit tile reaches it. A row that carries no objective keeps the old order shape and
        lets the engine choose.
        """
        action = {
            "type": "MILITARY", "operation": "ATTACK",
            "target": target, "army_id": army_id,
        }
        if objective:
            action["tile"] = objective
        return action

    def _barracks_orders(self, observation: dict[str, Any], profile: dict[str, Any],
                         rules: dict[str, Any], country: dict[str, Any]) -> list[dict[str, Any]]:
        eligible = [army for army in country.get("armies", [])
                    if army["barracks"] and army["strength"] and not army.get("march")]
        if not eligible:
            # Staff an empty camp before waiting for the training pool.
            standing = [a for a in country.get("armies", [])
                        if a["strength"] and not a.get("march") and a.get("tile")]
            empty = [camp for camp in country.get("barracks", []) if not camp["strength"]]
            if standing and empty and country["food"] > 40 and not country.get("at_war_with"):
                army, camp = min(((a, c) for a in standing for c in empty),
                                 key=lambda pair: sum(abs(x-y) for x,y in zip(pair[0]["tile"], pair[1]["tile"])))
                col, row = camp["tile"]
                if army["tile"] != [col, row]:
                    return [{"type": "MILITARY", "operation": "MARCH",
                             "army_id": army["army_id"], "tile": {"col": col, "row": row}}]
            return []
        army = next((a for a in eligible if a["recruiting"]), eligible[0])
        camp = next((c for c in country.get("barracks", [])
                     if c["army_id"] == army["army_id"]), {})
        units = rules["units"]
        technology = country.get("technology", {})
        fallback = rules["military"]["barracks"]["default_unit"]
        for unit in dict.fromkeys([*profile.get("units", []), fallback]):
            if unit not in units or camp.get("unit_rooms", {}).get(unit, 0) <= 0:
                continue
            spec = units[unit]
            need = spec.get("required_technology")
            if need and technology.get(need["field"], 0) < need["level"]:
                continue
            if country["food"] < spec["cost"]["food"]:
                continue
            action = {"type": "MILITARY", "army_id": army["army_id"]}
            if army["preferred_unit"] != unit:
                return [{**action, "operation": "SET_RECRUIT_UNIT", "unit": unit}]
            if not army["recruiting"]:
                return [{**action, "operation": "SET_RECRUITING", "recruiting": True}]
            return []
        return []

    async def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        country = observation["country"]
        rules = observation["rules"]
        personality = personality_definition(country.get("personality"))
        profile = personality["mock"]
        actions: list[dict[str, Any]] = []

        incoming_pacts = observation.get("diplomacy", {}).get("incoming_pacts", [])
        if incoming_pacts:
            pact = sorted(incoming_pacts, key=lambda item: item["created_day"])[0]
            actions.append({
                "type": "DIPLOMACY", "operation": "ACCEPT_PACT",
                "proposal_id": pact["proposal_id"],
            })

        wars = country.get("at_war_with", [])
        campaigns = observation.get("campaign_options", {})
        if wars:
            if country.get("war_exhaustion", 0) >= self._peace_gate(profile, country, rules):
                actions.append({
                    "type": "DIPLOMACY", "operation": "PROPOSE_PEACE", "target": wars[0],
                })
            strike = None
            limit = rules["max_actions_per_decision"]
            defending = self._defense_orders(
                observation.get("incoming_threats", []), max(0, limit - len(actions)))
            actions.extend(defending)
            held = tuple(order["army_id"] for order in defending)
            blockading = self._siege_orders(
                campaigns, wars, max(0, limit - len(actions)), skip=held)
            actions.extend(blockading)
            held = held + tuple(order["army_id"] for order in blockading)
            if len(actions) < limit:
                strike = self._strike_choice(campaigns, wars, held)
            if strike:
                actions.append(self._strike_action(
                    strike["target"], strike["army_id"], strike["objective"]))
                # Mass first, split second: the gates are met by whoever is standing at them.
                for army_id in strike["reinforce"]:
                    if len(actions) >= limit:
                        break
                    actions.append({
                        "type": "MILITARY", "operation": "MARCH", "army_id": army_id,
                        "tile": strike["rally"],
                    })
                if strike["companion"] and len(actions) < limit:
                    actions.append(self._strike_action(
                        strike["target"], strike["companion"], strike["companion_objective"]))
        elif observation["world_day"] >= profile["recruit_day"]:
            army = country.get("army_strength", 0)
            size = 0
            if army == 0 and country["food"] > 30:
                size = 2
            elif army < profile["army_target"] and country["food"] > 40:
                size = 1
            if size:
                unit_id = self._recruit_choice(observation, profile, size)
                if unit_id:
                    actions.append({
                        "type": "MILITARY", "operation": "RECRUIT",
                        "unit": unit_id, "strength": size,
                    })

        limit = rules["max_actions_per_decision"]
        for order in self._barracks_orders(observation, profile, rules, country)[:2]:
            if len(actions) >= limit:
                break
            actions.append(order)

        incoming = sorted(
            observation["pending_incoming_trades"],
            key=lambda trade: trade["offer_food"] / max(trade["request_treasury"], 0.001),
            reverse=True,
        )
        if incoming:
            trade = incoming[0]
            affordable = country["treasury"] >= trade["request_treasury"]
            fair = trade["request_treasury"] <= max(0.5, trade["offer_food"] * 0.75)
            operation = "ACCEPT" if affordable and fair else "REJECT"
            actions.append({
                "type": "TRADE", "operation": operation,
                "proposal_id": trade["proposal_id"],
            })

        research = self._research_choice(country)
        if country["research_field"] != research and len(actions) < 4:
            actions.append({"type": "SET_RESEARCH", "field": research})
        policy = self.preference["policy"]
        if country["policy"] != policy and len(actions) < 4:
            actions.append({"type": "SET_POLICY", "priority": policy})

        desired_workers = self._workers(country)
        if country["workers"] != desired_workers and len(actions) < 4:
            actions.append({"type": "ALLOCATE_WORKERS", **desired_workers})

        settle = self._settle_action(country, observation, rules, profile, actions)
        if settle:
            actions.append(settle)

        projected_treasury = country["treasury"]
        chosen_tiles = set()
        chosen_buildings = set()
        for _ in range(2):
            if len(country["projects"]) + sum(1 for action in actions if action["type"] == "BUILD") >= 2 or len(actions) >= 4:
                break
            building = self._building_choice(country, rules)
            spec = rules["buildings"][building]
            if spec.get("max_per_country") and (
                building in chosen_buildings
                or sum(value == building for value in country["buildings"].values())
                + sum(project["building"] == building for project in country["projects"]) >= spec["max_per_country"]
            ):
                break
            cost = spec["cost"]
            reserve = profile.get("build_reserve", 0.0)
            if country["outposts"] >= rules["expansion"]["max_outposts"]:
                reserve = 0.0
            if projected_treasury < cost + reserve:
                break
            tile = self._choose_tile(
                [tile for tile in observation["tiles"] if (tile["col"], tile["row"]) not in chosen_tiles],
                building,
            )
            if tile is None:
                break
            chosen_tiles.add((tile["col"], tile["row"]))
            chosen_buildings.add(building)
            projected_treasury -= cost
            actions.append({
                "type": "BUILD",
                "tile": {"col": tile["col"], "row": tile["row"]},
                "building": building,
            })

        diplomacy = observation.get("diplomacy", {})
        relations = diplomacy.get("relations", {})
        active_partners = {
            partner for pact in diplomacy.get("active_pacts", [])
            for partner in (pact["sender"], pact["target"]) if partner != self.country_id
        }
        partners = [partner for partner in sorted(relations) if partner not in wars]
        if partners and len(actions) < 4:
            partner = partners[(observation["world_day"] // 90) % len(partners)]
            relation = relations.get(partner, 0)
            has_pending = any(
                proposal["sender"] == self.country_id and proposal["target"] == partner
                for proposal in observation.get("diplomacy", {}).get("outgoing_pacts", [])
            )
            if relation >= profile["pact_relation"] and partner not in active_partners and not has_pending:
                actions.append({
                    "type": "DIPLOMACY", "operation": "PROPOSE_PACT",
                    "target": partner, "pact": "TRADE",
                })
            elif relation < profile["gift_relation"] and country["treasury"] > profile["gift_treasury"]:
                actions.append({
                    "type": "DIPLOMACY", "operation": "GIFT",
                    "target": partner, "amount": min(profile["gift_max"], round(country["treasury"] * 0.06, 2)),
                })

        if (
            not wars and country["food"] >= 30 * country.get("military_upkeep_food", 0)
            and country.get("army_strength", 0) >= profile["war_min_army"]
            and observation["world_day"] >= profile["recruit_day"] and len(actions) < 4
        ):
            foreign = observation.get("foreign_military", {})
            rivals = [
                partner for partner, relation in relations.items()
                if relation <= profile["war_relation"] and foreign.get(partner, 0) < country["army_strength"]
                and campaigns.get(partner, {}).get("reachable")
            ]
            if rivals and ((observation["world_day"] // 90 + len(self.country_id)) % profile["war_roll"] == 0):
                # Strike the nearest quarrel: the march is paid for out of the campaign season.
                rival = min(rivals, key=lambda partner: (campaigns[partner]["days"], partner))
                actions.append({
                    "type": "DIPLOMACY", "operation": "DECLARE_WAR",
                    "target": rival,
                })

        outgoing = observation["pending_outgoing_trades"]
        if country["food"] > profile["trade_food"] and not outgoing and len(actions) < 4:
            # A trading partner chosen from the whole continent would be a realm this one is
            # busy invading, and the engine refuses trade with it: rotate over the peaceful.
            partners = sorted(set(observation["known_countries"]) - {self.country_id} - set(wars))
            if partners:
                # Deterministic rotation without relying on model output or hidden state.
                target = partners[(observation["world_day"] // 90) % len(partners)]
                actions.append({
                    "type": "TRADE", "operation": "PROPOSE", "target": target,
                    "offer_food": 5.0, "request_treasury": 3.0,
                })

        if not actions:
            actions.append({"type": "NO_OP"})
        return {
            "country_id": self.country_id,
            "world_day": observation["world_day"],
            "actions": actions[: rules["max_actions_per_decision"]],
            "reason": self._reason(country, personality),
        }

    def _settle_action(
        self,
        country: dict[str, Any],
        observation: dict[str, Any],
        rules: dict[str, Any],
        profile: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        expansion = rules["expansion"]
        if len(actions) >= profile["expand_actions"]:
            return None
        if country["outposts"] >= expansion["max_outposts"]:
            return None
        if country["treasury"] < expansion["treasury_cost"] + profile["expand_reserve"]:
            return None
        if country["food"] < profile["expand_food"]:
            return None
        if len(country.get("buildings", {})) < profile.get("expand_after_buildings", 0):
            return None
        candidates = observation.get("frontier_candidates") or []
        if not candidates:
            return None
        target = max(
            candidates,
            key=lambda tile: (1 if tile["river"] else 0, tile["biome"] == "grassland"),
        )
        return {"type": "SETTLE", "tile": {"col": target["col"], "row": target["row"]}}

    def _workers(self, country: dict[str, Any]) -> dict[str, float]:
        if country["food"] < 60 or country["last_economy"].get("food_shortage", 0) > 0:
            return {"agriculture": 0.7, "mining": 0.1, "construction": 0.2}
        profile = personality_definition(country.get("personality"))["mock"]
        if profile.get("workers"):
            if country.get("personality") == "conservative" and country["food"] < 150:
                return {"agriculture": 0.75, "mining": 0.05, "construction": 0.2}
            return dict(profile["workers"])
        if self.preference["policy"] == "INDUSTRY":
            return {"agriculture": 0.45, "mining": 0.35, "construction": 0.2}
        if self.preference["policy"] in {"TRADE", "RESEARCH"}:
            return {"agriculture": 0.5, "mining": 0.3, "construction": 0.2}
        return {"agriculture": 0.65, "mining": 0.15, "construction": 0.2}

    def _research_choice(self, country: dict[str, Any]) -> str:
        technology = country["technology"]
        preferred = self.preference["research"]
        if technology.get("writing", 0) < 1:
            return "writing"
        if technology.get(preferred, 1) < 2:
            return preferred
        if technology.get("writing", 0) < 2:
            return "writing"
        # Advance the era instead of pouring everything into one line forever.
        lowest = min(technology.values())
        balanced_candidates = [
            field for field in ("agriculture", "bronze_metallurgy", "writing")
            if technology.get(field, 0) == lowest
        ]
        if preferred in balanced_candidates:
            return preferred
        return balanced_candidates[0] if balanced_candidates else preferred

    def _building_choice(self, country: dict[str, Any], rules: dict[str, Any]) -> str:
        technology = country["technology"]
        counts = {}
        for building in country["buildings"].values():
            counts[building] = counts.get(building, 0) + 1
        for project in country["projects"]:
            counts[project["building"]] = counts.get(project["building"], 0) + 1
        personality = country.get("personality", "neutral")
        wonders = (
            ("GREAT_GRANARY", "agriculture", 3, 220),
            ("GREAT_FORGE", "bronze_metallurgy", 3, 260),
            ("GREAT_LIBRARY", "writing", 3, 300),
        )
        if personality == "aggressive":
            wonders = (wonders[1], wonders[0], wonders[2])
        elif personality == "conservative":
            wonders = (wonders[0], wonders[2], wonders[1])
        for name, field, level, cost in wonders:
            if technology.get(field, 0) >= level and not counts.get(name) and country["treasury"] >= cost:
                return name
        cities = country.get("cities", [])
        low_health = any(float(city.get("health", 100)) < 45 for city in cities)
        low_security = any(float(city.get("security", 100)) < 45 for city in cities)
        if low_health and technology.get("agriculture", 0) >= 2 and not counts.get("GRANARY"):
            return "GRANARY"
        if low_security and technology.get("writing", 0) >= 1 and not counts.get("TEMPLE"):
            return "TEMPLE"
        crowded = country["population"] / 1000.0 >= country["housing"] - 1
        if country["stability"] < 55 and technology.get("writing", 0) >= 1 and not counts.get("TEMPLE"):
            return "TEMPLE"
        near_storage_limit = country["food"] >= country.get("food_capacity", 99999) - 70
        if (country["food"] < 110 or crowded or near_storage_limit) and technology.get("agriculture", 0) >= 2 and not counts.get("GRANARY"):
            return "GRANARY"
        if country["food"] < 100:
            return "FARM"
        barracks = rules["buildings"].get("BARRACKS")
        profile = personality_definition(personality)["mock"]
        if barracks and profile.get("barracks", 0) > 0:
            need = barracks.get("required_technology") or {}
            ready = technology.get(need.get("field", ""), 0) >= need.get("level", 0)
            economy = country.get("last_economy") or {}
            reserve = country["food"] + 30 * (
                economy.get("food_production", 0) - economy.get("food_consumption", 0)
                - country.get("military_upkeep_food", 0)
            )
            if ready and country["treasury"] >= barracks["cost"] and reserve >= 120:
                return "BARRACKS"
        road_target = int(personality_definition(personality)["mock"].get("roads", 0))
        if road_target and counts.get("ROAD", 0) < road_target:
            # Paving is the one investment that changes what an army can reach, so a
            # road-hungry realm finishes it before it builds its industry.
            return "ROAD"
        if technology.get("bronze_metallurgy", 0) >= 2 and not counts.get("WORKSHOP") and country["treasury"] > 35:
            return "WORKSHOP"
        if technology.get("writing", 0) >= 1 and not counts.get("LIBRARY") and country["treasury"] > 28:
            return "LIBRARY"
        if technology.get("agriculture", 0) >= 3 and not counts.get("MARKET") and country["treasury"] > 40:
            return "MARKET"
        if technology.get("writing", 0) >= 1 and not counts.get("TEMPLE") and country["stability"] < 72:
            return "TEMPLE"
        food_full = country["food"] >= country.get("food_capacity", 99999) - 10
        if food_full:
            return "MINE"
        if personality == "aggressive":
            if technology.get("bronze_metallurgy", 0) >= 2 and counts.get("WORKSHOP", 0) < 2 and country["treasury"] > 40:
                return "WORKSHOP"
            if counts.get("MINE", 0) < 3 and country["food"] > 90:
                return "MINE"
        elif personality == "conservative":
            if technology.get("writing", 0) >= 1 and counts.get("TEMPLE", 0) < 1 and country["treasury"] > 25:
                return "TEMPLE"
            if technology.get("writing", 0) >= 1 and counts.get("LIBRARY", 0) < 1 and country["treasury"] > 28:
                return "LIBRARY"
        preferred = self.preference["building"]
        preferred_count = counts.get(preferred, 0) + sum(
            project["building"] == preferred for project in country["projects"]
        )
        if preferred_count >= 2:
            if country["food"] < country.get("food_capacity", 99999) - 50:
                return "FARM"
            return "MINE"
        return preferred

    @staticmethod
    def _choose_tile(tiles: list[dict[str, Any]], building: str) -> dict[str, Any] | None:
        candidates = [tile for tile in tiles if not tile["building"] and not tile["project"]]
        if not candidates:
            return None
        def score(tile: dict[str, Any]) -> tuple[float, int, int]:
            if building == "FARM":
                biome = {"grassland": 4.0, "wetland": 4.5, "savanna": 3.5,
                         "forest": 2.8, "steppe": 2.0, "desert": 0.4}.get(tile["biome"], 0.0)
                value = biome + (1.5 if tile["river"] else 0.0) - tile["elevation_m"] / 2000.0
            elif building == "MINE":
                value = tile["elevation_m"] / 500.0 + (0.4 if tile["river"] else 0.0)
            else:
                value = 2.0 - abs(tile["elevation_m"] - 400.0) / 1000.0 + (0.5 if tile["river"] else 0.0)
            return value, -tile["row"], -tile["col"]
        return max(candidates, key=score)

    def _reason(self, country: dict[str, Any], personality: dict[str, Any]) -> str:
        return (
            f"{self.country_id} · {personality['label']}性格（{personality['tagline']}）："
            f"优先 {self.preference['building']}、{self.preference['research']} 研究与 "
            f"{self.preference['policy']} 政策；当前粮食 {country['food']:.1f}，"
            f"财政 {country['treasury']:.1f}。"
        )
