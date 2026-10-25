import textwrap
from pathlib import Path

import pytest

from daily_brief import brief

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN = ["secret-project", "family-matters"]

GOOD = textwrap.dedent("""\
    ---
    version: 1
    generated: 2026-09-27
    source: hand
    tv_safe: true
    priorities:
      - id: p1
        text: "Land an AI PM role"
        signals: [evals]
      - id: p2
        text: "Reliable agents"
        signals: [harnesses]
    active_projects: ["ExampleProject"]
    already_known: ["RAG basics"]
    ignore: ["crypto"]
    depth: {default: practitioner, go_deeper_on: [evaluation]}
    tone: "Calm."
    ---
    # notes
    """)


def write(tmp_path, text):
    p = tmp_path / "brief.md"
    p.write_text(text)
    return p


def test_repo_brief_is_valid():
    b = brief.load_brief(REPO / "brief" / "brief.md", ["secret-project", "family-matters"])
    assert b.priorities[0].id == "p1" and b.tv_safe


def test_good_brief_parses(tmp_path):
    b = brief.load_brief(write(tmp_path, GOOD), FORBIDDEN)
    assert [p.id for p in b.priorities] == ["p1", "p2"]
    assert b.priority_ids() == {"p1", "p2"}
    assert b.version == 1


@pytest.mark.parametrize("mutate,match", [
    (lambda s: s.replace("---\nversion", "version", 1), "front matter"),
    (lambda s: s.replace("version: 1\n", ""), "version"),
    (lambda s: s.replace("source: hand", "source: vibes"), "source"),
    (lambda s: s.replace("id: p2", "id: p1"), "duplicate"),
    (lambda s: s.replace("id: p2", "id: second"), "p<number>"),
    (lambda s: s.replace('text: "Reliable agents"', 'text: ""'), "text"),
    (lambda s: s.replace('already_known: ["RAG basics"]', "already_known: RAG"), "already_known"),
    (lambda s: s.replace("tv_safe: true", "tv_safe: yes please"), "tv_safe"),
])
def test_invalid_briefs_name_the_problem(tmp_path, mutate, match):
    with pytest.raises(brief.BriefError, match=match):
        brief.load_brief(write(tmp_path, mutate(GOOD)), FORBIDDEN)


def test_tv_safe_brief_rejects_personal_terms(tmp_path):
    text = GOOD.replace('ignore: ["crypto"]', 'ignore: ["crypto", "Secret-Project notes"]')
    with pytest.raises(brief.BriefError, match="tv_safe.*secret-project"):
        brief.load_brief(write(tmp_path, text), FORBIDDEN)


def test_non_tv_safe_brief_may_mention_them(tmp_path):
    text = GOOD.replace("tv_safe: true", "tv_safe: false").replace('ignore: ["crypto"]', 'ignore: ["secret-project"]')
    assert not brief.load_brief(write(tmp_path, text), FORBIDDEN).tv_safe


def test_prompt_text_contains_front_matter_only_facts(tmp_path):
    b = brief.load_brief(write(tmp_path, GOOD), FORBIDDEN)
    txt = b.prompt_text()
    assert "p1" in txt and "Land an AI PM role" in txt and "RAG basics" in txt
