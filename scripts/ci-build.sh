#!/usr/bin/env bash
set -euo pipefail

mkdir -p ci-output/packages
python scripts/resolve.py --output ci-output/plan.json

# mkarchroot 使用宿主的官方仓库配置。不给构建环境注入发布密钥。
if python -c 'import json; import sys; sys.exit(not json.load(open("ci-output/plan.json"))["order"])'; then
  # mkarchroot 使用 readlink -f：父目录必须存在，但 root 本身不能存在。
  chroot_dir=$(mktemp -d "${TMPDIR:-/tmp}/btstream-build.XXXXXXXX")
  mkarchroot "$chroot_dir/root" base-devel
  # 不额外生成未在 .SRCINFO 声明的自动 debug 包。
  # shellcheck disable=SC2016 # 表达式需要在 chroot 读取配置时展开。
  printf '\nOPTIONS=("${OPTIONS[@]/#debug/!debug}")\n' >> "$chroot_dir/root/etc/makepkg.conf"
  python scripts/build.py "$chroot_dir"
fi
