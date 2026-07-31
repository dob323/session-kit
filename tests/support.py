from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
from typing import Iterable


REPO = Path(__file__).resolve().parents[1]
RELEASE_TOOL = REPO / "deploy" / "session-kit-release"
HELPERS = ("sp", "shpool_login", "shpool_status", "shpool_reaper", "codex_resume_here")


def run(
    argv: Iterable[os.PathLike[str] | str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["PYTHONDONTWRITEBYTECODE"] = "1"
    if env:
        merged.update(env)
    proc = subprocess.run(
        [os.fspath(v) for v in argv],
        cwd=cwd,
        env=merged,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode:
        raise AssertionError(
            f"command failed ({proc.returncode}): {list(argv)!r}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo).stdout.strip()


def make_git_repo(base: Path, version: str = "v1") -> tuple[Path, str]:
    repo = base / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Session Kit Tests")
    git(repo, "config", "user.email", "tests@example.invalid")
    write_runtime(repo, version)
    git(
        repo,
        "add",
        "bin",
        "lib",
        "bashrc",
        "config",
        "deploy",
        "shpool",
        "README.md",
    )
    git(repo, "commit", "-qm", f"runtime {version}")
    return repo, git(repo, "rev-parse", "HEAD")


def write_runtime(repo: Path, version: str) -> str:
    bindir = repo / "bin"
    bindir.mkdir(exist_ok=True)
    for helper in HELPERS:
        path = bindir / helper
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s:%s' {version!r} \"${{0##*/}}\"\n"
            "printf ':%s' \"$@\"\n"
            "printf '\\n'\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    common = bindir / "session_kit_common"
    common.write_text("# fixture shared shell helpers\n", encoding="utf-8")
    common.chmod(0o644)
    (repo / "README.md").write_text(f"fixture {version}\n", encoding="utf-8")
    required = {
        "lib/session_inventory.py": "# fixture inventory\n",
        "lib/sessionkit_inventory/__init__.py": "# fixture package\n",
        "lib/sessionkit_inventory/common.py": "# fixture common helpers\n",
        "lib/sessionkit_inventory/lifecycle.py": "# fixture lifecycle helpers\n",
        "lib/sessionkit_inventory/providers.py": "# fixture provider helpers\n",
        "lib/sessionkit_inventory/state_io.py": "# fixture state helpers\n",
        "bashrc/shpool.bashrc": "# fixture bashrc\n",
        "config/projects.example.tsv": "# alias\tprovider\tcwd\n",
        "config/session-inventory.example.json": '{"schema_version":1}\n',
        "config/shpool.example.toml": (
            "session_restore_mode = { lines = 500 }\n"
            "output_spool_lines = 1000\n"
            "vt100_output_spool_width = 200\n"
        ),
        "shpool/config.toml": (
            "session_restore_mode = { lines = 500 }\n"
            "output_spool_lines = 1000\n"
            "vt100_output_spool_width = 200\n"
        ),
    }
    for relative, content in required.items():
        path = repo / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8")
    deploy_dir = repo / "deploy"
    deploy_dir.mkdir(exist_ok=True)
    launcher = deploy_dir / "session-kit-launcher"
    launcher.write_text(
        (REPO / "deploy/session-kit-launcher").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return version


def commit_runtime(repo: Path, version: str) -> str:
    write_runtime(repo, version)
    git(repo, "add", "bin", "lib", "bashrc", "config", "deploy", "README.md")
    git(repo, "commit", "-qm", f"runtime {version}")
    return git(repo, "rev-parse", "HEAD")


def make_backup_bundle(
    base: Path,
    canonical_head: str,
    *,
    home_prefix: str = "home/sessionuser",
) -> Path:
    bundle = base / "backup"
    bundle.mkdir(parents=True)
    payloads = {
        "bundle.sha256": b"fixture bundle digest\n",
        "canonical-state.txt": f"head={canonical_head}\nstatus_entries=0\n".encode(),
        "current-identities.json": b'{"schema_version":1,"sessions":[]}\n',
        "shpool-list.json": b'{"sessions":[]}\n',
    }
    run(
        [
            "git",
            "bundle",
            "create",
            bundle / "canonical-head.bundle",
            "--all",
        ],
        cwd=base / "source",
    )
    installed = bundle / "installed-files.tar"
    with tarfile.open(installed, mode="w") as archive:
        for name in (
            f"{home_prefix}/.local/bin/shpool_login",
            f"{home_prefix}/.local/bin/shpool_reaper",
            f"{home_prefix}/.local/bin/shpool_status",
            f"{home_prefix}/.local/bin/sp",
            f"{home_prefix}/.local/bin/codex_resume_here",
            f"{home_prefix}/.bashrc",
            f"{home_prefix}/.config/shpool/config.toml",
            f"{home_prefix}/.config/systemd/user/shpool.service",
            f"{home_prefix}/.config/systemd/user/shpool.socket",
            f"{home_prefix}/.config/systemd/user/shpool-reaper.service",
            f"{home_prefix}/.config/systemd/user/shpool-reaper.timer",
        ):
            source = bundle / ".tar-source"
            source.write_text(f"{name}\n", encoding="utf-8")
            source.chmod(0o755 if "/.local/bin/" in name else 0o644)
            archive.add(source, arcname=name, recursive=False)
    (bundle / ".tar-source").unlink()
    checksum_lines: list[str] = []
    stat_lines: list[str] = []
    with tarfile.open(installed, mode="r:") as archive:
        for member in archive:
            extracted = archive.extractfile(member)
            assert extracted is not None
            content = extracted.read()
            checksum_lines.append(
                f"{hashlib.sha256(content).hexdigest()}  {member.name}\n"
            )
            stat_lines.append(
                f"{member.mode & 0o777:o}\\t{member.uid}\\t{member.gid}"
                f"\\t{member.size}\\t1\\t2020-01-01 00:00:00 +0000"
                f"\\t{member.name}\n"
            )
    payloads["source-files.before.sha256"] = "".join(checksum_lines).encode()
    payloads["source-files.after.sha256"] = "".join(checksum_lines).encode()
    payloads["source-files.stat"] = "".join(stat_lines).encode()
    for name, content in payloads.items():
        (bundle / name).write_bytes(content)
    payload_names = sorted([*payloads, "canonical-head.bundle", "installed-files.tar"])
    manifest = bundle / "MANIFEST.sha256"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256((bundle / name).read_bytes()).hexdigest()}  {name}\n"
            for name in payload_names
        ),
        encoding="utf-8",
    )
    marker = {
        "schema_version": 1,
        "complete": True,
        "created_at": "2020-01-01T00:00:00+00:00",
        "source": "session-kit-predeploy",
        "canonical_head": canonical_head,
        "manifest": "MANIFEST.sha256",
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "manifest_scope": (
            "all regular payload files except MANIFEST.sha256 and BACKUP_COMPLETE.json"
        ),
    }
    (bundle / "BACKUP_COMPLETE.json").write_text(
        json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8"
    )
    return bundle


def make_sentinels(home: Path) -> None:
    for name in (".no_shpool", ".no_shpool_reaper"):
        (home / name).write_text("", encoding="utf-8")


def json_stdout(proc: subprocess.CompletedProcess[str]) -> dict:
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"not JSON: {proc.stdout!r}") from exc


class IsolatedTree:
    def __init__(self) -> None:
        # Some supported hosts mount /tmp noexec. Release launch tests need to
        # execute immutable fixture helpers, so keep the disposable tree on the
        # repository's executable filesystem.
        self.temp = tempfile.TemporaryDirectory(prefix=".session-kit-test-", dir=REPO)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.root = self.home / ".local/lib/session-kit"
        self.release_root = self.root / "releases"
        self.bin_dir = self.home / ".local/bin"
        self.state_dir = self.home / ".local/state/session-kit/deploy"

    def close(self) -> None:
        self.temp.cleanup()

    def build(self, repo: Path, sha: str) -> Path:
        run(
            [
                RELEASE_TOOL,
                "build",
                "--repo",
                repo,
                "--sha",
                sha,
                "--release-root",
                self.release_root,
            ]
        )
        return self.release_root / sha

    def activation_args(self, sha: str, backup: Path) -> list[os.PathLike[str] | str]:
        proof = self.state_dir / f"integration-proof-{sha}"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text(f"session-kit-integration-v1 {sha}\n", encoding="utf-8")
        proof.chmod(0o600)
        return [
            "--commit",
            sha,
            "--backup-bundle",
            backup,
            "--root",
            self.root,
            "--home",
            self.home,
            "--bin-dir",
            self.bin_dir,
            "--state-dir",
            self.state_dir,
            "--integration-marker",
            self.state_dir.parent / "integration-ready-v1",
            "--integration-proof",
            proof,
        ]


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)
