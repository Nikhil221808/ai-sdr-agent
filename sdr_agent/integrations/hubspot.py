"""Write the prospect and their draft to HubSpot.

Upserts the contact by email, then attaches the research brief and email draft
as a Note on the contact record. Needs a HubSpot private app token with
`crm.objects.contacts.read` and `crm.objects.contacts.write` scopes.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

import httpx

from ..models import SDRResult

BASE_URL = "https://api.hubapi.com"
NOTE_TO_CONTACT = 202  # HubSpot-defined association type id: note -> contact


class HubSpotError(RuntimeError):
    pass


class HubSpot:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.http = client or httpx.Client(base_url=BASE_URL, timeout=20)
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _req(self, method: str, path: str, body: dict) -> dict:
        resp = self.http.request(method, path, json=body, headers=self.headers)
        if resp.status_code >= 400:
            raise HubSpotError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def find_contact(self, email: str) -> str | None:
        data = self._req("POST", "/crm/v3/objects/contacts/search", {
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email},
            ]}],
            "properties": ["email"],
            "limit": 1,
        })
        results = data.get("results", [])
        return results[0]["id"] if results else None

    def upsert_contact(self, result: SDRResult) -> str:
        p = result.prospect
        if not p.email:
            raise HubSpotError("Prospect has no email; HubSpot sync needs one to dedupe on.")
        props = {
            "email": p.email,
            "firstname": p.first_name,
            "lastname": p.last_name,
            "jobtitle": p.title,
            "company": result.brief.company_name,
            "website": p.company_domain,
        }
        existing = self.find_contact(p.email)
        if existing:
            self._req("PATCH", f"/crm/v3/objects/contacts/{existing}", {"properties": props})
            return existing
        return self._req("POST", "/crm/v3/objects/contacts", {"properties": props})["id"]

    def add_note(self, contact_id: str, result: SDRResult) -> str:
        body = render_note_html(result)
        data = self._req("POST", "/crm/v3/objects/notes", {
            "properties": {
                "hs_timestamp": datetime.now(timezone.utc).isoformat(),
                "hs_note_body": body,
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_CONTACT}],
            }],
        })
        return data["id"]

    def sync(self, result: SDRResult) -> tuple[str, str]:
        contact_id = self.upsert_contact(result)
        note_id = self.add_note(contact_id, result)
        return contact_id, note_id


def render_note_html(result: SDRResult) -> str:
    e = html.escape
    b, d = result.brief, result.final.draft
    triggers = "".join(
        f"<li>{e(t.summary)} ({e(t.date)}) <a href=\"{e(t.source_url)}\">source</a></li>"
        for t in b.triggers
    ) or "<li>none found</li>"
    status = "approved by grader" if result.approved else "needs human edit"
    return (
        f"<p><strong>AI SDR draft</strong> ({status}, {result.final.grade.overall:.1f}/5)</p>"
        f"<p><strong>ICP fit:</strong> {b.icp_fit_score}/100: {e(b.icp_fit_reason)}</p>"
        f"<p><strong>Why now:</strong></p><ul>{triggers}</ul>"
        f"<p><strong>Subject:</strong> {e(d.subject)}</p>"
        f"<p>{e(d.body).replace(chr(10), '<br>')}</p>"
    )
