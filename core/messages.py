"""Event-driven, short vernacular ruler messages for the live dashboard."""
from __future__ import annotations

from typing import Any

RESEARCH_LABELS = {
    "bronze_metallurgy": "科技",
    "writing": "政治",
    "agriculture": "经济",
}
HAZARD_LABELS = {"flood": "洪水", "drought": "干旱"}
BIOME_LABELS = {
    "grassland": "草原", "forest": "森林", "rainforest": "雨林", "savanna": "稀树草原",
    "steppe": "半干旱草原", "wetland": "湿地", "desert": "沙漠", "alpine": "高山",
    "tundra": "冻原", "ocean": "海洋",
}


def _country_name(state, country_id: str) -> str:
    country = state.countries.get(country_id)
    return country.name if country else country_id


def _building_name(state, building: str) -> str:
    return state.config.get("buildings", {}).get(building, {}).get("label", building)


def _unit_name(state, unit_id: str) -> str:
    return state.config.get("units", {}).get(unit_id, {}).get("label", unit_id)


def _loss_text(state, units: dict[str, Any]) -> str:
    if not units:
        return "无人阵亡"
    return "、".join(f"{_unit_name(state, unit_id)}×{count}" for unit_id, count in units.items())


def _messages_for_event(state, event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one or more candidate messages; the composer picks one per country."""
    kind = event.get("type")
    if kind == "war_declared":
        attacker, defender = event.get("attacker"), event.get("defender")
        return [
            {
                "country_id": attacker, "priority": 100, "title": "边境开战", "mood": "警觉",
                "text": f"我已经向{_country_name(state, defender)}宣战。先把边境的军队和粮草调过去。",
            },
            {
                "country_id": defender, "priority": 100, "title": "遭到宣战", "mood": "警觉",
                "text": f"{_country_name(state, attacker)}向我们宣战了。边境进入戒备，先稳住民心。",
            },
        ]
    if kind == "battle":
        attacker, defender = event.get("attacker"), event.get("defender")
        captured = bool(event.get("captured"))
        ground = BIOME_LABELS.get(event.get("biome"), "边境")
        attacker_losses = _loss_text(state, event.get("attacker_units") or {})
        defender_losses = _loss_text(state, event.get("defender_units") or {})
        mass = len(event.get("attacker_mass") or [])
        mass_text = f"{mass} 支军队在同一缺口集结，" if captured and mass > 1 else ""
        levy = event.get("tribute") or {}
        levy_share = f"{levy.get('share', 0.0) * 100:.0f}%"
        for_attacker = (
            f"那儿的{levy.get('label')}此后 {levy.get('days')} 天要向我们交出其收入的 {levy_share}"
            f"（约每日 {levy.get('daily')}）。" if levy else ""
        )
        for_defender = (
            f"我们的{levy.get('label')}落进了敌手，此后 {levy.get('days')} 天要白交收入的 {levy_share}"
            f"（每日约 {levy.get('daily')}）。" if levy else ""
        )
        if captured:
            return [
                {
                    "country_id": attacker, "priority": 95, "title": "边境得手", "mood": "振奋",
                    "text": f"刚从{_country_name(state, defender)}手里拿下一块{ground}。{mass_text}我军{attacker_losses}，补给要跟上。{for_attacker}",
                },
                {
                    "country_id": defender, "priority": 95, "title": "边境失地", "mood": "焦虑",
                    "text": f"{_country_name(state, attacker)}夺走了我们一块{ground}，{defender_losses}。先稳住阵脚，别让恐慌蔓延。{for_defender}",
                },
            ]
        return [
            {
                "country_id": attacker, "priority": 92, "title": "边境交火", "mood": "谨慎",
                "text": f"在{ground}和{_country_name(state, defender)}打了一仗，谁也没占到便宜，我军{attacker_losses}。先补兵。",
            },
            {
                "country_id": defender, "priority": 92, "title": "边境交火", "mood": "谨慎",
                "text": f"{_country_name(state, attacker)}在{ground}试探我们，{defender_losses}。守住阵线，别贸然追击。",
            },
        ]
    if kind == "siege_started":
        attacker, defender = event.get("attacker"), event.get("defender")
        days = event.get("days_left")
        promise = f"按圈内现有兵力约 {days} 天城破" if days else "圈里暂时凑不出兵力，账不会动"
        wall = "他们那儿还有城墙，得多啃几天。" if event.get("walls") else "中心格还没起城墙。"
        return [
            {
                "country_id": attacker, "priority": 94, "title": "围城开局", "mood": "谨慎",
                "text": f"我军已经站进{_country_name(state, defender)}的{event.get('city', '城池')}封锁圈，账开下来了：{promise}。{wall}兵不能撤，撤了这账就作废。",
            },
            {
                "country_id": defender, "priority": 96, "title": "城池被围", "mood": "焦虑",
                "text": f"{_country_name(state, attacker)}围住了{event.get('city', '城池')}，{promise}。要解围就得把圈里的兵打掉或夺回那一格，不然只能先求和——野地打赢一仗不会让墙停下来。",
            },
        ]
    if kind == "city_fallen":
        attacker, defender = event.get("attacker"), event.get("defender")
        seized = "、".join(_building_name(state, building) for building in event.get("buildings") or []) or "空地"
        levy = event.get("tribute") or []
        paid = f"往后 {levy[0]['days']} 天还要交其收入的 {levy[0]['share'] * 100:.0f}%。" if levy else ""
        return [
            {
                "country_id": attacker, "priority": 98, "title": "敌城陷落", "mood": "振奋",
                "text": f"{event.get('city', '城池')}整座归了我们：{event.get('tiles')} 格城域、{event.get('population')} 口人、{seized}都换了主人。{paid}迁入的民政要先接住。",
            },
            {
                "country_id": defender, "priority": 99, "title": "城池易主", "mood": "焦虑",
                "text": f"{event.get('city', '城池')}守不住了，{event.get('tiles')} 格城域和 {event.get('population')} 口人一起落到{_country_name(state, attacker)}手里。民心已经跌下去，先稳住剩下的城。",
            },
        ]
    if kind == "realm_annexed":
        attacker = event.get("attacker")
        cities = len(event.get("cities") or [])
        return [
            {
                "country_id": attacker, "priority": 100, "title": "敌国覆灭", "mood": "振奋",
                "text": (
                    f"{event.get('name', '敌国')}的都城一破就散了架：{event.get('tiles')} 格土地、"
                    f"{cities} 座城、{event.get('population')} 口人整建制归了我们，库里的 "
                    f"{event.get('treasury')} 财政与 {event.get('food')} 粮草也搬进我们的账上。"
                    "它已经不在这张地图上了——留下的兵营明天就能募兵，但别国也会学这一手。"
                ),
            },
        ]
    if kind == "peace_accepted":
        sender, target = event.get("sender"), event.get("target")
        return [
            {
                "country_id": sender, "priority": 78, "title": "停战", "mood": "沉稳",
                "text": f"和{_country_name(state, target)}停战了。先把战争疲劳压下去，恢复生产。",
            },
            {
                "country_id": target, "priority": 78, "title": "停战", "mood": "沉稳",
                "text": f"和{_country_name(state, sender)}停战了。边境终于能喘口气。",
            },
        ]
    if kind in {"trade_accepted", "pact_accepted"}:
        sender, target = event.get("sender"), event.get("target")
        label = "贸易协定" if kind == "pact_accepted" else "一笔交易"
        return [
            {
                "country_id": sender, "priority": 55, "title": "生意谈成", "mood": "沉稳",
                "text": f"和{_country_name(state, target)}的{label}谈成了，粮和钱都动了起来。",
            },
            {
                "country_id": target, "priority": 55, "title": "生意谈成", "mood": "沉稳",
                "text": f"和{_country_name(state, sender)}的{label}谈成了，关系也缓和了一些。",
            },
        ]
    if kind == "diplomatic_gift":
        sender, target = event.get("sender"), event.get("target")
        return [
            {
                "country_id": sender, "priority": 50, "title": "外交赠礼", "mood": "沉稳",
                "text": f"给{_country_name(state, target)}送了礼，先把关系稳住。",
            },
            {
                "country_id": target, "priority": 50, "title": "收到赠礼", "mood": "沉稳",
                "text": f"{_country_name(state, sender)}送来礼物，关系缓和了一些。",
            },
        ]
    if kind == "city_founded":
        cid = event.get("country_id")
        return [{
            "country_id": cid, "priority": 68, "title": "建立新城", "mood": "振奋",
            "text": f"{event.get('name', '新地')}已经立起来了。先解决住房和粮食，再谈发展。",
        }]
    if kind == "construction_completed":
        cid = event.get("country_id")
        return [{
            "country_id": cid, "priority": 42, "title": "工程完工", "mood": "沉稳",
            "text": f"{_building_name(state, event.get('building', ''))}完工了，终于能派上用场。",
        }]
    if kind == "research_completed":
        cid = event.get("country_id")
        field = RESEARCH_LABELS.get(event.get("field"), event.get("field", "研究"))
        return [{
            "country_id": cid, "priority": 48, "title": "研究进展", "mood": "沉稳",
            "text": f"研究有了进展：{field}到了{event.get('level')}级。下一步要把它变成实际能力。",
        }]
    if kind == "food_shortage":
        cid = event.get("country_id")
        return [{
            "country_id": cid, "priority": 90, "title": "粮食告急", "mood": "焦虑",
            "text": "粮仓见底了，先保都城和军队。再不想办法，人口会掉。",
        }]
    if kind == "stability_crisis":
        cid = event.get("country_id")
        return [{
            "country_id": cid, "priority": 84, "title": "社会不稳", "mood": "焦虑",
            "text": "社会不稳，先稳住粮价和税负，别让局面继续滑下去。",
        }]
    if kind == "city_unrest":
        cid = event.get("country_id")
        city = event.get("city_name", "城市")
        return [{
            "country_id": cid, "priority": 82, "title": "治安事件", "mood": "警觉",
            "text": f"{city}出了治安事件，先压住乱局，再查原因。",
        }]
    if kind == "city_epidemic":
        cid = event.get("country_id")
        city = event.get("city_name", "城市")
        return [{
            "country_id": cid, "priority": 88, "title": "疫病", "mood": "焦虑",
            "text": f"{city}爆发疫病，预算得往卫生上挪一点，先保住人口。",
        }]
    if kind == "army_recruited":
        cid = event.get("country_id")
        unit = _unit_name(state, event.get("unit", ""))
        return [{
            "country_id": cid, "priority": 45, "title": "招募军队", "mood": "谨慎",
            "text": f"又募了{event.get('amount', 1)}支{unit}。军费不低，但边境不能空着。",
        }]
    if kind == "barracks_recruit":
        cid = event.get("country_id")
        unit = _unit_name(state, event.get("unit", ""))
        return [{
            "country_id": cid, "priority": 42, "title": "兵营募兵", "mood": "踏实",
            "text": f"兵营练出一支{unit}，训练花财政，成兵后只吃粮。",
        }]
    if kind == "army_desertion":
        cid = event.get("country_id")
        unit = _unit_name(state, event.get("unit", ""))
        return [{
            "country_id": cid, "priority": 72, "title": "军队逃散", "mood": "焦虑",
            "text": f"粮仓供不上口粮，跑掉了{unit}。得先稳住军队。",
        }]
    if kind == "hazard_impact":
        cid = event.get("country_id")
        hazard = HAZARD_LABELS.get(event.get("hazard_type"), event.get("hazard_type", "灾害"))
        return [{
            "country_id": cid, "priority": 70, "title": hazard, "mood": "焦虑",
            "text": f"这场{hazard}让粮食受了损失，得补仓，别让缺口扩大。",
        }]
    return []


def compose_ruler_messages(
    state,
    new_events: list[dict[str, Any]],
    last_message_days: dict[str, int],
    player_country: str | None = None,
    cooldown_days: int = 12,
) -> list[dict[str, Any]]:
    """Choose at most one highest-priority message per country from a batch."""
    candidates: dict[str, dict[str, Any]] = {}
    for event in new_events:
        for message in _messages_for_event(state, event):
            message["source"] = event.get("type", "event")
            cid = message.get("country_id")
            if not cid or cid not in state.countries or cid == player_country:
                continue
            current = candidates.get(cid)
            if current is None or message["priority"] > current["priority"]:
                candidates[cid] = message
    chosen = []
    for cid, message in candidates.items():
        last_day = last_message_days.get(cid, -10_000)
        if (
            message["priority"] < 90
            and state.day_index - last_day < cooldown_days
        ):
            continue
        chosen.append(message)
        last_message_days[cid] = state.day_index
    return chosen


def record_ruler_messages(state, messages: list[dict[str, Any]]) -> None:
    for message in messages:
        state.record(
            "ruler_message",
            country_id=message["country_id"],
            title=message["title"],
            mood=message["mood"],
            text=message["text"],
            priority=message["priority"],
            source=message.get("source", "event"),
            provider="AI 短讯",
        )
