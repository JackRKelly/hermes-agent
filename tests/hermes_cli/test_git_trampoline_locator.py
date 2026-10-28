"""OS-neutral locator fixtures; native Windows execution lives in the live suite."""

import subprocess

import pytest

from hermes_cli import update_cmd_git as git


@pytest.mark.parametrize("arch", ["mingw64", "mingw32", "clangarm64"])
@pytest.mark.parametrize("entry", ["cmd", "bin"])
def test_locate_real_git_from_path_install(tmp_path, monkeypatch, arch, entry):
    # The locked Windows CI PortableGit is outside Program Files and Hermes home.
    root = tmp_path / "setup-pm" / "tools" / "git-install"
    shim_dir = root / entry
    shim_dir.mkdir(parents=True)
    (shim_dir / "git.exe").write_bytes(b"shim fixture, not executed")
    core = root / arch / "libexec" / "git-core" / "git.exe"
    core.parent.mkdir(parents=True)
    core.write_bytes(b"core fixture, not executed")
    monkeypatch.setenv("PATH", str(shim_dir))
    monkeypatch.setattr(git, "_portable_git_candidates", list)
    probed = []

    def probe(argv, *, require_version):
        assert require_version is True
        probed.append(argv)
        return False if argv == [str(core)] else None

    monkeypatch.setattr(git, "_probe_fork_bomb", probe)
    assert git._locate_real_git() == core
    assert [str(shim_dir / "git.exe")] not in probed


@pytest.mark.parametrize(
    "returncode,stdout,stderr,expected",
    [
        (0, "git version 2.53.0.windows.3\n", "", False),
        (1, "git version 2.53.0.windows.3\n", "failure", None),
        (0, "", "", None),
        (0, "not git\n", "", None),
        (1, "", "BUG (fork bomb): tried to spawn itself", True),
        (0, "git version 2.53.0\n", "BUG (fork bomb)", True),
    ],
)
def test_probe_requires_successful_git_version(
    monkeypatch, returncode, stdout, stderr, expected
):
    from hermes_cli import update_custody

    def run_git(argv, args, **kwargs):
        assert argv == ["candidate"]
        assert args == ["--version"]
        assert kwargs["timeout"] == 15
        return subprocess.CompletedProcess(argv + args, returncode, stdout, stderr)

    monkeypatch.setattr(update_custody, "run_git", run_git)
    assert git._probe_fork_bomb(["candidate"], require_version=True) is expected
    # Existing detection callers only ask whether the guard text was present.
    assert git._probe_fork_bomb(["candidate"]) is (False if expected is None else expected)


@pytest.mark.parametrize("rejected", [True, None])
def test_locator_skips_guarded_or_failed_core(tmp_path, monkeypatch, rejected):
    first = tmp_path / "broken" / "mingw64" / "libexec" / "git-core" / "git.exe"
    second = tmp_path / "healthy" / "mingw64" / "libexec" / "git-core" / "git.exe"
    for core in (first, second):
        core.parent.mkdir(parents=True)
        core.write_bytes(b"fixture, not executed")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(git, "_portable_git_candidates", lambda: [first, second])
    monkeypatch.setattr(
        git, "_probe_fork_bomb",
        lambda argv, **kwargs: False if argv == [str(second)] else rejected,
    )
    assert git._locate_real_git() == second


def test_locator_does_not_accept_directory_or_path_shim(tmp_path, monkeypatch):
    root = tmp_path / "Git"
    shim_dir = root / "cmd"
    shim_dir.mkdir(parents=True)
    (shim_dir / "git.exe").write_bytes(b"shim fixture, not executed")
    (root / "mingw64" / "libexec" / "git-core" / "git.exe").mkdir(parents=True)
    monkeypatch.setenv("PATH", str(shim_dir))
    monkeypatch.setattr(git, "_portable_git_candidates", list)
    monkeypatch.setattr(
        git, "_probe_fork_bomb",
        lambda argv, **kwargs: False if str(root) in argv[0] else None,
    )
    assert git._locate_real_git() is None


def test_failed_spawn_is_not_a_healthy_probe(monkeypatch):
    from hermes_cli import update_custody

    def fail(*args, **kwargs):
        raise OSError("fixture spawn failure")

    monkeypatch.setattr(update_custody, "run_git", fail)
    assert git._probe_fork_bomb(["candidate"]) is None
    assert git._git_is_trampoline(["candidate"]) is False
