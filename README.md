# complexity-ratchet

A composite GitHub Action that fails CI when Python code touched by a PR
regresses in [Radon](https://radon.readthedocs.io/) cyclomatic complexity.

It compares functions and methods (not classes — see below) between the
merge-base of `origin/main` and the head revision, for files that changed
between those two points, and fails when:

- a function/method exists only at head and its complexity is worse than
  grade B (complexity > 10), or
- a function/method exists at both revisions, its complexity increased, and
  the resulting (head) complexity leaves grade A (complexity > 5).

A regression that lands the block anywhere within grade A — e.g. 1 → 2, or
4 → 5 — is allowed: grade A is already the lowest, self-imposed complexity
band, so a one-point wobble inside it (a single added guard clause, say)
isn't the kind of creep this ratchet exists to catch. Once a block's head
complexity leaves grade A, any increase is flagged, including further growth
of an already-worse-than-A block.

New code is held to a real bar; existing code you didn't touch is never
penalized; and code you did touch can only get simpler, stay the same, or
wobble within grade A.

## Why classes are excluded

Radon reports a class's "complexity" as roughly
`average(method_complexities) + 1` — not a measurement of anything you
changed. Adding one perfectly reasonable method whose complexity is above a
class's current average will always raise that average, regardless of how
clean the method is. Ratcheting on that number punishes normal class growth
and rewards gaming it (e.g. moving methods to module-level functions purely
to dodge the average). Per-function/method complexity doesn't have this
problem, since each one is a real, independent unit — so only those are
ratcheted.

## Usage

Your workflow needs to check out enough history to resolve a merge-base
(`fetch-depth: 0`, or at least enough to reach the base branch), and needs
Python + `radon` available on `PATH` (or via whatever command you pass as
`python-command`) before calling this action.

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0

- uses: astral-sh/setup-uv@v5
- run: uv sync

- uses: relomy/complexity-ratchet@main
  with:
    python-command: "uv run python"
```

Pin to a commit SHA or tag once one exists, rather than tracking `main`, so a
change here can't silently affect every caller repo's CI on its next run.

### Inputs

| Input             | Default                                   | Description                                                        |
| ------------------ | ------------------------------------------ | -------------------------------------------------------------------- |
| `base`            | merge-base of `origin/main` and `head`    | Base revision to compare against.                                   |
| `head`            | `HEAD`                                    | Head revision to check.                                             |
| `python-command`  | `python`                                  | Command used to invoke Python (e.g. `uv run python`, `poetry run python`). |

## Known limitations

- Assumes the base branch is named `main` when `base` isn't given.
- No path-exclusion mechanism — every changed `.py` file is in scope
  (including tests, migrations, generated code).
- A block renamed and made more complex in the same change is treated as
  "new" (checked against the grade-B bar) rather than as a regression of
  the old block, since blocks are matched by qualified name.
- Requires enough git history to compute a merge-base; a shallow checkout
  can make that fail.

## Local development

```bash
pip install pytest radon
pytest -q
```
