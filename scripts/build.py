#!/usr/bin/env python3
"""拓扑顺序构建；每个包使用新容器，只挂载当前源码和需要的依赖。"""

import json
from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

from repo_model import Package
from resolve import choose


def pkginfo(path):
    text = subprocess.check_output(["bsdtar", "-xOf", str(path), ".PKGINFO"], text=True)
    fields = {}
    for line in text.splitlines():
        if " = " in line:
            key, value = line.split(" = ", 1)
            fields.setdefault(key, []).append(value)
    return Package(
        fields["pkgname"][0], fields["pkgver"][0],
        fields.get("pkgbase", fields["pkgname"])[0],
        fields.get("provides", []), fields.get("depend", []),
    )


def stage_sources(root, directory, workspace):
    with subprocess.Popen(
        ["git", "archive", "HEAD", f"{directory}/"], cwd=root, stdout=subprocess.PIPE,
    ) as process:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            archive.extractall(workspace, filter="data")
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, process.args)
    return workspace / directory


def container_command(image, source, dependencies, filenames):
    return [
        "docker", "run", "--rm",
        "--mount", f"type=bind,src={source},dst=/build",
        "--mount", f"type=bind,src={dependencies},dst=/dependencies,readonly",
        "--workdir", "/build",
        image, "bash", "-euc",
        'if (( $# )); then pacman -U --noconfirm --needed -- "$@"; fi\n'
        'exec runuser -u builder -- makepkg --syncdeps --noconfirm --nosign --cleanbuild',
        "build-package", *[f"/dependencies/{name}" for name in filenames],
    ]


def main():
    image = sys.argv[1]
    root = Path.cwd()
    plan = json.loads((root / "ci-output/plan.json").read_text())
    output = root / "ci-output/packages"
    built = []

    @lru_cache(maxsize=None)
    def compare(version, wanted):
        return int(subprocess.check_output(
            ["docker", "run", "--rm", "--network=none", image, "vercmp", version, wanted],
            text=True,
        ))

    for directory in plan["order"]:
        selected = {}

        def include(expression):
            provider = choose(expression, [p for p, _ in built], plan["providers"], compare)
            if not provider or provider.name in selected:
                return
            path = next(path for p, path in built if p is provider)
            selected[provider.name] = path
            for child in provider.depends:
                include(child)

        for expression in plan["requirements"][directory]:
            include(expression)
        with tempfile.TemporaryDirectory(prefix="btstream-package-", dir=root) as temporary:
            scratch = Path(temporary)
            staged = stage_sources(root, directory, scratch / "source")
            dependencies = scratch / "dependencies"
            dependencies.mkdir()
            filenames = []
            for path in selected.values():
                (dependencies / path.name).hardlink_to(path)
                filenames.append(path.name)
            subprocess.run(container_command(
                image, staged, dependencies, filenames,
            ), check=True)
            artifacts = sorted(staged.glob("*.pkg.tar.zst"))
            if not artifacts:
                raise RuntimeError(f"{directory} 没有生成 .pkg.tar.zst")
            for artifact in artifacts:
                if artifact.is_symlink() or not artifact.is_file():
                    raise RuntimeError(f"构建产物不是普通文件：{artifact.name}")
                package = pkginfo(artifact)
                if plan["inventory"].get(package.name) != package.base:
                    raise RuntimeError(f"意外构建产物：{artifact.name}")
                target = output / artifact.name
                shutil.move(artifact, target)
                built.append((package, target))


if __name__ == "__main__":
    main()
