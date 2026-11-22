# btstream-pkgs

自用 Arch Linux PKGBUILD 和公开二进制仓库。GitHub Actions 在干净 chroot 中构建，
统一签名后上传 OneDrive；HTTP 服务由仓库维护者自行部署。

## 安装二进制包

首先从可信渠道核对维护者的 **完整 GPG 指纹**，下载 `btstream-pkgs.key`：

```bash
curl --fail -LO https://你的域名/仓库路径/btstream-pkgs.key
gpg --show-keys --fingerprint btstream-pkgs.key
# 确认指纹与维护者公布的指纹一致后：
sudo pacman-key --add btstream-pkgs.key
sudo pacman-key --lsign-key 完整指纹
```

在 `/etc/pacman.conf` 添加：

```ini
[btstream-pkgs]
SigLevel = Required
Server = https://你的域名/仓库路径/$arch
```

然后运行 `sudo pacman -Syu`，使用 `pacman -S 包名` 或 `paru -S 包名` 安装。
`any` 包与 `x86_64` 包均放在 `x86_64/` 下。数据库和软件包都必须通过签名验证。

## 维护者：首次配置

### 1. 配置 OneDrive

在本机使用 `rclone config` 创建名为 `onedrive` 的 remote，并完成 OAuth 授权。
创建一个**专用于此仓库的空文件夹**，默认目标为：

```text
onedrive:archlinux-pkgs
```

CI 使用完整的 rclone 配置，不只是一段 token。用 `rclone config file`
查找配置文件；把所需 remote 的完整配置段保存为 GitHub Secret `RCLONE_CONFIG`。
必须包含相应的 `type`、`token`、`drive_id`、`drive_type` 等实际配置。
使用未加密的配置文本；不要提交到 Git。

工作流传入 `--onedrive-no-versions`，防止覆盖数据库时累积 OneDrive 文件版本。
已有的历史版本以及租户保留策略可能仍占空间，应在 OneDrive 侧另行确认。

OAuth 刷新后的配置只保留在当前临时 runner，不会自动回写 GitHub Secret。
授权到期、修改密码或撤销授权后，需要重新授权并更新 Secret；
本方案不声称凭据可以永久免维护。

### 2. 准备专用签名密钥

建议单独创建用于此仓库的签名密钥，安全备份私钥及吊销证书：

```bash
gpg --quick-generate-key 'btstream-pkgs <你的邮箱>' ed25519 sign 2y
gpg --list-secret-keys --keyid-format long --with-fingerprint
gpg --armor --export-secret-keys 完整指纹
```

**当前 CI 仅支持无口令的专用私钥**。创建时选择空口令，不要直接使用日常主密钥。
私钥泄露允许攻击者伪造受客户端信任的软件包，应立即撤销并通过可信渠道更换公钥。

### 3. 配置 GitHub

在 GitHub 创建 Environment：`repository-publish`，建议只允许 `main` 分支使用，
按需设置人工审批。将以下 Secrets 配置到该 Environment：

| Secret | 内容 |
| --- | --- |
| `GPG_PRIVATE_KEY` | ASCII armored 私钥全文 |
| `GPG_KEY_ID` | 签名密钥完整指纹 |
| `RCLONE_CONFIG` | 包含 `onedrive` remote 的完整 rclone 配置文本 |

在该 Environment 配置 Variables：

| Variable | 默认值 / 用途 |
| --- | --- |
| `REPO_REMOTE` | `onedrive:archlinux-pkgs` |
| `REPO_INITIALIZE` | 默认 `false`；仅首次初始化设为 `true` |

首次部署：

1. 确认 OneDrive 目标文件夹为空。
2. 设置 `REPO_INITIALIZE=true`。
3. 在 Actions 中手动运行 **btstream-pkgs**，执行全量构建。
4. 成功后将 `REPO_INITIALIZE` 改回 `false`。
5. 将 OneDrive 目录映射到自己的 HTTP 服务，并在可信渠道公布 GPG 指纹。

根目录没有 `state.json` 时，正常发布会失败，不会把网络、权限或状态损坏当成空仓库。
不要把目标设为个人 OneDrive 根目录或含其他用途文件的目录。

## 新增、更新和删除软件包

每个包放在仓库一级目录中，保留现有目录名即可，不强制目录名等于 `pkgbase`：

```text
example/
  PKGBUILD
  .SRCINFO
  其他构建必需文件
```

提交前在该目录生成 `.SRCINFO`：

```bash
makepkg --printsrcinfo > .SRCINFO
```

CI 以 `makepkg --printsrcinfo` 的实际输出为准，不对动态下载 URL 做严格文本比较，
因此也支持当前 WPS 的时间签名 URL。`pkgname`、`pkgbase` 必须在仓库内唯一。

### 依赖规则

- 官方仓库依赖由 pacman 安装。
- 非官方依赖必须手动把 PKGBUILD 和所需文件加入本仓库；**不访问 AUR**。
- 检查运行依赖、构建依赖和测试依赖；不自动处理可选依赖。
- 支持版本约束、`provides`、split package 和当前架构的依赖字段。
- 本仓库能满足的依赖优先于官方仓库；多个本仓库虚拟 provider 必须显式选择。
- 在 `.ci/providers.json` 指定本仓库 provider，例如 `{"virtual-name": "actual-package"}`。
- 官方虚拟依赖（如 `java-runtime`）只检查能否满足，由 chroot 内的 pacman
  在 `--noconfirm` 模式下选择实现；若必须使用特定官方实现，在 PKGBUILD 中依赖具体包名。
- 缺失依赖、版本不满足、本仓库 provider 歧义、循环依赖会明确失败。
- 冲突和更复杂的安装约束由 chroot 内的 pacman 最终检查。
- 首版不支持打破循环依赖的 bootstrap 构建，也不自动生成 debug 子包。

依赖解析验证整个仓库。构建采用拓扑顺序，并把前面完成的必要依赖通过
`makechrootpkg -I` 安装进当前包的干净 chroot；split package 的所有声明产物都会发布。
构建 job 不获得签名私钥或 OneDrive 凭据。

### 触发规则

- push 到 `main`：以**上次成功工作流的提交**为基线，构建变化目录中的包，
  以及它们的仓库内依赖和受影响的反向依赖。
- 首次运行、修改公共构建配置或手动运行：全量构建。
- 删除整个包目录：成功发布时从数据库和保留清单移除，删除其历史包和签名。
- 删除所有包不会自动发布空数据库，需要人工处理。
- 上游 `-git` 源变化本身不会触发工作流，需要手动全量构建。
- 所有构建成功才发布；失败不会发布部分成果。

依赖会随本次任务重新构建，不下载远端旧包复用。首版优先保证依赖一致性和隔离，
暂不提供源码缓存或并行拓扑分层构建。

### 保留最近两次构建

按 **`pkgname + arch`** 各自保留最近两次成功发布的构建产物及签名，
不是按最近两次仓库发布或文件修改时间清理。

例如依次发布 A1、A2、B2，A1 不会因为 B 更新而被误删；发布 A3 才删除 A1。
包文件名带有 GitHub run ID 和 attempt：

```text
example-1.0-1-any.build-123456-1.pkg.tar.zst
```

`repo-add` 直接记录该文件名。相同版本重建也不会覆盖旧文件；
但 pacman 仍按包内版本判断升级，**要向已安装用户推送更新，必须提高 `pkgver` 或 `pkgrel`**。
OneDrive 自身回收站、版本历史和租户保留策略不属于 CI 的两版本清理范围。

## 远端目录和发布顺序

```text
archlinux-pkgs/
  btstream-pkgs.key
  state.json
  x86_64/
    btstream-pkgs.db
    btstream-pkgs.db.sig
    btstream-pkgs.db.tar.gz
    btstream-pkgs.db.tar.gz.sig
    btstream-pkgs.files
    btstream-pkgs.files.sig
    btstream-pkgs.files.tar.gz
    btstream-pkgs.files.tar.gz.sig
    *.pkg.tar.zst
    *.pkg.tar.zst.sig
  snapshots/
    <构建编号>/
      state.json
      btstream-pkgs.db.tar.gz
      btstream-pkgs.db.tar.gz.sig
      btstream-pkgs.files.tar.gz
      btstream-pkgs.files.tar.gz.sig
```

所有远端文件都是真实文件，不上传符号链接。`snapshots/` 只保留当前与上一代的
小型元数据快照，不重复存放软件包。

发布按“包及签名 → 元数据快照 → 根目录数据库 → state.json → 清理”执行。
旧数据库和签名从 `state.json` 指向的已提交快照恢复并验证，上传白名单不包括 `.old`。

**OneDrive 无跨文件原子提交**：数据库与签名替换期间仍可能短暂不匹配，
严格的无中断服务需要 HTTP 层提供快照切换。请勿同时运行其他发布工具修改该目录。
GitHub concurrency 不保证 FIFO 发布；工作流以最后成功提交为基线补齐变更，
并在发布时跳过落后于远端已发布提交的工作流，防止旧任务覆盖新仓库。
重写或分叉源代码历史会拒绝自动发布，需要维护者人工确认状态迁移。

如果在 `state.json` 提交前上传中断，下次有效发布使用最后已提交快照继续。
要立即恢复服务，可手动把该快照的数据库和签名复制回 `x86_64/`，
同时把 `.db.tar.gz` / `.files.tar.gz` 内容复制到 `.db` / `.files` 及其签名别名。
首次初始化中断且未生成 `state.json` 时，先检查专用文件夹中的残留，
确认无有效仓库后再手动清空并重新初始化；不要盲目删除已有仓库。

## 本地测试

在 Arch Linux 安装 `python`、`gnupg`、`libarchive`、`git`（`pacman` 自带仓库工具），运行：

```bash
python -m unittest discover -s tests -v
bash -n scripts/ci-build.sh
```

测试会生成临时测试密钥和模拟包，验证依赖处理、真实包/数据库签名、
增量入库、每包两次保留、删除包及错误保护；不连接 OneDrive。

## 继续使用 PKGBUILD 仓库

仍可在 `/etc/paru.conf` 配置：

```ini
[btstream-pkgs]
Url = https://github.com/btstream/archlinux-pkgs.git
```

运行 `paru -Syu --pkgbuilds`。建议保留构建脚本审核，不启用 `SkipReview`。

## 再分发说明

所有仓库内的包均参与构建和发布，包括专有软件重打包。
维护者需自行确认上游授权是否允许公开再分发，以及机构专属配置是否适合公开；
PKGBUILD 可公开获取或添加免责声明均不等于获得二进制再分发许可。
