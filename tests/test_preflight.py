import json
import subprocess

import pytest

from daily_brief import llm, preflight

PATTERNS = ["Failed to authenticate", "OAuth session expired"]


def test_check_llm_auth_error_and_other_failure():
    with pytest.raises(llm.LLMAuthError):
        preflight.check_llm(lambda p: (1, "Failed to authenticate: OAuth session expired", ""), PATTERNS)
    with pytest.raises(preflight.PreflightError, match="exit 2"):
        preflight.check_llm(lambda p: (2, "", "boom"), PATTERNS)
    prompts = []
    preflight.check_llm(lambda p: prompts.append(p) or (0, "OK", ""), PATTERNS)
    assert prompts == ["Reply with OK."]


def test_auth_status_parses_json_without_a_model_call():
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"loggedIn": False, "authMethod": "none"}), "")

    out = preflight.auth_status("/bin/claude", {"X": "1"}, run=run)
    assert out["loggedIn"] is False and seen["cmd"] == ["/bin/claude", "auth", "status"]
    assert seen["env"]["X"] == "1"
    with pytest.raises(preflight.PreflightError):
        preflight.auth_status("c", {}, run=lambda c, **k: subprocess.CompletedProcess(c, 0, "not json", ""))


def test_the_wait_for_network_and_nas_is_gone():
    assert not hasattr(preflight, "wait_ready") and not hasattr(preflight, "probe_mount")
