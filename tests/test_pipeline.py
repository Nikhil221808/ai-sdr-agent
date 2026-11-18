import json
from pathlib import Path

import httpx
import pytest

from sdr_agent import cli, pipeline
from sdr_agent.backends import DemoBackend, FIXTURES_DIR, word_count
from sdr_agent.integrations import hubspot, slack
from sdr_agent.models import Prospect
from sdr_agent.render import to_markdown

SELLER = "Pipewise sells deal-risk intelligence."


def prospect(domain="northwind-freight.example", email="dana@northwind-freight.example"):
    return Prospect(company_domain=domain, first_name="Dana", last_name="Reyes",
                    title="VP of Sales", email=email)


@pytest.mark.parametrize("domain,attempts", [
    ("northwind-freight.example", 2),   # generic first draft gets rejected
    ("brightpath-health.example", 1),   # passes first time
    ("quillstack.example", 2),          # first draft hallucinates facts
])
def test_revise_until_pass(domain, attempts):
    result = pipeline.run(prospect(domain), DemoBackend(), SELLER)
    assert len(result.attempts) == attempts
    assert result.approved


def test_stops_at_max_revisions():
    result = pipeline.run(prospect(), DemoBackend(), SELLER, max_revisions=0)
    assert len(result.attempts) == 1
    assert not result.approved


def test_fixture_triggers_cite_known_sources():
    """Every trigger in the demo briefs must point at a URL from the research."""
    for path in FIXTURES_DIR.glob("*.json"):
        data = json.loads(path.read_text())
        urls = {s["url"] for s in data["research"]["sources"]}
        for t in data["brief"]["triggers"]:
            assert t["source_url"] in urls, f"{path.name}: {t['source_url']}"


def test_approved_demo_drafts_meet_length_rule():
    for path in FIXTURES_DIR.glob("*.json"):
        data = json.loads(path.read_text())
        final = data["drafts"][-1]
        assert 60 <= word_count(final["body"]) <= 110, path.name
        assert 2 <= len(final["subject"].split()) <= 5, path.name


def test_markdown_report_mentions_each_draft():
    md = to_markdown(pipeline.run(prospect(), DemoBackend(), SELLER))
    assert "## Draft 1" in md and "## Draft 2" in md
    assert "Approved by grader" in md


def test_slack_payload():
    payload = slack.build_payload(pipeline.run(prospect(), DemoBackend(), SELLER))
    assert payload["blocks"][0]["type"] == "header"
    assert "new reps, same forecast" in json.dumps(payload)


def test_hubspot_creates_contact_and_note():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        if request.url.path.endswith("/contacts/search"):
            return httpx.Response(200, json={"results": []})
        if request.url.path.endswith("/contacts"):
            return httpx.Response(201, json={"id": "101"})
        if request.url.path.endswith("/notes"):
            return httpx.Response(201, json={"id": "555"})
        return httpx.Response(404)

    client = httpx.Client(base_url=hubspot.BASE_URL, transport=httpx.MockTransport(handler))
    result = pipeline.run(prospect(), DemoBackend(), SELLER)
    contact_id, note_id = hubspot.HubSpot("token", client=client).sync(result)

    assert (contact_id, note_id) == ("101", "555")
    note_body = calls[-1][2]
    assert note_body["associations"][0]["to"]["id"] == "101"
    assert note_body["associations"][0]["types"][0]["associationTypeId"] == 202


def test_hubspot_updates_existing_contact():
    methods = []

    def handler(request):
        methods.append((request.method, request.url.path))
        if request.url.path.endswith("/contacts/search"):
            return httpx.Response(200, json={"results": [{"id": "77"}]})
        return httpx.Response(200, json={"id": "77" if "contacts" in request.url.path else "9"})

    client = httpx.Client(base_url=hubspot.BASE_URL, transport=httpx.MockTransport(handler))
    hubspot.HubSpot("t", client=client).sync(pipeline.run(prospect(), DemoBackend(), SELLER))
    assert ("PATCH", "/crm/v3/objects/contacts/77") in methods


def test_hubspot_requires_email():
    result = pipeline.run(prospect(email=None), DemoBackend(), SELLER)
    with pytest.raises(hubspot.HubSpotError):
        hubspot.HubSpot("t", client=httpx.Client()).upsert_contact(result)


def test_cli_batch_demo(tmp_path: Path):
    csv_path = Path(__file__).resolve().parent.parent / "data" / "prospects_demo.csv"
    code = cli.main(["batch", str(csv_path), "--demo", "--out", str(tmp_path)])
    assert code == 0
    assert len(list(tmp_path.glob("*.md"))) == 3
    assert len(list(tmp_path.glob("*.json"))) == 3
