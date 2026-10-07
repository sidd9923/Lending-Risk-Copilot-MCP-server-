"""Linear: the action-capable source.

Writes are where "graceful degradation" means something different. If we
can't create the ticket we must NOT silently drop it, and we must NOT retry
blindly (double tickets). So on failure we return the fully drafted ticket so
a human can paste it, and say exactly why it wasn't created.

Docs: https://developers.linear.app/docs/graphql/working-with-the-graphql-api
"""

from __future__ import annotations

import httpx

from ..errors import AuthError, SourceError, SourceResult, Status
from ..resilience import call_with_retry
from .base import BaseSource

GRAPHQL = "https://api.linear.app/graphql"

CREATE_ISSUE = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier url title }
  }
}
"""


class LinearTickets(BaseSource):
    name = "linear"
    rate_per_s = 1.0

    def __init__(self, *args, api_key: str | None = None, team_id: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_key = api_key
        self.team_id = team_id

    def configured(self) -> bool:
        return bool(self.api_key and self.team_id)

    async def create_issue(self, title: str, description: str, priority: int = 2) -> SourceResult:
        draft = {"title": title, "description": description, "priority": priority}
        if not self.configured():
            return _draft_only(draft, "Linear not configured (LINEAR_API_KEY / LINEAR_TEAM_ID)")
        if not self.breaker.allow():
            return _draft_only(draft, "Linear circuit open after repeated failures")

        await self.bucket.acquire()
        payload = {"query": CREATE_ISSUE, "variables": {"input": {**draft, "teamId": self.team_id}}}
        try:
            # retries=0: a timeout on a write might mean it succeeded. Don't double-create.
            resp = await call_with_retry(
                self.name,
                lambda: self.client.post(
                    GRAPHQL, json=payload, headers={"Authorization": self.api_key}
                ),
                retries=0,
            )
            body = resp.json()
        except AuthError as exc:
            return _draft_only(draft, f"Linear rejected credentials: {exc.message}")
        except (SourceError, httpx.HTTPError, ValueError) as exc:
            self.breaker.record_failure()
            return _draft_only(draft, f"Linear call failed, NOT retried to avoid duplicates: {exc}")

        if body.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in body["errors"])
            return _draft_only(draft, f"Linear GraphQL error: {msg}")

        self.breaker.record_success()
        result = body.get("data", {}).get("issueCreate", {})
        if not result.get("success"):
            return _draft_only(draft, "Linear returned success=false")
        return SourceResult.ok(self.name, {"created": True, "issue": result["issue"]})


def _draft_only(draft: dict, reason: str) -> SourceResult:
    return SourceResult(
        source="linear",
        status=Status.DEGRADED,
        data={"created": False, "draft": draft},
        warnings=[reason, "ticket drafted but not created; paste it manually or retry later"],
    )
