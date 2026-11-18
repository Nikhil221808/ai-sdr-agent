"""Two interchangeable backends for the pipeline's four LLM steps.

- ClaudeBackend: real research (web search + web fetch) and writing via the Claude API.
- DemoBackend:   replays recorded fixtures so the project runs offline with no API key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from . import prompts
from .models import (
    AccountBrief, EmailDraft, Grade, Prospect, ResearchNotes, Source,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "demo"

T = TypeVar("T", bound=BaseModel)


class LLMRefusal(RuntimeError):
    """Claude (and its fallback) declined the request."""


class Backend(Protocol):
    def research(self, prospect: Prospect) -> ResearchNotes: ...
    def brief(self, prospect: Prospect, research: ResearchNotes, seller: str) -> AccountBrief: ...
    def draft(
        self, prospect: Prospect, brief: AccountBrief, seller: str,
        previous: EmailDraft | None, feedback: str | None, attempt: int,
    ) -> EmailDraft: ...
    def grade(
        self, prospect: Prospect, brief: AccountBrief, draft: EmailDraft,
        threshold: float, attempt: int,
    ) -> Grade: ...


def word_count(text: str) -> int:
    return len(text.split())


def _full_name(p: Prospect) -> str:
    return f"{p.first_name} {p.last_name}".strip()


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #

class ClaudeBackend:
    """Runs each step against the Claude API.

    Uses server-side refusal fallbacks (beta `server-side-fallback-2026-07-01`,
    `fallbacks="default"`): if a request is declined, the API re-runs it on a
    fallback model inside the same call.
    """

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, model: str | None = None, max_searches: int = 5):
        import anthropic  # imported lazily so demo mode needs no key

        self.client = anthropic.Anthropic()
        self.model = model or os.getenv("SDR_MODEL", "claude-opus-5-5")
        self.max_searches = max_searches

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _check_refusal(response) -> None:
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMRefusal(f"Request declined (category={category})")

    def _parse(self, schema: type[T], system: str, user: str, effort: str) -> T:
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            betas=[self.FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": effort},
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        self._check_refusal(response)
        if response.parsed_output is None:
            raise RuntimeError(f"No structured output (stop_reason={response.stop_reason})")
        return response.parsed_output

    # -- steps -------------------------------------------------------------- #

    def research(self, prospect: Prospect) -> ResearchNotes:
        linkedin = f"LinkedIn: {prospect.linkedin_url}" if prospect.linkedin_url else ""
        messages: list[dict] = [{
            "role": "user",
            "content": prompts.RESEARCH_USER.format(
                domain=prospect.company_domain, name=_full_name(prospect),
                title=prospect.title, linkedin_line=linkedin,
            ),
        }]
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": self.max_searches},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
        ]

        sources: dict[str, Source] = {}
        text_parts: list[str] = []

        # Server tools run on Anthropic's side; a long turn may come back as
        # `pause_turn`, which we resume by sending the partial turn back.
        for _ in range(5):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=[self.FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": "medium"},
                system=prompts.RESEARCH_SYSTEM,
                tools=tools,
                messages=messages,
            )
            self._check_refusal(response)

            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "web_search_tool_result":
                    # Success -> list of results; error -> a single error object.
                    if isinstance(block.content, list):
                        for r in block.content:
                            sources.setdefault(r.url, Source(title=r.title, url=r.url))

            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})

        return ResearchNotes(notes="".join(text_parts).strip(), sources=list(sources.values()))

    def brief(self, prospect: Prospect, research: ResearchNotes, seller: str) -> AccountBrief:
        sources = "\n".join(f"- {s.title}: {s.url}" for s in research.sources) or "none"
        return self._parse(
            AccountBrief,
            system=prompts.BRIEF_SYSTEM.format(seller_context=seller),
            user=prompts.BRIEF_USER.format(
                name=_full_name(prospect), title=prospect.title,
                domain=prospect.company_domain, notes=research.notes, sources=sources,
            ),
            effort="medium",
        )

    def draft(self, prospect, brief, seller, previous, feedback, attempt) -> EmailDraft:
        revision = ""
        if previous and feedback:
            revision = prompts.REVISION_BLOCK.format(
                subject=previous.subject, body=previous.body, feedback=feedback,
            )
        return self._parse(
            EmailDraft,
            system=prompts.WRITER_SYSTEM.format(seller_context=seller),
            user=prompts.WRITER_USER.format(
                name=_full_name(prospect), title=prospect.title,
                brief=brief.model_dump_json(indent=2), revision_block=revision,
            ),
            effort="high",
        )

    def grade(self, prospect, brief, draft, threshold, attempt) -> Grade:
        return self._parse(
            Grade,
            system=prompts.GRADER_SYSTEM.format(threshold=threshold),
            user=prompts.GRADER_USER.format(
                brief=brief.model_dump_json(indent=2), subject=draft.subject,
                body=draft.body, word_count=word_count(draft.body),
            ),
            effort="medium",
        )


# --------------------------------------------------------------------------- #
# Demo (offline)
# --------------------------------------------------------------------------- #

class DemoBackend:
    """Replays recorded outputs from fixtures/demo/<domain>.json.

    Each fixture holds the research, brief, and an ordered list of drafts and
    grades, so the demo exercises the real revise-until-pass loop.
    """

    def __init__(self, fixtures_dir: Path = FIXTURES_DIR):
        self.fixtures_dir = fixtures_dir

    def _load(self, domain: str) -> dict:
        path = self.fixtures_dir / f"{domain}.json"
        if not path.exists():
            available = sorted(p.stem for p in self.fixtures_dir.glob("*.json"))
            raise FileNotFoundError(
                f"No demo fixture for '{domain}'. Demo domains: {', '.join(available)}"
            )
        return json.loads(path.read_text())

    def research(self, prospect):
        return ResearchNotes.model_validate(self._load(prospect.company_domain)["research"])

    def brief(self, prospect, research, seller):
        return AccountBrief.model_validate(self._load(prospect.company_domain)["brief"])

    def draft(self, prospect, brief, seller, previous, feedback, attempt):
        drafts = self._load(prospect.company_domain)["drafts"]
        return EmailDraft.model_validate(drafts[min(attempt, len(drafts) - 1)])

    def grade(self, prospect, brief, draft, threshold, attempt):
        grades = self._load(prospect.company_domain)["grades"]
        return Grade.model_validate(grades[min(attempt, len(grades) - 1)])
