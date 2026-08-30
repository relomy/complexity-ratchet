import importlib.util
import subprocess
import sys
from pathlib import Path

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
    return {"name": name, "complexity": complexity, "rank": rank, "classname": classname}


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
        "pkg/mod.py::Widget.build_inner": _block("build_inner", 5, "A", classname="Widget"),
    }
    head_blocks = {
        "pkg/mod.py::Widget.render": _block("render", 8, "B", classname="Widget"),
        "pkg/mod.py::Widget.build_inner": _block("build_inner", 12, "C", classname="Widget"),
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
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    messy_body = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(15))
    (repo / "mod.py").write_text(f"def messy(x):\n{messy_body}\n    return -1\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "add messy function")
    head_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
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
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
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
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
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
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    (repo / "README.md").write_text("hello again\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "docs")
    head_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    assert ratchet.check_revisions(base_rev, head_rev, repo_root=repo) == []
