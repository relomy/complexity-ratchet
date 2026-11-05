import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "complexity_ratchet.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("complexity_ratchet", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_module()


def _block(name, complexity, rank, classname=None):
    return {
        "name": name,
        "complexity": complexity,
        "rank": rank,
        "classname": classname,
    }


def test_flatten_blocks_skips_class_entries():
    radon_json = {
        "/repo/pkg/mod.py": [
            {"type": "class", "name": "Widget", "complexity": 4, "rank": "A"},
            {
                "type": "method",
                "name": "render",
                "complexity": 3,
                "rank": "A",
                "classname": "Widget",
            },
        ]
    }

    blocks = ratchet.flatten_blocks(radon_json, Path("/repo"))

    assert list(blocks) == ["pkg/mod.py::Widget.render"]


def test_new_messy_function_fails():
    base_blocks = {}
    head_blocks = {"pkg/mod.py::messy": _block("messy", 15, "C")}

    violations = ratchet.compare_blocks(base_blocks, head_blocks)

    assert len(violations) == 1
    assert violations[0].qualified_name == "pkg/mod.py::messy"
    assert violations[0].base_complexity is None


def test_new_simple_function_passes():
    base_blocks = {}
    head_blocks = {"pkg/mod.py::simple": _block("simple", 4, "A")}

    assert ratchet.compare_blocks(base_blocks, head_blocks) == []


def test_increased_complexity_fails():
    base_blocks = {"pkg/mod.py::fn": _block("fn", 11, "C")}
    head_blocks = {"pkg/mod.py::fn": _block("fn", 15, "C")}

    violations = ratchet.compare_blocks(base_blocks, head_blocks)

    assert len(violations) == 1
    assert violations[0].base_complexity == 11
    assert violations[0].head_complexity == 15


def test_increased_complexity_within_grade_a_passes():
    base_blocks = {"pkg/mod.py::fn": _block("fn", 1, "A")}
    head_blocks = {"pkg/mod.py::fn": _block("fn", 2, "A")}

    assert ratchet.compare_blocks(base_blocks, head_blocks) == []


def test_increased_complexity_still_within_grade_a_at_ceiling_passes():
    base_blocks = {"pkg/mod.py::fn": _block("fn", 4, "A")}
    head_blocks = {"pkg/mod.py::fn": _block("fn", 5, "A")}

    assert ratchet.compare_blocks(base_blocks, head_blocks) == []


def test_increased_complexity_leaving_grade_a_fails():
    base_blocks = {"pkg/mod.py::fn": _block("fn", 5, "A")}
    head_blocks = {"pkg/mod.py::fn": _block("fn", 6, "B")}

    violations = ratchet.compare_blocks(base_blocks, head_blocks)

    assert len(violations) == 1
    assert violations[0].base_complexity == 5
    assert violations[0].head_complexity == 6


def test_improved_complexity_passes():
    base_blocks = {"pkg/mod.py::fn": _block("fn", 15, "C")}
    head_blocks = {"pkg/mod.py::fn": _block("fn", 11, "C")}

    assert ratchet.compare_blocks(base_blocks, head_blocks) == []


def test_unchanged_complexity_passes():
    base_blocks = {"pkg/mod.py::fn": _block("fn", 9, "B")}
    head_blocks = {"pkg/mod.py::fn": _block("fn", 9, "B")}

    assert ratchet.compare_blocks(base_blocks, head_blocks) == []


def test_matches_methods_and_nested_blocks_by_qualified_name():
    base_blocks = {
        "pkg/mod.py::Widget.render": _block("render", 8, "B", classname="Widget"),
        "pkg/mod.py::Widget.build_inner": _block(
            "build_inner", 5, "A", classname="Widget"
        ),
    }
    head_blocks = {
        "pkg/mod.py::Widget.render": _block("render", 8, "B", classname="Widget"),
        "pkg/mod.py::Widget.build_inner": _block(
            "build_inner", 12, "C", classname="Widget"
        ),
    }

    violations = ratchet.compare_blocks(base_blocks, head_blocks)

    assert len(violations) == 1
    assert violations[0].qualified_name == "pkg/mod.py::Widget.build_inner"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def test_check_revisions_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "mod.py").write_text("def simple():\n    return 1\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "base")
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    messy_body = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(15))
    (repo / "mod.py").write_text(f"def messy(x):\n{messy_body}\n    return -1\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "add messy function")
    head_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    violations = ratchet.check_revisions(base_rev, head_rev, repo_root=repo)

    assert len(violations) == 1
    assert "messy" in violations[0].qualified_name


def test_check_revisions_ignores_class_average_regression(tmp_path):
    """A new method that is individually grade B, but pushes the *class's*
    averaged complexity score up, must not fail the ratchet -- only a real
    per-method/function regression should (see module docstring)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "mod.py").write_text(
        "class Widget:\n"
        "    def a(self):\n"
        "        return 1\n"
        "\n"
        "    def b(self):\n"
        "        if True:\n"
        "            return 1\n"
        "        return 2\n"
        "\n"
        "    def c(self):\n"
        "        for i in range(3):\n"
        "            if i:\n"
        "                return i\n"
        "        return 0\n"
    )
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "base")
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (repo / "mod.py").write_text(
        "class Widget:\n"
        "    def a(self):\n"
        "        return 1\n"
        "\n"
        "    def b(self):\n"
        "        if True:\n"
        "            return 1\n"
        "        return 2\n"
        "\n"
        "    def c(self):\n"
        "        for i in range(3):\n"
        "            if i:\n"
        "                return i\n"
        "        return 0\n"
        "\n"
        "    def f(self, x):\n"
        "        if x == 1:\n"
        "            return 1\n"
        "        if x == 2:\n"
        "            return 2\n"
        "        if x == 3:\n"
        "            return 3\n"
        "        if x == 4:\n"
        "            return 4\n"
        "        if x == 5:\n"
        "            return 5\n"
        "        return 0\n"
    )
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "add method f")
    head_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert ratchet.check_revisions(base_rev, head_rev, repo_root=repo) == []


def test_check_revisions_no_changed_python_files_passes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (repo / "README.md").write_text("hello again\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "docs")
    head_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert ratchet.check_revisions(base_rev, head_rev, repo_root=repo) == []


def test_check_worktree_detects_uncommitted_regression(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "mod.py").write_text("def fn(x):\n    return x\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "base")
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    messy_body = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(15))
    (repo / "mod.py").write_text(f"def fn(x):\n{messy_body}\n    return -1\n")

    violations = ratchet.check_worktree(base_rev, repo_root=repo)

    assert len(violations) == 1
    assert violations[0].qualified_name == "mod.py::fn"
    assert violations[0].base_complexity == 1


def test_check_worktree_includes_untracked_python_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    messy_body = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(15))
    (repo / "new_mod.py").write_text(f"def messy(x):\n{messy_body}\n    return -1\n")

    violations = ratchet.check_worktree(base_rev, repo_root=repo)

    assert len(violations) == 1
    assert violations[0].qualified_name == "new_mod.py::messy"
    assert violations[0].base_complexity is None


def _git_output(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _rev(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _grade_c_source(extra_branches=0):
    body = "\n".join(
        f"    if x == {i}:\n        return {i}" for i in range(10 + extra_branches)
    )
    return f"def busy(x):\n{body}\n    return -1\n"


def _compact_complex_source(name):
    # Short enough that appending it keeps git's rename similarity above 50%.
    cond = " or ".join(f"x == {i}" for i in range(11))
    return f"def {name}(x):\n    return {cond}\n"


def _repo_with_grade_c_module(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    _init_repo(repo)
    (repo / "pkg" / "a.py").write_text(_grade_c_source())
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _rev(repo)


def test_check_revisions_pure_rename_passes(tmp_path):
    repo, base_rev = _repo_with_grade_c_module(tmp_path)
    (repo / "pkg" / "sub").mkdir()
    _git(repo, "mv", "pkg/a.py", "pkg/sub/a.py")
    _git(repo, "commit", "-q", "-m", "move")

    assert ratchet.check_revisions(base_rev, _rev(repo), repo_root=repo) == []


def test_check_worktree_pure_rename_passes(tmp_path):
    repo, base_rev = _repo_with_grade_c_module(tmp_path)
    (repo / "pkg" / "sub").mkdir()
    _git(repo, "mv", "pkg/a.py", "pkg/sub/a.py")

    assert ratchet.check_worktree(base_rev, repo_root=repo) == []


def test_check_revisions_rename_with_regression_fails(tmp_path):
    repo, base_rev = _repo_with_grade_c_module(tmp_path)
    (repo / "pkg" / "sub").mkdir()
    _git(repo, "mv", "pkg/a.py", "pkg/sub/a.py")
    (repo / "pkg" / "sub" / "a.py").write_text(_grade_c_source(extra_branches=1))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "move and grow")

    violations = ratchet.check_revisions(base_rev, _rev(repo), repo_root=repo)

    assert len(violations) == 1
    assert violations[0].qualified_name == "pkg/sub/a.py::busy"
    assert violations[0].base_complexity == 11
    assert violations[0].head_complexity == 12


def test_check_worktree_rename_with_regression_fails(tmp_path):
    repo, base_rev = _repo_with_grade_c_module(tmp_path)
    (repo / "pkg" / "sub").mkdir()
    _git(repo, "mv", "pkg/a.py", "pkg/sub/a.py")
    (repo / "pkg" / "sub" / "a.py").write_text(_grade_c_source(extra_branches=1))

    violations = ratchet.check_worktree(base_rev, repo_root=repo)

    assert len(violations) == 1
    assert violations[0].base_complexity == 11
    assert violations[0].head_complexity == 12


def _nul(*records):
    return "".join(f"{r}\0" for r in records)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (_nul("A", "new.py", "M", "pkg/mod.py"), {"new.py": "new.py", "pkg/mod.py": "pkg/mod.py"}),
        (_nul("R100", "old.py", "new.py"), {"new.py": "old.py"}),
        (_nul("R087", "old.py", "new.txt"), {}),
        (_nul("R100", "old.txt", "new.py"), {"new.py": "new.py"}),
        (_nul("C100", "old.py", "copy.py"), {"copy.py": "old.py"}),
        (_nul("M", "README.md"), {}),
        (_nul("M", "café.py"), {"café.py": "café.py"}),
        ("", {}),
    ],
)
def test_parse_name_status(output, expected):
    assert ratchet._parse_name_status(output) == expected


def test_check_revisions_moved_file_with_new_complex_function(tmp_path):
    repo, base_rev = _repo_with_grade_c_module(tmp_path)
    (repo / "pkg" / "sub").mkdir()
    _git(repo, "mv", "pkg/a.py", "pkg/sub/a.py")
    moved = repo / "pkg" / "sub" / "a.py"
    moved.write_text(
        moved.read_text() + "\n\n" + _compact_complex_source("fresh")
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "move and add")

    violations = ratchet.check_revisions(base_rev, _rev(repo), repo_root=repo)

    assert len(violations) == 1
    assert violations[0].qualified_name == "pkg/sub/a.py::fresh"
    assert violations[0].base_complexity is None


def test_check_worktree_moved_file_with_new_complex_function(tmp_path):
    repo, base_rev = _repo_with_grade_c_module(tmp_path)
    (repo / "pkg" / "sub").mkdir()
    _git(repo, "mv", "pkg/a.py", "pkg/sub/a.py")
    moved = repo / "pkg" / "sub" / "a.py"
    moved.write_text(
        moved.read_text() + "\n\n" + _compact_complex_source("fresh")
    )

    violations = ratchet.check_worktree(base_rev, repo_root=repo)

    assert len(violations) == 1
    assert violations[0].qualified_name == "pkg/sub/a.py::fresh"
    assert violations[0].base_complexity is None


def test_check_revisions_rename_below_similarity_threshold_counts_as_new(tmp_path):
    # Documents a known limitation: git only detects a rename at >= 50%
    # similarity, so a move plus a heavy rewrite looks like delete + add and
    # the unchanged `busy` block is reported as new.
    repo, base_rev = _repo_with_grade_c_module(tmp_path)
    (repo / "pkg" / "sub").mkdir()
    _git(repo, "mv", "pkg/a.py", "pkg/sub/a.py")
    unrelated = "".join(f"CONSTANT_{i} = {i} * 1000 + {i}\n" for i in range(200))
    (repo / "pkg" / "sub" / "a.py").write_text(_grade_c_source() + "\n" + unrelated)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "move and rewrite")

    status = _git_output(repo, "diff", "-M", "--name-status", base_rev, "HEAD")
    assert not any(line.startswith("R") for line in status.splitlines())

    violations = ratchet.check_revisions(base_rev, _rev(repo), repo_root=repo)

    assert len(violations) == 1
    assert violations[0].qualified_name == "pkg/sub/a.py::busy"
    assert violations[0].base_complexity is None


def _non_ascii_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "café.py").write_text("def busy(x):\n    return x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _rev(repo)


def test_check_revisions_non_ascii_path(tmp_path):
    repo, base_rev = _non_ascii_repo(tmp_path)
    (repo / "café.py").write_text(_grade_c_source())
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "grow")

    violations = ratchet.check_revisions(base_rev, _rev(repo), repo_root=repo)

    assert len(violations) == 1
    assert violations[0].qualified_name == "café.py::busy"
    assert violations[0].base_complexity == 1


def test_check_worktree_non_ascii_paths(tmp_path):
    repo, base_rev = _non_ascii_repo(tmp_path)
    (repo / "café.py").write_text(_grade_c_source())
    (repo / "nové.py").write_text(_grade_c_source().replace("busy", "fresh"))

    violations = ratchet.check_worktree(base_rev, repo_root=repo)

    assert {v.qualified_name for v in violations} == {"café.py::busy", "nové.py::fresh"}
