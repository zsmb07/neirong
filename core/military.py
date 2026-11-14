"""Strategic armies as unit rosters: recruitment, marching, battles, upkeep, exhaustion."""
from __future__ import annotations

from core.actions import ActionRejected, ActionResult
from core.content import army_upkeep, field_cap
from core.movement import Movement, march_days
from core.society import stability_factor
from core.state import (
    ArmyState,
    GameState,
    MarchOrder,
    RECRUIT_STALL_QUIET,
    army_payload,
    barracks_target,
    calendar_date,
    camp_garrison,
    recruit_block,
    siege_besiegers,
    siege_ledger,
    stall_label,
    tile_army_capacity,
    tribute_base,
    tribute_transfers,
    unit_unlocked,
)
from worldgen import hex_distance


class Military:
    def __init__(self, state: GameState) -> None:
        self.state = state
        self.config = state.config["military"]
        self.movement_config = state.config["movement"]
        self.barracks = self.config["barracks"]
        self.siege_rules = self.config["siege"]
        self.annex_rules = self.config["annexation"]
        self.rout_rules = self.config["rout"]
        self.units = state.config["units"]
        self.order = list(self.units)

    def _owner_of(self, coord: tuple[int, int]) -> str | None:
        return next(
            (country.country_id for country in self.state.countries.values()
             if coord in country.tiles),
            None,
        )

    def _owners(self) -> dict[tuple[int, int], str]:
        """Who holds what, built once per order or per day instead of per tile probe."""
        return {
            coord: country.country_id
            for country in self.state.countries.values()
            for coord in country.tiles
        }

    def _roads(self) -> set[tuple[int, int]]:
        """Every road on the continent, whoever paid for it: paved ground is shared."""
        roads = {
            (int(cell["tile"]["col"]), int(cell["tile"]["row"]))
            for cell in self.state.world.data.get("road_cells") or []
            if isinstance(cell.get("tile"), dict)
        }
        for country in self.state.countries.values():
            roads.update(
                coord for coord, building in country.buildings.items() if building == "ROAD"
            )
        return roads

    def movement(self, actor: str | None = None) -> Movement:
        """March costs over the live world: terrain, roads, the daily budget and borders.

        With an `actor` the price is that realm's: every hex another crown holds is marched
        on at `movement.hostile_cost` slower. Without one the table is pure geography, which
        is what a routed column escaping over the nearest open hex and the map editor's
        draft preview both want — neither of them is marching through a country.
        """
        world = self.state.world
        tiles = world.tiles
        roads = self._roads()
        owners = self._owners() if actor is not None else {}

        def state_of(coord: tuple[int, int]) -> dict | None:
            tile = tiles.get(coord)
            if tile is None:
                return None
            return {
                "land": tile["land"], "biome": tile["biome"], "road": coord in roads,
                "hostile": owners.get(coord) not in (None, actor),
            }

        return Movement(self.state.config, state_of, lambda coord: world.neighbors(*coord))

    def _marchable(self, actor: str, tile: tuple[int, int],
                   owners: dict[tuple[int, int], str]) -> bool:
        """Where a column may be left standing at the end of a march order.

        Crossing is not parking. An army walks over any ground the continent offers and pays
        the border surcharge for the privilege, but a march order can only *end* on its own
        soil or on nobody's: a realm does not get to install a garrison inside another one's
        borders by asking it to stand there politely. The hex next to a foreign camp is the
        one place enemy ground is worth reaching, and that is an attack order, not a march.
        """
        return owners.get(tile) in (None, actor)

    @staticmethod
    def _tile(coord: tuple[int, int] | None) -> dict[str, int] | None:
        return None if coord is None else {"col": coord[0], "row": coord[1]}

    def unlocked(self, country, unit_id: str) -> bool:
        return unit_unlocked(country, unit_id, self.state.config)

    def _army_of(self, country, army_id: str | None):
        """Resolve an optional army handle; omitting it means the strongest field army."""
        if army_id is None:
            return country.main_army
        found = country.find_army(army_id)
        if found is not None:
            return found
        for other in self.state.countries.values():
            if other.find_army(army_id) is not None:
                raise ActionRejected("NOT_OWNED", f"{army_id} 属于 {other.country_id}，不能下令")
        raise ActionRejected("UNKNOWN_ARMY", f"未知军队: {army_id!r}")

    def recruit(self, actor: str, unit_id: str, amount: int,
                army_id: str | None = None) -> dict:
        if unit_id not in self.units:
            raise ActionRejected("UNKNOWN_UNIT", f"未知兵种: {unit_id!r}")
        batch_max = self.config["recruit_batch_max"]
        if not 1 <= amount <= batch_max:
            raise ActionRejected("INVALID_STRENGTH", f"单次招募规模为 1-{batch_max}")
        country = self.state.countries[actor]
        army = self._army_of(country, army_id)
        if army and army.march and army.march.rendezvous:
            raise ActionRejected("MARCH_IN_PROGRESS", "不能向会师途中的分队补兵")
        spec = self.units[unit_id]
        if not self.unlocked(country, unit_id):
            requirement = spec["required_technology"]
            branch = self.state.config["tech_tree"][requirement["field"]]["label"]
            raise ActionRejected(
                "TECHNOLOGY_REQUIRED",
                f"{spec['label']} 需要{branch}达到 {requirement['level']} 级",
            )
        if country.army_strength + amount > self._field_cap(country):
            raise ActionRejected("ARMY_LIMIT", "军队规模达到上限")
        if country.army_units.get(unit_id, 0) + amount > spec["max_count"]:
            raise ActionRejected("UNIT_LIMIT", f"{spec['label']} 最多编成 {spec['max_count']} 支")
        capacity = int(self.barracks["garrison_capacity"])
        if army is not None and country.buildings.get(army.tile) == "BARRACKS":
            if army.strength + amount > capacity:
                raise ActionRejected(
                    "GARRISON_LIMIT",
                    f"这一营只编 {capacity} 支定额，已站 {army.strength} 支，"
                    f"要募更多就先分兵或再修一座兵营",
                )
        treasury_cost = amount * float(spec["cost"]["treasury"])
        food_cost = amount * float(spec["cost"]["food"])
        if country.treasury + 1e-9 < treasury_cost:
            raise ActionRejected("INSUFFICIENT_TREASURY", "财政不足以招募")
        if country.food + 1e-9 < food_cost:
            raise ActionRejected("INSUFFICIENT_FOOD", "粮食不足以供养新兵")
        country.treasury -= treasury_cost
        country.food -= food_cost
        army = army or country.new_army()
        army.units[unit_id] = army.units.get(unit_id, 0) + amount
        if army.tile is None:
            capital = self.state.world.polities[actor]["capital"]
            army.tile = (capital["col"], capital["row"])
        self.state.record(
            "army_recruited", country_id=actor, army_id=army.army_id, unit=unit_id,
            amount=amount, strength=country.army_strength,
            treasury_cost=round(treasury_cost, 3), food_cost=round(food_cost, 3),
        )
        return {
            "strength": country.army_strength,
            "army_id": army.army_id,
            "unit": unit_id,
            "cost": treasury_cost,
            "food_cost": food_cost,
        }

    def _field_cap(self, country) -> int:
        """Units this realm may keep under arms: one fixed garrison per finished barracks."""
        return field_cap(country.buildings.values(), self.state.config)

    def _mass(self, country, tile, army=None) -> tuple[list, float]:
        """The armies that actually fight a battle at `tile`, and what that is worth.

        A realm's whole roster sets the ceiling of its power, but only the share of it
        standing in the ring around the contested tile charges at full weight: converging
        the garrisons of several barracks onto one gate is what turns a raid into a strike,
        and one army attacking alone pays `minimum_mass_share`. Armies still marching,
        wrecked or empty contribute nothing to either side.
        """
        radius = int(self.config["mass_radius"])
        floor = float(self.config["minimum_mass_share"])
        present = [
            other for other in sorted(country.armies, key=lambda item: item.army_id)
            if other.march is None and other.strength > 0 and other.tile is not None
            and (other is army or hex_distance(other.tile, tile) <= radius)
        ]
        share = sum(other.strength for other in present) / max(1, country.army_strength)
        return present, floor + (1.0 - floor) * min(1.0, share)

    def _loss_plan(self, attackers, defenders) -> tuple[int, int, bool]:
        """How many soldiers each side pays for one fight, from the ring it stands in.

        An undefended gate is the whole point of taking the edge cities first: nobody is
        there to shoot back, so the column loses `unopposed_loss_rate` of the men it
        brought and nothing is faked onto the defender's far-away camps. A real battle
        still costs both sides at least one man, so a 1-unit raid cannot be free.
        """
        if not defenders:
            return (int(sum(other.strength for other in attackers)
                        * self.config["unopposed_loss_rate"]), 0, True)
        return (max(1, int(sum(other.strength for other in attackers)
                           * self.config["attacker_loss_rate"])),
                max(1, int(sum(other.strength for other in defenders)
                           * self.config["defender_loss_rate"])),
                False)

    def _defended_city(self, country, tile: tuple[int, int]):
        """The city whose ground this tile is, if any: the only stage a rout can happen on."""
        return next((city for city in country.cities if tile in city.tiles), None)

    @staticmethod
    def _rout_tally(armies, rate: float) -> dict:
        """Dead and walking for one side, army by army, exactly as `_apply_rout` spends them."""
        stood = sum(other.strength for other in armies)
        kept = sum(
            min(other.strength, Military._survivors(other.strength, rate)) for other in armies
        )
        return {"deaths": stood - kept, "survivors": kept}

    def _rout_frame(self, defender, tile, attackers, guards) -> dict | None:
        """The rout book for a fight at `tile`, as pure arithmetic — nothing searched.

        None means there is no rout to price: the hex lies outside every city domain the
        defender holds, or nobody stands there to be broken, and the fight keeps the plain
        field attrition. The attacker's own `fate`/`retreat` stay empty here because naming
        the hex it falls back to costs a road search; the quote that a player or an AI pays
        for an order fills them from `_retreat_tile`, and the alert that only has to warn
        leaves them out. Same survivors either way.
        """
        city = self._defended_city(defender, tile)
        if city is None or not guards:
            return None
        rate = float(self.rout_rules["survivor_rate"])
        looted = float(self.rout_rules["barracks_lost_survivor_rate"])
        barracks_lost = defender.buildings.get(tile) == "BARRACKS"
        return {
            "city_id": city.city_id, "city": city.name, "center": self._tile(city.center),
            "survivor_rate": rate, "barracks_lost": barracks_lost,
            "attacker": {**self._rout_tally(attackers, rate), "fate": None, "retreat": None},
            "garrison": {
                **self._rout_tally(guards, looted if barracks_lost else rate),
                "fate": "annihilated" if barracks_lost else "routed",
            },
        }

    @staticmethod
    def _survivors(strength: int, rate: float) -> int:
        """How many of a broken army's men walk away.

        `rate` of them, rounded, and never the last man: the promise behind 「不死完」 is that a
        lost city fight breaks a column instead of erasing it. A rate of 0.0 is the one case the
        content table is allowed to spend that floor on — a garrison that also lost the barracks
        it was raised in dies with it.
        """
        if rate <= 0.0:
            return 0
        return max(1, int(strength * rate + 0.5))

    def _retreat_tile(self, loser, enemy_id: str, start: tuple[int, int], *,
                      homeward: bool) -> tuple[int, int] | None:
        """Where a broken column falls back to: the nearest hex that is not the winner's ground.

        Rout moves are not march orders — no budget, no queue, no second battle on the way out —
        because the withdrawal *is* the defeat. A raiding column settles the ring around the hex
        it lost and prefers the way back onto its own soil (「优先离开敌国领地」); a routed
        garrison only accepts its own remaining territory, and if the ring has already turned
        enemy, it keeps looking outward until it finds it (「退回国内领地」). Nothing qualifies
        at all and there is no exit: the column is cut off and dies where it stands.
        """
        radius = int(self.rout_rules["retreat_radius"])
        owners = self._owners()
        costs, _ = self.movement().search(start)

        def ranked(limit: int | None) -> list:
            rows = []
            for coord, cost in costs.items():
                if coord == start:
                    continue
                distance = hex_distance(coord, start)
                if limit is not None and distance > limit:
                    continue
                owner = owners.get(coord)
                if owner == enemy_id:
                    continue
                if homeward and owner != loser.country_id:
                    continue
                rows.append((distance, 0 if owner == loser.country_id else 1,
                             cost, coord[1], coord[0], coord))
            return sorted(rows)

        nearest = ranked(radius)
        if nearest or not homeward:
            return nearest[0][-1] if nearest else None
        outward = ranked(None)
        return outward[0][-1] if outward else None

    def _disband(self, country, army, reason: str) -> None:
        """Strike a broken army off the roster and off every blockade it was counted in.

        The serial never rewinds, so an id retired here can never be mistaken for a new camp
        tomorrow; `sync_barracks` will raise a fresh army on any barracks that still stands.
        """
        country.armies.remove(army)
        self._purge_siege_ledger(country.country_id, {army.army_id}, reason)

    def _purge_siege_ledger(self, attacker_id: str, army_ids: set[str], reason: str) -> None:
        """A blockade is a list of armies; when one of them dies the ledger loses it today."""
        for holder in self.state.countries.values():
            for city in holder.cities:
                siege = city.siege
                if not siege or siege["attacker"] != attacker_id:
                    continue
                left = [army_id for army_id in siege["armies"] if army_id not in army_ids]
                if len(left) == len(siege["armies"]):
                    continue
                siege["armies"] = left
                if not left:
                    self._lift_siege(holder, city, reason)

    def _apply_rout(self, loser, enemy_id: str, armies, *, rate: float,
                    homeward: bool) -> tuple[dict, dict]:
        """Break a lost column: keep the survivors, bury the dead, march what runs to safety.

        Deaths fall on the armies that actually stood there, type by type, and the national
        roster is whatever is left of them — an unengaged camp no longer pays for a defeat it
        was not part of. Returns the merged per-type death roster, in the same shape
        `_remove_losses` hands a battle, plus the block the event and the panel read.
        """
        removed: dict[str, int] = {}
        columns = []
        for army in armies:
            before = army.strength
            keep = min(before, self._survivors(before, rate))
            target = (self._retreat_tile(loser, enemy_id, army.tile, homeward=homeward)
                      if keep > 0 else None)
            trapped = keep > 0 and target is None
            if trapped:
                keep = 0
            fallen = self._remove_losses(dict(army.units), before - keep)
            self._apply_army_losses([army], fallen)
            for unit_id, count in fallen.items():
                removed[unit_id] = removed.get(unit_id, 0) + count
            if army.strength <= 0:
                self._disband(loser, army, "ring_empty")
                columns.append({
                    "army_id": army.army_id, "deaths": before, "survivors": 0,
                    "fate": "annihilated", "reason": "no_exit" if trapped else "barracks_lost",
                    "retreat": None,
                })
                continue
            army.tile = target
            columns.append({
                "army_id": army.army_id, "deaths": before - keep, "survivors": army.strength,
                "fate": "routed", "reason": None, "retreat": self._tile(target),
            })
        block = {
            "survivor_rate": rate,
            "deaths": sum(row["deaths"] for row in columns),
            "survivors": sum(row["survivors"] for row in columns),
            "columns": columns,
        }
        return removed, block

    def _barracks_unit(self, country, army) -> str:
        """What this barracks is raising: the army's own choice, else the fallback type."""
        return barracks_target(country, army, self.state.config)

    def _raise_block(self, country, army, unit_id: str) -> str | None:
        """Why a barracks cannot hand over a unit today; None means it can."""
        return recruit_block(country, army, unit_id, self.state.config) or None

    def sync_barracks(self) -> None:
        """Every finished barracks garrisons its own army, including on older saves."""
        for country in self.state.countries.values():
            garrisoned = {army.tile for army in country.armies}
            for coord, building in country.buildings.items():
                if building == "BARRACKS" and coord not in garrisoned:
                    country.new_army(tile=coord, recruiting=True)

    def barracks_daily(self) -> None:
        """Barracks turn a daily treasury trickle into trained units, camp by camp.

        The daily wage is spent even when a cap blocks delivery, and the progress that
        wage bought is capped instead of refunded, so a stalled barracks keeps waiting.
        Whatever it is waiting on gets written to `army.stall`, the same code the panel
        reads, and an involuntary stop is announced once rather than every single day.
        Deliberately RNG-free so an action log replays identically.
        """
        self.sync_barracks()
        wage = float(self.barracks["daily_cost"])
        cap_ratio = float(self.barracks["progress_cap_ratio"])
        for country in self.state.countries.values():
            for coord, building in country.buildings.items():
                if building != "BARRACKS":
                    continue
                army = camp_garrison(country, coord, self.state.config)
                if army is None:
                    self._stall(country, None, "NO_GARRISON", coord, wage)
                    continue
                if not army.strength and coord in country.barracks_dispatches:
                    self._stall(country, army, "NO_GARRISON", coord, wage)
                    continue
                if not army.recruiting:
                    self._stall(country, army, "PAUSED", coord, wage)
                    continue
                if country.treasury + 1e-9 < wage:
                    self._stall(country, army, "INSUFFICIENT_WAGE", coord, wage)
                    continue
                country.treasury -= wage
                army.progress += wage
                unit_id = self._barracks_unit(country, army)
                price = float(self.units[unit_id]["cost"]["treasury"])
                block = self._raise_block(country, army, unit_id)
                if block:
                    army.progress = min(army.progress, price * cap_ratio)
                    self._stall(country, army, block, coord, wage, unit_id)
                    continue
                if army.progress + 1e-9 < price:
                    self._stall(country, army, "", coord, wage, unit_id)
                    continue
                food_cost = float(self.units[unit_id]["cost"]["food"])
                country.food -= food_cost
                army.progress = max(0.0, army.progress - price)
                army.units[unit_id] = army.units.get(unit_id, 0) + 1
                army.raised += 1
                self._stall(country, army, "", coord, wage, unit_id)
                self.state.record(
                    "barracks_recruit", country_id=country.country_id, army_id=army.army_id,
                    unit=unit_id, amount=1, strength=country.army_strength,
                    tile={"col": coord[0], "row": coord[1]},
                    food_cost=round(food_cost, 3),
                )

    def _stall(self, country, army, code: str, tile, wage: float,
               unit_id: str | None = None) -> None:
        """Record why this camp delivers nothing today.

        The reason lands on `army.stall` every single day -- the panel and the quotes read
        it live -- but the announcement is rate limited: one camp may complain about the
        same reason once per `stall_notice_days`, because a treasury hovering around the
        daily wage flips the code on and off almost every day.
        """
        unit_id = unit_id or (self._barracks_unit(country, army) if army else "")
        if army is not None:
            if army.stall == code:
                return
            army.stall = code
        if code in RECRUIT_STALL_QUIET:
            return
        day = self.state.day_index
        cooldown = int(self.barracks["stall_notice_days"])
        last = army.stall_notices.get(code) if army is not None else None
        if last is not None and day - last < cooldown:
            return
        if army is not None:
            army.stall_notices[code] = day
        spec = self.units.get(unit_id) or {}
        self.state.record(
            "barracks_stalled", country_id=country.country_id,
            army_id=army.army_id if army else None, tile={"col": tile[0], "row": tile[1]},
            unit=unit_id, label=spec.get("label", unit_id), code=code,
            reason=stall_label(code), wage=round(wage, 3),
            progress=round(army.progress, 3) if army else 0.0,
        )

    def _require_marchable(self, army):
        if army is None or army.strength <= 0 or army.tile is None:
            raise ActionRejected("NO_ARMY", "没有可移动的军队")
        if army.march is not None:
            raise ActionRejected(
                "MARCH_IN_PROGRESS",
                f"{army.army_id} 正在行军，抵达目的地之前不能再下令",
            )

    def _order(self, country, army, plan: dict, purpose: str, target: str | None = None,
               objective: tuple[int, int] | None = None, expedition: bool = False) -> dict:
        """Hand the army a priced route; the daily settlement walks it, nothing happens now."""
        army.march = MarchOrder(
            purpose=purpose, route=plan["route"], cumulative=plan["cumulative"],
            target=target, objective=objective, expedition=expedition,
            issued_sequence=len(self.state.event_log) + 1,
        )
        route = plan["route"]
        self.state.record(
            "march_ordered", country_id=country.country_id, army_id=army.army_id,
            purpose=purpose, target=target, objective=self._tile(objective),
            expedition=expedition, from_tile=self._tile(route[0]), tile=self._tile(route[-1]),
            tiles=len(route) - 1, cost=plan["cost"], days=plan["days"],
        )
        return {
            "army_id": army.army_id, "destination": self._tile(route[-1]),
            "tiles": len(route) - 1, "cost": plan["cost"], "days": plan["days"],
            "expedition": expedition,
        }

    def _road(self, actor: str, start: tuple[int, int], target: tuple[int, int], *,
              movement: Movement | None = None, explain: bool = False) -> dict:
        """The road a column would take to this hex, and the reason it cannot.

        Borders slow an army; they no longer stop one. Every land hex on the continent is
        therefore enterable, and a refused march is one of exactly three answers: water, a
        realm walled off by sea, or a road longer than one order can pay. `explain` spends
        the second, uncapped search that tells the last two apart — the daily quotes skip it,
        the player's click and a refused order never do, because 「距离太远」 and 「没有路」 are
        different sentences to act on.
        """
        movement = self.movement(actor) if movement is None else movement
        if movement.cost(target) is None:
            return {"plan": None, "reason": "SEA", "cost": None, "days": None}
        costs, previous = movement.search(start)
        if target in costs:
            return {"plan": self._road_plan(movement, previous, start, target),
                    "reason": None, "cost": None, "days": None}
        if not explain:
            return {"plan": None, "reason": "NO_ROAD", "cost": None, "days": None}
        reached, previous = movement.search(start, cap=float("inf"))
        if target not in reached:
            return {"plan": None, "reason": "ENCLAVED", "cost": None, "days": None}
        needed = float(reached[target])
        return {"plan": None, "reason": "ORDER_LIMIT", "cost": round(needed, 3),
                "days": movement.days(needed)}

    @staticmethod
    def _road_plan(movement: Movement, previous, start, target) -> dict:
        route = movement.route(previous, start, target)
        totals = movement.cumulative(route)
        return {
            "route": route,
            "cumulative": totals,
            "cost": round(totals[-1], 3),
            "days": movement.days(totals[-1]),
        }

    def _expedition(self, actor: str, plan: dict, owners: dict) -> bool:
        """Whether a route walks over ground another crown holds."""
        return any(owners.get(coord) not in (None, actor) for coord in plan["route"][1:])

    def march(self, actor: str, target: tuple[int, int], army_id: str | None = None) -> dict:
        country = self.state.countries[actor]
        army = self._army_of(country, army_id)
        self._require_marchable(army)
        if army.tile == target:
            raise ActionRejected("ALREADY_THERE", "军队已经驻在这格")
        owners = self._owners()
        if not self._marchable(actor, target, owners):
            raise ActionRejected(
                "FOREIGN_GROUND",
                f"({target[0]},{target[1]}) 是 {owners[target]} 的领土：军队能开过去，"
                "但不能驻扎在别国境内——要打它，用进攻令",
            )
        road = self._road(actor, army.tile, target, explain=True)
        plan = road["plan"]
        if plan is None:
            raise ActionRejected(
                *self._refusal(target, road,
                               float(self.movement_config["maximum_order_cost"])))
        return self._order(country, army, plan, "MARCH",
                           expedition=self._expedition(actor, plan, owners))

    def rendezvous_capacity(self, country, tile, host=None) -> dict:
        city = self._city_of(country, tile)
        capital = city is not None and self.state.is_capital(country, city)
        capacity = tile_army_capacity(country, self.state.config, capital=capital)
        stationed = self._camps_of(country, tile)
        if host is not None and host.march is not None:
            # Preview the host's planned arrival, while keeping the slot shared
            # with any other army already waiting there.
            stationed.append(host)
        used = sum(army.strength for army in stationed)
        camp = country.buildings.get(tile) == "BARRACKS"
        unit_room = {}
        if camp:
            for unit_id, spec in self.units.items():
                limit = min(int(self.barracks["garrison_capacity"]), int(spec["max_count"]))
                present = sum(army.units.get(unit_id, 0) for army in stationed)
                unit_room[unit_id] = max(0, limit - present)
        return {"capacity": capacity, "room": max(0, capacity - used),
                "capital": capital, "unit_room": unit_room}

    def rendezvous_hosts(self, actor: str) -> list[dict]:
        country = self.state.countries[actor]
        return [
            {"army_id": army.army_id, "tile": self._tile(army.tile),
             **self.rendezvous_capacity(country, army.tile)}
            for army in country.armies
            if army.march is None and army.tile is not None and army.strength
        ]

    def _rendezvous_errors(self, units: dict, source: dict, capacity: dict) -> list[dict]:
        errors = []
        for unit_id, count in units.items():
            if not 0 <= count <= source.get(unit_id, 0):
                errors.append({"code": "SOURCE_UNIT_LIMIT", "unit": unit_id,
                               "text": f"{self.units[unit_id]['label']}：源军兵种名额到底"})
        if sum(units.values()) > capacity["room"]:
            errors.append({"code": "HOST_CAPACITY", "text": f"装不下，还能装 {capacity['room']} 支"})
        for unit_id, room in capacity["unit_room"].items():
            if units.get(unit_id, 0) > room:
                errors.append({"code": "CAMP_UNIT_LIMIT", "unit": unit_id,
                               "text": f"{self.units[unit_id]['label']}：宿主兵营同兵种名额已满（还能收 {room} 支）"})
        if not errors and not sum(units.values()):
            errors.append({"code": "EMPTY_DISPATCH", "text": "至少选择 1 支兵"})
        return errors

    def rendezvous_quote(self, actor: str, source_id: str, host_id: str, units: dict) -> dict:
        country = self.state.countries[actor]
        source, host = country.find_army(source_id), country.find_army(host_id)
        if source is None or host is None or source is host or not host.strength:
            raise ActionRejected("INVALID_RENDEZVOUS", "请选择两支不同的本国军队")
        self._require_marchable(source)
        if host.march and host.march.rendezvous:
            raise ActionRejected("INVALID_RENDEZVOUS", "会师途中的分队不能担任宿主")
        tile = host.march.destination if host.march else host.tile
        if tile is None or not self._marchable(actor, tile, self._owners()):
            raise ActionRejected("FOREIGN_GROUND", "会师目标格不可驻扎")
        capacity = self.rendezvous_capacity(country, tile, host)
        unit_limit = sum(min(count, capacity["unit_room"].get(unit, count))
                         for unit, count in source.units.items())
        errors = self._rendezvous_errors(units, source.units, capacity)
        if not errors and source.tile != tile:
            road = self._road(actor, source.tile, tile, explain=True)
            if road["plan"] is None:
                code, message = self._refusal(tile, road, self.movement_config["maximum_order_cost"])
                errors.append({"code": code, "text": message})
        return {
            **capacity, "destination": self._tile(tile),
            "source_strength": source.strength, "source_units": dict(source.units),
            "source_labels": {u: self.units[u]["label"] for u in source.units},
            "maximum": min(unit_limit, capacity["room"]),
            "errors": errors,
        }

    def rendezvous(self, actor: str, source_id: str, host_id: str, units: dict) -> dict:
        country = self.state.countries[actor]
        quote = self.rendezvous_quote(actor, source_id, host_id, units)
        if quote["errors"]:
            error = quote["errors"][0]
            raise ActionRejected(error["code"], error["text"])
        source = country.find_army(source_id)
        tile = (quote["destination"]["col"], quote["destination"]["row"])
        if source.tile == tile:
            plan = {"route": [tile], "cumulative": [0.0], "cost": 0.0, "days": 0}
        else:
            road = self._road(actor, source.tile, tile, explain=True)
            plan = road["plan"]
            if plan is None:
                raise ActionRejected(*self._refusal(tile, road, self.movement_config["maximum_order_cost"]))
        dispatched = {unit: count for unit, count in units.items() if count}
        # An in-transit detachment uses the existing combat and march machinery.
        # It receives a permanent army number only if it has to camp independently.
        sequence = len(self.state.event_log) + 1
        detachment = ArmyState(army_id=f"{source_id}~{sequence}", tile=source.tile, units=dispatched)
        for unit, count in dispatched.items():
            remaining = source.units[unit] - count
            if remaining:
                source.units[unit] = remaining
            else:
                del source.units[unit]
        if not source.strength:
            self._disband(country, source, "rendezvous_dispatch")
            country.army_tombstones[source_id] = {
                "day": self.state.day_index, "host_id": host_id,
                "tile": self._tile(source.tile), "units": dispatched.copy(),
                "recruiting": source.recruiting, "progress": source.progress,
                "preferred_unit": source.preferred_unit, "reason": "RENDEZVOUS_DISPATCH",
            }
        country.armies.append(detachment)
        result = self._order(country, detachment, plan, "RENDEZVOUS",
                             expedition=self._expedition(actor, plan, self._owners()))
        detachment.march.rendezvous = {
            "source_id": source_id, "host_id": host_id, "origin": self._tile(source.tile),
            "ordered_day": self.state.day_index,
            "action_id": f"A{self.state.next_action_number - 1:08d}",
        }
        if country.buildings.get(source.tile) == "BARRACKS":
            country.barracks_dispatches[source.tile] = {
                "army_id": source_id, "host_id": host_id, "units": dispatched.copy(),
                "remaining": source.strength, "progress": source.progress,
                "day": self.state.day_index,
            }
        self.state.record("rendezvous_dispatched", country_id=actor, source_id=source_id,
                          host_id=host_id, units=dispatched.copy(),
                          remaining=source.strength, progress=source.progress)
        if len(plan["route"]) == 1:
            self._arrive(country, detachment, detachment.march)
        return {**result, "source_id": source_id, "host_id": host_id}

    def _finish_rendezvous(self, country, army, order, *, cancelled=False):
        details = order.rendezvous
        transit_id = army.army_id
        tile = order.destination
        owners = self._owners()
        legal = self._marchable(country.country_id, tile, owners)
        camps = [camp for camp in self._camps_of(country, tile) if camp is not army]
        host = next((camp for camp in camps if camp.army_id == details["host_id"]), None)
        # A preceding arrival may already have established the host here.
        if camps and (host is None or host.arrival_day == self.state.day_index):
            host = min(camps, key=lambda camp: (
                camp.arrival_day, camp.arrival_sequence, camp.army_id,
            ))
        errors = []
        if host is not None and legal and not cancelled:
            army.march = order  # Exclude the incoming detachment from occupied capacity.
            capacity = self.rendezvous_capacity(country, tile)
            army.march = None
            errors = self._rendezvous_errors(army.units, army.units, capacity)
        merged = host is not None and legal and not cancelled and not errors
        code = "RENDEZVOUS_MERGED"
        reason = "会师完成"
        if merged:
            for unit, count in army.units.items():
                host.units[unit] = host.units.get(unit, 0) + count
            country.armies.remove(army)
        elif army.strength:
            code = "RENDEZVOUS_CAMPED"
            reason = "会师未成、就地驻扎"
            if cancelled or not legal:
                legal_tiles = [point for point in reversed(order.route[:order.index+1])
                               if self._marchable(country.country_id, point, owners)
                               and not any(self._camps_of(self.state.countries[enemy], point)
                                           for enemy in country.at_war_with if enemy in self.state.countries)]
                # If borders moved across the entire route, fall back to friendly land.
                army.tile = next(iter(legal_tiles), None)
                if army.tile is None:
                    army.tile = next(iter(country.tiles), None)
                code = "RENDEZVOUS_LANDING_REFUSED"
                reason = "会师未成：无法在目标格驻扎，已撤回可驻地"
            country.army_serial += 1
            army.army_id = f"{country.country_id}-A{country.army_serial}"
            if errors:
                reason += " · " + errors[0]["text"]
        else:
            if any(army is current for current in country.armies):
                self._disband(country, army, "rendezvous_lost")
            code, reason = "RENDEZVOUS_LOST", "会师途中已无剩余兵力"
        operation = {"type": "MILITARY", "operation": "RENDEZVOUS",
                     "army_id": details["source_id"], "host_id": details["host_id"],
                     "tile": self._tile(tile)}
        result = ActionResult(details["action_id"], country.country_id, "MILITARY",
                              code in {"RENDEZVOUS_MERGED", "RENDEZVOUS_CAMPED"},
                              code, reason, self.state.day_index)
        if self.state.day_index > details["ordered_day"] or len(order.route) > 1:
            self.state.add_receipt(result, operation)
        self.state.record("army_arrived" if not cancelled else "march_cancelled",
                          country_id=country.country_id, army_id=transit_id,
                          purpose="RENDEZVOUS", tile=self._tile(army.tile), reason=reason)
        self.state.record("rendezvous_arrived", country_id=country.country_id,
                          source_id=details["source_id"], army_id=host.army_id if merged else army.army_id,
                          reason=reason, code=code, tile=self._tile(army.tile))

    @staticmethod
    def _refusal(target, road: dict, cap: float) -> tuple[str, str]:
        """The code and the sentence a refused march answers with."""
        if road["reason"] == "SEA":
            return "INVALID_TERRAIN", "军队不能进入海域"
        if road["reason"] == "ENCLAVED":
            return "NO_LAND_LINK", (
                f"({target[0]},{target[1]}) 与本国之间只有海：陆军到不了对岸")
        if road["reason"] == "ORDER_LIMIT":
            return "ORDER_LIMIT", (
                f"到 ({target[0]},{target[1]}) 要走 {road['cost']} 点行军、约 {road['days']} 天，"
                f"超过单条命令的 {cap} 点上限：先在中途驻一脚，再下第二段令")
        return "NO_ROAD", f"到 ({target[0]},{target[1]}) 没有可行军的路"

    def _staging_map(self, defender, city_centers, movement: Movement):
        """{tile an army can camp on: enemy tile it could seize from there}."""
        staging: dict[tuple[int, int], tuple[int, int]] = {}
        for objective in sorted(defender.tiles, key=lambda coord: (coord[1], coord[0])):
            if objective in city_centers:
                continue
            for coord in self.state.world.neighbors(*objective):
                if coord in defender.tiles or movement.cost(coord) is None:
                    continue
                staging.setdefault(coord, objective)
        return staging

    def _camp_map(self, defender, movement: Movement) -> dict:
        """{hex an army may sleep on: the foreign camp standing on or beside it}.

        An army that has pitched outside its own borders — in the wilderness, on a third
        realm's ground, or on ours — has no wall in front of it, so the fight is the whole
        order and the camp's own hex is a legal place to stand and give it. Ground the
        camp's own realm holds is left to `_staging_map`, which prices that land as the
        prize it is instead.
        """
        staging: dict[tuple[int, int], tuple[int, int]] = {}
        for army in sorted(defender.armies, key=lambda item: item.army_id):
            if army.march is not None or army.strength <= 0 or army.tile is None:
                continue
            objective = army.tile
            if objective in defender.tiles or movement.cost(objective) is None:
                continue
            for coord in (objective, *self.state.world.neighbors(*objective)):
                if coord in defender.tiles or movement.cost(coord) is None:
                    continue
                staging.setdefault(coord, objective)
        return staging

    def _best_approach(self, army, staging, movement: Movement, costs, previous):
        """Cheapest camp tile already priced by `costs`, as (plan, objective)."""
        reached = [coord for coord in staging if coord in costs]
        if not reached:
            return None
        hold = min(reached, key=lambda coord: (costs[coord], coord[1], coord[0]))
        route = movement.route(previous, army.tile, hold)
        totals = movement.cumulative(route)
        return {
            "route": route,
            "cumulative": totals,
            "cost": round(totals[-1], 3),
            "days": movement.days(totals[-1]),
        }, hold, staging[hold]

    def _approach(self, actor: str, army, defender, city_centers):
        """Cheapest march ending beside a tile this army can seize.

        One search over the whole continent prices it: hostile ground is dearer to walk on,
        not forbidden, so the road the settlement offers is simply the cheapest one and the
        expedition flag is whatever that road happens to cross.
        """
        movement = self.movement(actor)
        staging = self._staging_map(defender, city_centers, movement)
        if not staging:
            return None
        costs, previous = movement.search(army.tile)
        found = self._best_approach(army, staging, movement, costs, previous)
        if found is None:
            return None
        plan, _hold, objective = found
        return plan, objective, self._expedition(actor, plan, self._owners())

    def _approach_to(self, actor: str, army, defender, objective):
        """Cheapest single order that ends beside one named tile, as (plan, hold, expedition).

        `strike_options` picks whichever frontier tile is nearest; a player pointing at a
        tile deserves the price of *that* tile, so the route is planned against its own ring.
        The ring may lie on anybody's soil — a march through a hostile province is priced by
        the border surcharge it pays, and only the fight at the far end is penalised.
        """
        movement = self.movement(actor)
        staging = [
            coord for coord in self.state.world.neighbors(*objective)
            if coord not in defender.tiles and movement.cost(coord) is not None
        ]
        if not staging:
            return None
        costs, previous = movement.search(army.tile)
        reached = [coord for coord in staging if coord in costs]
        if not reached:
            return None
        hold = min(reached, key=lambda coord: (costs[coord], coord[1], coord[0]))
        plan = self._road_plan(movement, previous, army.tile, hold)
        return plan, hold, self._expedition(actor, plan, self._owners())

    def siege_opportunities(self, actor: str, targets: list[str] | None = None) -> dict:
        """The walls this realm's waiting armies are standing beside, priced by the ledger.

        An army inside an enemy city's ring can only blockade it, so the agent plans with
        the same `siege_ledger` the settlement runs: a fall date quoted here is a date the
        engine reaches. `open` says whether the blockade is already on the books, and
        `taken_by` who holds it when that realm is not this one.
        """
        country = self.state.countries[actor]
        wanted = sorted(country.at_war_with if targets is None else targets)
        waiting = [
            army for army in country.armies
            if army.march is None and army.strength > 0 and army.tile is not None
        ]
        rows: dict[str, list] = {}
        if not waiting:
            return rows
        for target_id in wanted:
            defender = self.state.countries.get(target_id)
            if defender is None or target_id == actor:
                continue
            for city in sorted(defender.cities, key=lambda item: item.city_id):
                present = [army for army in waiting if self._stands_besieging(army, city)]
                if not present:
                    continue
                ledger = siege_ledger(
                    self.state.countries, defender, city, self.state.config,
                ) or self._siege_projection(country, defender, city)
                rows.setdefault(target_id, []).append({
                    "city_id": city.city_id, "name": ledger["name"],
                    "center": self._tile(city.center),
                    "armies": [army.army_id for army in present],
                    "open": city.siege is not None,
                    "taken_by": None if city.siege is None or city.siege["attacker"] == actor
                    else city.siege["attacker"],
                    "capital": self.state.is_capital(defender, city),
                    "days_left": ledger["days_left"], "progress": ledger["progress"],
                    "walls": ledger["walls"], "share": ledger["share"],
                })
        return rows

    def siege_marches(self, actor: str, targets: list[str] | None = None,
                      limit: int | None = None) -> dict[str, list[dict]]:
        """The walls this realm has not invested, with the column one order could deliver.

        `siege_opportunities` only sees a wall an army already lies beside, and on a fresh war
        that is nobody: a general reading only that table trades gate tiles forever and never
        starves a city whole. This is the other half of the same book — every enemy center
        nobody has blockaded, priced with the ledger it will run on the day a column sleeps
        against those walls. A rival's blockade is left out (one wall lies to one realm) and so
        is a center this realm already stands around, because that order is free today and
        belongs to the first table. Each waiting army reports its own cheapest wall, so a
        realm with three columns sees three walls, not one wall three times.

        A wall behind a hostile province is offered too, priced as the dearer road it is: the
        border surcharge slows the column on the way and the 远征 penalty weighs on the day it
        arrives, but the wall is no longer out of reach just because somebody's land lies in
        the middle. A column that has to fight its way there can be ordered to, which is what
        the strike rows and the 边缘城池 rule are for.
        """
        country = self.state.countries[actor]
        wanted = sorted(country.at_war_with if targets is None else targets)
        waiting = [
            army for army in country.armies
            if army.march is None and army.strength > 0 and army.tile is not None
        ]
        rows: dict[str, list[dict]] = {}
        if not wanted or not waiting:
            return rows
        movement = self.movement(actor)
        owners = self._owners()
        columns = int(self.config["strike_columns"] if limit is None else limit)
        for target_id in wanted:
            defender = self.state.countries.get(target_id)
            if defender is None or target_id == actor:
                continue
            walls: dict[str, tuple] = {}
            for city in sorted(defender.cities, key=lambda item: item.city_id):
                if city.siege is not None or any(
                    self._stands_besieging(army, city) for army in waiting
                ):
                    continue
                staging = {
                    coord: city.center for coord in self.state.world.neighbors(*city.center)
                    if coord not in defender.tiles and movement.cost(coord) is not None
                }
                if staging:
                    walls[city.city_id] = (city, staging)
            if not walls:
                continue
            best: dict[str, dict] = {}
            for army in waiting:
                costs, previous = movement.search(army.tile)
                for city_id, (city, staging) in walls.items():
                    found = self._best_approach(army, staging, movement, costs, previous)
                    if found is None:
                        continue
                    plan, camp, _objective = found
                    earlier = best.get(army.army_id)
                    if earlier is not None and earlier["days"] <= plan["days"]:
                        continue
                    ledger = self._siege_projection(
                        country, defender, city, listed=[army.army_id],
                        at={army.army_id: camp},
                    )
                    best[army.army_id] = {
                        "city_id": city_id, "name": city.name,
                        "center": self._tile(city.center), "camp": self._tile(camp),
                        "army_id": army.army_id, "days": plan["days"],
                        "cost": round(plan["cost"], 3),
                        "expedition": self._expedition(actor, plan, owners),
                        "capital": self.state.is_capital(defender, city),
                        "walls": ledger["walls"], "share": ledger["share"],
                        "days_left": ledger["days_left"],
                    }
            ranked = sorted(
                best.values(),
                key=lambda row: (row["days"] + (row["days_left"] or 9_999), row["army_id"]),
            )[:max(0, columns)]
            if ranked:
                rows[target_id] = ranked
        return rows

    def strike_options(self, actor: str, targets: list[str] | None = None) -> dict[str, dict]:
        """Price the attack each waiting army could order, with the settlement's own rules.

        The AI and the player read these numbers instead of a parallel hop count, so a
        strike an agent picks is one the engine can deliver in the days it was promised.
        `targets` defaults to the realms already at war; the war decision asks for all of
        them, because reachability is judged before a declaration is worth ordering.
        """
        country = self.state.countries[actor]
        wanted = sorted(
            country.at_war_with if targets is None else targets
        )
        waiting = [
            army for army in country.armies
            if army.march is None and army.strength > 0 and army.tile is not None
        ]
        if not wanted or not waiting:
            return {}
        movement = self.movement(actor)
        owners = self._owners()
        staging: dict[str, dict] = {}
        defended: dict[str, Any] = {}
        for target_id in wanted:
            defender = self.state.countries.get(target_id)
            if defender is None or target_id == actor:
                continue
            staging[target_id] = self._staging_map(
                defender, {city.center for city in defender.cities}, movement
            )
            # An army pitched outside its own borders has no gate to fight for first: the
            # camp itself is the objective, so its ring joins the same board. Land the realm
            # holds keeps its own, better price — taking a hex beats taking a fight.
            for coord, objective in self._camp_map(defender, movement).items():
                staging[target_id].setdefault(coord, objective)
            defended[target_id] = defender
        best: dict[str, dict[str, dict]] = {}

        def consider(target_id: str, candidate: dict) -> None:
            """Keep each army's own cheapest approach, so a column can be sent per army."""
            held = best.setdefault(target_id, {}).get(candidate["army_id"])
            if held is None or candidate["days"] < held["days"]:
                best[target_id][candidate["army_id"]] = candidate

        for army in waiting:
            reached: tuple | None = None
            for target_id, staging_tiles in staging.items():
                if army.tile in staging_tiles:
                    consider(target_id, self._strike(
                        country, army, army.tile, {"days": 0, "cost": 0.0}, False,
                        staging_tiles[army.tile], defended[target_id],
                    ))
                    continue
                if reached is None:
                    # One search per column, read against every frontier: hostile ground is
                    # priced on the way now, so there is no second attempt to fall back to.
                    reached = movement.search(army.tile)
                costs, previous = reached
                found = self._best_approach(army, staging_tiles, movement, costs, previous)
                if found is None:
                    continue
                plan, hold, objective = found
                consider(target_id, self._strike(
                    country, army, hold, plan, self._expedition(actor, plan, owners),
                    objective, defended[target_id]))
        result: dict[str, dict] = {}
        sieges = self.siege_opportunities(actor, wanted)
        marches = self.siege_marches(actor, wanted)
        for target_id in staging:
            ranked = sorted(
                best.get(target_id, {}).values(),
                key=lambda option: (option["days"], -option["mass"], option["army_id"]),
            )[:self.config["strike_columns"]]
            result[target_id] = {**ranked[0], "options": ranked} if ranked else {
                "army_id": None, "reachable": False, "can_attack": False, "expedition": False,
                "days": None, "cost": None, "march_target": None, "objective": None,
                "mass": 0, "mass_share": None, "options": [],
            }
            # A ring already reached is a different order to give: the wall, not the gate.
            result[target_id]["siege"] = sieges.get(target_id, [])
            # And a wall nobody lies beside is a third: march a column to it, blockade on arrival.
            result[target_id]["siege_marches"] = marches.get(target_id, [])
        return result

    def _strike(self, country, army, hold, plan: dict, expedition: bool,
                objective: tuple[int, int], defender) -> dict:
        """One army's attack order as the settlement would price it, mass included.

        The loss preview reads the ring the defender is holding *today*: it is the same
        `_loss_plan` the battle will run, so a panel that says "无守军 · 损耗 0" is a
        promise the settlement keeps unless the defender moves between order and arrival.
        """
        present, share = self._mass(country, objective, army)
        guards = self._mass(defender, objective)[0]
        attacker_loss, defender_loss, unopposed = self._loss_plan(present, guards)
        rout = self._rout_frame(defender, objective, present, guards)
        if rout is not None:
            # The one part of the rout book that costs a road: the hex the column would
            # fall back to, read from the staging ground it will actually be standing on.
            escape = self._retreat_tile(country, defender.country_id, hold, homeward=False)
            rout["attacker"]["retreat"] = self._tile(escape)
            rout["attacker"]["fate"] = "routed" if escape is not None else "annihilated"
        return {
            "army_id": army.army_id, "reachable": True, "can_attack": plan["days"] == 0,
            "expedition": expedition, "days": plan["days"], "cost": round(plan["cost"], 3),
            "march_target": self._tile(hold), "objective": self._tile(objective),
            "mass": len(present), "mass_share": round(share, 4),
            "unopposed": unopposed, "attacker_loss": attacker_loss,
            "defender_loss": defender_loss, "guards": len(guards), "rout": rout,
        }

    def tile_strikes(self, actor: str | None, tile: tuple[int, int]) -> dict:
        """What one tile holds, and what each commanded realm's armies would pay to take it.

        The panel reads the settlement's own numbers here: the same movement table, the same
        mass rule, so a price printed on a tile is the price the battle will actually use.
        `actor` is None in observer view, which still answers "who is standing on this tile".
        """
        tile = tuple(tile)
        owners = self._owners()
        owner = owners.get(tile)
        garrison = [
            {**army_payload(army, country, self.state.config),
             "country_id": country.country_id}
            for country in self.state.countries.values()
            for army in country.armies if army.tile == tile
        ]
        payload = {
            "tile": self._tile(tile), "owner": owner, "garrison": garrison,
            "building": None if owner is None
            else self.state.countries[owner].buildings.get(tile),
            "tribute": None,
            "city_center": False, "at_war": False, "reason": None, "strikes": [],
            "siege": None, "siege_taken": None, "march": None, "field": [],
        }
        if actor is None or actor not in self.state.countries:
            payload["reason"] = "NO_COMMAND"
            return payload
        country = self.state.countries[actor]
        waiting = [
            army for army in country.armies
            if army.march is None and army.strength > 0 and army.tile is not None
        ]
        # The road and the fight are both quoted before the ownership tests, because both of
        # them exist on a hex nobody owns: an empty province is somewhere to march to, and a
        # hostile camp in one is something to attack.
        payload["march"] = self._march_frame(actor, tile, waiting, owners)
        payload["field"] = self._field_frames(actor, tile, owners, garrison, waiting)
        if owner is None:
            payload["reason"] = "NO_OWNER"
            return payload
        if owner == actor:
            # Own walls get the same ledger on the panel: a realm should see its own city
            # starving from the tile it clicks, not only from the city card.
            home = self.state.countries[actor]
            city = self._city_of(home, tile)
            if city is not None:
                payload["city_center"] = True
                payload["siege"] = siege_ledger(
                    self.state.countries, home, city, self.state.config)
            payload["reason"] = "OWN_TERRITORY"
            return payload
        defender = self.state.countries[owner]
        building = payload["building"]
        payload["at_war"] = owner in country.at_war_with
        center = self._city_of(defender, tile)
        payload["city_center"] = center is not None
        if building and building not in self.config["capture_tribute_exempt"]:
            share = float(self.config["capture_tribute_share"])
            owed = sum(float(levy["share"]) for levy in defender.levies if levy["to"] == actor)
            payload["tribute"] = {
                "label": self.state.config["buildings"][building]["label"],
                "share": round(share, 4),
                "days": int(self.config["capture_tribute_days"]),
                "daily": round(tribute_base(defender) * share, 4),
                "cap": round(float(self.config["capture_tribute_cap"]), 4),
                "owed": round(owed, 4),
            }
        if not payload["at_war"]:
            payload["reason"] = "NOT_AT_WAR"
            return payload
        rival = None
        if center is not None:
            live = siege_ledger(self.state.countries, defender, center, self.state.config)
            rival = live if live is not None and live["attacker"] != actor else None
            # A wall is quoted, not priced for one afternoon of fighting: the panel reads the
            # blockade ledger the settlement will run — this realm's own if one is open, the
            # rival's if one already lies here, and otherwise what the ring would starve today.
            payload["siege_taken"] = rival
            payload["siege"] = (
                live if live is not None and rival is None
                else None if rival is not None
                else self._siege_projection(country, defender, center)
            )
        for army in waiting:
            present, share = self._mass(country, tile, army)
            row = {
                "army_id": army.army_id, "mass": len(present),
                "mass_share": round(share, 4),
            }
            if center is None:
                # A tile that will be fought for gets the same preview the battle will use.
                # A center is not fought for: an army that arrives there opens a blockade.
                guards = self._mass(defender, tile)[0]
                attacker_loss, defender_loss, unopposed = self._loss_plan(present, guards)
                rout = self._rout_frame(defender, tile, present, guards)
                if rout is not None:
                    escape = self._retreat_tile(country, defender.country_id, army.tile,
                                               homeward=False)
                    rout["attacker"]["retreat"] = self._tile(escape)
                    rout["attacker"]["fate"] = "routed" if escape is not None else "annihilated"
                row.update({
                    "unopposed": unopposed, "attacker_loss": attacker_loss,
                    "defender_loss": defender_loss, "guards": len(guards), "rout": rout,
                })
            if center is not None:
                # The ring is what a blockade counts, not the one hex a pitched battle needs,
                # and one wall only ever lies to one realm.
                row["can_attack"] = rival is None and self._stands_besieging(army, center)
                if not row["can_attack"]:
                    approach = self._approach_to(actor, army, defender, tile)
                    if approach is None:
                        row.update({"reachable": False, "expedition": False, "days": None,
                                    "cost": None, "march_target": None})
                    else:
                        plan, hold, expedition = approach
                        row.update({"reachable": True, "expedition": expedition, "days": plan["days"],
                                    "cost": plan["cost"], "march_target": self._tile(hold)})
                else:
                    row.update({"reachable": True, "expedition": False, "days": 0, "cost": 0.0,
                                "march_target": self._tile(army.tile)})
            elif hex_distance(army.tile, tile) == 1:
                row.update({"reachable": True, "can_attack": True, "expedition": False,
                            "days": 0, "cost": 0.0, "march_target": self._tile(army.tile)})
            else:
                approach = self._approach_to(actor, army, defender, tile)
                if approach is None:
                    row.update({"reachable": False, "can_attack": False, "expedition": False,
                                "days": None, "cost": None, "march_target": None})
                else:
                    plan, hold, expedition = approach
                    row.update({"reachable": True, "can_attack": False, "expedition": expedition,
                                "days": plan["days"], "cost": plan["cost"],
                                "march_target": self._tile(hold)})
            payload["strikes"].append(row)
        payload["strikes"].sort(
            key=lambda row: (row["days"] is None, row["days"] or 0, -row["mass"], row["army_id"])
        )
        if not payload["strikes"]:
            payload["reason"] = "NO_ARMY"
        return payload

    def _march_frame(self, actor: str, tile: tuple[int, int], waiting: list,
                     owners: dict) -> dict:
        """The road half of the tile panel: what each column would pay to sleep on this hex.

        Any land hex answers the question, whoever holds it, because a border no longer stops
        a march — it only prices one. What still stops an army is *stopping*: a march order
        ends on own or unclaimed ground, and the refusal the frame prints is the sentence
        `march` itself raises. The four old answers hidden behind 「没有路」 are named apart
        here — water, a realm walled off by sea, and a road longer than one order can pay,
        with the cost and the days it would actually need.
        """
        frame = {
            "tile": self._tile(tile), "owner": owners.get(tile), "reason": None,
            "cap": round(float(self.movement_config["maximum_order_cost"]), 3),
            "daily_budget": float(self.movement_config["daily_budget"]),
            "rows": [],
        }
        owner = owners.get(tile)
        if owner not in (None, actor):
            frame["reason"] = "FOREIGN_GROUND"
            return frame
        movement = self.movement(actor)
        if movement.cost(tile) is None:
            frame["reason"] = "SEA"
            return frame
        if not waiting:
            frame["reason"] = "NO_ARMY"
            return frame
        for army in waiting:
            row = {
                "army_id": army.army_id, "strength": int(army.strength),
                "from": self._tile(army.tile), "days": None, "cost": None,
                "expedition": False, "cost_needed": None, "days_needed": None,
            }
            if army.tile == tile:
                row.update({"reachable": True, "days": 0, "cost": 0.0,
                            "reason": "ALREADY_THERE"})
            else:
                road = self._road(actor, army.tile, tile, movement=movement, explain=True)
                plan = road["plan"]
                if plan is None:
                    row.update({
                        "reachable": False, "reason": road["reason"],
                        "cost_needed": road["cost"], "days_needed": road["days"],
                    })
                else:
                    row.update({
                        "reachable": True, "reason": None, "days": plan["days"],
                        "cost": round(plan["cost"], 3),
                        "expedition": self._expedition(actor, plan, owners),
                    })
            frame["rows"].append(row)
        frame["rows"].sort(
            key=lambda row: (not row["reachable"], row["days"] or 0,
                             -row["strength"], row["army_id"])
        )
        return frame

    def _field_frames(self, actor: str, tile: tuple[int, int], owners: dict,
                      garrison: list, waiting: list) -> list[dict]:
        """Soldiers standing on ground their own realm does not claim, priced for a fight.

        The border ledger quotes battle only on the enemy's own soil, so a column pitched in
        the open — in nobody's province, parked on ours, or wintering under a third crown's
        flag — looked untouchable, and the panel said 「目标格不在其领土上」 about men in plain
        sight. This is `_attack_camp` read the same way: the same ring, the same `_loss_plan`,
        the same road to its edge. `field` marks the one difference a player has to know
        before ordering it — the win breaks the column and moves no border.
        """
        country = self.state.countries[actor]
        frames: list[dict] = []
        rivals = sorted({row["country_id"] for row in garrison} - {actor, owners.get(tile)})
        for foe_id in rivals:
            foe = self.state.countries.get(foe_id)
            if foe is None:
                continue
            camps = self._camps_of(foe, tile)
            if not camps:
                continue
            pitched = {army.army_id for army in camps}
            at_war = foe_id in country.at_war_with
            frame = {
                "target": foe_id, "target_name": foe.name, "at_war": at_war,
                "field": True, "reason": "NOT_AT_WAR" if not at_war else None,
                "armies": sorted(pitched),
                "strength": int(sum(army.strength for army in camps)),
                "power": round(sum(float(row["power"]) for row in garrison
                                   if row["army_id"] in pitched), 3),
                "strikes": [],
            }
            if not at_war:
                frames.append(frame)
                continue
            guards = self._mass(foe, tile)[0]
            for army in waiting:
                present, share = self._mass(country, tile, army)
                attacker_loss, defender_loss, unopposed = self._loss_plan(present, guards)
                rout = self._rout_frame(foe, tile, present, guards)
                row = {
                    "army_id": army.army_id, "mass": len(present),
                    "mass_share": round(share, 4), "guards": len(guards),
                    "unopposed": unopposed, "attacker_loss": attacker_loss,
                    "defender_loss": defender_loss, "rout": rout,
                    "objective": self._tile(tile),
                }
                if hex_distance(army.tile, tile) <= 1:
                    if rout is not None:
                        escape = self._retreat_tile(country, foe_id, army.tile, homeward=False)
                        rout["attacker"]["retreat"] = self._tile(escape)
                        rout["attacker"]["fate"] = "routed" if escape is not None else "annihilated"
                    row.update({"reachable": True, "can_attack": True, "expedition": False,
                                "days": 0, "cost": 0.0, "march_target": self._tile(army.tile)})
                else:
                    approach = self._approach_to(actor, army, foe, tile)
                    if approach is None:
                        row.update({"reachable": False, "can_attack": False,
                                    "expedition": False, "days": None, "cost": None,
                                    "march_target": None})
                    else:
                        plan, hold, expedition = approach
                        row.update({"reachable": True, "can_attack": False,
                                    "expedition": expedition, "days": plan["days"],
                                    "cost": round(plan["cost"], 3),
                                    "march_target": self._tile(hold)})
                frame["strikes"].append(row)
            frame["strikes"].sort(
                key=lambda row: (row["days"] is None, row["days"] or 0,
                                 -row["mass"], row["army_id"])
            )
            if not frame["strikes"]:
                frame["reason"] = "NO_ARMY"
            frames.append(frame)
        return frames

    def incoming_threats(self, with_relief: bool = False,
                         only_victim: str | None = None) -> dict[str, list[dict]]:
        """Every column still walking onto somebody's soil, filed under the realm it lands on.

        The alert quotes the march order the settlement is already paying down — the same
        route, the same daily budget, the same `_loss_plan` the battle will run — so the day
        the panel prints is the day `_arrive` fires, and a warning that says the gate is
        undefended is saying it with the defender's own garrison count. `days_left` counts the
        budgets still owed and an order given today spends tonight's, so the landing is
        stamped `day_index + days_left - 1` — the same day number the `battle` event carries.
        Nothing is repriced
        here: a column whose corridor changes hands is cancelled by `march_daily` and drops
        off this list the same day, and a realm taken at its capital is off the board, so it
        never appears as a victim at all.

        `with_relief` adds the one number a defender cannot read off a route: the cheapest
        of its own waiting columns and the days that one would take to stand on the threatened
        hex. It runs a search per army, so only the agent decision asks for it — the map and
        the panel paint from the cheap quote. `only_victim` drops every other realm's alerts
        before they are priced, which is what keeps a per-country observation from searching
        the whole board's relief roads twelve times over.
        """
        owners = self._owners()
        budget = float(self.movement_config["daily_budget"])
        radius = int(self.config["mass_radius"])
        relief_paths: dict[str, Any] = {}
        grouped: dict[str, list[dict]] = {}
        for country in self.state.countries.values():
            for army in country.armies:
                order = army.march
                if order is None or army.strength <= 0 or order.destination is None:
                    continue
                landing = order.objective if order.objective is not None else order.destination
                held_by = owners.get(landing)
                victim = held_by or (
                    order.target
                    if order.target not in (None, country.country_id)
                    and order.target in self.state.countries else None
                )
                if victim is None or victim == country.country_id:
                    continue
                if only_victim is not None and victim != only_victim:
                    continue
                # A column walking into empty wilderness with no realm named at it is a
                # movement, not an invasion; and an order aimed at a tile our own settlement
                # already holds is a reinforcement.
                if held_by is None and order.objective is None and order.target is None:
                    continue
                defender = self.state.countries[victim]
                guards = self._mass(defender, landing)[0]
                # Whoever is already standing near the landing hex fights beside this column
                # the day it arrives, so the alert prices the strike the arrival will make.
                convoy = [
                    other for other in sorted(country.armies, key=lambda item: item.army_id)
                    if other is army or (
                        other.march is None and other.strength > 0 and other.tile is not None
                        and hex_distance(other.tile, landing) <= radius)
                ]
                attacker_loss, _defender_loss, unopposed = self._loss_plan(convoy, guards)
                city = self._city_of(defender, landing)
                days_left = max(1, march_days(order.remaining, budget))
                row = {
                    "attacker": country.country_id, "attacker_name": country.name,
                    "army_id": army.army_id, "purpose": order.purpose,
                    "expedition": order.expedition,
                    "strength": sum(other.strength for other in convoy),
                    "columns": len(convoy),
                    "from": self._tile(army.tile),
                    "tile": self._tile(landing), "destination": self._tile(order.destination),
                    "city_id": city.city_id if city else None,
                    "city": city.name if city else None,
                    "capital": bool(city is not None and self.state.is_capital(defender, city)),
                    "defenders": len(guards), "unopposed": unopposed,
                    "attacker_loss": attacker_loss,
                    "rout": self._rout_frame(defender, landing, convoy, guards),
                    "days_left": days_left,
                    "arrival_day": self.state.day_index + days_left - 1,
                    "arrival_date": calendar_date(
                        self.state.day_index + days_left - 1,
                        self.state.start_astronomical_year,
                    )["label"],
                }
                if with_relief:
                    row["relief"] = self._relief(defender, landing, owners, relief_paths)
                grouped.setdefault(victim, []).append(row)
        for rows in grouped.values():
            rows.sort(key=lambda row: (row["days_left"], -row["strength"], row["army_id"]))
        return grouped

    def alarm_watch(self) -> set[str]:
        """Which realms cannot wait for the next scheduled council, read from the alert itself.

        `planning_interval_days` is a rhythm for ordinary business, but a blockade runs on
        `fall_threshold / daily_progress` — about seventeen days — and a relief march is only
        worth ordering inside the window the alert already printed. So a realm with a wall
        already being starved, or with a column whose order will open one, is asked to think
        today. A raid onto a gate hex is not: it costs a battle, not a realm, and waking every
        neighbour for every traded hex would only turn the council into a clock. Same books as
        `incoming_threats`, nothing re-timed here.
        """
        alarmed = {
            country.country_id
            for country in self.state.countries.values()
            for city in country.cities if city.siege
        }
        for victim, rows in self.incoming_threats().items():
            if any(row["city_id"] for row in rows):
                alarmed.add(victim)
        return alarmed

    def _relief(self, defender, tile, owners: dict, caches: dict) -> dict | None:
        """Our own waiting column that could stand on `tile` soonest, as (army, days, cost).

        A defender cannot read this off the attacker's route: the road a relief column walks
        is priced with *its* borders — dearer where it crosses them — and a march order can
        only end on ground its own realm holds or nobody does. A hex the invader has already
        taken is therefore not a relief destination but an attack objective, and `march` would
        refuse it; the alert says so instead of quoting days no order can keep. The cache keeps
        one pricing pass per realm, so a realm answered about four columns does not rebuild the
        ownership map four times. A column already standing on the threatened hex is not relief
        — it is the garrison the alert already counted in `defenders`, and ordering it to walk
        nowhere would burn the day's action slot.
        """
        if not self._marchable(defender.country_id, tile, owners):
            return None
        if defender.country_id not in caches:
            caches[defender.country_id] = self.movement(defender.country_id)
        movement = caches[defender.country_id]
        best: dict | None = None
        for army in sorted(defender.armies, key=lambda item: item.army_id):
            if army.march is not None or army.strength <= 0 or army.tile is None:
                continue
            if tuple(army.tile) == (tile[0], tile[1]):
                continue
            plan = self._road(defender.country_id, army.tile, tile, movement=movement)["plan"]
            if plan is None:
                continue
            if best is None or (plan["days"], plan["cost"]) < (best["days"], best["cost"]):
                best = {"army_id": army.army_id, "days": plan["days"],
                        "cost": round(plan["cost"], 3)}
        return best

    def set_recruiting(self, actor: str, army_id: str | None, recruiting: bool) -> dict:
        """Pause or resume the wage a barracks pays for its own garrison army."""
        country = self.state.countries[actor]
        army = self._army_of(country, army_id)
        if army is None:
            raise ActionRejected("NO_ARMY", "国家还没有军队可以下令")
        if recruiting:
            if country.buildings.get(army.tile) != "BARRACKS":
                raise ActionRejected("NO_BARRACKS", "这支军队没有驻在本国兵营上，募兵开关只对有兵营的军队有效")
            rival = next(
                (other for other in country.armies
                 if other is not army and other.recruiting and other.tile == army.tile),
                None,
            )
            if rival is not None:
                raise ActionRejected("BARRACKS_BUSY", f"这座兵营已由 {rival.army_id} 募兵")
        army.recruiting = recruiting
        self.state.record(
            "recruiting_changed", country_id=actor, army_id=army.army_id, recruiting=recruiting,
            tile={"col": army.tile[0], "row": army.tile[1]} if army.tile else None,
        )
        return {"army_id": army.army_id, "recruiting": recruiting}

    def set_recruit_unit(self, actor: str, army_id: str | None, unit_id: str) -> dict:
        """Choose which type a barracks garrison trains next."""
        if unit_id not in self.units:
            raise ActionRejected("UNKNOWN_UNIT", f"未知兵种: {unit_id!r}")
        country = self.state.countries[actor]
        army = self._army_of(country, army_id)
        if army is None:
            raise ActionRejected("NO_ARMY", "国家还没有军队可以下令")
        if not self.unlocked(country, unit_id):
            requirement = self.units[unit_id]["required_technology"]
            branch = self.state.config["tech_tree"][requirement["field"]]["label"]
            raise ActionRejected(
                "TECHNOLOGY_REQUIRED",
                f"{self.units[unit_id]['label']} 需要{branch}达到 {requirement['level']} 级",
            )
        army.preferred_unit = unit_id
        self.state.record(
            "recruit_unit_changed", country_id=actor, army_id=army.army_id, unit=unit_id,
        )
        return {"army_id": army.army_id, "unit": unit_id}

    def _counter_modifier(self, unit_id: str, enemy_units: dict[str, int]) -> float:
        """Weight this type's counters by each enemy type's share of its army."""
        counters = self.units[unit_id].get("counters") or {}
        total = sum(enemy_units.values())
        if not counters or total <= 0:
            return 1.0
        modifier = 1.0 + sum(
            float(counters.get(rival, 0.0)) * count / total
            for rival, count in enemy_units.items()
        )
        return max(0.0, modifier)

    def _side_value(self, units: dict[str, int], statistic: str, enemies: dict[str, int],
                    biome: str, use_terrain: bool) -> float:
        value = 0.0
        for unit_id, count in units.items():
            modifier = self._counter_modifier(unit_id, enemies)
            if use_terrain:
                modifier *= float((self.units[unit_id].get("terrain") or {}).get(biome, 1.0))
            value += count * float(self.units[unit_id][statistic]) * modifier
        return value

    def _remove_losses(self, units: dict[str, int], losses: int) -> dict[str, int]:
        """Split losses across types by count share, largest remainder first.

        Deliberately RNG-free: battles must replay identically from the action log.
        """
        active = {unit_id: count for unit_id, count in units.items() if count > 0}
        total = sum(active.values())
        if losses <= 0 or total <= 0:
            return {}
        if losses >= total:
            return active
        exact = {unit_id: losses * count / total for unit_id, count in active.items()}
        removed = {unit_id: int(value) for unit_id, value in exact.items()}
        pool = losses - sum(removed.values())
        ranked = sorted(
            active,
            key=lambda unit_id: (-(exact[unit_id] - removed[unit_id]), self.order.index(unit_id)),
        )
        for unit_id in ranked:
            if pool <= 0:
                break
            if removed[unit_id] < active[unit_id]:
                removed[unit_id] += 1
                pool -= 1
        for unit_id in self.order:
            if pool <= 0:
                break
            room = active.get(unit_id, 0) - removed.get(unit_id, 0)
            if room > 0:
                take = min(pool, room)
                removed[unit_id] = removed.get(unit_id, 0) + take
                pool -= take
        return {unit_id: count for unit_id, count in removed.items() if count > 0}

    @staticmethod
    def _bleed_order(country, present: list) -> list:
        """Casualties fall on the armies standing at the gate, then on the rest of the roster.

        The order is fixed rather than chosen, so replaying an action log gives the same
        armies the same losses.
        """
        return present + [army for army in country.armies if army not in present]

    def _apply_army_losses(self, armies, removed: dict[str, int]) -> None:
        """Take per-type casualties out of the given armies, in list order.

        Battles resolve the merged national roster, so which army a loss came from is
        bookkeeping; the fixed order is what keeps an action-log replay identical.
        """
        for unit_id, count in removed.items():
            remaining = count
            for army in armies:
                if remaining <= 0:
                    break
                take = min(army.units.get(unit_id, 0), remaining)
                if take <= 0:
                    continue
                left = army.units[unit_id] - take
                if left > 0:
                    army.units[unit_id] = left
                else:
                    del army.units[unit_id]
                remaining -= take

    def attack(self, actor: str, target_id: str, army_id: str | None = None,
               tile: tuple[int, int] | None = None) -> dict:
        attacker = self.state.countries[actor]
        if target_id not in self.state.countries or target_id == actor:
            raise ActionRejected("UNKNOWN_COUNTRY", "目标国家无效")
        if target_id not in attacker.at_war_with:
            raise ActionRejected("NOT_AT_WAR", "必须先宣战")
        army = self._army_of(attacker, army_id)
        self._require_marchable(army)
        defender = self.state.countries[target_id]
        city_centers = {city.center for city in defender.cities}
        if tile is not None:
            return self._attack_tile(attacker, defender, army, tuple(tile))
        adjacent = [
            coord for coord in defender.tiles
            if hex_distance(army.tile, coord) == 1 and coord not in city_centers
        ]
        if adjacent:
            return self._battle(
                attacker, defender, army,
                sorted(adjacent, key=lambda coord: (coord[1], coord[0]))[0],
                expedition=False,
            )
        siege = [
            city for city in sorted(defender.cities, key=lambda item: item.city_id)
            if hex_distance(army.tile, city.center) <= int(self.siege_rules["blockade_radius"])
        ]
        if siege:
            return self._begin_siege(attacker, defender, army, siege[0])
        approach = self._approach(actor, army, defender, city_centers)
        if approach is None:
            raise ActionRejected("NO_PATH", "军队与目标国家之间没有可用的陆上行军路线")
        plan, objective, expedition = approach
        return self._order(attacker, army, plan, "ATTACK", target=target_id,
                           objective=objective, expedition=expedition)

    def _attack_tile(self, attacker, defender, army, tile) -> dict:
        """Take the tile the player pointed at: give battle now, or order the march to it."""
        if tile not in defender.tiles:
            return self._attack_camp(attacker, defender, army, tile)
        city_center = self._city_of(defender, tile)
        if city_center is not None:
            if self._stands_besieging(army, city_center):
                return self._begin_siege(attacker, defender, army, city_center)
            approach = self._approach_to(attacker.country_id, army, defender, tile)
            if approach is None:
                raise ActionRejected(
                    "NO_PATH", f"没有军队能在一条命令内走到 ({tile[0]},{tile[1]}) 旁边：先分段推进"
                )
            plan, _hold, expedition = approach
            return self._order(attacker, army, plan, "ATTACK", target=defender.country_id,
                               objective=tile, expedition=expedition)
        if hex_distance(army.tile, tile) == 1:
            return self._battle(attacker, defender, army, tile, expedition=False)
        approach = self._approach_to(attacker.country_id, army, defender, tile)
        if approach is None:
            raise ActionRejected(
                "NO_PATH", f"没有军队能在一条命令内走到 ({tile[0]},{tile[1]}) 旁边：先分段推进"
            )
        plan, _hold, expedition = approach
        return self._order(attacker, army, plan, "ATTACK", target=defender.country_id,
                           objective=tile, expedition=expedition)

    def _camps_of(self, country, tile: tuple[int, int]) -> list:
        """The armies standing still on this hex — a camp, not a column on the road."""
        return [
            other for other in sorted(country.armies, key=lambda item: item.army_id)
            if other.march is None and other.strength > 0 and other.tile == tile
        ]

    def _attack_camp(self, attacker, defender, army, tile) -> dict:
        """Give battle to an army that has pitched itself outside its own borders.

        A realm's soldiers can stand in the open, on ground nobody claims, or on *our* soil,
        and they are still soldiers to be broken. The order is the same ATTACK at the same
        hex; what differs is the prize — `_battle` declines to move a border it does not own,
        so a won field fight costs the enemy its column and gains no land. An army only
        passing through is not a camp: a march order has no stationary target to hit, and
        inventing one would promise a battle the arrival could not deliver.
        """
        if not self._camps_of(defender, tile):
            raise ActionRejected(
                "NOT_ENEMY_TILE",
                f"({tile[0]},{tile[1]}) 不是 {defender.country_id} 的领土，那里也没有它驻扎的军队",
            )
        if hex_distance(army.tile, tile) <= 1:
            return self._battle(attacker, defender, army, tile, expedition=False)
        approach = self._approach_to(attacker.country_id, army, defender, tile)
        if approach is None:
            raise ActionRejected(
                "NO_PATH", f"没有军队能在一条命令内走到 ({tile[0]},{tile[1]}) 旁边：先分段推进"
            )
        plan, _hold, expedition = approach
        return self._order(attacker, army, plan, "ATTACK", target=defender.country_id,
                           objective=tile, expedition=expedition)

    def _city_of(self, defender, tile: tuple[int, int]):
        """The city whose center this tile is, if any: only a center can be blockaded."""
        return next((city for city in defender.cities if city.center == tile), None)

    def _stands_besieging(self, army, city) -> bool:
        """Whether this army already stands in the ring around those walls."""
        if army.tile is None or army.march is not None or army.strength <= 0:
            return False
        return hex_distance(army.tile, city.center) <= int(self.siege_rules["blockade_radius"])

    def _siege_projection(self, country, defender, city, listed=None, at=None) -> dict:
        """What a blockade would run on if this realm ordered one with the armies standing near.

        The panel quotes the fall date before the order exists, so the projection is built
        from the same ledger the settlement will use the day the order is given. `listed`
        narrows that to the armies a march order would actually deliver — the wall an army
        reaches in twelve days is starved by that army, not by the ones still at home — and
        `at` puts it on the ring tile it will sleep on, because the count is the walls it
        lies to, not the camp it is still a week away from.
        """
        return siege_ledger(
            self.state.countries, defender, city, self.state.config,
            siege={
                "attacker": country.country_id,
                "armies": [
                    army.army_id for army in siege_besiegers(
                        country, city, self.state.config,
                        listed=[other.army_id for other in country.armies]
                        if listed is None else listed,
                        at=at,
                    )
                ],
                "progress": 0.0,
                "started_day": self.state.day_index,
            },
            at=at,
        )

    def _begin_siege(self, attacker, defender, army, city) -> dict:
        """Stand around an enemy city: open its blockade ledger, or add this army to it.

        A wall is blockaded by one realm at a time, so a second attacker is pointed at the
        army that already lies there instead of starting a rival ledger on the same city.
        """
        if not self._stands_besieging(army, city):
            raise ActionRejected(
                "NOT_IN_RING",
                f"{army.army_id} 不在 ({city.center[0]},{city.center[1]}) 的封锁圈内：先行军到城墙旁边",
            )
        siege = city.siege
        if siege and siege["attacker"] != attacker.country_id:
            raise ActionRejected(
                "SIEGE_TAKEN", f"{city.name} 已被 {siege['attacker']} 围困：一座城同时只有一方围得"
            )
        opened = siege is None
        if opened:
            city.siege = {
                "attacker": attacker.country_id, "armies": [army.army_id],
                "progress": 0.0, "started_day": self.state.day_index,
            }
            joined = False
        else:
            joined = army.army_id not in siege["armies"]
            if joined:
                siege["armies"].append(army.army_id)
        ledger = siege_ledger(self.state.countries, defender, city, self.state.config)
        if opened or joined:
            # Re-affirming a ledger this army is already in changes no book, so it says nothing:
            # an alarmed realm now meets every day of a siege and would otherwise diary the same
            # opening once a day until the wall falls.
            self.state.record(
                "siege_started" if not joined else "siege_joined",
                attacker=attacker.country_id, defender=defender.country_id,
                city_id=city.city_id, city=city.name, army_id=army.army_id,
                armies=list(ledger["armies"]), progress=ledger["progress"],
                days_left=ledger["days_left"], walls=ledger["walls"],
            )
        return {"siege": ledger, "joined": joined, "city_id": city.city_id,
                "army_id": army.army_id}

    def siege_daily(self) -> None:
        """Every blockade eats the wall a little further each day; nobody around, nobody starves.

        The share the ledger quotes is the share this settlement applies, so a panel's fall date
        moves only when the arithmetic around the wall moves with it.
        """
        table = self.siege_rules
        threshold = float(table["fall_threshold"])
        # A capital's fall strikes a realm off the board, so the sweep runs on a snapshot.
        for defender in list(self.state.countries.values()):
            # A fall moves the city to the victor's list, so the sweep runs on a snapshot.
            for city in list(defender.cities):
                siege = city.siege
                if not siege:
                    continue
                attacker = self.state.countries.get(siege["attacker"])
                if attacker is None or defender.country_id not in attacker.at_war_with:
                    self._lift_siege(defender, city, "peace")
                    continue
                ledger = siege_ledger(self.state.countries, defender, city, self.state.config)
                siege["armies"] = list(ledger["armies"])
                if not ledger["armies"]:
                    # Every army that lay here has marched off or been bled out; a wall is
                    # not starved by a ledger nobody is standing around.
                    self._lift_siege(defender, city, "ring_empty")
                    continue
                siege["progress"] = min(
                    threshold, float(siege["progress"]) + ledger["daily"]
                )
                attacker.war_exhaustion += float(table["exhaustion_per_day"])
                defender.war_exhaustion += float(table["exhaustion_per_day"])
                if siege["progress"] + 1e-9 >= threshold:
                    self._fall_city(attacker, defender, city, siege)

    def _lift_siege(self, defender, city, reason: str) -> None:
        """Take a blockade off the books: the ring is empty, the war ended, or the wall is ours."""
        siege = city.siege
        if not siege:
            return
        city.siege = None
        self.state.record(
            "siege_lifted", defender=defender.country_id, city_id=city.city_id,
            city=city.name, attacker=siege["attacker"], reason=reason,
            progress=round(float(siege["progress"]), 4),
        )

    def _fall_city(self, attacker, defender, city, siege) -> dict:
        """A city that has starved long enough changes hands whole: walls, ground and people."""
        tiles = list(city.tiles)
        seized: list[str] = []
        for coord in tiles:
            if coord in defender.tiles:
                defender.tiles.remove(coord)
            attacker.tiles.append(coord)
            if coord in defender.outposts:
                defender.outposts.remove(coord)
            building = defender.buildings.pop(coord, None)
            if building is not None:
                attacker.buildings[coord] = building
                seized.append(building)
            defender.projects = [
                project for project in defender.projects if tuple(project["tile"]) != coord
            ]
        defender.cities.remove(city)
        attacker.cities.append(city)
        # The people move with the walls. Reconciling the national totals here is what keeps
        # the handover worth winning: otherwise the victor's own books would shave the
        # captured folk off its capital, since a realm's population is the sum of its cities.
        moved = max(0, int(city.population))
        defender.population = max(0, defender.population - moved)
        attacker.population += moved
        # Only one realm ever lies around a city, and the only ledger on a city that has just
        # fallen is therefore the victor's own: it stands around its own wall now, so the
        # blockade closes here rather than starving a city that is already ours.
        self._lift_siege(defender, city, "captured")
        city.managed_by = "ai"
        # Starved-out walls hand the camp over with the ground, so this garrison loses a city
        # and not its barracks: the same `survivor_rate` of the men still standing on that
        # ground withdraws to whatever soil the realm keeps. Keep none and they die in place.
        occupied = set(tiles)
        garrison = [
            other for other in defender.armies
            if other.march is None and other.strength > 0 and other.tile in occupied
        ]
        rout = None
        if garrison:
            _, rout = self._apply_rout(
                defender, attacker.country_id, garrison,
                rate=float(self.rout_rules["survivor_rate"]), homeward=True,
            )
            rout.update({
                "side": "defender", "city_id": city.city_id, "city": city.name,
                "center": self._tile(city.center), "barracks_lost": False,
            })
        defender.stability = max(
            0.0, defender.stability - float(self.siege_rules["stability_loss"])
        )
        tribute = [
            row for building in seized
            if (row := self._tribute(attacker, defender, building)) is not None
        ]
        self.state.sync_city_population(defender)
        self.state.sync_city_population(attacker)
        self.state.record(
            "city_fallen", attacker=attacker.country_id, defender=defender.country_id,
            city_id=city.city_id, city=city.name, center=self._tile(city.center),
            tiles=len(tiles), population=city.population, buildings=seized,
            tribute=tribute, stability=round(defender.stability, 2), rout=rout,
        )
        result = {
            "city_id": city.city_id, "city": city.name, "tiles": len(tiles),
            "buildings": seized, "population": city.population, "tribute": tribute,
            "rout": rout,
        }
        if self.state.is_capital(defender, city):
            result["annexation"] = self._annex(attacker, defender, capital=city)
        return result

    def _annex(self, attacker, defender, capital=None) -> dict:
        """A capital is the realm: whoever starves it out takes the rest of it whole.

        Every hex, wall, camp and city the fallen court still held moves in one pass, its
        treasury and granaries are emptied into the victor's books, and only then is the
        court struck off the board — `eliminate` is what guarantees nothing downstream
        still names a realm with no soil: no pact, no proposal, no tribute, no seat.
        The content table caps each seizure at 1.0, so the victor lifts what exists and
        never invents a surplus out of a conquest. `capital` is the domain that crossed a
        moment earlier in the same stroke; a realm whose capital was its last hex leaves
        `tiles` empty here and the ledger still has to show ground changing hands.
        """
        tiles = list(defender.tiles)
        cities = sorted(city.city_id for city in defender.cities)
        seized: list[str] = []
        for coord in tiles:
            defender.tiles.remove(coord)
            attacker.tiles.append(coord)
            building = defender.buildings.pop(coord, None)
            if building is not None:
                attacker.buildings[coord] = building
                seized.append(building)
        attacker.outposts = sorted(set(attacker.outposts) | set(defender.outposts))
        defender.outposts = []
        for city in list(defender.cities):
            defender.cities.remove(city)
            city.managed_by = "ai"
            attacker.cities.append(city)
        abandoned = len(defender.projects)
        defender.projects = []
        moved = max(0, int(defender.population))
        attacker.population += moved
        defender.population = 0
        self.state.sync_city_population(attacker)
        treasury_share = float(self.annex_rules["treasury_seizure"])
        food_share = float(self.annex_rules["food_seizure"])
        seized_treasury = max(0.0, defender.treasury) * treasury_share
        seized_food = max(0.0, defender.food) * food_share
        defender.treasury -= seized_treasury
        defender.food -= seized_food
        attacker.treasury += seized_treasury
        attacker.food += seized_food
        tally = self.state.eliminate(defender.country_id, heir=attacker.country_id)
        capital_tiles = len(capital.tiles) if capital is not None else 0
        self.state.record(
            "realm_annexed", attacker=attacker.country_id, fallen=defender.country_id,
            name=defender.name, tiles=len(tiles), capital_tiles=capital_tiles,
            cities=cities, buildings=seized,
            population=moved, treasury=round(seized_treasury, 4),
            food=round(seized_food, 4), armies=tally["armies"],
            projects=abandoned, stability=round(attacker.stability, 2),
            day=self.state.day_index,
        )
        return {
            "fallen": defender.country_id, "name": defender.name, "tiles": len(tiles),
            "capital_tiles": capital_tiles,
            "cities": cities, "buildings": seized, "population": moved,
            "treasury": round(seized_treasury, 4), "food": round(seized_food, 4),
            "armies": tally["armies"], "day": self.state.day_index,
        }

    def _battle(self, attacker, defender, army, target_tile, expedition: bool, *, capture_ground=True) -> dict:
        biome = self.state.world.tile(target_tile)["biome"]
        equipment = 1 + (
            attacker.technology.get("bronze_metallurgy", 1)
            * self.config["metallurgy_power_per_level"]
        )
        defender_equipment = 1 + (
            defender.technology.get("bronze_metallurgy", 1)
            * self.config["metallurgy_power_per_level"]
        )
        roll_low, roll_high = self.config["battle_random_range"]
        attackers, attacker_share = self._mass(attacker, target_tile, army)
        defenders, defender_share = self._mass(defender, target_tile)
        attack_power = (
            self._side_value(attacker.army_units, "power", defender.army_units, biome, True)
            * equipment
            * stability_factor(attacker.stability, self.state.config)
            * self.state.weather.uniform(roll_low, roll_high)
            * attacker_share
            * (self.config["expedition_factor"] if expedition else 1.0)
        )
        defense_power = (
            self._side_value(defender.army_units, "defense", attacker.army_units, biome, False)
            * defender_equipment
            * stability_factor(defender.stability, self.state.config)
            * defender_share
            + self.config["defense_bonus_flat"]
        )
        attacker_loss, defender_loss, unopposed = self._loss_plan(attackers, defenders)
        won = attack_power > defense_power
        # A rout needs a wall to happen inside of, and a garrison to break: open ground and
        # undefended gates keep the plain attrition arithmetic they always had.
        city = self._defended_city(defender, target_tile)
        barracks_lost = won and defender.buildings.get(target_tile) == "BARRACKS"
        captured = False
        tribute = None
        # A border only moves where the defender actually holds the ground. A column caught
        # in the open — on nobody's soil, or parked on ours — loses the fight and nothing
        # else: there is no hex to seize, so the victory is paid for in soldiers alone.
        seizes = capture_ground and target_tile in defender.tiles
        if won and seizes:
            defender.tiles.remove(target_tile)
            attacker.tiles.append(target_tile)
            if target_tile in defender.outposts:
                defender.outposts.remove(target_tile)
            seized = defender.buildings.pop(target_tile, None)
            defender.projects = [
                project for project in defender.projects if tuple(project["tile"]) != target_tile
            ]
            if city is not None:
                city.tiles.remove(target_tile)
                city.zones.pop(target_tile, None)
            new_city = min(
                attacker.cities,
                key=lambda held: hex_distance(held.center, target_tile),
            )
            if target_tile not in new_city.tiles:
                new_city.tiles.append(target_tile)
                new_city.zones[target_tile] = "unzoned"
            defender.stability = max(0, defender.stability - self.config["capture_stability_loss"])
            tribute = self._tribute(attacker, defender, seized)
            captured = True
        rout = None
        if city is None or not defenders:
            attacker_removed = self._remove_losses(attacker.army_units, attacker_loss)
            defender_removed = self._remove_losses(defender.army_units, defender_loss)
            self._apply_army_losses(self._bleed_order(attacker, attackers), attacker_removed)
            self._apply_army_losses(self._bleed_order(defender, defenders), defender_removed)
        else:
            # Inside a defended city the loser is settled army by army: the victor still pays
            # its field attrition, the broken side keeps `survivor_rate` of the men that stood
            # here and walks out of the ring, and the dead are counted against those columns
            # alone instead of bleeding onto camps three hundred kilometres away.
            winner, winner_armies, winner_loss = (
                (attacker, attackers, attacker_loss) if won
                else (defender, defenders, defender_loss)
            )
            loser, loser_armies = (defender, defenders) if won else (attacker, attackers)
            winner_removed = self._remove_losses(winner.army_units, winner_loss)
            self._apply_army_losses(self._bleed_order(winner, winner_armies), winner_removed)
            rate = float(
                self.rout_rules["barracks_lost_survivor_rate"] if barracks_lost
                else self.rout_rules["survivor_rate"]
            )
            # A routed garrison goes home; a routed raiding column only needs out of the
            # winner's land, which is the same sentence read from the other side.
            loser_removed, rout = self._apply_rout(
                loser, winner.country_id, loser_armies, rate=rate, homeward=won
            )
            rout.update({
                "side": "defender" if won else "attacker",
                "city_id": city.city_id, "city": city.name,
                "center": self._tile(city.center), "barracks_lost": barracks_lost,
            })
            attacker_removed = winner_removed if won else loser_removed
            defender_removed = loser_removed if won else winner_removed
        attacker_losses = sum(attacker_removed.values())
        defender_losses = sum(defender_removed.values())
        attacker.war_exhaustion += 1.0
        defender.war_exhaustion += 1.0
        self.state.record(
            "battle", attacker=attacker.country_id, defender=defender.country_id,
            attacker_army=army.army_id,
            attacker_mass=[other.army_id for other in attackers],
            attacker_share=round(attacker_share, 4),
            defender_mass=[other.army_id for other in defenders],
            defender_share=round(defender_share, 4),
            attacker_power=round(attack_power, 3), defender_power=round(defense_power, 3),
            attacker_loss=attacker_losses, defender_loss=defender_losses,
            attacker_units=attacker_removed, defender_units=defender_removed,
            biome=biome, captured=captured, field=not seizes, expedition=expedition,
            unopposed=unopposed, tribute=tribute, rout=rout,
            tile={"col": target_tile[0], "row": target_tile[1]},
        )
        return {
            "battle": True, "captured": captured, "field": not seizes,
            "attacker_army": army.army_id,
            "tribute": tribute,
            "attacker_mass": [other.army_id for other in attackers],
            "attacker_share": round(attacker_share, 3),
            "defender_mass": [other.army_id for other in defenders],
            "defender_share": round(defender_share, 3),
            "attacker_loss": attacker_losses, "defender_loss": defender_losses,
            "attacker_units": attacker_removed, "defender_units": defender_removed,
            "biome": biome, "unopposed": unopposed, "rout": rout,
            "tile": {"col": target_tile[0], "row": target_tile[1]},
        }

    def march_daily(self) -> None:
        """Spend each ordered army's daily budget and settle what it reaches.

        Routes keep the price they were quoted when the order was issued, so a road
        finished afterwards speeds the next march rather than this one. A hex changing
        hands under a column no longer stops it: hostile ground is marched on at the price
        the border adds, and an army that arrives where the war moved on to finds whatever
        is standing there, which is what the 行军中敌土减速 rule promises.
        """
        budget = float(self.movement_config["daily_budget"])
        for country in list(self.state.countries.values()):
            ordered = sorted(country.armies, key=lambda army:
                             (bool(army.march and army.march.rendezvous),
                              army.march.issued_sequence if army.march else 0))
            for army in ordered:
                if not any(army is current for current in country.armies):
                    continue
                order = army.march
                if order is None:
                    continue
                if army.strength <= 0 or army.tile is None or len(order.route) < 2:
                    self._cancel_march(country, army, "军队已无力继续行军")
                    continue
                from_index = order.index
                order.spent += budget
                to_index = order.index
                if order.rendezvous and self._intercept_rendezvous(
                    country, army, order, from_index, to_index
                ):
                    continue
                if to_index > from_index:
                    army.tile = order.route[to_index]
                if to_index < len(order.route) - 1:
                    continue
                self._arrive(country, army, order)

    def _intercept_rendezvous(self, country, army, order, start, end) -> bool:
        for tile in order.route[start+1:end+1]:
            enemy = next((self.state.countries[enemy_id]
                          for enemy_id in country.at_war_with
                          if enemy_id in self.state.countries
                          and self._camps_of(self.state.countries[enemy_id], tile)), None)
            if enemy is None:
                continue
            army.tile = tile
            army.march = None
            self._battle(country, enemy, army, tile, order.expedition, capture_ground=False)
            self._finish_rendezvous(country, army, order, cancelled=True)
            return True
        return False

    def _cancel_march(self, country, army, reason: str) -> None:
        order = army.march
        army.march = None
        if order and order.rendezvous:
            self._finish_rendezvous(country, army, order, cancelled=True)
            return
        self.state.record(
            "march_cancelled", country_id=country.country_id, army_id=army.army_id,
            reason=reason, tile=self._tile(army.tile),
        )

    def _arrive(self, country, army, order: MarchOrder) -> None:
        """The route is paid for: the army either camps on the far tile or gives battle."""
        army.march = None
        army.arrival_day = self.state.day_index
        army.arrival_sequence = order.issued_sequence
        if order.rendezvous:
            self._finish_rendezvous(country, army, order)
            return
        defender = self.state.countries.get(order.target or "")
        objective = order.objective
        # The column fights on arrival if the realm it was ordered against still holds the
        # hex — or still has an army standing on it, which is the field battle the order was
        # priced as. Either way the target has to be there when the route is paid for.
        ground = self._owner_of(objective) == order.target if objective is not None else False
        camped = (
            objective is not None and defender is not None
            and bool(self._camps_of(defender, objective))
        )
        strike = (
            order.purpose == "ATTACK" and defender is not None and objective is not None
            and order.target in country.at_war_with and (ground or camped)
        )
        self.state.record(
            "army_arrived", country_id=country.country_id, army_id=army.army_id,
            purpose=order.purpose, target=order.target, fought=strike,
            reason=None if strike or order.purpose != "ATTACK" else "抵达时已无仗可打",
            objective=self._tile(objective), tile=self._tile(order.destination),
        )
        if strike:
            center = self._city_of(defender, objective)
            if center is not None:
                # Walls are not taken by one afternoon of fighting: an army that arrives at a
                # city center starts (or joins) the blockade of those walls instead.
                rival = center.siege["attacker"] if center.siege else None
                if rival in (None, country.country_id):
                    self._begin_siege(country, defender, army, center)
                else:
                    self.state.record(
                        "siege_refused", country_id=country.country_id, army_id=army.army_id,
                        city_id=center.city_id, attacker=rival, reason="一座城同时只有一方围得",
                    )
            else:
                self._battle(country, defender, army, objective, order.expedition)

    def _desert(self, army) -> str:
        """An unpaid army sheds its costliest remaining type."""
        return max(
            army.units,
            key=lambda unit_id: (
                float(self.units[unit_id]["upkeep_per_day"]), -self.order.index(unit_id),
            ),
        )

    def upkeep_daily(self) -> None:
        for country in self.state.countries.values():
            for army in country.armies:
                if army.strength <= 0:
                    continue
                cost = army_upkeep(army.units, self.state.config)
                paid = min(country.treasury, cost)
                country.treasury -= paid
                if paid + 1e-9 < cost:
                    unit_id = self._desert(army)
                    self._apply_army_losses([army], {unit_id: 1})
                    self.state.record(
                        "army_desertion", country_id=country.country_id, army_id=army.army_id,
                        unit=unit_id, strength=country.army_strength,
                    )

    def tribute_daily(self) -> None:
        """Collect one day of occupation tribute: the payer's own treasury is the ceiling."""
        for payer in self.state.countries.values():
            if not payer.levies:
                continue
            for transfer in tribute_transfers(payer, self.state.countries, self.state.config):
                amount = min(payer.treasury, transfer["daily"])
                if amount <= 0.0:
                    continue
                payer.treasury -= amount
                self.state.countries[transfer["to"]].treasury += amount
            for levy in payer.levies:
                levy["days_left"] = int(levy["days_left"]) - 1
            payer.levies = [
                levy for levy in payer.levies
                if levy["days_left"] > 0 and levy["to"] in self.state.countries
            ]

    def _tribute(self, attacker, defender, seized: str | None) -> dict | None:
        """A captured building is worth more than its rubble: the loser pays a small annuity.

        Roads are exempt because paved ground was never exclusive — every realm already
        marches on them, so taking one transfers no income.
        """
        if seized is None or seized in self.config["capture_tribute_exempt"]:
            return None
        share = float(self.config["capture_tribute_share"])
        days = int(self.config["capture_tribute_days"])
        defender.levies.append({
            "to": attacker.country_id, "building": seized, "share": share, "days_left": days,
        })
        return {
            "from": defender.country_id, "to": attacker.country_id, "building": seized,
            "label": self.state.config["buildings"][seized]["label"],
            "share": round(share, 4), "days": days,
            "daily": round(tribute_base(defender) * share, 4),
        }

    def update_monthly(self) -> None:
        relief = self.config["war_exhaustion_relief"]
        for country in self.state.countries.values():
            if country.at_war_with:
                country.war_exhaustion += self.config["war_exhaustion_per_month"]
                country.stability = max(
                    0.0, country.stability
                    - self.config["war_stability_penalty_per_exhaustion"],
                )
            else:
                country.war_exhaustion = max(0.0, country.war_exhaustion - relief)