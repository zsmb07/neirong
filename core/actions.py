"""Strict, JSON-only action protocol shared by heuristic and future LLM agents."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

PUBLIC_ACTIONS = {
    "BUILD",
    "ALLOCATE_WORKERS",
    "SET_TAX",
    "SET_RESEARCH",
    "TRADE",
    "SET_POLICY",
    "SETTLE",
    "DIPLOMACY",
    "MILITARY",
    "SET_CITY_FOCUS",
    "SET_CITY_ZONE",
    "SET_CITY_GOVERNANCE",
    "SET_CITY_BUDGET",
}
INTERNAL_ACTIONS = {"NO_OP"}
ALL_ACTIONS = PUBLIC_ACTIONS | INTERNAL_ACTIONS


class ActionRejected(ValueError):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


@dataclass
class ActionResult:
    action_id: str
    actor: str
    action_type: str
    accepted: bool
    reason_code: str
    reason_text: str
    applied_day: int
    effects: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "actor": self.actor,
            "action_type": self.action_type,
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "effects": self.effects,
            "applied_day": self.applied_day,
        }


def _keys(action: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(action) - allowed
    if unknown:
        raise ActionRejected("INVALID_SCHEMA", f"未知动作字段: {sorted(unknown)}")


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionRejected("INVALID_PARAMETER", f"{name} 必须是数值")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ActionRejected(
            "INVALID_PARAMETER", f"{name} 必须在 [{minimum}, {maximum}] 内且为有限数"
        )
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionRejected("INVALID_PARAMETER", f"{name} 必须是整数")
    return value


def validate_action(actor: str, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(actor, str) or not actor:
        raise ActionRejected("INVALID_ACTOR", "缺少动作提交国")
    if not isinstance(raw, dict):
        raise ActionRejected("INVALID_SCHEMA", "动作必须是 JSON 对象")
    action_type = raw.get("type")
    if not isinstance(action_type, str) or action_type not in ALL_ACTIONS:
        raise ActionRejected("UNKNOWN_ACTION", f"不支持的动作类型: {action_type!r}")
    _keys(raw, {"type", "tile", "building", "agriculture", "mining", "construction",
                "rate", "field", "priority", "operation", "target", "offer_food",
                "request_treasury", "proposal_id", "amount", "pact", "strength", "unit",
                "focus", "city_id", "zone", "managed_by", "budget", "army_id", "recruiting", "host_id", "units", "orders"})

    if action_type == "NO_OP":
        _keys(raw, {"type"})
        return {"type": action_type}
    if action_type == "BUILD":
        _keys(raw, {"type", "tile", "building"})
        tile = raw.get("tile")
        if not isinstance(tile, dict) or set(tile) != {"col", "row"}:
            raise ActionRejected("INVALID_PARAMETER", "tile 必须仅含 col、row 整数")
        building = raw.get("building")
        if not isinstance(building, str) or building not in config["buildings"]:
            raise ActionRejected("INVALID_PARAMETER", f"未知建筑: {building!r}")
        return {
            "type": action_type,
            "tile": {"col": _integer(tile["col"], "col"), "row": _integer(tile["row"], "row")},
            "building": building,
        }
    if action_type == "SETTLE":
        _keys(raw, {"type", "tile"})
        tile = raw.get("tile")
        if not isinstance(tile, dict) or set(tile) != {"col", "row"}:
            raise ActionRejected("INVALID_PARAMETER", "tile 必须仅含 col、row 整数")
        return {
            "type": action_type,
            "tile": {
                "col": _integer(tile["col"], "col"),
                "row": _integer(tile["row"], "row"),
            },
        }
    if action_type == "SET_CITY_GOVERNANCE":
        _keys(raw, {"type", "city_id", "managed_by"})
        city_id = raw.get("city_id")
        managed_by = raw.get("managed_by")
        if not isinstance(city_id, str) or not city_id:
            raise ActionRejected("INVALID_PARAMETER", "city_id 必须是非空字符串")
        if managed_by not in {"ai", "manual", "llm"}:
            raise ActionRejected("INVALID_PARAMETER", "managed_by 只能是 ai、manual 或 llm")
        return {"type": action_type, "city_id": city_id, "managed_by": managed_by}
    if action_type == "SET_CITY_ZONE":
        _keys(raw, {"type", "city_id", "tile", "zone"})
        tile = raw.get("tile")
        if not isinstance(tile, dict) or set(tile) != {"col", "row"}:
            raise ActionRejected("INVALID_PARAMETER", "tile 必须仅含 col、row 整数")
        if not isinstance(raw.get("zone"), str):
            raise ActionRejected("INVALID_PARAMETER", "zone 必须是字符串")
        return {
            "type": action_type,
            "city_id": str(raw.get("city_id", "")),
            "tile": {"col": _integer(tile["col"], "col"), "row": _integer(tile["row"], "row")},
            "zone": raw["zone"],
        }
    if action_type == "SET_CITY_FOCUS":
        _keys(raw, {"type", "city_id", "focus"})
        city_id = raw.get("city_id")
        focus = raw.get("focus")
        if not isinstance(city_id, str) or not city_id:
            raise ActionRejected("INVALID_PARAMETER", "city_id 必须是非空字符串")
        if not isinstance(focus, str) or not focus:
            raise ActionRejected("INVALID_PARAMETER", "focus 必须是非空字符串")
        return {"type": action_type, "city_id": city_id, "focus": focus}
    if action_type == "SET_CITY_BUDGET":
        _keys(raw, {"type", "city_id", "budget"})
        city_id = raw.get("city_id")
        budget = raw.get("budget")
        if not isinstance(city_id, str) or not city_id:
            raise ActionRejected("INVALID_PARAMETER", "city_id 必须是非空字符串")
        if budget not in {"austerity", "balanced", "investment"}:
            raise ActionRejected("INVALID_PARAMETER", "budget 只能是紧缩、均衡或投资")
        return {"type": action_type, "city_id": city_id, "budget": budget}
    if action_type == "MILITARY":
        _keys(raw, {"type", "operation", "tile", "strength", "target", "unit",
                    "army_id", "recruiting", "host_id", "units", "orders"})
        operation = raw.get("operation")
        army_id = raw.get("army_id")
        if army_id is not None and (not isinstance(army_id, str) or not army_id):
            raise ActionRejected("INVALID_PARAMETER", "army_id 必须是非空字符串或省略")
        if operation == "DISPATCH":
            _keys(raw, {"type", "operation", "orders"})
            orders = raw.get("orders")
            if not isinstance(orders, list) or not 1 <= len(orders) <= 200:
                raise ActionRejected("INVALID_PARAMETER", "请选择 1 至 200 支军队")
            if any(not isinstance(o, dict) or o.get("type") != "MILITARY"
                   or o.get("operation") not in ("MARCH", "ATTACK", "RENDEZVOUS")
                   for o in orders):
                raise ActionRejected("INVALID_PARAMETER", "派兵只接受驻扎、攻打或会师")
            validated = [validate_action(actor, o, config) for o in orders]
            ids = [o.get("army_id") for o in validated]
            if None in ids or len(set(ids)) != len(ids):
                raise ActionRejected("INVALID_PARAMETER", "每支军队只能选择一次")
            return {"type": action_type, "operation": operation, "orders": validated}
        if operation == "RENDEZVOUS":
            _keys(raw, {"type", "operation", "army_id", "host_id", "units"})
            host = raw.get("host_id")
            units = raw.get("units")
            if not army_id or not isinstance(host, str) or not host or not isinstance(units, dict):
                raise ActionRejected("INVALID_PARAMETER", "会师需要源军、宿主和兵种数量")
            if set(units) - config["units"].keys():
                raise ActionRejected("INVALID_PARAMETER", "会师包含未知兵种")
            return {"type": action_type, "operation": operation, "army_id": army_id,
                    "host_id": host, "units": {k: _integer(v, k) for k, v in units.items()}}
        if operation == "ATTACK":
            _keys(raw, {"type", "operation", "target", "army_id", "tile"})
            target = raw.get("target")
            if not isinstance(target, str) or not target:
                raise ActionRejected("INVALID_PARAMETER", "target 必须是国家 ID")
            tile = raw.get("tile")
            if tile is None:
                objective = None
            else:
                if not isinstance(tile, dict) or set(tile) != {"col", "row"}:
                    raise ActionRejected("INVALID_PARAMETER", "tile 必须仅含 col、row 整数，或省略")
                objective = {"col": _integer(tile["col"], "col"),
                             "row": _integer(tile["row"], "row")}
            return {
                "type": action_type, "operation": operation,
                "target": target, "army_id": army_id, "tile": objective,
            }
        if operation == "RECRUIT":
            _keys(raw, {"type", "operation", "unit", "strength", "army_id"})
            unit = raw.get("unit")
            if not isinstance(unit, str) or unit not in config["units"]:
                raise ActionRejected("INVALID_PARAMETER", f"未知兵种: {unit!r}")
            return {
                "type": action_type, "operation": operation, "unit": unit,
                "strength": _integer(raw.get("strength"), "strength"), "army_id": army_id,
            }
        if operation == "MARCH":
            _keys(raw, {"type", "operation", "tile", "army_id"})
            tile = raw.get("tile")
            if not isinstance(tile, dict) or set(tile) != {"col", "row"}:
                raise ActionRejected("INVALID_PARAMETER", "tile 必须仅含 col、row 整数")
            return {
                "type": action_type, "operation": operation,
                "tile": {"col": _integer(tile["col"], "col"), "row": _integer(tile["row"], "row")},
                "army_id": army_id,
            }
        if operation == "SET_RECRUITING":
            _keys(raw, {"type", "operation", "army_id", "recruiting"})
            recruiting = raw.get("recruiting")
            if not isinstance(recruiting, bool):
                raise ActionRejected("INVALID_PARAMETER", "recruiting 必须是布尔值")
            return {
                "type": action_type, "operation": operation,
                "army_id": army_id, "recruiting": recruiting,
            }
        if operation == "SET_RECRUIT_UNIT":
            _keys(raw, {"type", "operation", "army_id", "unit"})
            unit = raw.get("unit")
            if not isinstance(unit, str) or unit not in config["units"]:
                raise ActionRejected("INVALID_PARAMETER", f"未知兵种: {unit!r}")
            return {"type": action_type, "operation": operation, "army_id": army_id, "unit": unit}
        raise ActionRejected("INVALID_PARAMETER", "未知军事操作")
    if action_type == "DIPLOMACY":
        _keys(raw, {"type", "operation", "target", "amount", "pact", "proposal_id"})
        operation = raw.get("operation")
        if operation in {"DECLARE_WAR", "PROPOSE_PEACE"}:
            _keys(raw, {"type", "operation", "target"})
            target = raw.get("target")
            if not isinstance(target, str) or not target:
                raise ActionRejected("INVALID_PARAMETER", "target 必须是国家 ID")
            return {"type": action_type, "operation": operation, "target": target}
        if operation == "GIFT":
            _keys(raw, {"type", "operation", "target", "amount"})
            target = raw.get("target")
            if not isinstance(target, str) or not target:
                raise ActionRejected("INVALID_PARAMETER", "target 必须是国家 ID")
            return {
                "type": action_type,
                "operation": operation,
                "target": target,
                "amount": _number(raw.get("amount"), "amount", 0.001, 1000.0),
            }
        if operation == "PROPOSE_PACT":
            _keys(raw, {"type", "operation", "target", "pact"})
            target = raw.get("target")
            pact = raw.get("pact")
            if not isinstance(target, str) or not target or not isinstance(pact, str):
                raise ActionRejected("INVALID_PARAMETER", "target 与 pact 必须是字符串")
            if pact not in {"TRADE"}:
                raise ActionRejected("INVALID_PARAMETER", "当前只支持 TRADE 贸易协定")
            return {"type": action_type, "operation": operation, "target": target, "pact": pact}
        if operation in {"ACCEPT_PACT", "REJECT_PACT"}:
            _keys(raw, {"type", "operation", "proposal_id"})
            proposal_id = raw.get("proposal_id")
            if not isinstance(proposal_id, str) or not proposal_id:
                raise ActionRejected("INVALID_PARAMETER", "proposal_id 必须是非空字符串")
            return {"type": action_type, "operation": operation, "proposal_id": proposal_id}
        raise ActionRejected("INVALID_PARAMETER", "未知外交操作")
    if action_type == "ALLOCATE_WORKERS":
        _keys(raw, {"type", "agriculture", "mining", "construction"})
        values = {
            key: _number(raw.get(key), key, 0.0, 1.0)
            for key in ("agriculture", "mining", "construction")
        }
        if abs(sum(values.values()) - 1.0) > 1e-9:
            raise ActionRejected("INVALID_ALLOCATION", "三项劳动力比例之和必须等于 1")
        return {"type": action_type, **values}
    if action_type == "SET_TAX":
        _keys(raw, {"type", "rate"})
        return {
            "type": action_type,
            "rate": _number(raw.get("rate"), "rate", 0.0, config["maximum_tax_rate"]),
        }
    if action_type == "SET_RESEARCH":
        _keys(raw, {"type", "field"})
        field_name = raw.get("field")
        if not isinstance(field_name, str) or field_name not in config["research_fields"]:
            raise ActionRejected("INVALID_PARAMETER", f"未知研究领域: {field_name!r}")
        return {"type": action_type, "field": field_name}
    if action_type == "SET_POLICY":
        _keys(raw, {"type", "priority"})
        priority = raw.get("priority")
        if not isinstance(priority, str) or priority not in config["policies"]:
            raise ActionRejected("INVALID_PARAMETER", f"未知政策优先级: {priority!r}")
        return {"type": action_type, "priority": priority}
    if action_type == "TRADE":
        _keys(raw, {"type", "operation", "target", "offer_food", "request_treasury", "proposal_id"})
        operation = raw.get("operation", "PROPOSE")
        if not isinstance(operation, str) or operation not in {"PROPOSE", "ACCEPT", "REJECT"}:
            raise ActionRejected("INVALID_PARAMETER", f"未知交易操作: {operation!r}")
        if operation in {"ACCEPT", "REJECT"}:
            _keys(raw, {"type", "operation", "proposal_id"})
            proposal_id = raw.get("proposal_id")
            if not isinstance(proposal_id, str) or not proposal_id:
                raise ActionRejected("INVALID_PARAMETER", "proposal_id 必须是非空字符串")
            return {"type": action_type, "operation": operation, "proposal_id": proposal_id}
        _keys(raw, {"type", "operation", "target", "offer_food", "request_treasury"})
        target = raw.get("target")
        if not isinstance(target, str) or not target:
            raise ActionRejected("INVALID_PARAMETER", "target 必须是国家 ID")
        return {
            "type": action_type,
            "operation": operation,
            "target": target,
            "offer_food": _number(raw.get("offer_food"), "offer_food", 0.001, 1_000_000.0),
            "request_treasury": _number(
                raw.get("request_treasury"), "request_treasury", 0.001, 1_000_000.0
            ),
        }
    raise AssertionError("unreachable")
