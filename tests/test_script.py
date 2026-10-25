import pytest

from daily_brief import script
from daily_brief.config import Build

RULES = script.Rules(words_per_minute=150, min_minutes=4, max_minutes=20, max_over=0.15, max_turn_words=90,
                     max_same_speaker_run=3, hype_words=["game-changer", "insane"],
                     priority_ids=frozenset({"p1", "p2"}))


def turns(n=14, words=50):
    return [{"speaker": "AB"[i % 2], "text": " ".join(["word"] * words) + "."} for i in range(n)]  # 700 words


def episode(**over):
    d = {"title": "Evals in practice", "why": "You are building agents, so this shows you what to measure first.",
         "chapters": [{"topic": "eval the trace", "why": "It is the check your pipeline lacks.", "priority": "p2"}],
         "so_what": "Add one trace check to your pipeline this week.", "turns": turns()}
    return d | over


def test_validate_accepts_a_complete_episode():
    script.validate(episode(), RULES)


def test_validate_accepts_skip():
    script.validate({"skip": True, "reason": "It is a funding-round interview."}, RULES)


def test_validate_rejects_a_skip_without_a_reason():
    for bad in ({"skip": True}, {"skip": True, "reason": "  "}):
        with pytest.raises(ValueError, match="reason"):
            script.validate(bad, RULES)


def test_validate_rejects_long_turn_hype_and_unknown_priority():
    long_turn = turns()
    long_turn[3] = {"speaker": "B", "text": "word " * 91}
    with pytest.raises(ValueError, match=r"turns\[3\] is 91 words"):
        script.validate(episode(turns=long_turn), RULES)
    hype = turns()
    hype[2]["text"] = "This is a game-changer for everyone."
    with pytest.raises(ValueError, match="hype word.*game-changer"):
        script.validate(episode(turns=hype), RULES)
    with pytest.raises(ValueError, match=r"chapters\[0\]\.priority.*p1.*p2"):
        script.validate(episode(chapters=[{"topic": "t", "why": "w", "priority": "p9"}]), RULES)


@pytest.mark.parametrize("key", ["title", "why", "so_what"])
def test_validate_requires_the_text_fields(key):
    with pytest.raises(ValueError, match=key):
        script.validate(episode(**{key: " "}), RULES)


@pytest.mark.parametrize("chapters", [[], "x", [{"topic": "t", "why": "w", "priority": "p1"}] * 9,
                                      [{"topic": "x" * 61, "why": "w", "priority": "p1"}],
                                      [{"topic": "t", "why": "", "priority": "p1"}], ["not an object"]])
def test_validate_rejects_bad_chapters(chapters):
    with pytest.raises(ValueError, match="chapters"):
        script.validate(episode(chapters=chapters), RULES)


def test_validate_rejects_a_bad_speaker_or_empty_text():
    bad = turns()
    bad[0]["speaker"] = "C"
    with pytest.raises(ValueError, match=r"turns\[0\]\.speaker"):
        script.validate(episode(turns=bad), RULES)
    bad = turns()
    bad[1]["text"] = ""
    with pytest.raises(ValueError, match=r"turns\[1\]\.text"):
        script.validate(episode(turns=bad), RULES)
    with pytest.raises(ValueError, match="turns"):
        script.validate(episode(turns=[]), RULES)


def test_validate_limits_the_same_speaker_run():
    run = turns()
    for i in (4, 5, 6, 7):
        run[i]["speaker"] = "A"  # four A turns in a row (turn 3 is B)
    with pytest.raises(ValueError, match=r"turns\[7\].*same speaker"):
        script.validate(episode(turns=run), RULES)
    run[7]["speaker"] = "B"
    script.validate(episode(turns=run), RULES)  # three in a row is the limit, not a violation


def test_validate_bounds_the_total_length_and_tells_the_model_to_skip_instead_of_padding():
    with pytest.raises(ValueError, match=r"under the 600-word minimum.*skip"):
        script.validate(episode(turns=turns(8, 50)), RULES)  # 400 words
    with pytest.raises(ValueError, match=r"over the 3450-word limit"):
        script.validate(episode(turns=turns(70, 50)), RULES)  # 3500 words
    script.validate(episode(turns=turns(69, 50)), RULES)  # 3450 words: 20 min + 15% slack is allowed


@pytest.mark.parametrize("text", ["Use **bold** here.", "# Heading", "- first point", "1. first point", "see `code`",
                                  "read [this](x)", "Go to https://example.com now", "visit www.example.com",
                                  "great job \U0001F600"])
def test_validate_rejects_markdown_urls_and_emoji_in_the_script(text):
    bad = turns()
    bad[5]["text"] = text
    with pytest.raises(ValueError, match=r"turns\[5\]"):
        script.validate(episode(turns=bad), RULES)


def test_validate_rejects_markdown_in_the_phone_text():
    with pytest.raises(ValueError, match="why"):
        script.validate(episode(why="You should **read** this."), RULES)
    with pytest.raises(ValueError, match=r"chapters\[0\]\.why"):
        script.validate(episode(chapters=[{"topic": "t", "why": "See https://x.y", "priority": "p1"}]), RULES)


def test_a_dollar_sign_percent_and_apostrophes_are_fine():
    ok = turns()
    ok[0]["text"] = "It's 40% faster, and it costs $5 per run. That's the point."
    script.validate(episode(turns=ok), RULES)


def test_rules_from_config():
    b = Build(interval_minutes=60, run_timeout_minutes=90, per_run=1, max_attempts=2, max_new_per_source_per_day=1, max_skips_per_run=2, min_minutes=4,
              max_minutes=20, max_share_of_source=0.4, max_over=0.15, words_per_minute=150, chars_per_word=6, max_turn_words=90,
              max_same_speaker_run=3, hype_words=["insane"], history_days=30)
    r = script.Rules.from_config(b, {"p1", "p2"})
    assert r.priority_ids == {"p1", "p2"} and r.max_turn_words == 90 and r.hype_words == ["insane"]


def test_rules_with_max_overrides_only_the_maximum():
    r = RULES.with_max(7.4)
    assert r.max_minutes == 7.4 and r.min_minutes == RULES.min_minutes and r.words_per_minute == 150
    with pytest.raises(ValueError, match="over the"):  # 7.4 min + 15% = 1,277 words
        script.validate(episode(turns=[{"speaker": "AB"[i % 2], "text": "word " * 60} for i in range(24)]), r)


# ---- prompts/episode.md: the merged extract + edit + script rules ----------------------------------------

PROMPT = (__import__("pathlib").Path(__file__).resolve().parent.parent / "prompts" / "episode.md").read_text()


@pytest.mark.parametrize("rule", [
    "TRANSCRIPT",                                  # facts from the transcript only
    "Do not add facts from your own knowledge",
    "already_known", "ignore", "p1",              # the brief's lists and priority ids
    "covered_recently", "listener_feedback", "skip_topics", "go_deeper", "more_of",
    "DEFINE BEFORE YOU USE", "EVERY part",         # frameworks are walked through part by part
    "ALEX", "SAM", "skeptic", "framed as analogies",
    "Short sentences", "no markdown", "no URLs", "no emoji",
    "Cold open", "what this means for you this week", "sign-off", "active_projects",
    "calm and direct", "No hype words", "never pad",
    '"skip": true', "second person", "BUDGET_MINUTES",
])
def test_episode_prompt_keeps_the_editorial_rules(rule):
    assert rule.lower() in PROMPT.lower(), rule


def test_episode_prompt_example_matches_the_validated_shape():
    import json
    import re

    fences = re.findall(r"```json\n(.*?)```", PROMPT, re.DOTALL)
    skip, full = (json.loads(f.replace("...", "x")) for f in fences)
    script.validate(skip, RULES)
    assert set(full) == {"title", "why", "chapters", "so_what", "turns"}
    assert set(full["chapters"][0]) == {"topic", "why", "priority"}


def test_the_four_old_prompts_are_gone():
    root = __import__("pathlib").Path(__file__).resolve().parent.parent / "prompts"
    assert sorted(p.name for p in root.iterdir()) == ["episode.md"]
