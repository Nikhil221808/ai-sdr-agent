"""Checks the live Claude backend against a mocked HTTP layer (no API calls, no cost).

Verifies the request each step sends (model, betas, fallbacks, tools, effort)
and that responses are parsed into the right types.
"""

import json

import anthropic
import httpx2
import pytest

from sdr_agent.backends import ClaudeBackend, LLMRefusal
from sdr_agent.models import AccountBrief, EmailDraft, Prospect, ResearchNotes

PROSPECT = Prospect(company_domain="acme.example", first_name="Ana", title="CRO")


def message(content, stop_reason="end_turn", **extra):
    return {
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-opus-5-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10}, **extra,
    }


def make_backend(responses, monkeypatch):
    """Backend whose HTTP calls return `responses` in order; records requests."""
    monkeypatch.delenv("SDR_MODEL", raising=False)
    sent = []
    queue = list(responses)

    def handler(request: httpx2.Request):
        sent.append({"headers": request.headers, "body": json.loads(request.content)})
        return httpx2.Response(200, json=queue.pop(0))

    client = anthropic.Anthropic(
        api_key="test", max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return ClaudeBackend(client=client), sent


def test_research_collects_text_and_sources_and_resumes_pause(monkeypatch):
    search_result = {
        "type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
        "content": [{"type": "web_search_result", "title": "Acme raises", "url": "https://acme.example/news",
                     "encrypted_content": "x", "page_age": None}],
    }
    fetch_result = {
        "type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_2",
        "content": {"type": "web_fetch_result", "url": "https://acme.example/careers", "retrieved_at": None,
                    "content": {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "jobs"},
                                "title": None, "citations": None}},
    }
    backend, sent = make_backend([
        message([{"type": "text", "text": "Part one.", "citations": None}, search_result], stop_reason="pause_turn"),
        message([fetch_result, {"type": "text", "text": "Part two.", "citations": None}]),
    ], monkeypatch)

    notes = backend.research(PROSPECT)

    assert isinstance(notes, ResearchNotes)
    assert notes.notes == "Part one.\nPart two."
    assert [s.url for s in notes.sources] == ["https://acme.example/news", "https://acme.example/careers"]

    assert len(sent) == 2, "pause_turn should trigger exactly one resume"
    first = sent[0]["body"]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in sent[0]["headers"]["anthropic-beta"]
    assert {t["type"] for t in first["tools"]} == {"web_search_20260209", "web_fetch_20260209"}
    assert "thinking" not in first  # Opus 5.5 runs adaptive thinking by default
    # The resumed request carries the paused assistant turn
    assert sent[1]["body"]["messages"][-1]["role"] == "assistant"


def test_structured_steps_parse_and_send_schema(monkeypatch):
    draft = {"subject": "hi there", "body": "b", "angle": "a", "trigger_used": "none"}
    backend, sent = make_backend([message([{"type": "text", "text": json.dumps(draft), "citations": None}])], monkeypatch)
    brief = AccountBrief(
        company_name="Acme", one_liner="x", industry="x", employee_range="x", icp_fit_score=50,
        icp_fit_reason="x", likely_pains=[], triggers=[], persona_angle="x", personalization_hooks=[],
    )

    out = backend.draft(PROSPECT, brief, "seller", None, None, 0)

    assert out == EmailDraft(**draft)
    body = sent[0]["body"]
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"


def test_refusal_raises(monkeypatch):
    backend, _ = make_backend([message(
        [], stop_reason="refusal",
        stop_details={"type": "refusal", "category": None, "explanation": None},
    )], monkeypatch)
    with pytest.raises(LLMRefusal):
        backend.research(PROSPECT)


def test_empty_model_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SDR_MODEL", "")
    backend = ClaudeBackend(client=object())
    assert backend.model == "claude-opus-5-5"
