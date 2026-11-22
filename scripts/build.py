#!/usr/bin/env python3
"""拓扑顺序构建；仅把当前包需要的已构建依赖通过 -I 注入 chroot。"""

import json
from pathlib import Path
import shutil
import subprocess
import sys

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


def main():
    root = Path.cwd()
    plan = json.loads((root / "ci-output/plan.json").read_text())
    output = root / "ci-output/packages"
    built = []
    for directory in plan["order"]:
        selected = {}

        def include(expression):
            provider = choose(expression, [p for p, _ in built], plan["providers"])
            if not provider or provider.name in selected:
                return
            path = next(path for p, path in built if p is provider)
            selected[provider.name] = path
            for child in provider.depends:
                include(child)

        for expression in plan["requirements"][directory]:
            include(expression)
        command = ["runuser", "-u", "builder", "--", "makechrootpkg",
                   "-c", "-r", sys.argv[1]]
        for path in selected.values():
            command.extend(["-I", str(path)])
        command.extend(["--", "--noconfirm"])
        subprocess.run(command, cwd=root / directory, check=True)
        artifacts = sorted((root / directory).glob("*.pkg.tar.zst"))
        if not artifacts:
            raise RuntimeError(f"{directory} 没有生成 .pkg.tar.zst")
        for artifact in artifacts:
            package = pkginfo(artifact)
            if plan["inventory"].get(package.name) != package.base:
                raise RuntimeError(f"意外构建产物：{artifact.name}")
            target = output / artifact.name
            shutil.move(artifact, target)
            built.append((package, target))


if __name__ == "__main__":
    main()
