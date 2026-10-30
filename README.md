# debug-ic-fuzzer

逐提交比较 baseline / sut 的 Kotlin/Native Debug 增量编译结果，记录缓存重编，并在终点或异常时做 clean 对照。

需要 Python 3.12+、JDK、Git 和项目所需构建环境。

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/debug-ic-fuzzer --compilers docs/compilers.yaml --projects docs/projects.yaml --run-dir runs/run-1
```

先修改两份独立的 YAML，字段说明就在配置旁。运行目录必须不存在；结果见 `project-*/results/summary.json`，文件内容按 MD5 去重保存。

[执行流程](docs/execution-flow.html)（下载后用浏览器打开） · [编译器配置](docs/compilers.yaml) · [项目配置](docs/projects.yaml)

```sh
.venv/bin/python -m pytest
```

已验证 Linux x64、Kotlin 2.4.21 原版 A/A 的正逆回放和 clean 一致性；未验证实际 digest 优化版。缓存越界会记录后停止，不适配自定义 autoCacheDir。
