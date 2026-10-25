import pytest

from daily_brief import llm


def fake(outputs):
    """A runner that returns queued (returncode, stdout) and records each prompt."""
    calls = []

    def run(prompt):
        calls.append(prompt)
        rc, out = outputs.pop(0)
        return rc, out, ""

    run.calls = calls
    return run


def needs_key(d):
    if "ideas" not in d:
        raise ValueError("missing ideas")


def test_extracts_fenced_json():
    r = fake([(0, 'Sure!\n```json\n{"ideas": [1]}\n```\nbye')])
    data, meta = llm.call_json("P", {"t": "x"}, needs_key, r, max_attempts=2)
    assert data == {"ideas": [1]} and meta["attempts"] == 1


def test_accepts_bare_json():
    data, _ = llm.call_json("P", {}, needs_key, fake([(0, '{"ideas": []}')]), max_attempts=1)
    assert data == {"ideas": []}


def test_inputs_are_labelled_in_prompt():
    r = fake([(0, '{"ideas": []}')])
    llm.call_json("PROMPT", {"brief": "B-TEXT", "ideas": "I-TEXT"}, needs_key, r, max_attempts=1)
    p = r.calls[0]
    assert p.startswith("PROMPT") and "=== BRIEF ===\nB-TEXT" in p and "=== IDEAS ===\nI-TEXT" in p


def test_retry_feeds_back_validation_error():
    r = fake([(0, '{"nope": 1}'), (0, '{"ideas": [2]}')])
    data, meta = llm.call_json("P", {}, needs_key, r, max_attempts=2)
    assert data == {"ideas": [2]} and meta["attempts"] == 2
    assert "missing ideas" in r.calls[1]


def test_retry_after_bad_json():
    r = fake([(0, "```json\n{broken\n```"), (0, '{"ideas": []}')])
    _, meta = llm.call_json("P", {}, needs_key, r, max_attempts=2)
    assert meta["attempts"] == 2 and "not valid JSON" in r.calls[1]


def test_nonzero_exit_is_retried_then_raises():
    r = fake([(1, ""), (1, "")])
    with pytest.raises(llm.LLMError, match="exit 1"):
        llm.call_json("P", {}, needs_key, r, max_attempts=2)
    assert len(r.calls) == 2


def test_gives_up_with_last_error():
    with pytest.raises(llm.LLMError, match="missing ideas"):
        llm.call_json("P", {}, needs_key, fake([(0, "{}"), (0, "{}")]), max_attempts=2)


def test_claude_runner_builds_command(monkeypatch):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "{}", ""

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"], seen["timeout"] = cmd, kw["input"], kw["timeout"]
        return R()

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    run = llm.claude_runner("/x/claude", ["-p"], model="opus", timeout_s=9)
    assert run("hello") == (0, "{}", "")
    assert seen["cmd"] == ["/x/claude", "-p", "--model", "opus"] and seen["input"] == "hello" and seen["timeout"] == 9


def test_empty_stderr_falls_back_to_stdout_tail():
    with pytest.raises(llm.LLMError, match="usage limit reached"):
        llm.call_json("P", {}, needs_key, fake_err([(1, "Claude AI usage limit reached", "")]), max_attempts=1)


def fake_err(outputs):
    def run(prompt):
        rc, out, err = outputs.pop(0)
        return rc, out, err
    return run


def test_call_json_raises_auth_error_without_retry():
    calls = []

    def run(prompt):
        calls.append(prompt)
        return 1, "", "Failed to authenticate: OAuth session expired and could not be refreshed"

    with pytest.raises(llm.LLMAuthError):
        llm.call_json("P", {}, needs_key, run, max_attempts=3, auth_patterns=["Failed to authenticate"])
    assert len(calls) == 1


def test_auth_error_is_an_llm_error_and_plain_failures_still_retry():
    assert issubclass(llm.LLMAuthError, llm.LLMError)
    r = fake([(1, "rate limited"), (0, '{"ideas": []}')])
    _, meta = llm.call_json("P", {}, needs_key, r, max_attempts=2, auth_patterns=["Failed to authenticate"])
    assert meta["attempts"] == 2


def test_runner_passes_token_env(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw["env"]
        return __import__("subprocess").CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    monkeypatch.setenv("KEEP_ME", "1")
    llm.claude_runner("claude", ["-p"], "", 5, env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"})("hi")
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok" and seen["env"]["KEEP_ME"] == "1"
    plain = llm.claude_runner("claude", ["-p"], "", 5, auth_patterns=["X"])
    plain("hi")
    assert plain.auth_patterns == ["X"]
    assert seen["env"] is None  # no override: inherit the process environment


DOWN = ["Overloaded", "usage limit"]


def test_timeout_is_unavailable_after_one_attempt():
    calls = []

    def run(prompt):
        calls.append(prompt)
        return 124, "", "timed out after 600s"

    with pytest.raises(llm.LLMUnavailable, match="timed out"):
        llm.call_json("P", {}, lambda d: None, run, max_attempts=3, unavailable_patterns=DOWN)
    assert len(calls) == 1 and issubclass(llm.LLMUnavailable, llm.LLMError)


def test_unavailable_pattern_on_a_failed_exit_is_unavailable_without_retry():
    calls = []

    def run(prompt):
        calls.append(1)
        return 1, "", "API Error: Overloaded"

    with pytest.raises(llm.LLMUnavailable):
        llm.call_json("P", {}, lambda d: None, run, max_attempts=3, unavailable_patterns=DOWN)
    assert len(calls) == 1


def test_auth_beats_unavailable_and_bad_json_stays_a_plain_error():
    def logged_out(prompt):
        return 1, "Failed to authenticate (Overloaded)", ""

    with pytest.raises(llm.LLMAuthError):
        llm.call_json("P", {}, lambda d: None, logged_out, 2, auth_patterns=["Failed to authenticate"],
                      unavailable_patterns=DOWN)
    with pytest.raises(llm.LLMError) as e:  # Claude answered, badly (and even mentions a limit): charged as before
        llm.call_json("P", {}, lambda d: None, lambda p: (0, "usage limit talk, no json", ""), 2,
                      unavailable_patterns=DOWN)
    assert not isinstance(e.value, llm.LLMUnavailable)


def test_runner_carries_unavailable_patterns():
    assert llm.claude_runner("claude", [], "", 5, unavailable_patterns=["X"]).unavailable_patterns == ["X"]
