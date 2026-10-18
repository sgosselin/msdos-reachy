#!/usr/bin/env python3
"""Apply the versioned SDK patch in a new worktree; leave the source untouched."""

import argparse
import subprocess
from pathlib import Path

REVISION = "f60347aeb851a1fe9438cfc388721776aa24f275"


def git(source, *args):
    return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True, text=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("../muse-gadget-sdk"))
    parser.add_argument("--destination", type=Path, default=Path("artifacts/sdk-worktree"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    patch = root / "muse-gadget-sdk-patches" / (REVISION + ".patch")
    if git(args.source, "rev-parse", "HEAD") != REVISION:
        parser.error("Source HEAD does not match the patch revision")
    if git(args.source, "status", "--porcelain"):
        parser.error("Source checkout must be clean")
    if args.destination.exists():
        parser.error("Destination already exists; reuse it or select a new destination")
    git(args.source, "apply", "--check", str(patch))
    git(args.source, "worktree", "add", "--detach", str(args.destination.resolve()), REVISION)
    git(args.destination, "apply", "--check", str(patch))
    git(args.destination, "apply", str(patch))
    print("Prepared SDK worktree at the pinned revision with the service hook patch.")


if __name__ == "__main__":
    main()
