"""One (swarm, role) must be exactly one process: zombies get reaped."""

import subprocess
import sys
import time

from relay.cli import procs


def _fake_worker(swarm: str, role: str) -> subprocess.Popen:
    # sys.argv junk after -c lands in the ps command line, matching a real worker's
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         "relay.workers.run", "--swarm", swarm, "--role", role],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def test_find_and_reap_orphans() -> None:
    zombie = _fake_worker("reaptest", "builder")
    keeper = _fake_worker("reaptest", "builder")
    other_role = _fake_worker("reaptest", "analyst")
    other_swarm = _fake_worker("reaptest-2", "builder")
    try:
        time.sleep(0.3)
        found = procs.find_workers("reaptest", "builder")
        assert zombie.pid in found and keeper.pid in found
        assert other_role.pid not in found and other_swarm.pid not in found

        reaped = procs.reap_orphans("reaptest", "builder", keep=keeper.pid)
        assert reaped == [zombie.pid]
        assert zombie.wait(timeout=3) != 0           # reaped (wait also clears the defunct entry)
        assert keeper.poll() is None                 # the tracked one survives
        assert other_role.poll() is None             # other roles untouched
        assert other_swarm.poll() is None            # other swarms untouched

        assert set(procs.reap_swarm("reaptest")) == {keeper.pid, other_role.pid}
    finally:
        for proc in (zombie, keeper, other_role, other_swarm):
            proc.kill()
