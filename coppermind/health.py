"""The shape of a health and readiness answer, shared by every service.

`/healthz` says the process is up. `/readyz` says it can do its job, and it
tells the truth: with PostgreSQL stopped the store is not ready, so the API is
not ready either, and a load balancer stops sending it work. Nothing reports
ready by answering a request successfully.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Check(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class Readiness(BaseModel):
    ready: bool
    checks: list[Check] = Field(default_factory=list)

    @classmethod
    def of(cls, checks: list[Check]) -> Readiness:
        return cls(ready=all(check.ok for check in checks), checks=checks)


class Health(BaseModel):
    status: str = "ok"
    service: str
    version: str
