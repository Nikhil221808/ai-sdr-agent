"""The SDR pipeline: research -> brief -> draft -> grade -> (revise) -> result."""

from __future__ import annotations

from collections.abc import Callable

from .backends import Backend, word_count
from .models import Attempt, EmailDraft, Grade, Prospect, SDRResult

Log = Callable[[str], None]

MIN_WORDS, MAX_WORDS = 60, 110
MIN_CRITERION = 3


def enforce_rules(grade: Grade, draft: EmailDraft, threshold: float) -> Grade:
    """Apply the pass rule in code instead of trusting the grader's own verdict.

    The LLM grader supplies the per-criterion scores and unsupported claims; the
    arithmetic and the hard rules (length, no criterion below 3, no unsupported
    claims) are deterministic.
    """
    if not grade.scores:
        return grade.model_copy(update={"passed": False})

    overall = round(sum(s.score for s in grade.scores) / len(grade.scores), 2)
    problems: list[str] = []
    words = word_count(draft.body)
    if not MIN_WORDS <= words <= MAX_WORDS:
        problems.append(f"Body is {words} words; it must be {MIN_WORDS}-{MAX_WORDS}.")
    low = [s.criterion for s in grade.scores if s.score < MIN_CRITERION]
    if low:
        problems.append(f"Scored below {MIN_CRITERION} on: {', '.join(low)}.")
    if grade.unsupported_claims:
        problems.append("Remove claims the brief does not support.")
    if overall < threshold:
        problems.append(f"Overall {overall} is below the {threshold} pass mark.")

    passed = not problems
    instructions = grade.revision_instructions
    if not passed and grade.passed:
        # Grader said pass but a hard rule failed: tell the writer why.
        instructions = " ".join(problems)
    return grade.model_copy(update={
        "overall": overall, "passed": passed, "revision_instructions": instructions,
    })


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
        grade = enforce_rules(
            backend.grade(prospect, brief, draft, pass_threshold, i), draft, pass_threshold,
        )
        attempts.append(Attempt(draft=draft, grade=grade))
        verdict = "PASS" if grade.passed else "FAIL"
        log(f"  graded {grade.overall:.1f}/5 -> {verdict}")
        if grade.passed:
            break
        previous, feedback = draft, grade.revision_instructions

    return SDRResult(prospect=prospect, research=research, brief=brief, attempts=attempts)
