import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from repo_model import Package, parse_srcinfo, satisfies
from resolve import choose, dependency_graph, order
import publish


class ModelTests(unittest.TestCase):
    def test_build_bootstraps_chroot_under_an_existing_parent(self):
        script = Path(__file__).resolve().parents[1] / "scripts/ci-build.sh"
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            tools = workspace / "bin"
            tools.mkdir()
            scratch = workspace / "scratch"
            scratch.mkdir()
            commands = {
                "python": """#!/bin/sh
set -eu
case "$1" in
  scripts/resolve.py) printf '{"order":["example"]}\\n' > "$3" ;;
  scripts/build.py)
    grep -q '!debug' "$2/root/etc/makepkg.conf"
    test -s "$2/root/etc/pacman.conf"
    touch built
    ;;
  *) exec "$TEST_PYTHON" "$@" ;;
esac
""",
                "mkarchroot": """#!/bin/sh
set -eu
test "$1" = -C
test "$2" = /etc/pacman.conf
shift 2
root=$(readlink -f "$1")
test ! -e "$root"
test "$2" = base-devel
mkdir "$root"
mkdir "$root/etc"
# 模拟 arch-nspawn 通过 -C 导入配置，而非依赖 pacstrap 生成配置。
printf '[options]\\n' > "$root/etc/pacman.conf"
""",
            }
            for name, content in commands.items():
                executable = tools / name
                executable.write_text(content)
                executable.chmod(0o755)

            result = subprocess.run(
                ["bash", str(script)], cwd=workspace, text=True, capture_output=True,
                env={
                    **os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
                    "TMPDIR": str(scratch), "TEST_PYTHON": sys.executable,
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((workspace / "built").is_file())

    def test_all_jobs_install_git_before_checkout(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/repository.yml"
        ).read_text()
        jobs = re.findall(
            r"(?ms)^  (verify|build|publish):\n(.*?)(?=^  [a-z][a-z_-]*:\n|\Z)",
            workflow,
        )
        self.assertEqual({name for name, _ in jobs}, {"verify", "build", "publish"})
        for name, body in jobs:
            with self.subTest(job=name):
                install = body.index("pacman -Syu")
                checkout = body.index("uses: actions/checkout@")
                self.assertLess(install, checkout)
                self.assertIn("git", body[install:].splitlines()[0].split())
                self.assertIn("git rev-parse --verify HEAD", body)
                trust = body.index('git config --global --add safe.directory "$GITHUB_WORKSPACE"')
                self.assertLess(checkout, trust)
                self.assertLess(trust, body.index("git rev-parse --verify HEAD"))

    def test_split_architecture_and_versions(self):
        packages, requirements = parse_srcinfo("""
pkgbase = example
pkgver = 2.0
pkgrel = 3
epoch = 1
arch = any
depends = shared
makedepends_x86_64 = compiler
checkdepends = tester
pkgname = example-one
provides = virtual=2
pkgname = example-two
depends = different
optdepends = optional: not required
""", "example")
        self.assertEqual(packages[0].version, "1:2.0-3")
        self.assertEqual(packages[0].depends, ["shared"])
        self.assertEqual(packages[1].depends, ["different"])
        self.assertEqual(requirements, ["compiler", "different", "shared", "tester"])

    @unittest.skipUnless(shutil.which("vercmp"), "需要 vercmp")
    def test_provides_and_constraints(self):
        package = Package("real", "3-1", "real", ["virtual=2", "unversioned"])
        self.assertTrue(satisfies(package, "real>=3"))
        self.assertTrue(satisfies(package, "virtual>=2"))
        self.assertFalse(satisfies(package, "virtual>2"))
        self.assertFalse(satisfies(package, "unversioned>=1"))

    def test_ambiguous_provider_requires_override(self):
        packages = [Package(n, "1", n, ["virtual"]) for n in ("a", "b")]
        with self.assertRaises(ValueError):
            choose("virtual", packages, {})
        self.assertEqual(choose("virtual", packages, {"virtual": "b"}).name, "b")

    def test_topology_and_cycles(self):
        self.assertEqual(order({"a": {"b"}, "b": {"c"}, "c": set()}, {"a"}),
                         ["c", "b", "a"])
        with self.assertRaises(ValueError):
            order({"a": {"b"}, "b": {"a"}}, {"a"})

    def test_missing_dependency_requires_manual_package(self):
        with self.assertRaisesRegex(ValueError, "app.*missing.*手动"):
            dependency_graph(
                {"app": [Package("app", "1", "app")]},
                {"app": ["missing"]}, [], {},
            )

    def test_local_dependency_overrides_official(self):
        graph = dependency_graph(
            {"app-dir": [Package("app", "1", "app")],
             "library-dir": [Package("library", "1", "library")]},
            {"app-dir": ["library"], "library-dir": ["official"]},
            [Package("library", "2", ""), Package("official", "1", "")], {},
        )
        self.assertEqual(order(graph, {"app-dir"}), ["library-dir", "app-dir"])

    def test_official_virtual_dependency_defers_provider_selection_to_pacman(self):
        graph = dependency_graph(
            {"app": [Package("app", "1", "app")]},
            {"app": ["java-runtime"]},
            [Package("jdk-openjdk", "25", "", ["java-runtime=25"]),
             Package("jre-openjdk", "25", "", ["java-runtime=25"])],
            {},
        )
        self.assertEqual(graph, {"app": set()})

    @unittest.skipUnless(shutil.which("vercmp"), "需要 vercmp")
    def test_official_virtual_dependency_still_checks_version(self):
        with self.assertRaisesRegex(ValueError, "java-runtime>=25"):
            dependency_graph(
                {"app": [Package("app", "1", "app")]},
                {"app": ["java-runtime>=25"]},
                [Package("jre17-openjdk", "17", "", ["java-runtime=17"]),
                 Package("jre21-openjdk", "21", "", ["java-runtime=21"])],
                {},
            )

    def test_retention_is_per_package_and_rejects_overwrite(self):
        history = []
        for number in range(1, 4):
            history = publish.retain(history, {
                "filename": f"a-{number}.pkg.tar.zst", "sha256": str(number),
            })
        self.assertEqual([r["sha256"] for r in history], ["3", "2"])
        with self.assertRaises(ValueError):
            publish.retain(history, {"filename": "a-3.pkg.tar.zst", "sha256": "x"})
        self.assertEqual(publish.retain(history, history[0]), history)

    def test_old_workflow_cannot_replace_newer_publication(self):
        self.assertFalse(publish.stale_publication("same", "same"))
        with patch.object(publish.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            self.assertFalse(publish.stale_publication("older", "newer"))
        with patch.object(publish.subprocess, "run", side_effect=[
            SimpleNamespace(returncode=1), SimpleNamespace(returncode=0),
        ]):
            self.assertTrue(publish.stale_publication("newer", "older"))
        with patch.object(publish.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(ValueError, "历史"):
                publish.stale_publication("branch-a", "branch-b")


class LocalRemote:
    """用本地目录替代 OneDrive；其他签名和 repo-add 步骤均运行真实工具。"""

    def __init__(self, target):
        self.root = Path(target)

    def path(self, relative=""):
        return str(self.root / relative)

    def get(self, relative, local):
        shutil.copyfile(self.root / relative, local)

    def put(self, local, relative, immutable=False):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if immutable and target.exists() and publish.sha256(local) != publish.sha256(target):
            raise ValueError("immutable")
        shutil.copyfile(local, target)

    def call(self, command, *args, capture=False):
        path = Path(args[0])
        if command == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
        elif command == "lsjson":
            items = list(path.iterdir())
            if "--files-only" in args:
                items = [p for p in items if p.is_file()]
            if "--dirs-only" in args:
                items = [p for p in items if p.is_dir()]
            return json.dumps([{"Name": p.name} for p in items])
        elif command == "delete":
            for name in Path(args[2]).read_text().splitlines():
                (path / name).unlink()
        elif command == "purge":
            shutil.rmtree(path)
        else:
            raise AssertionError(command)


@unittest.skipUnless(all(shutil.which(t) for t in ("git", "gpg", "repo-add", "repo-remove", "bsdtar")),
                     "集成测试需要 Git、Arch 仓库工具和 GPG")
class PublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(
            prefix="btstream-tests-", dir="/tmp/opencode" if Path("/tmp/opencode").exists() else None,
        )
        cls.root = Path(cls.temp.name)
        cls.gpg = cls.root / "gnupg"
        cls.gpg.mkdir(mode=0o700)
        subprocess.run([
            "gpg", "--homedir", str(cls.gpg), "--batch", "--pinentry-mode", "loopback",
            "--passphrase", "", "--quick-generate-key", "Repository Test <test@example.invalid>",
            "ed25519", "sign", "0",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        keys = subprocess.check_output([
            "gpg", "--homedir", str(cls.gpg), "--with-colons", "--list-secret-keys",
        ], text=True)
        cls.key = next(line.split(":")[9] for line in keys.splitlines()
                       if line.startswith("fpr:"))

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", str(cls.gpg), "--kill", "gpg-agent"],
                       check=False)
        cls.temp.cleanup()

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        self.repository = self.case / "repository"
        subprocess.run(["git", "init", "--quiet", str(self.repository)], check=True)
        subprocess.run([
            "git", "-C", str(self.repository), "-c", "user.name=Repository Test",
            "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty",
            "-m", "Create isolated test repository",
        ], check=True)
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.repository), "rev-parse", "HEAD"], text=True,
        ).strip()
        self.remote = self.case / "remote"
        self.source = self.case / "artifacts"
        (self.source / "packages").mkdir(parents=True)
        self.inventory = {"a": "a", "b": "b"}

    def artifact(self, name, version):
        target = self.source / "packages" / f"{name}-{version}-any.pkg.tar.zst"
        content = (
            f"pkgname = {name}\npkgbase = {name}\npkgver = {version}\n"
            "pkgdesc = test\nbuilddate = 1700000000\nsize = 1\narch = any\n"
        ).encode()
        # libarchive 按文件魔数读取；测试用 gzip 避免依赖额外压缩库。
        with tarfile.open(target, "w:gz") as archive:
            info = tarfile.TarInfo(".PKGINFO")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        return target.name

    def test_container_ownership_exception_is_scoped_to_workspace(self):
        env = {
            **os.environ,
            "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
            "GIT_CONFIG_GLOBAL": str(self.case / "gitconfig"),
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GITHUB_WORKSPACE": str(self.repository),
            "LC_ALL": "C",
        }
        before = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=self.repository,
            env=env, text=True, capture_output=True,
        )
        self.assertEqual(before.returncode, 128)
        self.assertIn("dubious ownership", before.stderr)
        after = subprocess.run(
            ["sh", "-ec",
             'git config --global --add safe.directory "$GITHUB_WORKSPACE"\n'
             "git rev-parse --verify HEAD"],
            cwd=self.repository, env=env, text=True, capture_output=True,
        )
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(after.stdout.strip(), self.commit)
        configured = subprocess.check_output(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            env=env, text=True,
        ).splitlines()
        self.assertEqual(configured, [str(self.repository)])

    def execute(self, generation, builds, full=False):
        for existing in (self.source / "packages").iterdir():
            existing.unlink()
        for name, version in builds.items():
            self.artifact(name, version)
        (self.source / "plan.json").write_text(json.dumps({
            "inventory": self.inventory, "full": full, "expected": list(builds),
            "commit": self.commit,
        }))
        env = {
            "GNUPGHOME": str(self.gpg), "GPG_KEY_ID": self.key,
            "BUILD_ID": generation, "REPO_REMOTE": str(self.remote),
            "REPO_INITIALIZE": "true",
        }
        with patch.dict(os.environ, env), patch.object(
            sys, "argv", ["publish.py", str(self.source)],
        ), patch.object(publish, "Remote", LocalRemote), contextlib.chdir(
            self.repository,
        ), contextlib.redirect_stdout(io.StringIO()):
            publish.main()

    def test_incremental_retention_signatures_and_removal(self):
        self.execute("1", {"a": "1-1", "b": "1-1"}, full=True)
        self.execute("2", {"a": "2-1"})
        self.execute("3", {"b": "2-1"})
        self.assertTrue((self.remote / "x86_64/a-1-1-any.build-1.pkg.tar.zst").exists())
        self.execute("4", {"a": "3-1"})
        self.assertFalse((self.remote / "x86_64/a-1-1-any.build-1.pkg.tar.zst").exists())
        self.assertTrue((self.remote / "x86_64/a-2-1-any.build-2.pkg.tar.zst").exists())
        self.assertTrue((self.remote / "x86_64/b-1-1-any.build-1.pkg.tar.zst").exists())
        self.assertEqual({p.name for p in (self.remote / "snapshots").iterdir()}, {"3", "4"})
        for kind in ("db", "files"):
            target = self.remote / f"x86_64/btstream-pkgs.{kind}"
            self.assertFalse(target.is_symlink())
            subprocess.run([
                "gpg", "--homedir", str(self.gpg), "--verify", str(target) + ".sig", str(target),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for package in (self.remote / "x86_64").glob("*.pkg.tar.zst"):
            subprocess.run([
                "gpg", "--homedir", str(self.gpg), "--verify",
                str(package) + ".sig", str(package),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with tarfile.open(self.remote / "x86_64/btstream-pkgs.db") as archive:
            descriptions = [
                archive.extractfile(member).read().decode()
                for member in archive if member.name.endswith("/desc")
            ]
        self.assertTrue(any("a-3-1-any.build-4.pkg.tar.zst" in d for d in descriptions))
        self.assertTrue(any("b-2-1-any.build-3.pkg.tar.zst" in d for d in descriptions))
        del self.inventory["b"]
        self.execute("5", {})
        self.assertFalse(list((self.remote / "x86_64").glob("b-*.pkg.tar.zst*")))

    def test_initial_publish_requires_full_build(self):
        with self.assertRaisesRegex(ValueError, "全量"):
            self.execute("1", {"a": "1-1"})

    def test_missing_state_is_not_treated_as_empty_repository(self):
        self.remote.mkdir()
        (self.remote / "unrelated.txt").write_text("do not delete")
        with self.assertRaisesRegex(ValueError, "空的专用目录"):
            self.execute("1", {"a": "1-1", "b": "1-1"}, full=True)
        self.assertTrue((self.remote / "unrelated.txt").exists())

    def test_corrupt_database_aborts_before_upload(self):
        self.execute("1", {"a": "1-1", "b": "1-1"}, full=True)
        before = (self.remote / "state.json").read_bytes()
        (self.remote / "snapshots/1/btstream-pkgs.db.tar.gz").write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.execute("2", {"a": "2-1"})
        self.assertEqual((self.remote / "state.json").read_bytes(), before)
        self.assertFalse((self.remote / "x86_64/a-2-1-any.build-2.pkg.tar.zst").exists())

    def test_same_version_rebuild_uses_immutable_filenames(self):
        self.execute("1", {"a": "1-1", "b": "1-1"}, full=True)
        self.execute("2", {"a": "1-1"})
        state = json.loads((self.remote / "state.json").read_text())
        self.assertEqual(len(state["packages"]["a@any"]), 2)
        self.assertTrue((self.remote / "x86_64/a-1-1-any.build-1.pkg.tar.zst").exists())
        self.assertTrue((self.remote / "x86_64/a-1-1-any.build-2.pkg.tar.zst").exists())

    def test_interrupted_upload_preserves_state_and_recovers_from_snapshot(self):
        self.execute("1", {"a": "1-1", "b": "1-1"}, full=True)
        before = (self.remote / "state.json").read_bytes()
        original_put = LocalRemote.put

        def fail_alias(remote, local, relative, immutable=False):
            if relative == "x86_64/btstream-pkgs.db":
                raise OSError("模拟上传中断")
            return original_put(remote, local, relative, immutable)

        with patch.object(LocalRemote, "put", fail_alias):
            with self.assertRaisesRegex(OSError, "中断"):
                self.execute("2", {"a": "2-1"})
        self.assertEqual((self.remote / "state.json").read_bytes(), before)
        self.assertTrue((self.remote / "x86_64/a-1-1-any.build-1.pkg.tar.zst").exists())
        self.execute("3", {"a": "3-1"})
        self.assertFalse((self.remote / "x86_64/a-2-1-any.build-2.pkg.tar.zst").exists())
        self.assertEqual(
            json.loads((self.remote / "state.json").read_text())["generation"], "3",
        )


if __name__ == "__main__":
    unittest.main()
