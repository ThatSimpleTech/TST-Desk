"""The daemon's own git runs no filter driver (TD-5157).

TD-5108 left this open: a filter driver's ``clean`` ran on the
checkpointer's and memory commit's ``git add`` and on ``git status``, and
its ``smudge`` on the drift revert's ``checkout-index``. The command lives
in git config (Class C to write), but the attribute that routes a file
through it can be an in-tree ``.gitattributes``, an ordinary Class A
write. Each test below plants a driver the user "configured", lets the
agent write ``.gitattributes``, and checks the daemon never runs it.

Staging hashes bytes itself and restoring writes blob bytes, so the
tests also pin CRLF and git-lfs files. Line endings come out as plain git
would make them (git's rules are redone in ``tstd.git_eol``, checked here
against git itself); an LFS file is stored whole in a checkpoint, and a
revert fills an LFS pointer from the local store.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_charter import VALID_YAML_ONLY
from tests.test_checkpoint import _git, make_repo
from tstd import git_stage
from tstd.autonomy import Checkpointer
from tstd.autonomy.charter import charter_path, charter_start_error, sign_charter
from tstd.autonomy.checkpoint import DecisionClass, auto_branch
from tstd.autonomy.revert import restore_auto_branch
from tstd.autonomy.supervisor import _diff_since
from tstd.git_eol import (
    EolConfig,
    checkin_eol,
    checkout_eol,
    eol_config,
    has_text_crlf,
    path_attributes,
)
from tstd.git_raw import GitRawError, filter_pins
from tstd.git_restore import lfs_pointer
from tstd.memory_commit import MEMORY_COMMIT_SUBJECT, MemoryCommitter
from tstd.session import Session

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
    pytest.mark.skipif(os.name == "nt", reason="the planted filter is a POSIX shell command"),
]

BRANCH = auto_branch("filters")


def _blob(repo: Path, rev_path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", rev_path], capture_output=True, check=True
    ).stdout


def _plant(repo: Path, marker: Path, attributes: str, driver: str = "probe") -> None:
    """The user configured *driver*; the agent then wrote ``.gitattributes``."""
    _git(repo, "config", f"filter.{driver}.clean", f"sh -c 'touch {marker}; cat'")
    _git(repo, "config", f"filter.{driver}.smudge", f"sh -c 'touch {marker}; cat'")
    (repo / ".gitattributes").write_text(attributes)


async def _checkpoint(repo: Path, *paths: Path, call: str = "tc") -> str:
    outcome = await Checkpointer(repo, "s1", branch=BRANCH).checkpoint(
        list(paths),
        tool_name="fs_write",
        tool_call_id=call,
        decision_class=DecisionClass.A,
        decision_rule="in-workspace-edit",
    )
    assert outcome.status == "committed", outcome.reason
    assert outcome.commit is not None
    return outcome.commit


def _write_charter(repo: Path) -> Path:
    path = charter_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(VALID_YAML_ONLY, encoding="utf-8")
    return path


# ── The filter never runs ──────────────────────────────────────────────


def test_control_plain_git_add_runs_the_planted_filter(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _plant(repo, marker, "* filter=probe\n")
    (repo / "b.txt").write_text("x\n")
    _git(repo, "add", "b.txt")
    assert marker.exists()


async def test_checkpoint_stages_without_the_clean_filter(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _plant(repo, marker, "* filter=probe\n")
    (repo / "a.txt").write_text("the user's own edit\n")  # dirty: git status hashes it
    (repo / "b.txt").write_text("written by the agent\n")

    commit = await _checkpoint(repo, repo / "b.txt", repo / ".gitattributes")

    assert not marker.exists()
    assert _blob(repo, f"{commit}:b.txt") == b"written by the agent\n"


async def test_dirty_tree_notice_runs_status_with_filters_off(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _plant(repo, marker, "*.txt filter=probe\n")
    # Same size as "base\n": only hashing the file can tell it changed.
    (repo / "a.txt").write_text("BASE\n")

    notice = await Checkpointer(repo, "s1")._dirty_baseline_notice()

    assert notice is not None
    assert not marker.exists()


async def test_process_filter_is_switched_off_too(tmp_path: Path) -> None:
    """A long-running ``process`` driver marked ``required`` neither runs nor fails the call."""
    repo = make_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _git(repo, "config", "filter.lfsish.process", f"sh -c 'touch {marker}; exit 1'")
    _git(repo, "config", "filter.lfsish.required", "true")
    (repo / ".gitattributes").write_text("* filter=lfsish\n")
    (repo / "a.txt").write_text("the user's own edit\n")
    (repo / "b.txt").write_text("agent\n")

    await _checkpoint(repo, repo / "b.txt")

    assert not marker.exists()


async def test_revert_writes_blobs_without_the_smudge_filter(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _plant(repo, marker, "*.txt filter=probe\n")
    (repo / "feature.txt").write_text("good\n")
    good = await _checkpoint(repo, repo / "feature.txt", call="good")
    (repo / "feature.txt").write_text("drifted\n")
    await _checkpoint(repo, repo / "feature.txt", call="drift")
    marker.unlink(missing_ok=True)

    outcome = await restore_auto_branch(repo, good_sha=good, branch=BRANCH)

    assert outcome.status == "reverted"
    assert (repo / "feature.txt").read_text() == "good\n"
    assert not marker.exists()


async def test_memory_commit_runs_no_filter_and_keeps_the_index_in_step(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _plant(repo, marker, "* filter=probe\n")
    note = repo / ".tst" / "memory" / "gotchas.md"
    note.parent.mkdir(parents=True)
    note.write_text("- a gotcha\n")

    outcome = await MemoryCommitter(repo).commit([note])

    assert outcome.status == "committed"
    assert not marker.exists()
    assert _git(repo, "log", "-1", "--format=%s") == MEMORY_COMMIT_SUBJECT
    assert outcome.commit == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "reflog", "-1", "--format=%gs") == f"commit: {MEMORY_COMMIT_SUBJECT}"
    # The user's index holds what HEAD holds, so nothing reads as staged.
    _git(repo, "config", "--remove-section", "filter.probe")
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert ".tst/memory/gotchas.md" not in _git(repo, "status", "--porcelain")
    # Unchanged bytes: nothing to commit.
    again = await MemoryCommitter(repo).commit([note])
    assert again.status == "skipped"


async def test_memory_commit_leaves_the_users_other_staged_work_alone(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("staged by the user\n")
    _git(repo, "add", "a.txt")
    note = repo / ".tst" / "memory" / "MEMORY.md"
    note.parent.mkdir(parents=True)
    note.write_text("first\n")

    await MemoryCommitter(repo).commit([note])

    assert _git(repo, "show", "HEAD:a.txt") == "base"
    assert _git(repo, "show", "HEAD:.tst/memory/MEMORY.md") == "first"
    assert _git(repo, "diff", "--cached", "--name-only") == "a.txt"


async def test_memory_commit_is_signed_when_the_user_signs_commits(tmp_path: Path) -> None:
    """``commit-tree`` ignores ``commit.gpgSign``; ``-S`` is passed as git commit signed."""
    repo = make_repo(tmp_path)
    gpg = tmp_path / "fake-gpg"
    gpg.write_text(
        "#!/bin/sh\ncat >/dev/null\necho '[GNUPG:] SIG_CREATED ' >&2\n"
        "printf -- '-----BEGIN PGP SIGNATURE-----\\nfake\\n-----END PGP SIGNATURE-----\\n'\n"
    )
    gpg.chmod(0o755)
    _git(repo, "config", "commit.gpgSign", "true")
    _git(repo, "config", "gpg.program", str(gpg))
    note = repo / ".tst" / "memory" / "gotchas.md"
    note.parent.mkdir(parents=True)
    note.write_text("- a gotcha\n")

    outcome = await MemoryCommitter(repo).commit([note])

    assert outcome.status == "committed", outcome.reason
    assert "gpgsig " in _git(repo, "cat-file", "commit", "HEAD")


async def test_memory_commit_is_refused_mid_merge(tmp_path: Path) -> None:
    """As ``git commit -- <path>`` refuses a partial commit during a merge."""
    repo = make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    (repo / ".git" / "MERGE_HEAD").write_text(head + "\n")
    note = repo / ".tst" / "memory" / "gotchas.md"
    note.parent.mkdir(parents=True)
    note.write_text("- a gotcha\n")

    outcome = await MemoryCommitter(repo).commit([note])

    assert outcome.status == "error"
    assert _git(repo, "rev-parse", "HEAD") == head


def _racy_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A tracked file whose index entry is "racily clean" for every later index write."""
    repo = make_repo(tmp_path)
    racy = repo / "racy.txt"
    racy.write_text("tracked\n")
    _git(repo, "add", "racy.txt")
    _git(repo, "commit", "-q", "-m", "racy")
    future = 4_102_444_800  # 2100-01-01: newer than any index write in this test
    os.utime(racy, (future, future))
    _git(repo, "update-index", "--refresh")
    marker = tmp_path / "filter-ran"
    _plant(repo, marker, "* filter=probe\n")
    note = repo / ".tst" / "memory" / "gotchas.md"
    note.parent.mkdir(parents=True)
    note.write_text("- a gotcha\n")
    return repo, marker, note


async def test_writing_the_users_index_hashes_no_racy_entry_through_a_filter(
    tmp_path: Path,
) -> None:
    repo, marker, note = _racy_repo(tmp_path)

    outcome = await MemoryCommitter(repo).commit([note])

    assert outcome.status == "committed"
    assert not marker.exists()


async def test_control_without_the_pins_the_index_write_runs_the_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the user's index write carries the pins: git hashes the racy entry."""
    repo, marker, note = _racy_repo(tmp_path)

    async def no_pins(*_args: object) -> list[str]:
        return []

    monkeypatch.setattr(git_stage, "filter_pins", no_pins)
    await MemoryCommitter(repo).commit([note])

    assert marker.exists()


async def test_charter_sign_and_start_gate_run_no_filter(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _plant(repo, marker, "*.md filter=probe\n")
    _write_charter(repo)

    assert await sign_charter(repo) is None
    assert await charter_start_error(repo) is None
    assert await sign_charter(repo) is None  # already signed: no new commit
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert not marker.exists()


async def test_validator_diff_reads_the_working_tree_with_filters_off(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    marker = tmp_path / "filter-ran"
    _plant(repo, marker, "*.txt filter=probe\n")
    (repo / "a.txt").write_text("changed by the agent\n")
    session = Session(str(repo))
    session.autonomy_last_check_sha = _git(repo, "rev-parse", "HEAD")

    diff, _tip = await _diff_since(session)

    assert "+changed by the agent" in diff
    assert not marker.exists()


async def test_pins_cover_every_configured_driver(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _git(repo, "config", "filter.lfs.process", "git-lfs filter-process")
    _git(repo, "config", "filter.My.Odd.clean", "cat")

    pins = await filter_pins(repo, dict(os.environ))

    for name in ("lfs", "My.Odd"):
        for setting in ("clean=", "smudge=", "process=", "required=false"):
            assert f"filter.{name}.{setting}" in pins


async def test_a_driver_name_the_pins_cannot_carry_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _git(repo, "config", "filter.a=b.clean", "cat")
    with pytest.raises(GitRawError):
        await filter_pins(repo, dict(os.environ))


# ── Staging mirrors git add ────────────────────────────────────────────


async def test_staging_keeps_git_adds_shape(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    tool = repo / "tool.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "x.py").write_text("x = 1\n")
    (repo / "link").symlink_to("a.txt")
    (repo / "a.txt").unlink()

    commit = await _checkpoint(repo, tool, repo / "pkg", repo / "link", repo / "a.txt")

    tree = _git(repo, "ls-tree", "-r", commit)
    assert "100755 blob" in next(line for line in tree.splitlines() if line.endswith("tool.sh"))
    assert "pkg/x.py" in tree
    assert "120000 blob" in next(line for line in tree.splitlines() if line.endswith("link"))
    assert "\ta.txt" not in tree
    assert _blob(repo, f"{commit}:link") == b"a.txt"

    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("obj\n")
    outcome = await Checkpointer(repo, "s1", branch=BRANCH).checkpoint(
        [repo / "build" / "out.o"],
        tool_name="fs_write",
        tool_call_id="ign",
        decision_class=DecisionClass.A,
    )
    assert outcome.status == "error"
    assert "ignored" in outcome.reason


# ── CRLF ───────────────────────────────────────────────────────────────


def _crlf_repo(tmp_path: Path, how: str) -> Path:
    """``win.txt`` committed as LF, checked out as CRLF by *how*."""
    repo = make_repo(tmp_path)
    if how == "attributes":
        (repo / ".gitattributes").write_text("*.txt text eol=crlf\n")
        _git(repo, "add", ".gitattributes")
    else:
        _git(repo, "config", "core.autocrlf", "true")
    (repo / "win.txt").write_bytes(b"line1\r\nline2\r\n")
    _git(repo, "add", "win.txt")
    _git(repo, "commit", "-q", "-m", "crlf")
    return repo


@pytest.mark.parametrize("how", ["attributes", "autocrlf"])
async def test_crlf_file_round_trips_through_a_checkpoint(tmp_path: Path, how: str) -> None:
    repo = _crlf_repo(tmp_path, how)
    assert _blob(repo, "HEAD:win.txt") == b"line1\nline2\n"  # git normalised it
    win = repo / "win.txt"
    win.write_bytes(b"one\r\ntwo\r\n")
    good = await _checkpoint(repo, win, call="good")
    # Unchanged from git add: the checkpoint stores the normalised blob.
    assert _blob(repo, f"{good}:win.txt") == b"one\ntwo\n"
    assert _git(repo, "rev-parse", f"{good}:win.txt") == _git(repo, "hash-object", "win.txt")
    win.write_bytes(b"drifted\r\n")
    await _checkpoint(repo, win, call="drift")

    await restore_auto_branch(repo, good_sha=good, branch=BRANCH)

    assert win.read_bytes() == b"one\r\ntwo\r\n"


@pytest.mark.parametrize("how", ["attributes", "autocrlf"])
async def test_crlf_file_from_head_comes_back_with_crlf(tmp_path: Path, how: str) -> None:
    repo = _crlf_repo(tmp_path, how)
    (repo / "other.txt").write_bytes(b"x\r\n")
    good = await _checkpoint(repo, repo / "other.txt", call="good")  # win.txt still HEAD's
    (repo / "win.txt").write_bytes(b"drifted\r\n")
    await _checkpoint(repo, repo / "win.txt", call="drift")

    await restore_auto_branch(repo, good_sha=good, branch=BRANCH)

    assert (repo / "win.txt").read_bytes() == b"line1\r\nline2\r\n"
    assert "win.txt" not in _git(repo, "status", "--porcelain")


@pytest.mark.parametrize(
    ("data", "attrs", "cfg", "expected"),
    [
        (b"a\nb\n", {"text": "set"}, EolConfig(autocrlf="true"), b"a\r\nb\r\n"),
        (b"a\nb\n", {"text": "set"}, EolConfig(eol="crlf"), b"a\r\nb\r\n"),
        (b"a\nb\n", {"text": "set"}, EolConfig(native_crlf=False), b"a\nb\n"),
        (b"a\nb\n", {"text": "set"}, EolConfig(native_crlf=True), b"a\r\nb\r\n"),
        (b"a\nb\n", {"text": "set"}, EolConfig(autocrlf="input", eol="crlf"), b"a\nb\n"),
        (b"a\nb\n", {"eol": "crlf"}, EolConfig(), b"a\r\nb\r\n"),
        (b"a\nb\n", {"text": "auto", "eol": "lf"}, EolConfig(autocrlf="true"), b"a\nb\n"),
        (b"a\nb\n", {}, EolConfig(autocrlf="true"), b"a\r\nb\r\n"),
        (b"a\nb\n", {}, EolConfig(autocrlf="input"), b"a\nb\n"),
        (b"a\nb\n", {}, EolConfig(autocrlf="false", native_crlf=True), b"a\nb\n"),
        (b"a\nb\n", {"text": "unset"}, EolConfig(autocrlf="true"), b"a\nb\n"),
        (b"a\r\nb\n", {}, EolConfig(autocrlf="true"), b"a\r\nb\n"),  # safer autocrlf
        (b"a\r\nb\n", {"text": "set", "eol": "crlf"}, EolConfig(), b"a\r\nb\r\n"),
        (b"\x00\x01\n", {}, EolConfig(autocrlf="true"), b"\x00\x01\n"),  # binary
        (b"a\nb\n", {"crlf": "unset"}, EolConfig(autocrlf="true"), b"a\nb\n"),  # legacy crlf
    ],
)
def test_checkout_eol_follows_gits_rules(
    data: bytes, attrs: dict[str, str], cfg: EolConfig, expected: bytes
) -> None:
    assert checkout_eol(data, attrs, cfg) == expected


CONTENTS = [b"a\nb\n", b"a\r\nb\r\n", b"a\r\nb\n", b"a\rb\n", b"\x00a\r\n", b"", b"a"]
ATTRIBUTES = [
    "",
    "text",
    "text=auto",
    "-text",
    "text eol=crlf",
    "text eol=lf",
    "eol=crlf",
    "text=auto eol=crlf",
    "-crlf",
]


@pytest.mark.parametrize("autocrlf", ["false", "true", "input"])
@pytest.mark.parametrize("attribute", ATTRIBUTES)
async def test_eol_rules_match_git_itself(tmp_path: Path, autocrlf: str, attribute: str) -> None:
    """Differential: our checkin and checkout bytes equal git's, with no filter in play."""
    repo = make_repo(tmp_path)
    _git(repo, "config", "core.autocrlf", autocrlf)
    (repo / ".gitattributes").write_text(f"f.txt {attribute}\n" if attribute else "")
    env = dict(os.environ)
    attrs = (await path_attributes(repo, ["f.txt"], env, cached=False)).get("f.txt", {})
    cfg, _symlinks = await eol_config(repo, env)

    def git_bytes(*args: str, data: bytes = b"") -> bytes:
        return subprocess.run(
            ["git", "-C", str(repo), *args], input=data, capture_output=True, check=True
        ).stdout

    for data in CONTENTS:
        for index_blob in (None, b"x\r\ny\r\n"):  # untracked, or stored with CRLF
            if index_blob is None:
                _git(repo, "rm", "-q", "--cached", "--ignore-unmatch", "f.txt")
            else:
                oid = git_bytes("hash-object", "-w", "--stdin", data=index_blob).decode().strip()
                _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{oid},f.txt")
            (repo / "f.txt").write_bytes(data)
            _git(repo, "add", "f.txt")  # reads the index, for the "safer autocrlf" check
            expected = git_bytes("cat-file", "blob", ":f.txt")
            ours = checkin_eol(
                data,
                attrs,
                cfg,
                index_has_crlf=index_blob is not None and has_text_crlf(index_blob),
            )
            assert ours == expected, (data, index_blob)
        raw = git_bytes("hash-object", "-w", "--no-filters", "--stdin", data=data).decode().strip()
        checked_out = git_bytes("cat-file", "--filters", "--path=f.txt", raw)
        assert checkout_eol(data, attrs, cfg) == checked_out, data


# ── git-lfs ────────────────────────────────────────────────────────────

PAYLOAD = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def _pointer(data: bytes) -> bytes:
    oid = hashlib.sha256(data).hexdigest()
    return (
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(data)}\n"
    ).encode()


def _lfs_repo(tmp_path: Path, *, stored: bool) -> tuple[Path, Path]:
    """``big.bin`` committed as an LFS pointer, its object in the local store if *stored*."""
    repo = make_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    _git(repo, "add", ".gitattributes")
    pointer = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=_pointer(PAYLOAD),
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{pointer},big.bin")
    _git(repo, "commit", "-q", "-m", "lfs")
    (repo / "big.bin").write_bytes(PAYLOAD)  # what git-lfs smudge put on disk
    if stored:
        oid = hashlib.sha256(PAYLOAD).hexdigest()
        obj = repo / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        obj.parent.mkdir(parents=True)
        obj.write_bytes(PAYLOAD)
    marker = tmp_path / "lfs-ran"
    _git(repo, "config", "filter.lfs.process", f"sh -c 'touch {marker}; exit 1'")
    _git(repo, "config", "filter.lfs.clean", f"sh -c 'touch {marker}; cat'")
    _git(repo, "config", "filter.lfs.smudge", f"sh -c 'touch {marker}; cat'")
    _git(repo, "config", "filter.lfs.required", "true")
    return repo, marker


async def test_lfs_file_is_checkpointed_whole_and_round_trips(tmp_path: Path) -> None:
    repo, marker = _lfs_repo(tmp_path, stored=True)
    big = repo / "big.bin"
    big.write_bytes(PAYLOAD + b"v2")
    good = await _checkpoint(repo, big, call="good")
    # What changes: the content itself, not a pointer, is in the checkpoint.
    assert _blob(repo, f"{good}:big.bin") == PAYLOAD + b"v2"
    big.write_bytes(b"drifted")
    await _checkpoint(repo, big, call="drift")

    outcome = await restore_auto_branch(repo, good_sha=good, branch=BRANCH)

    assert big.read_bytes() == PAYLOAD + b"v2"
    assert outcome.pointers == ()
    assert not marker.exists()


async def test_lfs_pointer_from_head_is_filled_from_the_local_store(tmp_path: Path) -> None:
    repo, marker = _lfs_repo(tmp_path, stored=True)
    (repo / "other.txt").write_text("x\n")
    good = await _checkpoint(repo, repo / "other.txt", call="good")
    (repo / "big.bin").write_bytes(b"drifted")
    await _checkpoint(repo, repo / "big.bin", call="drift")

    outcome = await restore_auto_branch(repo, good_sha=good, branch=BRANCH)

    assert (repo / "big.bin").read_bytes() == PAYLOAD
    assert outcome.pointers == ()
    assert not marker.exists()


async def test_lfs_pointer_without_a_local_object_is_written_and_reported(
    tmp_path: Path,
) -> None:
    repo, marker = _lfs_repo(tmp_path, stored=False)
    (repo / "other.txt").write_text("x\n")
    good = await _checkpoint(repo, repo / "other.txt", call="good")
    (repo / "big.bin").write_bytes(b"drifted")
    await _checkpoint(repo, repo / "big.bin", call="drift")

    outcome = await restore_auto_branch(repo, good_sha=good, branch=BRANCH)

    assert (repo / "big.bin").read_bytes() == _pointer(PAYLOAD)
    assert outcome.pointers == ("big.bin",)
    assert not marker.exists()


def test_lfs_pointer_parsing() -> None:
    oid = hashlib.sha256(PAYLOAD).hexdigest()
    assert lfs_pointer(_pointer(PAYLOAD)) == (oid, len(PAYLOAD))
    assert lfs_pointer(PAYLOAD) is None
    assert lfs_pointer(b"version https://example.com/v1\noid sha256:" + b"0" * 64) is None
