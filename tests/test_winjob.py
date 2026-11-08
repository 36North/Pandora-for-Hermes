"""Windows job-object tests: closing the job must take its child down."""

from __future__ import annotations

import subprocess
import sys

import pytest

from pandora.pandora_winjob import KillOnCloseJob

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")


def test_job_object_is_active_on_windows():
    job = KillOnCloseJob()
    try:
        assert job.active
    finally:
        job.close()


def test_closing_the_job_kills_the_child():
    """The kernel must terminate members when the last job handle closes."""
    job = KillOnCloseJob()
    assert job.active
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert job.assign(proc.pid) is True
        job.close()
        proc.wait(timeout=10)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_player_shutdown_closes_the_job():
    import pandora.pandora_player as pp

    player = pp.PandoraPlayer()
    player._job = KillOnCloseJob()
    assert player._job.active
    player.shutdown()
    assert player._job is None
