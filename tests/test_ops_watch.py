import shutil
import subprocess

import pytest

from tests.conftest import REPO


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not installed")
def test_brief_watch_fixture_script_passes():
    r = subprocess.run(["bash", str(REPO / "ops" / "watcher" / "test-brief-watch.sh")], capture_output=True,
                       text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
