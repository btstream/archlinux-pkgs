#!/usr/bin/env python3
"""发布已构建产物。只解析包元数据，不执行 PKGBUILD 或包内脚本。"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from build import pkginfo

REPO = "btstream-pkgs"
ARCH = "x86_64"
SAFE_NAME = re.compile(r"[A-Za-z0-9@_+][A-Za-z0-9@_.+:-]*\Z")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe(value):
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
        raise ValueError(f"不安全的文件名或包名：{value!r}")
    return value


def retain(history, record):
    """相同文件名只允许完全一致的内容，不允许悄悄覆盖已发布包。"""
    for previous in history:
        if previous["filename"] == record["filename"]:
            if previous["sha256"] != record["sha256"]:
                raise ValueError(
                    f'{record["filename"]} 与已发布内容不同，请增加 pkgrel 后再构建'
                )
            return history
    return [record, *history][:2]


class Remote:
    def __init__(self, target):
        self.target = target.rstrip("/")

    def call(self, command, *args, capture=False):
        return subprocess.check_output(
            ["rclone", command, *map(str, args), "--onedrive-no-versions",
             "--retries", "3", "--low-level-retries", "10"],
            text=True,
        ) if capture else run(
            "rclone", command, *map(str, args), "--onedrive-no-versions",
            "--retries", "3", "--low-level-retries", "10",
        )

    def path(self, relative=""):
        return f"{self.target}/{relative}"

    def get(self, relative, local):
        self.call("copyto", self.path(relative), local)

    def put(self, local, relative, immutable=False):
        extra = ["--immutable", "--checksum"] if immutable else []
        self.call("copyto", local, self.path(relative), *extra)


def validate_state(state):
    if state.get("schema") != 1 or state.get("repository") != REPO:
        raise ValueError("远端状态格式或仓库名称不匹配")
    safe(state["generation"])
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", state["commit"]):
        raise ValueError("无效的源代码提交")
    for key, history in state["packages"].items():
        safe(key)
        if not 1 <= len(history) <= 2:
            raise ValueError(f"{key} 的保留记录数量错误")
        for record in history:
            safe(record["filename"])
            safe(record["name"])
            safe(record["base"])
            if key != record["name"] + "@" + record["arch"]:
                raise ValueError("状态中的包标识不一致")
            if record["arch"] not in ("any", ARCH):
                raise ValueError("不支持的架构")
            if not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
                raise ValueError("无效的包哈希")
    expected = {f"{REPO}.{kind}.tar.gz{suffix}"
                for kind in ("db", "files") for suffix in ("", ".sig")}
    if set(state["database"]) != expected:
        raise ValueError("数据库清单不完整")


def stale_publication(previous, current):
    if previous == current:
        return False
    if subprocess.run(["git", "merge-base", "--is-ancestor", previous, current]).returncode == 0:
        return False
    if subprocess.run(["git", "merge-base", "--is-ancestor", current, previous]).returncode == 0:
        return True
    raise ValueError("源代码历史发生分叉或被重写，请人工确认后迁移仓库状态")


def main():
    if os.environ.get("REPO_NAME", REPO) != REPO:
        raise ValueError(f"此仓库固定使用名称 {REPO}")
    key = os.environ["GPG_KEY_ID"].upper()
    if not re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", key):
        raise ValueError("GPG_KEY_ID 必须为完整指纹")
    run("gpg", "--batch", "--list-secret-keys", key)
    generation = safe(os.environ["BUILD_ID"])
    remote = Remote(os.environ.get("REPO_REMOTE", "onedrive:archlinux-pkgs"))
    source = Path(sys.argv[1]).resolve()
    plan = json.loads((source / "plan.json").read_text())
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if plan["commit"] != commit:
        raise ValueError("构建产物与发布 job 的源代码提交不一致")
    inventory = plan["inventory"]
    for name, base in inventory.items():
        safe(name)
        safe(base)
    new_paths = sorted((source / "packages").glob("*.pkg.tar.zst"))

    temporary_root = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="btstream-publish-", dir=temporary_root) as temporary:
        work = Path(temporary)
        database = work / ARCH
        database.mkdir()
        initialize = os.environ.get("REPO_INITIALIZE") == "true"
        if initialize:
            remote.call("mkdir", remote.path())
        listing = json.loads(remote.call(
            "lsjson", remote.path(), "--max-depth", "1", capture=True,
        ))
        names = {item["Name"] for item in listing}
        state_path = work / "state.json"
        if "state.json" in names:
            remote.get("state.json", state_path)
            old = json.loads(state_path.read_text())
            validate_state(old)
            if stale_publication(old["commit"], commit):
                print("远端已发布更新提交，跳过这个过时的工作流")
                return
            # 从最后一次已提交快照读取，而不是信任可能只上传了一半的根目录 DB。
            for filename, digest in old["database"].items():
                target = database / filename
                remote.get(f'snapshots/{old["generation"]}/{filename}', target)
                if sha256(target) != digest:
                    raise ValueError(f"远端数据库哈希错误：{filename}")
            for kind in ("db", "files"):
                archive = database / f"{REPO}.{kind}.tar.gz"
                run("gpg", "--batch", "--verify", str(archive) + ".sig", str(archive))
        else:
            if not initialize or names:
                raise ValueError(
                    "远端没有 state.json；首次发布须使用空的专用目录，"
                    "并设置 REPO_INITIALIZE=true"
                )
            if not plan["full"]:
                raise ValueError("首次发布必须手动触发全量构建")
            old = {"schema": 1, "repository": REPO, "packages": {}}

        histories = {
            identity: records
            for identity, records in old["packages"].items()
            if inventory.get(records[0]["name"]) == records[0]["base"]
        }
        removed = {
            records[0]["name"] for identity, records in old["packages"].items()
            if identity not in histories
        }
        produced = set()
        records = []
        for path in new_paths:
            safe(path.name)
            package = pkginfo(path)
            if inventory.get(package.name) != package.base:
                raise ValueError(f"产物不在本仓库清单中：{path.name}")
            if package.name in produced:
                raise ValueError(f"重复构建产物：{package.name}")
            produced.add(package.name)
            info = subprocess.check_output(
                ["bsdtar", "-xOf", str(path), ".PKGINFO"], text=True,
            )
            architectures = [line.split(" = ", 1)[1] for line in info.splitlines()
                             if line.startswith("arch = ")]
            if len(architectures) != 1 or architectures[0] not in ("any", ARCH):
                raise ValueError(f"产物架构不受支持：{path.name}")
            published_filename = path.name.removesuffix(".pkg.tar.zst") + (
                f".build-{generation}.pkg.tar.zst"
            )
            safe(published_filename)
            record = {
                "name": package.name, "base": package.base,
                "version": package.version, "arch": architectures[0],
                "filename": published_filename, "sha256": sha256(path),
                "build": generation,
            }
            identity = package.name + "@" + record["arch"]
            # arch 从 any 改为 x86_64（或反之）时不保留旧标识。
            for existing in list(histories):
                if histories[existing][0]["name"] == package.name and existing != identity:
                    del histories[existing]
            histories[identity] = retain(histories.get(identity, []), record)
            records.append((path, record))
        if produced != set(plan["expected"]):
            raise ValueError("构建产物与构建计划不一致，拒绝发布")
        if not records and not removed:
            print("没有需要发布的变更")
            return
        if not histories:
            raise ValueError("拒绝发布空仓库；删除最后一个包需要人工处理")
        if removed:
            run("repo-remove", str(database / f"{REPO}.db.tar.gz"), *sorted(removed))
        for path, record in records:
            target = database / record["filename"]
            shutil.copyfile(path, target)
            run("gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
                "--passphrase", "", "--local-user", key,
                "--output", str(target) + ".sig", "--detach-sign", str(target))
        if records:
            run("repo-add", "--sign", "--key", key, "--include-sigs",
                str(database / f"{REPO}.db.tar.gz"),
                *[str(database / record["filename"]) for _, record in records])
        else:
            for kind in ("db", "files"):
                archive = database / f"{REPO}.{kind}.tar.gz"
                run("gpg", "--batch", "--yes", "--local-user", key,
                    "--output", str(archive) + ".sig", "--detach-sign", str(archive))

        db_names = [f"{REPO}.{kind}.tar.gz{suffix}"
                    for kind in ("db", "files") for suffix in ("", ".sig")]
        state = {
            "schema": 1, "repository": REPO, "generation": generation,
            "commit": commit,
            "packages": histories,
            "database": {name: sha256(database / name) for name in db_names},
        }
        validate_state(state)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        public_key = work / f"{REPO}.key"
        public_key.write_bytes(subprocess.check_output(
            ["gpg", "--batch", "--armor", "--export", key],
        ))

        # 包先上传；相同版本不允许被不同内容覆盖。
        for _, record in records:
            filename = record["filename"]
            remote.put(database / filename, f"{ARCH}/{filename}", immutable=True)
            remote.put(database / (filename + ".sig"), f"{ARCH}/{filename}.sig")
        for name in db_names:
            remote.put(database / name, f"snapshots/{generation}/{name}")
        remote.put(state_path, f"snapshots/{generation}/state.json")
        remote.put(public_key, public_key.name)
        # 根目录只上传白名单，不把 repo-add 的 .old 文件带上去。
        for name in db_names:
            remote.put(database / name, f"{ARCH}/{name}")
        for kind in ("db", "files"):
            for suffix in ("", ".sig"):
                remote.put(
                    database / f"{REPO}.{kind}.tar.gz{suffix}",
                    f"{ARCH}/{REPO}.{kind}{suffix}",
                )
        # 状态最后提交；之后才允许删除任何旧包。
        remote.put(state_path, "state.json")

        keep = {record["filename"] for history in histories.values() for record in history}
        files = json.loads(remote.call(
            "lsjson", remote.path(ARCH), "--files-only", capture=True,
        ))
        obsolete = []
        for item in files:
            name = item["Name"]
            if name.endswith((".pkg.tar.zst", ".pkg.tar.zst.sig")):
                base = name.removesuffix(".sig")
                if base not in keep:
                    safe(name)
                    obsolete.append(name)
        if obsolete:
            deletion = work / "delete.txt"
            deletion.write_text("\n".join(obsolete) + "\n")
            remote.call("delete", remote.path(ARCH), "--files-from", deletion)
        # 仅保留当前和上次提交的元数据快照。快照不重复存放大包。
        snapshot_keep = {generation, old.get("generation")}
        snapshots = json.loads(remote.call(
            "lsjson", remote.path("snapshots"), "--dirs-only", capture=True,
        ))
        for item in snapshots:
            name = item["Name"]
            if name not in snapshot_keep:
                safe(name)
                remote.call("purge", remote.path(f"snapshots/{name}"))
        print(f"已发布 {generation}：{len(records)} 个新包，清理 {len(obsolete)} 个旧文件")


if __name__ == "__main__":
    main()
