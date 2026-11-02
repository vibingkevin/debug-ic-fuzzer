# debug-ic-fuzzer

逐提交比较 baseline / sut 的 Kotlin/Native Debug 增量编译结果，记录缓存重编，并在终点或异常时做 clean 对照。

需要 Python 3.12+、JDK、Git 和项目所需构建环境。

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/debug-ic-fuzzer --compilers configs/compilers.yaml --project configs/project.yaml
```

配置集中在 `configs/`，编译器与项目分开。把两版发行包分别放到 `configs/baseline.tgz`、`configs/sut.tgz`，或在 `compilers.yaml` 指定目录/压缩包；版本号按实际填写。`kotlin_native_home` 可省略，KGP 按 `kotlin_version` 使用或下载 prebuilt 编译器，位置为 `$KONAN_DATA_DIR/kotlin-native-prebuilt-<平台>-<架构>-<版本>`；未设置该环境变量时使用 `~/.konan`。两版指向同一缓存目录仍会报错。编译器相对路径以编译器 YAML 为准。项目 YAML 顶层直接填写一个项目，只指定一个完整任务路径；`gradle_args` 中用 `{config_dir}` 替换项目 YAML 所在目录，例如 `["-I", "{config_dir}/custom.init.gradle"]`，其余内容原样传给 Gradle。结果固定写入当前目录下的 `runs/<编译器 YAML 文件名>-<项目 YAML 文件名>/`（不含扩展名）；已有目录需先移走或删除。结果见 `results/summary.json`，文件内容按 MD5 去重保存。

仓库仅支持 Git URL，固定复用 `/tmp/debug-ic-fuzzer/<仓库名>-<URL摘要>/` 下的一份 clone 和一个 worktree。已有仓库会更新并切到指定分支。两版编译前轮流改名为同一 `project` 目录，结束后恢复，源码和构建缓存各自独立；首笔只清理工具记录的 IC 和产物目录，Gradle User Home 沿用用户环境（默认 `~/.gradle`），不再给两版单独建下载缓存。同一仓库不能同时跑两轮。

[执行流程](docs/execution-flow.html)（下载后用浏览器打开） · [编译器配置](configs/compilers.yaml) · [项目配置](configs/project.yaml) · [ic-sample 局部实现回放](configs/ic-sample.yaml) · [Animeko iOS 配置](configs/animeko.yaml) · [较长区间回放配置](configs/animeko-expanded.yaml)

```sh
.venv/bin/python -m pytest
```

已验证 Kotlin 2.4.21 原版 A/A：Linux x64 正逆回放，以及 Animeko iOS 三个静态 framework 的连续 12 提交正逆回放，均通过增量与 clean 对照，Animeko 正逆同提交结果也一致。缓存越界会记录后停止，不适配自定义 autoCacheDir。

Animeko（macOS）准备好 YAML 注释中的构建环境后运行：

```sh
.venv/bin/debug-ic-fuzzer --compilers configs/compilers.yaml --project configs/animeko.yaml
```

Animeko 两份配置均只编译应用实际接入的 `application.framework`（iOS Simulator ARM64 Debug），保留上游动态链接设置；区别仅在回放提交区间。之前三个工具模块的静态 framework 测试结果不代表完整应用 framework 已通过。

`debug_ic_fuzzer.py` 负责入口与回放；内部实现放在 `impl/`：`config.py` 管配置，`build.py` 管构建采集，`results.py` 管存储比较。

ic-sample 按提交回放源码变化，不应用 patch，也不设置特殊内联选项。产品构建任务为 `:app:linkDebugExecutableLinuxX64` 或 `:app:linkDebugFrameworkIosSimulatorArm64`；功能断言在 `commonTest`，由 `linuxX64Test` 等任务单独验证，不作为 fuzzer 的构建任务。

Configuration Cache 和 Build Cache 沿用项目设置；仅禁止最终被测 link 从 Build Cache 恢复，允许 `UP-TO-DATE`。临时采集路径固定，每次构建前清除旧记录，结束后归档到当前提交；未执行 link 时仍比较现有产物，不沿用旧重编清单。新实验清理两版各自根目录下的 `.gradle`、`.kotlin`，回放中保留状态；clean 对照清除已登记的 IC 和最终产物。

### KLIB 提前内联选项

内联模式与 demo 的源码场景独立，不编码进场景提交。工具不主动设置内联选项。
Kotlin/Native 2.4.21 的 `-Xklib-ir-inliner=<模式>` 支持：

| 模式 | 生成 KLIB 时的行为 |
|---|---|
| `default`（不指定） | 按语言版本选择默认模式 |
| `intra-module` | 提前内联当前模块内的 inline 函数 |
| `full` | 也提前内联依赖模块中的 inline 函数 |
| `disabled` | 关闭这个阶段的提前内联，后续 Native 内联仍可进行 |

2.4 默认启用模块内提前内联，2.5 默认扩展到跨模块内联，见[官方说明](https://kotlinlang.org/docs/whatsnew-eap.html#consistent-cross-module-function-inlining-during-klib-compilation)。
2.4.21 的 `full` 会给生成的 KLIB 标记 pre-release。
旧的 `-XXLanguage:-IrIntraModuleInlinerBeforeKlibSerialization` 关闭模块内提前内联；demo 不使用它。
提前内联会把函数体变化带进调用方 KLIB，因此不同模式下的 cache 重编数量可能不同。
这些是编译器参数，不能直接当作 Gradle 参数放进 `gradle_args`。
