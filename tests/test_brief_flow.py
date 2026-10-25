from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from daily_brief import api, brief
from daily_brief.config import load_config
from tests.conftest import write_config

REPO = Path(__file__).resolve().parent.parent
SEED = (REPO / "brief" / "brief.md").read_text()


@pytest.fixture
def env(tmp_path):
    cfg = load_config(write_config(tmp_path))
    return cfg, TestClient(api.create_app(cfg, player=object(), today=lambda: "2026-09-27"))


def draft(version=1, extra_ignore="sports"):
    return SEED.replace("version: 0", f"version: {version}").replace("source: hand\n", "source: profile-agent\n") \
               .replace('  - "sponsor reads and podcast housekeeping"',
                        f'  - "sponsor reads and podcast housekeeping"\n  - "{extra_ignore}"')


def test_effective_brief_is_seed_until_one_is_approved(env):
    cfg, _ = env
    assert brief.effective_path(cfg) == cfg.brief.path


def test_draft_approve_cycle(env):
    cfg, c = env
    assert c.get("/brief").json()["draft"] is None
    assert c.post("/brief/draft", json={"text": draft()}).status_code == 200
    view = c.get("/brief").json()
    assert view["draft"]["version"] == 1 and "+  - \"sports\"" in view["draft"]["diff"]
    assert c.post("/brief/approve").status_code == 200
    eff = brief.effective_path(cfg)
    assert eff != cfg.brief.path and "sports" in eff.read_text()
    assert list((cfg.paths.var_dir / "brief" / "history").glob("v1-*.md"))
    assert c.get("/brief").json()["draft"] is None


def test_invalid_draft_rejected_with_reason(tmp_path):
    cfg = load_config(write_config(tmp_path, {"tv_forbidden_terms = []": 'tv_forbidden_terms = ["health"]'}))
    c = TestClient(api.create_app(cfg, player=object(), today=lambda: "2026-09-27"))
    r = c.post("/brief/draft", json={"text": draft(extra_ignore="health tips")})  # tv_safe leak
    assert r.status_code == 422 and "personal terms" in r.json()["detail"]


def test_draft_must_bump_version(env):
    _, c = env
    r = c.post("/brief/draft", json={"text": SEED})  # still v0
    assert r.status_code == 422 and "version" in r.json()["detail"]


def test_reject_discards_draft(env):
    cfg, c = env
    c.post("/brief/draft", json={"text": draft()})
    assert c.post("/brief/reject").status_code == 200
    assert c.get("/brief").json()["draft"] is None and brief.effective_path(cfg) == cfg.brief.path


def test_approve_without_draft_is_409(env):
    _, c = env
    assert c.post("/brief/approve").status_code == 409
