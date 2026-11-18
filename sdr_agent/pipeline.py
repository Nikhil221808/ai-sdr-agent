"""The SDR pipeline: research -> brief -> draft -> grade -> (revise) -> result."""

from __future__ import annotations

from collections.abc import Callable

from .backends import Backend
from .models import Attempt, Prospect, SDRResult

Log = Callable[[str], None]


def run(
    prospect: Prospect,
    backend: Backend,
    seller_context: str,
    max_revisions: int = 2,
    pass_threshold: float = 4.0,
    log: Log = lambda _msg: None,
) -> SDRResult:
    log(f"Researching {prospect.company_domain}")
    research = backend.research(prospect)
    log(f"  {len(research.sources)} sources found")

    log("Building account brief")
    brief = backend.brief(prospect, research, seller_context)
    log(f"  ICP fit {brief.icp_fit_score}/100, {len(brief.triggers)} triggers")

    attempts: list[Attempt] = []
    previous = None
    feedback = None
    for i in range(max_revisions + 1):
        log(f"Writing draft {i + 1}")
        draft = backend.draft(prospect, brief, seller_context, previous, feedback, i)
        grade = backend.grade(prospect, brief, draft, pass_threshold, i)
        attempts.append(Attempt(draft=draft, grade=grade))
        verdict = "PASS" if grade.passed else "FAIL"
        log(f"  graded {grade.overall:.1f}/5 -> {verdict}")
        if grade.passed:
            break
        previous, feedback = draft, grade.revision_instructions

    return SDRResult(prospect=prospect, research=research, brief=brief, attempts=attempts)
