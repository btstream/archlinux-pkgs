FROM docker.io/library/archlinux:base-devel

ARG BUILDER_UID=1000
RUN pacman -Syu --noconfirm --needed git python sudo \
    && useradd -m -u "$BUILDER_UID" builder \
    && printf 'builder ALL=(root) NOPASSWD: /usr/bin/pacman\n' > /etc/sudoers.d/builder \
    && chmod 0440 /etc/sudoers.d/builder \
    && printf '\nOPTIONS=("${OPTIONS[@]/#debug/!debug}")\nPKGEXT=".pkg.tar.zst"\n' >> /etc/makepkg.conf \
    && pacman -Scc --noconfirm
