"""Fail CI when touched Python code regresses in cyclomatic complexity.

Compares Radon cyclomatic-complexity blocks between the merge-base of
``origin/main``/``HEAD`` (or explicit revisions) and the head revision, for
Python files changed between those two points. A block is identified by its
qualified name (module path + class + function/method name) so that renamed
files or reordered code don't produce false positives as long as the block's
name is stable.

Only function/method blocks are compared. Radon's class-level "complexity" is
an average of its methods' complexities (roughly ``avg(method_complexities) +
1``), not a real measurement of anything a change touches directly: adding a
single well-scoped method whose complexity is above the class's current
average always raises that average, regardless of how clean the method is.
Ratcheting on that number would punish normal class growth and reward
gaming it (e.g. moving methods to module-level functions purely to dodge the
average), so class blocks are excluded here; per-method/per-function
complexity is where a real regression would show up anyway.

Fails when:
  - a function or method exists only at head and its complexity is worse
    than grade B (i.e. complexity > 10), or
  - a function or method exists at both revisions, its complexity increased,
    and the resulting (head) complexity leaves grade A (i.e. complexity > 5).
    A regression that lands the block anywhere within grade A -- e.g. 1 -> 2,
    or 4 -> 5 -- is allowed: grade A is already the lowest, self-imposed
    complexity band, so a one-point wobble inside it (a single added guard
    clause, say) isn't the kind of creep this ratchet exists to catch. Once a
    block's head complexity leaves grade A, any increase is still flagged,
    including further growth of an already-worse-than-A block (e.g. 11 -> 15).

Run as a script (``uv run python complexity_ratchet.py``) or import
``compare_blocks``/``check_revisions``/``check_worktree`` for testing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

GRADE_A_MAX_COMPLEXITY = 5
GRADE_B_MAX_COMPLEXITY = 10


@dataclass(frozen=True)
class Violation:
    qualified_name: str
    file_path: str
    base_complexity: int | None
    base_rank: str | None
    head_complexity: int
    head_rank: str

    def describe(self) -> str:
        if self.base_complexity is None:
            return (
                f"{self.file_path}: {self.qualified_name} is new with complexity "
                f"{self.head_complexity} (grade {self.head_rank}); new code must be "
                "grade B or better"
            )
        return (
            f"{self.file_path}: {self.qualified_name} complexity increased from "
            f"{self.base_complexity} (grade {self.base_rank}) to {self.head_complexity} "
            f"(grade {self.head_rank})"
        )


def run_git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def get_merge_base(base_ref: str, head_ref: str, cwd: Path | None = None) -> str:
    return run_git(["merge-base", base_ref, head_ref], cwd=cwd).strip()


def get_changed_python_files(
    base_rev: str, head_rev: str, cwd: Path | None = None
) -> list[str]:
    output = run_git(
        ["diff", "--name-only", "--diff-filter=ACMR", base_rev, head_rev],
        cwd=cwd,
    )
    return [line for line in output.splitlines() if line.endswith(".py")]


def get_worktree_changed_python_files(
    base_rev: str, cwd: Path | None = None
) -> list[str]:
    """Return tracked and untracked Python files changed from ``base_rev``."""
    tracked_output = run_git(
        ["diff", "--name-only", "--diff-filter=ACMR", base_rev, "--"], cwd=cwd
    )
    untracked_output = run_git(["ls-files", "--others", "--exclude-standard"], cwd=cwd)
    paths = {
        line
        for output in (tracked_output, untracked_output)
        for line in output.splitlines()
        if line.endswith(".py")
    }
    return sorted(paths)


def file_exists_at_revision(
    revision: str, file_path: str, cwd: Path | None = None
) -> bool:
    try:
        run_git(["cat-file", "-e", f"{revision}:{file_path}"], cwd=cwd)
        return True
    except subprocess.CalledProcessError:
        return False


def materialize_files_at_revision(
    revision: str, file_paths: list[str], dest_dir: Path, cwd: Path | None = None
) -> list[Path]:
    """Write each file's content at ``revision`` into ``dest_dir``, preserving relative paths."""
    written: list[Path] = []
    for file_path in file_paths:
        if not file_exists_at_revision(revision, file_path, cwd=cwd):
            continue
        content = run_git(["show", f"{revision}:{file_path}"], cwd=cwd)
        target = dest_dir / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append(target)
    return written


def materialize_worktree_files(
    file_paths: list[str], dest_dir: Path, repo_root: Path
) -> list[Path]:
    """Copy current working-tree files into ``dest_dir`` preserving their paths."""
    written: list[Path] = []
    for file_path in file_paths:
        source = repo_root / file_path
        if not source.is_file():
            continue
        target = dest_dir / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        written.append(target)
    return written


def run_radon_json(paths: list[Path], cwd: Path) -> dict:
    if not paths:
        return {}
    result = subprocess.run(
        [sys.executable, "-m", "radon", "cc", "--json", *[str(p) for p in paths]],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def flatten_blocks(radon_json: dict, root: Path) -> dict[str, dict]:
    """Flatten Radon's JSON output into {qualified_name: block}, keyed by module + name.

    Class-level entries are skipped: Radon reports a class's "complexity" as
    an average over its methods, not a real measurement of any single change,
    so ratcheting on it produces false positives (see module docstring).
    Methods are unaffected -- Radon also lists them as their own top-level
    entries alongside the class.
    """
    blocks: dict[str, dict] = {}
    for file_path, entries in radon_json.items():
        module = Path(file_path).relative_to(root).as_posix()
        for entry in entries:
            if entry.get("type") == "class":
                continue
            qualified_name = _qualified_name(module, entry)
            blocks[qualified_name] = entry
    return blocks


def _qualified_name(module: str, entry: dict) -> str:
    classname = entry.get("classname")
    name = entry["name"]
    if classname:
        return f"{module}::{classname}.{name}"
    return f"{module}::{name}"


def compare_blocks(
    base_blocks: dict[str, dict], head_blocks: dict[str, dict]
) -> list[Violation]:
    violations: list[Violation] = []
    for qualified_name, head_entry in head_blocks.items():
        file_path = qualified_name.split("::", 1)[0]
        head_complexity = head_entry["complexity"]
        head_rank = head_entry["rank"]
        base_entry = base_blocks.get(qualified_name)
        if base_entry is None:
            if head_complexity > GRADE_B_MAX_COMPLEXITY:
                violations.append(
                    Violation(
                        qualified_name=qualified_name,
                        file_path=file_path,
                        base_complexity=None,
                        base_rank=None,
                        head_complexity=head_complexity,
                        head_rank=head_rank,
                    )
                )
            continue
        base_complexity = base_entry["complexity"]
        if (
            head_complexity > base_complexity
            and head_complexity > GRADE_A_MAX_COMPLEXITY
        ):
            violations.append(
                Violation(
                    qualified_name=qualified_name,
                    file_path=file_path,
                    base_complexity=base_complexity,
                    base_rank=base_entry["rank"],
                    head_complexity=head_complexity,
                    head_rank=head_rank,
                )
            )
    return violations


def check_revisions(
    base_rev: str, head_rev: str, repo_root: Path | None = None
) -> list[Violation]:
    repo_root = repo_root or Path.cwd()
    changed_files = get_changed_python_files(base_rev, head_rev, cwd=repo_root)
    if not changed_files:
        return []

    with (
        tempfile.TemporaryDirectory() as base_tmp,
        tempfile.TemporaryDirectory() as head_tmp,
    ):
        base_dir = Path(base_tmp)
        head_dir = Path(head_tmp)

        base_paths = materialize_files_at_revision(
            base_rev, changed_files, base_dir, cwd=repo_root
        )
        head_paths = materialize_files_at_revision(
            head_rev, changed_files, head_dir, cwd=repo_root
        )

        base_json = run_radon_json(base_paths, cwd=base_dir)
        head_json = run_radon_json(head_paths, cwd=head_dir)

        base_blocks = flatten_blocks(base_json, base_dir)
        head_blocks = flatten_blocks(head_json, head_dir)

    return compare_blocks(base_blocks, head_blocks)


def check_worktree(base_rev: str, repo_root: Path | None = None) -> list[Violation]:
    """Compare ``base_rev`` with the current working tree before committing."""
    repo_root = repo_root or Path.cwd()
    changed_files = get_worktree_changed_python_files(base_rev, cwd=repo_root)
    if not changed_files:
        return []

    with (
        tempfile.TemporaryDirectory() as base_tmp,
        tempfile.TemporaryDirectory() as head_tmp,
    ):
        base_dir = Path(base_tmp)
        head_dir = Path(head_tmp)

        base_paths = materialize_files_at_revision(
            base_rev, changed_files, base_dir, cwd=repo_root
        )
        head_paths = materialize_worktree_files(changed_files, head_dir, repo_root)

        base_json = run_radon_json(base_paths, cwd=base_dir)
        head_json = run_radon_json(head_paths, cwd=head_dir)

        base_blocks = flatten_blocks(base_json, base_dir)
        head_blocks = flatten_blocks(head_json, head_dir)

    return compare_blocks(base_blocks, head_blocks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=None,
        help="Base revision (default: merge-base of origin/main and --head)",
    )
    head_group = parser.add_mutually_exclusive_group()
    head_group.add_argument(
        "--head", default="HEAD", help="Head revision (default: HEAD)"
    )
    head_group.add_argument(
        "--worktree",
        action="store_true",
        help="Compare the base revision with the current working tree, including uncommitted files.",
    )
    args = parser.parse_args(argv)

    base_rev = args.base
    if base_rev is None:
        base_rev = get_merge_base("origin/main", args.head)

    if args.worktree:
        violations = check_worktree(base_rev)
    else:
        violations = check_revisions(base_rev, args.head)

    if violations:
        for violation in violations:
            print(violation.describe())
        print("::error::complexity ratchet found regressions in touched code")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
