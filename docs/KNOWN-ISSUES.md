# 已知问题（1.1.0）

本文件记录 1.1.0 发布时**仍然存在**、但尚未在正式版修复的缺陷，以及每个缺陷的
临时规避方法。目的是让合并记录、issue 与发布说明里提到的条目有**可打开的落点**——
此前这些内容只存在于内部评审目录（`.gitignore` 排除），外部读者永远看不到。

> 状态说明：**已修复** = 已进主线并带回归测试；**已规避** = 行为上不会伤到用户，
> 但根因还在；**未修复** = 仍会复现。
>
> 本文件按「影响面」排序，数据丢失类在最前。

---

## S2（高危 / 数据丢失）保留策略把「幽灵组」算作一份备份

**状态：已修复**（`manifest.BackupGroup.countable` + `engine.apply_retention` 的
`incomplete` 桶；回归测试 `tests/test_retention_ghost_group.py`）

### 现象

备份目录里的某一组**清单（manifest）还在、但清单里列的卷一个都不在盘上**（下称
"幽灵组"）时，保留策略会把它当成**一份完整的可信备份**：

- 它占掉 `keep_last` 的一个名额；
- 计入 `keep_days` 与 `max_total_gb` 的统计；
- 排序后可能落在"该保留的最新 N 份"区间里，从而把真正完好的备份挤到淘汰区。

实测（`keep_last=2`、2 份真备份 + 1 个幽灵组）：

```
=== find_backup_blobs ===
stem='real0.zip'  managed=True  complete=True   state='committed'
stem='real1.zip'  managed=True  complete=True   state='committed'
stem='ghost.zip'  managed=True  complete=False  state='committed'  files=[]
=== apply_retention ===
handled = ['real0.zip']
剩余真实备份 = ['real1.zip']            ← 本应保留 2 份，实际只剩 1 份
```

幽灵组本身还会**永久占盘**：它的清单没有任何代码去清理，于是"空清单"越积越多，
每次都在挤占配额。

### 根因（三处叠加）

1. `manifest.collect_groups` 对"清单在、卷全丢"的 stem 仍然建组（`files=[]`、
   `complete=False`）——这是对的，调用方需要看见它才能清理；
2. `BackupGroup.managed` 只判断 `manifest.is_committed`，**完全忽略 `complete`**；
3. `engine.apply_retention` 的 `trusted` 候选直接用 `b.managed`，从不读
   `complete`（全文件 `complete` 只有定义、传递、赋值三处，无任何判断）。

### 修复

把两种语义**显式分开**，这是本次修复的核心：

| 语义 | 判据 | 幽灵组 / 缺卷残包 |
| --- | --- | --- |
| 计入 `keep_last` / `keep_days` / `max_total_gb` 配额 | `BackupGroup.countable` = 有清单 + `committed` + `complete` | **否** |
| 可被保留策略回收清理 | `apply_retention` 的 `incomplete` 桶 | **是** |

- 幽灵组（`files` 为空）：盘上没有任何用户数据，只剩孤儿清单 → **无条件回收**
  （实际动作只是删掉那个 `.json`，零风险）；
- 缺卷残包（还有部分卷）：已无法完整恢复，但卷本身仍是用户数据 → 只在**已被一份
  更新的完整备份取代**时才回收，避免"备份盘瞬时不可读 → 所有卷被误判缺失"把用户
  唯一一份备份清走（默认 `move` 模式还会先进回收站）。

### 临时规避（若你用的是 1.1.0 正式版）

```bash
# 1) 先看有哪些"清单在、卷不在"的组（只读，不动文件）
python -m backupstudio info          # 环境与压缩支持
# 2) 手动核对 <备份目录>/.backupstudio/manifests/*.json 与盘上文件名
# 3) 确认某份清单对应的卷确实全丢了 → 删掉该 .json
# 4) 保守起见把清理方式设为 move，而不是 delete
```

配置里 `retention.on_cleanup` 默认是 `move`（挪到
`<备份目录>/.backupstudio/trash/<日期>/`，可反悔）。**不要**在没有核对清单的情况下
把它改成 `delete`。

---

## S1（安全 / 可被绕过）炸弹预检对「无 Size 容器」fail-open

**状态：已修复**（`extract._sum_size_lines` 的 `seen` 守卫；回归测试
`tests/test_bomb_unknown_size.py`）

### 现象

对**声明解压大小为空**的容器，压缩炸弹预检完全失效，可被用来做 zip bomb 攻击。

7-Zip 对**流式 / 未知大小**的容器只输出空值。实测 `7z l -slt -ba payload.tar.zst`：

```
Size =
Packed Size =
```

（容器头里是 `Method = header-open-only ... unknown-content-size`）——一个数字都没有。
于是 `_sum_size_lines` 返回 `0`，`_check_external_bomb` 的判定 `0 > limit` 恒假，
**静默放行**。

实测：把 `BACKUPSTUDIO_MAX_UNPACKED_BYTES` 压到 256 KiB，一个 4 MiB 的 `.tar.zst`
仍被完整解出来（`ok=True`）。

### 根因

`extract._sum_size_lines` 在"命令跑成功但一行 `Size =` 都没解析到"时返回 `0`
而不是 `None`；而 `_check_external_bomb` 只拦 `None`。

**不对称证据**：rar 路径的 `_declared_unpacked_unrar` 一直有 `seen` 守卫
（`return total if seen else None`），7z 路径漏了。

### 修复

`_sum_size_lines` 加 `seen` 守卫，零命中返回 `None`（与 rar 对齐）。修复后：

```
_sum_size_lines(无 Size 容器) -> None
extract_archive(disguised.7z, 上限 256 KiB) -> ok=False
    msg=无法预读 ... 的解压后大小，拒绝在未预检的情况下解压
```

正常的 7z / zip / tar.gz、以及**只含空目录**的包（`Size = 0` 可解析）都不受影响。

### 临时规避（若你用的是 1.1.0 正式版）

**不要**对来源不可信的 `.tar.zst` / `.tar.bz2` 等流式容器使用「解压」功能。
若必须解，先自行确认文件来源与体积。**切勿**为了"能解开"就把
`BACKUPSTUDIO_MAX_UNPACKED_BYTES` 设成 `0`——那等于关闭唯一的绝对上限。

---

## S3（体验 / 功能静默失效）内置 ZIP 忽略压缩级别

**状态：已修复**（`archives.zip_info_level`，三处写出点统一调用；回归测试
`tests/test_zip_level.py`）

### 现象

内置 ZIP 后端的"压缩级别"设置**完全无效**：级别 1 与级别 9 产出**位元组完全相同**的
压缩包。用户以为自己在"调小体积 / 调快速度"，实际永远是 zlib 的默认级别 6。

实测（584000 B 源）：

```
修复前： level=1 -> 17228 B    level=9 -> 17228 B     ← 完全相同
修复后： level=1 -> 27060 B    level=9 -> 17221 B     ← 级别真正生效
```

### 根因

三处写出点只设了 `zi.compress_type`，**没设 `zi.compress_level`**
（全项目 `compress_level` 此前零命中）：

- `archives.ZipCompressor._info`
- `py7zr_backend.Py7zrCompressor._zip_info`
- `py7zr_backend.ZipFileCompressor._info`

Python 3.12+ 的 `zipfile._ZipWriteFile` 读的是 `zinfo.compress_level`，
而 `ZipInfo.compress_level` 默认 `None` → 落到 `zlib.Z_DEFAULT_COMPRESSION`（恒 6）。

现有测试只断言了 `compress_type == ZIP_DEFLATED` 和 `ratio < 0.05`，
**级别差异完全没有断言**——这是"assert 太松"的典型，所以缺陷一直没被发现。

### 修复

新增 `archives.zip_info_level(zi, level, *, is_dir)` 作为三处写出点的**唯一真相来源**：

- `level == 0` 或目录条目 → `compress_type = STORED`，且**不设** `compress_level`
  （zlib 不参与，设了是无意义字段）；
- 其它 → `compress_type = DEFLATED` + `compress_level = clamp(level, 1, 9)`。

### 临时规避（若你用的是 1.1.0 正式版）

需要真正控制体积时，**改用 7z 格式或让 7-Zip 参与**（安装 7-Zip 后 ZIP 会走
`SevenZipCompressor`，其 `-mx=` 参数一直是对的）。内置 ZIP 后端在 1.1.0 上
一律等价于级别 6。

---

## 关联条目

- 合并记录与 issue 里提到的 `_pm/review-v11/REVIEW-fix-1.1-review.md` 是**内部评审
  材料**，`.gitignore` 第 20 行排除了整个 `_pm/` 目录，因此外部读者无法访问。
  本文件即该评审中三条残留缺陷的**公开可读版本**。
- 版本号一致性：`.github/workflows/build.yml` 的 `env.APP_VERSION` 与
  `backupstudio.__version__` 由 CI 的 `test-linux` job 强制对齐，不一致会让构建失败。
