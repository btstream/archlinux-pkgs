"""纯元数据处理；不下载或执行 AUR 构建脚本。"""

import re
import subprocess
from dataclasses import dataclass, field


@dataclass
class Package:
    name: str
    version: str
    base: str
    provides: list[str] = field(default_factory=list)
    depends: list[str] = field(default_factory=list)


def dependency(value):
    match = re.fullmatch(r"([^<>=\s]+)(>=|<=|=|>|<)?(.*)", value)
    if not match:
        raise ValueError(f"无效依赖：{value}")
    return match.groups()


def vercmp(version, wanted):
    return int(subprocess.check_output(["vercmp", version, wanted], text=True))


def satisfies(package, expression, compare=vercmp):
    name, operator, wanted = dependency(expression)
    candidates = [package.version] if package.name == name else []
    for provided in package.provides:
        provided_name, _, version = dependency(provided)
        if provided_name == name:
            candidates.append(version)
    for version in candidates:
        if not operator:
            return True
        if not version:
            continue
        comparison = compare(version, wanted)
        if {
            "=": comparison == 0,
            ">": comparison > 0,
            "<": comparison < 0,
            ">=": comparison >= 0,
            "<=": comparison <= 0,
        }[operator]:
            return True
    return False


def parse_srcinfo(text, directory, arch="x86_64"):
    common = {}
    sections = []
    current = common
    for line in text.splitlines():
        if " = " not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.strip().split(" = ", 1)
        if key == "pkgname":
            current = {"pkgname": [value]}
            sections.append(current)
        else:
            current.setdefault(key, []).append(value)
    base = common["pkgbase"][0]
    version = common["pkgver"][0] + "-" + common["pkgrel"][0]
    if common.get("epoch", ["0"])[0] != "0":
        version = common["epoch"][0] + ":" + version

    def values(section, key):
        return section.get(key, []) + section.get(f"{key}_{arch}", [])

    packages = []
    requirements = set(values(common, "makedepends") + values(common, "checkdepends"))
    for section in sections:
        architectures = section.get("arch", common.get("arch", []))
        if not ({"any", arch} & set(architectures)):
            raise ValueError(f"{directory} 不支持 {arch}")
        # split package 的显式 depends 覆盖公共 depends。
        runtime = values(section, "depends") if (
            "depends" in section or f"depends_{arch}" in section
        ) else values(common, "depends")
        provides = values(section, "provides") if (
            "provides" in section or f"provides_{arch}" in section
        ) else values(common, "provides")
        packages.append(Package(section["pkgname"][0], version, base, provides, runtime))
        requirements.update(runtime)
        requirements.update(values(section, "makedepends") + values(section, "checkdepends"))
    if not packages:
        raise ValueError(f"{directory} 没有 pkgname")
    return packages, sorted(requirements)


def parse_desc(text):
    fields = {}
    key = None
    for line in text.splitlines():
        if line.startswith("%") and line.endswith("%"):
            key = line.strip("%")
            fields[key] = []
        elif line and key:
            fields[key].append(line)
    return fields
