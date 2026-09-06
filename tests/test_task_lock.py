import json
import os
import subprocess
import sys
import time

from browser_use.webretriever.artifacts import TaskLock


def test_lock_blocks_other_process_and_releases_after_exit(tmp_path):
    path = tmp_path / 'task.lock'
    owner = TaskLock(path)
    assert owner.acquire()
    probe = (
        'from pathlib import Path; import sys; '
        'from browser_use.webretriever.artifacts import TaskLock; '
        'lock = TaskLock(Path(sys.argv[1])); '
        'sys.exit(0 if lock.acquire() else 2)'
    )
    try:
        result = subprocess.run([sys.executable, '-c', probe, str(path)], timeout=20)
        assert result.returncode == 2
    finally:
        owner.release()
    assert json.loads(path.read_text(encoding='utf-8'))['pid'] == os.getpid()
    result = subprocess.run([sys.executable, '-c', probe, str(path)], timeout=20)
    assert result.returncode == 0
    # The child exits without release(): the OS must recover its lock.
    assert owner.acquire()
    owner.release()


def test_blocking_lock_waits_for_owner(tmp_path):
    path = tmp_path / 'task.lock'
    waiting = tmp_path / 'waiting'
    acquired = tmp_path / 'acquired'
    owner = TaskLock(path)
    assert owner.acquire()
    child = subprocess.Popen([
        sys.executable, '-c',
        'from pathlib import Path; import sys; '
        'from browser_use.webretriever.artifacts import TaskLock; '
        'Path(sys.argv[2]).touch(); '
        'lock = TaskLock(Path(sys.argv[1])); lock.acquire(blocking=True); '
        'Path(sys.argv[3]).touch(); lock.release()',
        str(path), str(waiting), str(acquired),
    ])
    try:
        deadline = time.monotonic() + 20
        while not waiting.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert waiting.exists()
        time.sleep(0.2)
        assert child.poll() is None
        assert not acquired.exists()
        owner.release()
        assert child.wait(timeout=20) == 0
        assert acquired.exists()
    finally:
        owner.release()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
