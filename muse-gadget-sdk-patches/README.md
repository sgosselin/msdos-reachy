# Muse SDK patches

Each `<hash>.patch` applies to exactly that full upstream Git commit. The current baseline is `f60347aeb851a1fe9438cfc388721776aa24f275` from Meta's `facebookincubator/muse-gadget-sdk` repository.

The patch adds optional `Service.command_specs` and `Service.session_factory` fields. It adds three lines and replaces two in `linux/src/musegadget/service.py`. Defaults retain stock behavior; executor, pairing and cryptography are unchanged. The patch contains small excerpts of upstream Apache-2.0 licensed code; upstream copyright and license remain applicable.

From this repository, prepare an isolated checkout:

```sh
python3 scripts/prepare_sdk.py --source ../muse-gadget-sdk --destination artifacts/sdk-worktree
.venv/bin/python -m pip install --no-build-isolation -e artifacts/sdk-worktree/linux
.venv/bin/python -m pytest -q -c artifacts/sdk-worktree/linux/pyproject.toml artifacts/sdk-worktree/linux/tests
.venv/bin/python -m pytest -q
```

Preparation requires a new destination, verifies the upstream revision and clean source, checks the patch, then applies it only in the new worktree. It never modifies the original checkout. Reuse existing prepared worktrees without rerunning preparation.

For an upgrade:

1. Record the candidate commit and review changes in `Service`, `LinkSession`, `Executor` and `MuseTurn`.
2. Create a separate patch named for that commit. Keep previous patch files.
3. Run upstream and companion tests. The SDK integration tests use the upstream in-memory Noise fake VM. Set `MUSE_SDK_TESTS` to its `linux/tests` directory if using another worktree location.
4. Build wheels, record checksums privately and retain previous wheels for rollback.
5. Validate text/tool round trips and reconnect on Reachy. Repeat physical tests when hardware dependencies change.

Do not apply a patch to an unverified revision or edit installed `site-packages`.
