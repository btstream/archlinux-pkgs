#!/usr/bin/env python3
"""扫描本仓库和官方同步数据库，生成拓扑构建顺序。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tarfile

from repo_model import Package, dependency, parse_desc, parse_srcinfo, satisfies


def choose(expression, candidates, overrides):
    matches = [p for p in candidates if satisfies(p, expression)]
    name = dependency(expression)[0]
    if name in overrides:
        matches = [p for p in matches if p.name == overrides[name]]
    exact = [p for p in matches if p.name == name]
    if exact:
        matches = exact
    if len(matches) > 1:
        raise ValueError(
            f"{expression} 有多个 provider：{', '.join(p.name for p in matches)}；"
            "请在 .ci/providers.json 指定包名"
        )
    return matches[0] if matches else None


def order(graph, roots):
    result, active, finished = [], [], set()

    def visit(node):
        if node in active:
            raise ValueError("循环依赖：" + " → ".join(active + [node]))
        if node in finished:
            return
        active.append(node)
        for child in sorted(graph[node]):
            visit(child)
        active.pop()
        finished.add(node)
        result.append(node)

    for node in sorted(roots):
        visit(node)
    return result


def dependency_graph(packages, requirements, official, overrides):
    local = [p for group in packages.values() for p in group]
    bases = {p.base: directory for directory, group in packages.items() for p in group}
    if len({p.name for p in local}) != len(local):
        raise ValueError("本仓库存在重复 pkgname")
    graph = {directory: set() for directory in packages}
    for directory, expressions in requirements.items():
        for expression in expressions:
            provider = choose(expression, local, overrides)
            if provider:
                other = bases[provider.base]
                if other != directory:
                    graph[directory].add(other)
                continue
            # 官方仓库的虚拟依赖（如 java-runtime）可以有多个实现。
            # 这里仅验证可满足性，具体 provider 由 chroot 内 pacman 选择。
            # 显式覆盖仍校验所指定的官方包，但不代替 pacman 安装策略。
            if (
                choose(expression, official, overrides)
                if dependency(expression)[0] in overrides
                else any(satisfies(package, expression) for package in official)
            ):
                continue
            raise ValueError(
                f"{directory} 缺少依赖 {expression}：请手动把对应 PKGBUILD 加入本仓库"
            )
    order(graph, graph)
    return graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path.cwd()
    overrides_path = root / ".ci/providers.json"
    overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    packages, requirements, bases = {}, {}, {}
    for directory in sorted(root.iterdir()):
        if not (directory / "PKGBUILD").is_file():
            continue
        # 在无发布凭据的构建 job，以非 root 身份读取实际元数据。
        text = subprocess.check_output(
            ["runuser", "-u", "builder", "--", "bash", "-c",
             'cd "$1"; makepkg --printsrcinfo', "bash", str(directory)],
            text=True,
        )
        entries, dependencies = parse_srcinfo(text, directory.name)
        if entries[0].base in bases:
            raise ValueError(f"重复 pkgbase：{entries[0].base}")
        bases[entries[0].base] = directory.name
        packages[directory.name] = entries
        requirements[directory.name] = dependencies
    local = [p for group in packages.values() for p in group]
    official = []
    for database in sorted(Path("/var/lib/pacman/sync").glob("*.db")):
        with tarfile.open(database) as archive:
            for member in archive:
                if member.name.endswith("/desc"):
                    fields = parse_desc(archive.extractfile(member).read().decode())
                    official.append(Package(
                        fields["NAME"][0], fields["VERSION"][0], "",
                        fields.get("PROVIDES", []),
                    ))

    # 即使此次未触及，也不允许仓库保留循环依赖或缺失依赖。
    graph = dependency_graph(packages, requirements, official, overrides)
    full = os.environ.get("FULL_BUILD") == "true"
    before = os.environ.get("BEFORE_SHA", "")
    if not before or set(before) == {"0"}:
        full = True
    changed = []
    if not full:
        changed = subprocess.check_output(
            ["git", "diff", "--name-only", before, "HEAD"], text=True
        ).splitlines()
        if any(p.startswith(("scripts/", ".github/", ".ci/")) for p in changed):
            full = True
    roots = set(packages) if full else {
        directory for directory in packages
        if any(path.startswith(directory + "/") for path in changed)
    }
    # 本次明确重建的库可能改变 ABI，同时重建仓库内的反向依赖。
    while True:
        expanded = roots | {p for p, deps in graph.items() if deps & roots}
        if expanded == roots:
            break
        roots = expanded
    plan = {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "order": order(graph, roots),
        "requirements": requirements,
        "inventory": {p.name: p.base for p in local},
        "expected": sorted(p.name for directory in order(graph, roots)
                           for p in packages[directory]),
        "providers": overrides,
        "full": full,
    }
    Path(args.output).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
