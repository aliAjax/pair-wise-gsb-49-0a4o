"""再保险合约与巨灾暴露管理领域规则与状态转换。"""
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "quoted"
CREATE_ROLES = {'underwriter'}
ENDORSE_ROLES = {'underwriter'}
APPROVED_STATES = {'calculated', 'settled'}
ENDORSEMENT_DIRECTIONS = ['increase', 'decrease']
DIRECTION_LABELS = {'increase': '加保', 'decrease': '减保'}
ACTION_ROLES = {'bind': {'underwriter'}, 'submit_claim': {'claims_officer'}, 'calculate': {'claims_officer'}, 'settle': {'finance'}, 'reject': {'finance', 'claims_officer'}}
TRANSITIONS = {'bind': {'quoted': 'bound'}, 'submit_claim': {'bound': 'claim_submitted'}, 'calculate': {'claim_submitted': 'calculated'}, 'settle': {'calculated': 'settled'}, 'reject': {'claim_submitted': 'rejected', 'calculated': 'rejected'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_endorse(self, role: str) -> bool:
        return role == "admin" or role in ENDORSE_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "event_id")
        attachment = number(p, "attachment", 0)
        limit = number(p, "limit", 0)
        number(p, "cession_pct", 0, 1)
        number(p, "loss_amount", 0)
        number(p, "reinstatement_pct", 0, 1)
        number(p, "aggregate_prior", 0)
        if limit <= attachment:
            raise ValidationError("赔款限额必须高于起赔点")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        width = float(p["limit"]) - float(p["attachment"])
        retained_loss = max(0.0, float(p["loss_amount"]) - float(p["attachment"]))
        recovery = min(retained_loss, width) * float(p["cession_pct"])
        p["layer_width"] = round(width, 2)
        p["recoverable_amount"] = round(recovery, 2)
        p["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
        p["net_retention"] = round(float(p["loss_amount"]) - recovery, 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        event_id = payload.get("event_id")
        used = float(payload.get("aggregate_prior", 0))
        for item in existing:
            if item["state"] in {"rejected"} or item["payload"].get("event_id") != event_id:
                continue
            used += float(item["payload"].get("recoverable_amount", 0))
        capacity = float(payload["layer_width"]) * float(payload["cession_pct"])
        projected = min(max(0.0, float(payload["loss_amount"]) - float(payload["attachment"])), float(payload["layer_width"])) * float(payload["cession_pct"])
        if used + projected > capacity + 0.01:
            raise Conflict("同一事件累计摊回超过再保容量")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "bind":
            changes["bound_by"] = text(data, "underwriter_id")
            summary = "再保合约已绑定"
        elif action == "submit_claim":
            changes["claim_number"] = text(data, "claim_number")
            changes["claim_event_id"] = text(data, "event_id")
            summary = "赔案已提交"
        elif action == "calculate":
            loss = number(data, "approved_loss", 0)
            width = float(p["layer_width"])
            recovery = min(max(0.0, loss - float(p["attachment"])), width) * float(p["cession_pct"])
            changes["approved_loss"] = loss
            changes["recoverable_amount"] = round(recovery, 2)
            changes["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
            summary = "摊回金额已计算"
        elif action == "settle":
            if float(p["recoverable_amount"]) <= 0:
                raise ValidationError("无可结算摊回")
            changes["payment_reference"] = text(data, "payment_reference")
            summary = "摊回赔款已结算"
        elif action == "reject":
            changes["reject_reason"] = text(data, "reject_reason")
            summary = "赔案已拒绝"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def validate_endorsement(self, data: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(data or {})
        effective_date = text(p, "effective_date")
        try:
            effective_date = datetime.strptime(effective_date, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ValidationError("effective_date必须是YYYY-MM-DD格式的有效日期") from exc
        direction = choice(p, "direction", ENDORSEMENT_DIRECTIONS)
        amount = number(p, "amount", 0)
        if amount <= 0:
            raise ValidationError("amount必须大于0")
        reason = text(p, "reason")
        return {"effective_date": effective_date, "direction": direction, "amount": round(amount, 2), "reason": reason}

    def occupancy(self, record: Dict[str, Any]) -> Tuple[float, List[Dict[str, Any]]]:
        """已核定摊回占用：期初累计占用加已核定/已结算赔案的摊回金额。"""
        p = record["payload"]
        used = 0.0
        items: List[Dict[str, Any]] = []
        prior = round(float(p.get("aggregate_prior", 0) or 0), 2)
        if prior > 0:
            items.append({"kind": "aggregate_prior", "label": "期初累计占用", "amount": prior})
            used += prior
        if record["state"] in APPROVED_STATES:
            recovery = round(float(p.get("recoverable_amount", 0) or 0), 2)
            if recovery > 0:
                items.append({
                    "kind": "claim",
                    "label": "已核定赔案摊回",
                    "claim_number": p.get("claim_number", ""),
                    "approved_loss": p.get("approved_loss"),
                    "state": record["state"],
                    "amount": recovery,
                })
                used += recovery
        return round(used, 2), items

    def project_endorsement(self, record: Dict[str, Any], endorsements: List[Dict[str, Any]], new_id: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """按生效先后重算每版限额；减保后可用余额低于已核定摊回时抛Conflict。"""
        p = record["payload"]
        used, occupancy = self.occupancy(record)
        attachment = float(p["attachment"])
        cession = float(p["cession_pct"])
        limit = float(p["limit"])
        versions: List[Dict[str, Any]] = []
        for item in sorted(endorsements, key=lambda e: (e["effective_date"], e["id"])):
            delta = float(item["amount"]) if item["direction"] == "increase" else -float(item["amount"])
            limit = round(limit + delta, 2)
            capacity = round(max(0.0, limit - attachment) * cession, 2)
            versions.append({
                "id": item["id"],
                "effective_date": item["effective_date"],
                "direction": item["direction"],
                "amount": round(float(item["amount"]), 2),
                "reason": item["reason"],
                "resulting_limit": limit,
                "capacity": capacity,
                "used_amount": used,
                "remaining_amount": round(capacity - used, 2),
            })
        position = next(i for i, version in enumerate(versions) if version["id"] == new_id)
        current = versions[position]
        if current["direction"] == "decrease":
            affected = versions[position:]
            floor = min(version["resulting_limit"] for version in affected)
            if floor <= attachment:
                raise Conflict("减保后限额不能低于起赔点", details={
                    "contract": record["reference"],
                    "record_id": record["id"],
                    "projected_limit": floor,
                    "attachment": attachment,
                    "occupancy": occupancy,
                })
            shortfall = min(version["remaining_amount"] for version in affected)
            if shortfall < -0.01:
                raise Conflict("减保后可用余额低于已核定摊回", details={
                    "contract": record["reference"],
                    "record_id": record["id"],
                    "gap": round(-shortfall, 2),
                    "used": used,
                    "projected_limit": floor,
                    "occupancy": occupancy,
                })
        final = versions[-1]
        label = DIRECTION_LABELS.get(current["direction"], current["direction"])
        snapshot = {
            "endorsement_id": new_id,
            "summary": "批单已登记：{}{:,.2f}，生效{}，当前限额{:,.2f}，剩余{:,.2f}".format(label, current["amount"], current["effective_date"], final["resulting_limit"], final["remaining_amount"]),
            "current_limit": final["resulting_limit"],
            "capacity": final["capacity"],
            "used": used,
            "remaining": final["remaining_amount"],
            "occupancy": occupancy,
        }
        return versions, snapshot

    def ledger_view(self, record: Dict[str, Any], endorsements: List[Dict[str, Any]], snapshots: List[Dict[str, Any]]) -> Dict[str, Any]:
        """合约台账：当前限额、已核定占用和剩余承载力，附批单历史与余额快照。"""
        p = record["payload"]
        used, occupancy = self.occupancy(record)
        attachment = float(p["attachment"])
        cession = float(p["cession_pct"])
        base_limit = float(p["limit"])
        current_limit = round(float(endorsements[-1]["resulting_limit"]), 2) if endorsements else base_limit
        capacity = round(max(0.0, current_limit - attachment) * cession, 2)
        return {
            "record_id": record["id"],
            "reference": record["reference"],
            "state": record["state"],
            "event_id": p.get("event_id", ""),
            "base_limit": base_limit,
            "attachment": attachment,
            "cession_pct": cession,
            "current_limit": current_limit,
            "capacity": capacity,
            "used": used,
            "remaining": round(capacity - used, 2),
            "occupancy": occupancy,
            "endorsements": list(endorsements),
            "snapshots": list(snapshots),
        }
