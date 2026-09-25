"""业务用例编排、权限检查与审计。"""
from datetime import date
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .ledger_rules import (
    build_versions,
    endorsement_summary,
    format_amount,
    money,
    occupancy_as_of,
    plan_confirm,
    validate_claim,
    validate_endorsement,
    validate_treaty,
)
from .repository import Repository
from .rules import DomainRules


LEDGER_WRITE_ROLES = {'underwriter', 'admin'}
CLAIM_WRITE_ROLES = {'claims_officer', 'admin'}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ----- 合约台账 -----

    @staticmethod
    def _require_any(actor: Actor, roles: set) -> None:
        if actor.role not in roles:
            raise PermissionDenied("角色无权操作合约台账")

    def _ledger_bundle(self, treaty: Dict[str, Any]) -> Dict[str, Any]:
        treaty_id = int(treaty["id"])
        endorsements = self.repository.list_endorsements(treaty_id)
        claims = self.repository.list_claims(treaty_id)
        snapshots = self.repository.list_snapshots(treaty_id)
        confirmed = [e for e in endorsements if e["status"] == "confirmed"]
        versions = build_versions(float(treaty["initial_limit"]), confirmed)
        today = date.today().isoformat()
        effective_versions = [v for v in versions if v["effective_date"] <= today]
        current_limit = money(effective_versions[-1]["limit_after"]) if effective_versions else money(treaty["initial_limit"])
        used = occupancy_as_of(claims, today)
        remaining = money(current_limit - used)
        view = dict(treaty)
        view.update({
            "current_limit": current_limit,
            "used_approved_recoveries": used,
            "remaining_capacity": remaining,
            "over_committed": remaining < -0.01,
            "limit_versions": versions,
            "endorsements": endorsements,
            "approved_claims": claims,
            "snapshots": snapshots,
        })
        return view

    def create_treaty(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_any(actor, LEDGER_WRITE_ROLES)
        reference = text({"reference": reference}, "reference")
        data = validate_treaty(payload or {})
        treaty = self.repository.create_treaty(reference, data, actor.user_id)
        return self._ledger_bundle(treaty)

    def list_treaties(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        treaties = self.repository.list_treaties(limit=limit)
        today = date.today().isoformat()
        result = []
        for treaty in treaties:
            endorsements = self.repository.list_endorsements(int(treaty["id"]))
            claims = self.repository.list_claims(int(treaty["id"]))
            versions = build_versions(float(treaty["initial_limit"]), [e for e in endorsements if e["status"] == "confirmed"])
            effective = [v for v in versions if v["effective_date"] <= today]
            current_limit = money(effective[-1]["limit_after"]) if effective else money(treaty["initial_limit"])
            used = occupancy_as_of(claims, today)
            item = dict(treaty)
            item.update({
                "current_limit": current_limit,
                "used_approved_recoveries": used,
                "remaining_capacity": money(current_limit - used),
                "pending_endorsements": sum(1 for e in endorsements if e["status"] == "pending"),
            })
            result.append(item)
        return result

    def get_treaty(self, actor: Actor, treaty_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._ledger_bundle(self.repository.get_treaty(int(treaty_id)))

    def register_endorsement(self, actor: Actor, treaty_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_any(actor, LEDGER_WRITE_ROLES)
        treaty_id = int(treaty_id)
        self.repository.get_treaty(treaty_id)
        data = validate_endorsement(payload or {})
        return self.repository.add_endorsement(treaty_id, data, actor.user_id)

    def confirm_endorsement(self, actor: Actor, endorsement_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_any(actor, LEDGER_WRITE_ROLES)
        endorsement_id = int(endorsement_id)
        endorsement = self.repository.get_endorsement(endorsement_id)
        if endorsement["status"] != "pending":
            raise Conflict("批单已确认，不能重复确认")

        def planner(treaty, candidate, confirmed, claims):
            versions, candidate_version, _affected, seq = plan_confirm(treaty, candidate, confirmed, claims)
            snapshot_version = next(v for v in versions if v["endorsement_id"] == candidate["id"])
            snapshot = {
                "effective_date": snapshot_version["effective_date"],
                "limit_after": snapshot_version["limit_after"],
                "used_approved_recoveries": snapshot_version["used_approved_recoveries"],
                "remaining": money(snapshot_version["limit_after"] - snapshot_version["used_approved_recoveries"]),
                "summary": endorsement_summary(candidate),
            }
            return versions, seq, snapshot

        self.repository.confirm_endorsement(endorsement_id, actor.user_id, planner)
        return self._ledger_bundle(self.repository.get_treaty(int(endorsement["treaty_id"])))

    def register_claim(self, actor: Actor, treaty_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_any(actor, CLAIM_WRITE_ROLES)
        treaty_id = int(treaty_id)
        treaty = self.repository.get_treaty(treaty_id)
        data = validate_claim(payload or {})
        if data["source_record_id"] is not None:
            # 关联既有赔案工作流记录时必须存在
            self.repository.get(int(data["source_record_id"]))

        today = date.today().isoformat()
        endorsements = self.repository.list_endorsements(treaty_id)
        versions = build_versions(float(treaty["initial_limit"]), [e for e in endorsements if e["status"] == "confirmed"])
        effective = [v for v in versions if v["effective_date"] <= today]
        current_limit = money(effective[-1]["limit_after"]) if effective else money(treaty["initial_limit"])
        used = money(occupancy_as_of(self.repository.list_claims(treaty_id), today) + data["recoverable_amount"])
        remaining = money(current_limit - used)
        self.repository.add_claim(treaty_id, data, actor.user_id, snapshot={
            "kind": "claim_registered",
            "ref_table": "approved_claims",
            "version_seq": effective[-1]["seq"] if effective else 0,
            "effective_date": today,
            "limit_after": current_limit,
            "used_approved_recoveries": used,
            "remaining": remaining,
            "summary": "登记已核定赔案%s，摊回%s" % (data["claim_number"], format_amount(data["recoverable_amount"])),
            "actor_id": actor.user_id,
        })
        return self._ledger_bundle(treaty)
