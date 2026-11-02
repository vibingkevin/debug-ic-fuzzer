# debug-ic-fuzzer

编译所选第一笔 commit 的 baseline 和 sut，使用同一工程绝对路径、独立 worktree 和 Native cache。
当前只做两次产品编译，不遍历后续提交，不运行功能测试，不比较产物或统计重编。

需要 Python 3.12+、Git、JDK；支持 Linux x64、Apple Silicon Mac。iOS 构建还需 Xcode。

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/debug-ic-fuzzer --compilers configs/compilers.yaml --project configs/project.yaml
```

配置在 `configs/`，编译器和项目分开。`forward` 编译 `start_commit`，`reverse` 编译 `end_commit`。
Native home 指定目录时必须是绝对路径；`.tgz`/`.tar.gz` 可相对编译器 YAML，每次重新解压到 `/tmp`，运行结束删除。
不指定 home 时，只覆盖 KGP 版本，由 KGP 下载配套 Native 到 `$KONAN_DATA_DIR`（默认 `~/.konan`）。
不传 `kotlin.native.version`；两版解析到同一 Native home 就报错。
`gradle_args` 原样传给 Gradle，`{config_dir}` 替换为项目 YAML 所在目录。

运行流程：

1. 在 `repos/` clone 或更新公共 bare 仓库。
2. 创建 `run/<编译器配置名>-<项目配置名>-<timestamp>/`，从仓库创建两份 worktree。
3. 用 `git worktree move` 将 baseline 移到 `project`，运行指定 Gradle task，再移回。
4. sut 执行相同流程。日志直接写文件，编译结束后统一读取。

每版结果各写一个 `baseline.json`/`sut.json`，完整 Gradle 日志（含 konanc 调用参数）保存到共享
`store/gradle.log/gradle.log_<md5>`，文本统一 LF、去除首尾空白后去重。构建失败也保存日志并继续另一版。
只记录命令、编译器路径、耗时、退出码、最终 link 信息；暂不扫描、保存编译产物和 IC 文件。
源码和输出留在各自 worktree。`repos/`、`run/` 均不入 Git。

沿用 `GRADLE_USER_HOME`，未指定时使用全局 `~/.gradle`，不清理下载缓存。
Build Cache、Configuration Cache 沿用项目设置；仅禁止最终 Native link 读取和写入 Build Cache。
采集 Action 兼容 Configuration Cache，不强制关闭 UP-TO-DATE。当前新 worktree 的首次产品构建必须实际执行 link。

`debug_ic_fuzzer.py` 调度两版；`impl/config.py` 处理配置和发行包；`impl/repository.py` 准备仓库；
`impl/build.py` 执行并记录单次构建。`capture.init.gradle` 覆盖 KGP 版本、限制最终 link 缓存并输出实际编译器路径。

### 内联选项

demo 使用默认内联行为，场景提交不切换内联选项。Kotlin/Native 2.4.21 的 `-Xklib-ir-inliner=` 支持：

| 值 | 生成 KLIB 时的行为 |
|---|---|
| `default` | 根据语言版本选择默认模式 |
| `intra-module` | 提前内联本模块的 inline 函数 |
| `full` | 同时提前内联依赖模块的 inline 函数；2.4.21 会标记 KLIB 为 pre-release |
| `disabled` | 关闭该阶段的提前内联，后续 Native 内联仍可发生 |

2.4 默认模块内提前内联，2.5 扩展到跨模块内联，见[官方说明](https://kotlinlang.org/docs/whatsnew-eap.html#consistent-cross-module-function-inlining-during-klib-compilation)。
这些是编译器参数，不能直接当作 Gradle 参数传入。本工具不设置内联选项。

```sh
.venv/bin/python -m pytest -q
```
