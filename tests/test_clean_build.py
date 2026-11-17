"""Check clean-mode configuration, execution, and evidence safeguards."""

import importlib
import json
from pathlib import Path
import subprocess

from pydantic import ValidationError
import pytest

from impl.build import build_project
from impl.collection import collect_cache_files
from impl.config import Compiler, Project


def test_clean_build_requires_boolean():
    assert not Compiler(kotlin_version="2.4.21").clean_build
    with pytest.raises(ValidationError):
        Compiler(kotlin_version="2.4.21", clean_build="false")


@pytest.mark.parametrize("task_state,confirmed,error", [("EXECUTED", True, None), ("UP-TO-DATE", True, "must execute"), ("EXECUTED", False, "did not confirm")])
def test_clean_link_rejects_reused_or_cached_results(tmp_path, monkeypatch, task_state, confirmed, error):
    monkeypatch.setattr("impl.config.PROJECT_ROOT", tmp_path)
    project_directory = tmp_path / "project"
    project_directory.mkdir()
    output = project_directory / "app.kexe"
    output.touch()
    project = Project(repository="https://example.test/project.git", branch="main", start_commit="a", end_commit="b", task=":app:link")

    def fake_gradle(command, cwd, stdout, **kwargs):
        assert "-Pic.clean.build=true" in command
        assert "-Pic.force.link=true" in command
        assert "-Pkotlin.incremental.native=true" in command
        assert not any(argument.startswith("-Pkotlin.native.cacheKind=") for argument in command)
        suffix = "" if task_state == "EXECUTED" else f" {task_state}"
        print(f"> Task :app:link{suffix}", file=stdout)
        print("IC_FUZZER " + json.dumps(dict(home="/compiler", output=str(output), built_caches=str(project_directory / "built-caches.txt"), project_native_caches_empty=confirmed)), file=stdout)
        (project_directory / "built-caches.txt").write_text("")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(importlib.import_module("impl.build").subprocess, "run", fake_gradle)
    result = build_project(project_directory, project, Compiler(kotlin_version="2.4.21", clean_build=True), Path("/compiler"), "0000-clean")
    assert result["clean_build"]
    assert (result["error"] is None) if error is None else error in result["error"]
    if error is None:
        assert collect_cache_files(result, tmp_path / "store") == []
        assert result["rebuilt_cache_count"] == 0
