"""Per-process temporary directories: a process killed with SIGKILL never runs its
`finally`, so its temporary files are removed by the next process that starts."""
import os
import subprocess
import sys
import tempfile

import pytest

from app.core import tempdirs


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("TMPDIR", raising=False)
    return tmp_path


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_a_process_writes_its_temporary_files_in_a_directory_of_its_own(root, monkeypatch):
    d = tempdirs.use_process_dir(root)
    assert d == root / "tabularia" / str(os.getpid())
    fd, path = tempfile.mkstemp(prefix="dataprep_")
    os.close(fd)
    assert os.path.dirname(path) == str(d)
    assert os.environ["TMPDIR"] == str(d)  # subprocesses and DuckDB spills follow it


def test_the_directory_of_a_dead_process_is_removed(root):
    dead = root / "tabularia" / str(_dead_pid())
    dead.mkdir(parents=True)
    (dead / "dataprep_x.parquet").write_bytes(b"x" * 1024)
    tempdirs.use_process_dir(root)
    assert not dead.exists()


def test_the_directory_of_a_live_process_is_kept(root):
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        d = root / "tabularia" / str(live.pid)
        d.mkdir(parents=True)
        (d / "dataprep_running.parquet").write_bytes(b"x")
        tempdirs.use_process_dir(root)
        assert (d / "dataprep_running.parquet").exists()  # a long run in progress is never touched
    finally:
        live.kill()
        live.wait()


def test_a_zombie_counts_as_dead(root):
    z = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, z.pid, os.WEXITED | os.WNOWAIT)  # exited, not reaped yet
        d = root / "tabularia" / str(z.pid)
        d.mkdir(parents=True)
        assert tempdirs.sweep_dead(root) == 1 and not d.exists()
    finally:
        z.wait()


def test_entries_that_are_not_process_directories_are_left_alone(root):
    other = root / "tabularia" / "notes"
    other.mkdir(parents=True)
    tempdirs.sweep_dead(root)
    assert other.exists()


def test_loose_files_of_the_old_layout_are_removed_at_worker_start(root):
    for name in ("dataprep_a.parquet", "duckdb_b.parquet", "chext_c.parquet", "db_ingest_d.parquet", "upload_e"):
        (root / name).write_bytes(b"x")
    (root / "duckdb-spill1").mkdir()
    (root / "someone_else.txt").write_bytes(b"x")
    assert tempdirs.sweep_loose(root) == 6
    assert sorted(p.name for p in root.iterdir()) == ["someone_else.txt"]
