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


def _grade(scores, passed=True, claims=()):
    from sdr_agent.models import Grade, RubricScore
    crits = ["personalization", "relevance", "grounded", "clarity", "cta", "brevity"]
    return Grade(
        scores=[RubricScore(criterion=c, score=s, note="") for c, s in zip(crits, scores)],
        overall=5.0, passed=passed, unsupported_claims=list(claims), revision_instructions="none",
    )


def _draft(words=80):
    from sdr_agent.models import EmailDraft
    return EmailDraft(subject="a b c", body=" ".join(["w"] * words), angle="", trigger_used="")


def test_rules_override_grader_that_says_pass_on_hallucination():
    g = pipeline.enforce_rules(_grade([5] * 6, claims=["made-up team size"]), _draft(), 4.0)
    assert not g.passed
    assert "claims" in g.revision_instructions


def test_rules_recompute_overall_and_fail_low_criterion():
    g = pipeline.enforce_rules(_grade([5, 5, 5, 5, 5, 2]), _draft(), 4.0)
    assert g.overall == 4.5 and not g.passed


def test_rules_fail_long_body():
    assert not pipeline.enforce_rules(_grade([5] * 6), _draft(words=150), 4.0).passed


def test_rules_pass_clean_draft():
    assert pipeline.enforce_rules(_grade([4, 5, 5, 4, 4, 5]), _draft(), 4.0).passed


def test_cli_batch_skips_bad_csv_rows(tmp_path: Path):
    csv_path = tmp_path / "p.csv"
    csv_path.write_text(
        "﻿company_domain,first_name,last_name,title,email,linkedin_url\n"
        "brightpath-health.example,Marcus,Lee,Head of RevOps,,\n"
        "missing-title.example,Bo,,,,\n"
    )
    code = cli.main(["batch", str(csv_path), "--demo", "--out", str(tmp_path / "out")])
    assert code == 1  # one bad row reported
    assert len(list((tmp_path / "out").glob("*.md"))) == 1  # the good row still ran


def test_cli_batch_demo(tmp_path: Path):
    csv_path = Path(__file__).resolve().parent.parent / "data" / "prospects_demo.csv"
    code = cli.main(["batch", str(csv_path), "--demo", "--out", str(tmp_path)])
    assert code == 0
    assert len(list(tmp_path.glob("*.md"))) == 3
    assert len(list(tmp_path.glob("*.json"))) == 3
