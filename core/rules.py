"""The sole writer of dynamic world state: validation, atomic actions, and daily ticks."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from core.actions import ActionRejected, ActionResult, validate_action
from core.clock import GameClock
from core.city_governance import CityManager
from core.construction import Construction
from core.diplomacy import Diplomacy
from core.economy import Economy
from core.military import Military
from core.state import CityState, GameState
from worldgen import hex_distance


class RuleEngine:
    def __init__(
        self,
        state: GameState,
        clock: GameClock | None = None,
        planner: Any | None = None,
        enable_city_manager: bool = True,
        enable_frontier_ai: bool = True,
    ) -> None:
        self.state = state
        self.clock = clock or GameClock()
        self.planner = planner
        self.enable_city_manager = enable_city_manager
        self.enable_frontier_ai = enable_frontier_ai
        self.economy = Economy(state)
        self.construction = Construction(state)
        self.diplomacy = Diplomacy(state)
        self.military = Military(state)
        self.city_manager = CityManager(state)

    def apply_action(self, actor: str, raw_action: Any) -> ActionResult:
        action_id = f"A{self.state.next_action_number:08d}"
        self.state.next_action_number += 1
        action_type = raw_action.get("type") if isinstance(raw_action, dict) else "INVALID"
        if not isinstance(action_type, str):
            action_type = "INVALID"
        normalized = None
        try:
            if actor not in self.state.countries:
                raise ActionRejected("UNKNOWN_COUNTRY", f"提交国不存在: {actor}")
            normalized = validate_action(actor, raw_action, self.state.config)
            action_type = normalized["type"]
            effects = self._execute(actor, normalized)
            result = ActionResult(
                action_id, actor, action_type, True, "OK", "动作已审核并执行",
                self.state.day_index, effects,
            )
        except ActionRejected as exc:
            result = ActionResult(
                action_id, actor, action_type, False, exc.code, exc.text,
                self.state.day_index, {},
            )
        entry = {
            "action": normalized if normalized is not None else {"type": action_type},
            "result": result.to_dict(),
        }
        self.state.action_log.append(entry)
        self.state.add_receipt(result, entry["action"])
        self.state.record(
            "action_result",
            action_id=result.action_id,
            actor=actor,
            action_type=result.action_type,
            accepted=result.accepted,
            reason_code=result.reason_code,
            reason_text=result.reason_text,
            effects=result.effects,
        )
        return result

    def apply_decision(self, actor: str, decision: Any) -> list[ActionResult]:
        try:
            if not isinstance(decision, dict):
                raise ActionRejected("INVALID_DECISION", "国家决议必须是 JSON 对象")
            if decision.get("country_id") != actor:
                raise ActionRejected("IDENTITY_MISMATCH", "country_id 与 Agent 所代表国家不一致")
            if decision.get("world_day") != self.state.day_index:
                raise ActionRejected("STALE_DECISION", "决议中的 world_day 与当前游戏日不一致")
            actions = decision.get("actions")
            if not isinstance(actions, list):
                raise ActionRejected("INVALID_DECISION", "actions 必须是数组")
            if len(actions) > self.state.config["max_actions_per_decision"]:
                raise ActionRejected("TOO_MANY_ACTIONS", "单次决议动作数量超过上限")
            if "reason" in decision and not isinstance(decision["reason"], str):
                raise ActionRejected("INVALID_DECISION", "reason 必须是字符串")
        except ActionRejected as exc:
            self.state.record(
                "agent_decision_rejected",
                country_id=actor,
                reason_code=exc.code,
                reason_text=exc.text,
            )
            return [self.apply_action(actor, {"type": "NO_OP"})]
        self.state.record(
            "agent_decision",
            country_id=actor,
            reason=decision.get("reason", "")[:1000],
            action_count=len(actions),
        )
        return [self.apply_action(actor, action) for action in actions]

    def _execute(self, actor: str, action: dict[str, Any]) -> dict[str, Any]:
        country = self.state.countries[actor]
        action_type = action["type"]
        if action_type == "NO_OP":
            return {"no_op": True}
        if action_type == "SETTLE":
            coord = (action["tile"]["col"], action["tile"]["row"])
            tile = self.state.world.tile(coord)
            if not tile["land"]:
                raise ActionRejected("INVALID_TERRAIN", "不能在海域建立前哨")
            occupied_by = next(
                (
                    other.country_id for other in self.state.countries.values()
                    if coord in other.tiles
                ),
                None,
            )
            if occupied_by is not None:
                raise ActionRejected("TILE_ALREADY_OWNED", f"地块已属于 {occupied_by}")
            if tile["river"]:
                raise ActionRejected("RIVER_TILE", "不能在河道水面上建城；请选择相邻的干燥陆地")
            if not any(hex_distance(coord, own) == 1 for own in country.tiles):
                raise ActionRejected("NOT_ADJACENT", "前哨必须与本国现有领土相邻")
            expansion = self.state.config["expansion"]
            if len(country.outposts) >= expansion["max_outposts"]:
                raise ActionRejected("OUTPOST_LIMIT", "已达到前哨数量上限")
            if country.population < expansion["minimum_population"]:
                raise ActionRejected("POPULATION_REQUIRED", "人口不足以支持开拓")
            cost = float(expansion["treasury_cost"])
            if country.treasury + 1e-9 < cost:
                raise ActionRejected("INSUFFICIENT_TREASURY", f"开拓需要 {cost} 财政单位")
            def _city_name(tile: dict[str, Any], ordinal: int) -> str:
                roots = {
                    "grassland": "青原", "forest": "林湾", "savanna": "金野",
                    "steppe": "长风", "desert": "沙泉", "wetland": "泽城",
                    "alpine": "高岭", "tundra": "霜原",
                }
                root = roots.get(tile["biome"], "新地")
                return f"{root}城" if ordinal <= 1 else f"{root}城{ordinal}"

            country.treasury -= cost
            country.tiles.append(coord)
            country.outposts.append(coord)
            occupied = {
                cell for entity in self.state.countries.values() for cell in entity.tiles
            }
            claimed = [coord]
            from worldgen import neighbors
            queue = [coord]
            while queue and len(claimed) < self.state.config["expansion"].get("city_tiles", 5):
                current = queue.pop(0)
                for neighbor in self.state.world.neighbors(*current):
                    if len(claimed) >= self.state.config["expansion"].get("city_tiles", 5):
                        break
                    if neighbor in occupied or neighbor in claimed:
                        continue
                    tile = self.state.world.tile(neighbor)
                    if not tile["land"]:
                        continue
                    claimed.append(neighbor)
                    country.tiles.append(neighbor)
                    queue.append(neighbor)
            source_city = max(country.cities, key=lambda city: city.population)
            settlers = min(1500, max(0, source_city.population - 2000))
            source_city.population -= settlers
            city_number = len(country.cities) + 1
            from core.society import default_zone
            city = CityState(
                city_id=f"{actor}-C{city_number}",
                name=_city_name(self.state.world.tile(coord), len(country.outposts)),
                center=coord,
                population=settlers,
                focus="balanced",
                tiles=claimed,
                founded_day=self.state.day_index,
                zones={
                    cell: default_zone(coord, cell, self.state.world.tile(cell))
                    for cell in claimed
                },
                managed_by=source_city.managed_by,
            )
            country.cities.append(city)
            self.state.record(
                "city_founded", country_id=actor, city_id=city.city_id,
                name=city.name, population=settlers, tiles=len(claimed),
                tile={"col": coord[0], "row": coord[1]},
            )
            self.state.record(
                "outpost_founded",
                country_id=actor,
                tile=action["tile"],
                cost=cost,
                total_outposts=len(country.outposts),
            )
            return {"tile": action["tile"], "cost": cost, "outposts": len(country.outposts)}
        if action_type == "BUILD":
            coord = (action["tile"]["col"], action["tile"]["row"])
            if coord not in country.tiles:
                raise ActionRejected("TILE_NOT_OWNED", "目标地块不属于本国")
            tile = self.state.world.tile(coord)
            if not tile["land"]:
                raise ActionRejected("INVALID_TERRAIN", "海域不能建设")
            if coord in country.buildings or self.construction.project_for_tile(country, coord):
                raise ActionRejected("BUILDING_CONFLICT", "目标地块已有建筑或在建工程")
            building = action["building"]
            spec = self.state.config["buildings"][building]
            if tile["biome"] not in spec["allowed_biomes"]:
                raise ActionRejected("INVALID_TERRAIN", f"{tile['biome']} 不适合建设 {building}")
            if building == "FARM" and tile["biome"] == "desert" and not tile["river"]:
                raise ActionRejected("INVALID_TERRAIN", "无河道荒漠地块不能建设农田")
            required = spec.get("required_technology")
            if required and country.technology.get(required["field"], 0) < required["level"]:
                raise ActionRejected(
                    "TECHNOLOGY_REQUIRED",
                    f"{building} 需要 {required['field']} 达到 {required['level']} 级",
                )
            if spec.get("requires_river") and not tile["river"]:
                raise ActionRejected("REQUIRES_RIVER", f"{building} 必须建在河道地块")
            limit = spec.get("max_per_country")
            if limit is not None:
                existing = sum(value == building for value in country.buildings.values())
                existing += sum(project["building"] == building for project in country.projects)
                if existing >= limit:
                    raise ActionRejected(
                        "BUILDING_UNIQUE_LIMIT", f"{building} 全国最多建设 {limit} 座"
                    )
            if spec.get("on_city_center") and not any(
                city.center == coord for city in country.cities
            ):
                raise ActionRejected(
                    "NOT_CITY_CENTER", f"{building} 只能建在城池中心格上：先选中一座城"
                )
            cost = float(spec["cost"])
            if country.treasury + 1e-9 < cost:
                raise ActionRejected("INSUFFICIENT_TREASURY", f"建设需要 {cost} 财政单位")
            country.treasury -= cost
            project_id = f"P{self.state.next_project_number:06d}"
            self.state.next_project_number += 1
            project = {
                "project_id": project_id,
                "tile": coord,
                "building": building,
                "remaining_days": float(spec["days"]),
                "total_days": float(spec["days"]),
                "started_day": self.state.day_index,
                "cost": cost,
            }
            country.projects.append(project)
            self.state.record(
                "construction_started",
                country_id=actor,
                project_id=project_id,
                tile=action["tile"],
                building=building,
                cost=cost,
            )
            return {"project_id": project_id, "cost": cost, "duration_days": spec["days"]}
        if action_type == "ALLOCATE_WORKERS":
            previous = deepcopy(country.workers)
            country.workers = {
                key: action[key] for key in ("agriculture", "mining", "construction")
            }
            return {"previous": previous, "workers": deepcopy(country.workers)}
        if action_type == "SET_CITY_GOVERNANCE":
            city = next(
                (item for item in country.cities if item.city_id == action["city_id"]),
                None,
            )
            if city is None:
                raise ActionRejected("UNKNOWN_CITY", "城市不存在或不属于本国")
            previous = city.managed_by
            city.managed_by = action["managed_by"]
            self.state.record(
                "city_governance_changed", country_id=actor, city_id=city.city_id,
                previous=previous, managed_by=city.managed_by,
            )
            return {"city_id": city.city_id, "previous": previous, "managed_by": city.managed_by}
        if action_type == "SET_CITY_ZONE":
            city = next(
                (item for item in country.cities if item.city_id == action["city_id"]),
                None,
            )
            if city is None:
                raise ActionRejected("UNKNOWN_CITY", "城市不存在或不属于本国")
            coord = (action["tile"]["col"], action["tile"]["row"])
            if coord not in city.tiles:
                raise ActionRejected("TILE_NOT_IN_CITY", "地块不属于该城市")
            if action["zone"] not in self.state.config["city_zones"]:
                raise ActionRejected("INVALID_PARAMETER", "未知城市分区")
            previous = city.zones.get(coord, "unzoned")
            city.zones[coord] = action["zone"]
            self.state.record(
                "city_zone_changed", country_id=actor, city_id=city.city_id,
                tile=action["tile"], previous=previous, zone=action["zone"],
            )
            return {
                "city_id": city.city_id, "tile": action["tile"],
                "previous": previous, "zone": action["zone"],
            }
        if action_type == "SET_CITY_FOCUS":
            city = next(
                (item for item in country.cities if item.city_id == action["city_id"]),
                None,
            )
            if city is None:
                raise ActionRejected("UNKNOWN_CITY", "城市不存在或不属于本国")
            if action["focus"] not in self.state.config["city_focuses"]:
                raise ActionRejected("INVALID_PARAMETER", "未知城市发展方针")
            previous = city.focus
            city.focus = action["focus"]
            self.state.record(
                "city_focus_changed", country_id=actor, city_id=city.city_id,
                previous=previous, focus=city.focus,
            )
            return {"city_id": city.city_id, "previous": previous, "focus": city.focus}
        if action_type == "SET_CITY_BUDGET":
            city = next(
                (item for item in country.cities if item.city_id == action["city_id"]),
                None,
            )
            if city is None:
                raise ActionRejected("UNKNOWN_CITY", "城市不存在或不属于本国")
            if action["budget"] not in self.state.config["city_budgets"]:
                raise ActionRejected("INVALID_PARAMETER", "未知城市预算档位")
            previous = city.budget
            city.budget = action["budget"]
            self.state.record(
                "city_budget_changed", country_id=actor, city_id=city.city_id,
                previous=previous, budget=city.budget,
            )
            return {"city_id": city.city_id, "previous": previous, "budget": city.budget}
        if action_type == "SET_TAX":
            previous = country.tax_rate
            country.tax_rate = action["rate"]
            return {"previous_rate": previous, "rate": country.tax_rate}
        if action_type == "SET_RESEARCH":
            field = action["field"]
            if country.technology[field] >= self.state.config["research_max_level"]:
                raise ActionRejected("RESEARCH_MAXED", "该研究领域已达到当前上限")
            previous = country.research_field
            country.research_field = field
            return {"previous_field": previous, "field": field}
        if action_type == "DIPLOMACY":
            operation = action["operation"]
            if operation == "GIFT":
                return self.diplomacy.gift(actor, action)
            if operation == "PROPOSE_PACT":
                return self.diplomacy.propose_pact(actor, action)
            if operation == "DECLARE_WAR":
                return self.diplomacy.declare_war(actor, action["target"])
            if operation == "PROPOSE_PEACE":
                return self.diplomacy.propose_peace(actor, action["target"])
            return self.diplomacy.respond_pact(actor, action)
        if action_type == "MILITARY":
            operation = action["operation"]
            if operation == "RECRUIT":
                return self.military.recruit(
                    actor, action["unit"], action["strength"], action.get("army_id")
                )
            if operation == "DISPATCH":
                rows = []
                for order in action["orders"]:
                    try:
                        effects = self._execute(actor, order)
                        rows.append({"army_id": order["army_id"], "accepted": True, "effects": effects})
                    except ActionRejected as exc:
                        rows.append({"army_id": order["army_id"], "accepted": False,
                                     "code": exc.code, "reason": exc.text})
                if not any(row["accepted"] for row in rows):
                    raise ActionRejected(rows[0]["code"], "；".join(
                        f"{row['army_id']}：{row['reason']}" for row in rows))
                return {"orders": rows}
            if operation == "RENDEZVOUS":
                return self.military.rendezvous(
                    actor, action["army_id"], action["host_id"], action["units"]
                )
            if operation == "MARCH":
                return self.military.march(
                    actor, (action["tile"]["col"], action["tile"]["row"]), action.get("army_id")
                )
            if operation == "SET_RECRUITING":
                return self.military.set_recruiting(actor, action.get("army_id"), action["recruiting"])
            if operation == "SET_RECRUIT_UNIT":
                return self.military.set_recruit_unit(actor, action.get("army_id"), action["unit"])
            objective = action.get("tile")
            return self.military.attack(
                actor, action["target"], action.get("army_id"),
                None if objective is None else (objective["col"], objective["row"]),
            )
        if action_type == "SET_POLICY":
            previous = country.policy
            country.policy = action["priority"]
            return {"previous_policy": previous, "policy": country.policy}
        if action_type == "TRADE":
            if action["operation"] == "PROPOSE":
                return self.diplomacy.propose(actor, action)
            return self.diplomacy.respond(actor, action)
        raise ActionRejected("UNKNOWN_ACTION", "动作未实现")

    def step_day(self, force: bool = False) -> bool:
        if self.clock.paused and not force:
            return False
        self.diplomacy.expire_due()
        self.diplomacy.expire_proposals()
        interval = self.state.config["planning_interval_days"]
        if self.state.day_index % interval == 0:
            if self.planner is not None:
                self.planner.plan(self)
            if self.enable_city_manager:
                self.city_manager.plan(self)
        elif self.planner is not None:
            # A council held every 90 days cannot answer a 17-day blockade, so the realms the
            # alert is about sit down today; everyone else keeps the ordinary rhythm.
            alarmed = self.military.alarm_watch()
            if alarmed:
                self.planner.plan(self, only=alarmed)
        if self.enable_frontier_ai:
            self._expand_frontiers()
        current_weather = self.state.weather.sample(self.state.day_index)
        self.state.weather.observe_day(
            current_weather["rain"], current_weather["temperature"]
        )
        self.economy.settle_all(current_weather)
        self.military.tribute_daily()
        self.military.march_daily()
        self.military.siege_daily()
        self.military.upkeep_daily()
        self.construction.advance_all()
        self.military.barracks_daily()
        for country in self.state.countries.values():
            self.economy.advance_research(country)
        if self.state.weather.is_month_end(self.state.day_index):
            monthly = self.state.weather.complete_month(self.state.day_index)
            self.state.record("weather_month", **monthly)
            self._apply_hazards(monthly)
            self.economy.update_city_services_monthly()
            self.economy.update_city_livability_monthly()
            self.economy.grow_population_monthly()
            self.economy.update_stability_monthly()
            self.military.update_monthly()
            self.economy.apply_social_events()
        self.state.day_index += 1
        return True

    def _expand_frontiers(self) -> None:
        """AI frontier office: expand only when the country has waited long enough."""
        from core.frontier import choose_frontier, frontier_ready

        expansion = self.state.config["expansion"]
        interval = int(expansion.get("ai_interval_days", 120))
        reserve = float(expansion.get("ai_treasury_reserve", 20.0))
        player_countries = getattr(self.planner, "controlled_by_player", set()) or set()
        for country in self.state.countries.values():
            if country.country_id in player_countries:
                continue
            if not frontier_ready(self.state, country, interval, reserve):
                continue
            target = choose_frontier(self.state, country)
            if target is None:
                continue
            result = self.apply_action(
                country.country_id,
                {"type": "SETTLE", "tile": {"col": target[0], "row": target[1]}},
            )
            if result.accepted:
                self.state.record(
                    "ai_frontier_expansion",
                    country_id=country.country_id,
                    tile={"col": target[0], "row": target[1]},
                    outposts=len(country.outposts),
                )

    def _apply_hazards(self, monthly: dict[str, Any]) -> None:
        weather_config = self.state.config["weather"]
        for country_id, summary in monthly["countries"].items():
            country = self.state.countries.get(country_id)
            if country is None:
                # A realm taken at its capital still gets weather over its soil, but there is
                # no treasury left to burn and no granary left to empty.
                continue
            capital = self.state.world.polities[country_id]["capital"]
            tile = self.state.world.tile((capital["col"], capital["row"]))
            rain_ratio = summary["rain_anomaly_ratio"]
            specifications = (
                ("flood", weather_config["flood_monthly_rate"], max(0.0, rain_ratio - 0.85)),
                ("drought", weather_config["drought_monthly_rate"], max(0.0, 1.05 - rain_ratio)),
            )
            for hazard_type, rate, intensity in specifications:
                exposure = float(tile["hazards"][hazard_type])
                chance = min(0.95, rate * exposure * intensity)
                if chance <= 0.0 or self.state.weather.random() >= chance:
                    continue
                severity = self.state.weather.uniform(0.15, 0.85)
                loss = country.food * severity * weather_config["maximum_food_loss_fraction"]
                country.food = max(0.0, country.food - loss)
                self.state.record(
                    "hazard_impact",
                    country_id=country_id,
                    hazard_type=hazard_type,
                    severity=round(severity, 6),
                    exposure_index=exposure,
                    rain_anomaly_ratio=rain_ratio,
                    sampling_probability=round(chance, 8),
                    food_loss=round(loss, 6),
                    food_remaining=round(country.food, 6),
                    note="概率为游戏参数；暴露指数不是年发生概率",
                )
