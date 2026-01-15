
# 一些常用的Archlinux PKG

## 使用方式

如果使用paru，可以使用如下的方式进行使用：

1. 修改`/etc/paru.conf`

增加下面的内容：

```plaintext
[btstream-pkgs]
Url = https://github.com/btstream/archlinux-pkgs.git
SkipReview
```

2. 使用paru

更新paru，执行`paru -Syu --pkgbuilds`


之后可以正常使用
