"""Render a result as a readable markdown report."""

from __future__ import annotations

from .backends import word_count
from .models import SDRResult


def to_markdown(result: SDRResult) -> str:
    p, b = result.prospect, result.brief
    name = f"{p.first_name} {p.last_name}".strip()
    lines = [
        f"# {name}, {p.title} @ {b.company_name}",
        "",
        f"**Status:** {'Approved by grader' if result.approved else 'Needs human edit'}  ",
        f"**ICP fit:** {b.icp_fit_score}/100: {b.icp_fit_reason}",
        "",
        "## Account brief",
        f"- **What they do:** {b.one_liner}",
        f"- **Industry / size:** {b.industry} · {b.employee_range}",
        f"- **Persona angle:** {b.persona_angle}",
        "- **Likely pains:** " + "; ".join(b.likely_pains),
        "",
        "### Why now",
    ]
    lines += [f"- **{t.kind}** ({t.date}): {t.summary} [source]({t.source_url})" for t in b.triggers] or ["- none found"]

    for i, a in enumerate(result.attempts, 1):
        g = a.grade
        lines += [
            "",
            f"## Draft {i}: {g.overall:.1f}/5 {'PASS' if g.passed else 'FAIL'}",
            "",
            f"**Subject:** {a.draft.subject}",
            "",
            *[f"> {line}" if line else ">" for line in a.draft.body.splitlines()],
            "",
            f"*{word_count(a.draft.body)} words · angle: {a.draft.angle}*",
            "",
            "| Criterion | Score | Note |",
            "|---|---|---|",
            *[f"| {s.criterion} | {s.score} | {s.note} |" for s in g.scores],
        ]
        if g.unsupported_claims:
            lines += ["", "**Unsupported claims:** " + "; ".join(g.unsupported_claims)]
        if not g.passed:
            lines += ["", f"**Reviewer feedback:** {g.revision_instructions}"]

    if result.research.sources:
        lines += ["", "## Sources"] + [f"- [{s.title}]({s.url})" for s in result.research.sources]
    return "\n".join(lines) + "\n"
