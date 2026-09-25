"""合约台账规则：批单登记校验、按生效日重算限额版本、减保缺口检查。

纯函数，不访问数据库；事务编排见 service/repository。
"""
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, ValidationError, number, optional_text, text


INCREASE = "increase"
DECREASE = "decrease"
ENDORSEMENT_DIRECTIONS = (INCREASE, DECREASE)
DIRECTION_LABELS = {INCREASE: "加保", DECREASE: "减保"}

# 金额以分为单位四舍五入，避免浮点误差
MONEY_EPS = 0.01


def money(value: float) -> float:
    return round(float(value) + 0.0, 2)


def parse_day(value: Any, key: str, max_today: bool = False) -> str:
    """校验 YYYY-MM-DD 日期文本，返回规范化日期串。"""
    if not isinstance(value, str):
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key)
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key) from exc
    if max_today and parsed > date.today():
        raise ValidationError("%s不能晚于今天" % key)
    return parsed.isoformat()


def signed_amount(direction: str, amount: float) -> float:
    return money(amount if direction == INCREASE else -amount)


def validate_treaty(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    text(p, "name")
    initial_limit = number(p, "initial_limit", 0)
    if initial_limit <= 0:
        raise ValidationError("initial_limit必须大于0")
    return {
        "name": text(p, "name"),
        "currency": optional_text(p, "currency", "CNY") or "CNY",
        "inception_date": parse_day(p["inception_date"] if "inception_date" in p else str(date.today()), "inception_date"),
        "initial_limit": money(initial_limit),
    }


def validate_endorsement(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    direction = p.get("direction")
    if direction not in ENDORSEMENT_DIRECTIONS:
        raise ValidationError("direction只能是increase/decrease")
    amount = number(p, "amount", 0)
    if amount <= 0:
        raise ValidationError("amount必须大于0")
    return {
        "effective_date": parse_day(p["effective_date"], "effective_date"),
        "direction": direction,
        "amount": money(amount),
        "reason": text(p, "reason"),
    }


def validate_claim(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    amount = number(p, "recoverable_amount", 0)
    if amount <= 0:
        raise ValidationError("recoverable_amount必须大于0")
    result = {
        "claim_number": text(p, "claim_number"),
        "approved_date": parse_day(p.get("approved_date", str(date.today())), "approved_date", max_today=True),
        "recoverable_amount": money(amount),
        "source_record_id": None,
    }
    source = p.get("source_record_id")
    if source is not None:
        if isinstance(source, bool) or not isinstance(source, int) or source <= 0:
            raise ValidationError("source_record_id必须是正整数")
        result["source_record_id"] = source
    return result


def build_versions(initial_limit: float, confirmed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """已确认批单按（生效日，确认顺序）排序，累计得到每版限额。"""
    versions: List[Dict[str, Any]] = []
    limit = money(initial_limit)
    ordered = sorted(confirmed, key=lambda e: (e["effective_date"], e["seq"]))
    for endorsement in ordered:
        limit = money(limit + signed_amount(endorsement["direction"], endorsement["amount"]))
        versions.append({
            "endorsement_id": endorsement["id"],
            "effective_date": endorsement["effective_date"],
            "direction": endorsement["direction"],
            "amount": money(endorsement["amount"]),
            "seq": endorsement["seq"],
            "limit_after": limit,
        })
    return versions


def occupancy_as_of(claims: List[Dict[str, Any]], day: Optional[str]) -> float:
    """截至某日（含）已核定赔案的摊回占用；day为None时统计全部。"""
    total = 0.0
    for claim in claims:
        if day is None or claim["approved_date"] <= day:
            total += float(claim["recoverable_amount"])
    return money(total)


def plan_confirm(
    treaty: Dict[str, Any],
    candidate: Dict[str, Any],
    confirmed: List[Dict[str, Any]],
    claims: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]], int]:
    """试算确认批单后的全部限额版本。

    返回：(更新后的全部版本, 候选版本, 候选之后（含候选）的版本, 候选序号)。
    减保导致任一新版本可用余额低于当时已核定摊回时抛 Conflict，并在
    details 中给出合约、缺口与占用明细。
    """
    seq = max([int(e["seq"]) for e in confirmed], default=0) + 1
    trial = list(confirmed) + [dict(candidate, seq=seq)]
    versions = build_versions(float(treaty["initial_limit"]), trial)

    candidate_version = next(v for v in versions if v["endorsement_id"] == candidate["id"])

    # 加保只可能引入负限额问题（金额异常），不会造成缺口
    if candidate_version["limit_after"] < -MONEY_EPS:
        raise ValidationError("批单后限额不能为负")

    affected: List[Dict[str, Any]] = []
    reachable = False
    for version in versions:
        if version["endorsement_id"] == candidate["id"]:
            reachable = True
        if reachable:
            affected.append(version)

    if candidate["direction"] == DECREASE:
        breaches = []
        for version in affected:
            used = occupancy_as_of(claims, version["effective_date"])
            gap = money(used - version["limit_after"])
            if gap > MONEY_EPS:
                breaches.append((gap, version, used))
        if breaches:
            gap, version, used = max(breaches, key=lambda item: item[0])
            detail_claims = [
                {
                    "claim_number": c["claim_number"],
                    "approved_date": c["approved_date"],
                    "recoverable_amount": money(c["recoverable_amount"]),
                }
                for c in sorted(claims, key=lambda c: (c["approved_date"], c["id"]))
                if c["approved_date"] <= version["effective_date"]
            ]
            raise Conflict(
                "减保后可用余额低于已核定摊回，缺口%s" % format_amount(gap),
                {
                    "type": "capacity_shortfall",
                    "treaty_id": treaty["id"],
                    "treaty_reference": treaty["reference"],
                    "treaty_name": treaty["name"],
                    "endorsement_id": candidate["id"],
                    "effective_date": version["effective_date"],
                    "limit_after": version["limit_after"],
                    "used_approved_recoveries": used,
                    "available": money(version["limit_after"] - used),
                    "shortfall": gap,
                    "occupancies": detail_claims,
                },
            )

    snapshot_versions = []
    for version in versions:
        used = occupancy_as_of(claims, version["effective_date"])
        snapshot_versions.append({**version, "used_approved_recoveries": used})
    return snapshot_versions, candidate_version, affected, seq


def format_amount(value: float) -> str:
    return "{:,.2f}".format(money(value))


def endorsement_summary(endorsement: Dict[str, Any]) -> str:
    label = DIRECTION_LABELS.get(endorsement["direction"], endorsement["direction"])
    return "%s批单生效，%s金额%s" % (endorsement["effective_date"], label, format_amount(endorsement["amount"]))
