"""Instance-owned authorization, admission and audit; no device retry decisions."""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass

from ..domain.policy import Policy
from ..infrastructure.audit import Audit
from ..infrastructure.policy_file import read_policy_document

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Ticket:
    call_id: str
    tool: str
    operation: str
    write: bool
    reserved: bool
    started: float


class Execution:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.active = 0
        self.reserved = 0
        self.controls = 0
        self.audit = Audit(policy.audit_path)

    @classmethod
    def from_environment(cls, protocol, policy=None):
        if policy is None:
            filename = os.environ.get(protocol.upper() + "_POLICY_FILE")
            if filename:
                policy = Policy.from_dict(read_policy_document(filename))
            else:
                policy = Policy()
        return cls(policy)

    def admit(self, tool, operation, *, write, local):
        if self.policy.allowed_tools is not None and tool not in self.policy.allowed_tools:
            raise ValueError("Tool denied by configured policy")
        if self.policy.allowed_operations is not None and operation not in self.policy.allowed_operations:
            raise ValueError("Operation denied by configured policy")
        if write and self.policy.read_only:
            raise ValueError("Write denied by read-only policy")
        reserved = self.active >= self.policy.max_in_flight
        if reserved and (not local or write or self.reserved):
            raise ValueError("Server is busy; maximum in-flight requests reached")
        ticket = Ticket(uuid.uuid4().hex, tool, operation, write, reserved, time.monotonic())
        if write:
            self.audit.record(ticket, "intent", None)
        if reserved:
            self.reserved += 1
        else:
            self.active += 1
        self.controls += int(write)
        return ticket

    def finish(self, ticket, result):
        if ticket.reserved:
            self.reserved -= 1
        else:
            self.active -= 1
        self.controls -= int(ticket.write)
        if ticket.write:
            try:
                self.audit.record(ticket, "result", result)
            except OSError:
                log.exception("Audit result could not be persisted for call %s", ticket.call_id)
                if result is not None:
                    result.setdefault("meta", {})["audit_error"] = True
