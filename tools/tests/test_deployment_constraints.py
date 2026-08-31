#!/usr/bin/env python3
"""Deployment constraints the bridge must satisfy under the documented Docker
setup, not just under a bare-metal install.

Background: the single-instance lock originally lived next to the bridge
script. That works on a normal install and fails on every Docker install,
because the documented compose file mounts the tools directory read-only
(./modules/mod-llm-chatter/tools:/app:ro). The whole test suite was green
while the bridge could not start at all under the documented configuration.

These tests encode the constraint itself rather than the specific bug, so
anything else that tries to write into the read-only mount is caught too.

Run directly from the module root:
  python tools/tests/test_deployment_constraints.py
"""

import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm_chatter_bridge as bridge  # noqa: E402

TOOLS_DIR = Path(bridge.__file__).resolve().parent
SAMPLE_CONFIG = "/etc/mod_llm_chatter.conf"


def _runtime_dir(config=None):
    return bridge._resolve_runtime_dir(config)


def test_lock_never_lands_in_the_read_only_tools_mount():
    """The lock must not resolve to anything under the tools directory.

    In Docker that directory is /app, mounted :ro, so a lock placed there
    cannot be created and the bridge exits before it starts.
    """
    lock_path = Path(
        bridge._lock_file_path(SAMPLE_CONFIG, _runtime_dir())
    ).resolve()
    assert TOOLS_DIR not in lock_path.parents, (
        "lock resolves to %s, which is inside the read-only tools mount"
        % lock_path
    )


def test_taking_the_lock_writes_nothing_into_the_tools_directory():
    """Directly encode the guarantee: acquiring the lock leaves the
    (read-only, in Docker) tools tree byte-for-byte untouched."""
    before = {p: p.stat().st_mtime for p in TOOLS_DIR.rglob("*")
              if p.is_file() and ".git" not in p.parts}

    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, "instance.conf")
        bridge._acquire_single_instance_lock(cfg, None)

    after = {p: p.stat().st_mtime for p in TOOLS_DIR.rglob("*")
             if p.is_file() and ".git" not in p.parts}

    created = sorted(str(p.relative_to(TOOLS_DIR)) for p in set(after) - set(before))
    modified = sorted(
        str(p.relative_to(TOOLS_DIR))
        for p in set(before) & set(after)
        if before[p] != after[p]
    )
    assert not created, "files created in the read-only mount: %s" % created
    assert not modified, "files modified in the read-only mount: %s" % modified


def test_unwritable_runtime_dir_is_skipped_rather_than_fatal():
    """A configured runtime dir that cannot be written must fall through to
    a usable one instead of taking the bridge down."""
    with tempfile.TemporaryDirectory() as tmp:
        readonly = os.path.join(tmp, "readonly")
        os.makedirs(readonly)
        os.chmod(readonly, stat.S_IRUSR | stat.S_IXUSR)
        try:
            resolved = bridge._resolve_runtime_dir(
                {"LLMChatter.RuntimeDir": readonly}
            )
            assert os.path.realpath(resolved) != os.path.realpath(readonly), (
                "resolver returned a directory it cannot write to"
            )
            assert os.access(resolved, os.W_OK), (
                "resolver returned a non-writable directory: %s" % resolved
            )
        finally:
            # Restore so TemporaryDirectory can clean up.
            os.chmod(readonly, stat.S_IRWXU)


def test_separate_configs_get_separate_locks():
    """Two bridges deliberately run against different configs must not lock
    each other out; two launches of the same config must collide."""
    rt = _runtime_dir()
    a1 = bridge._lock_file_path("/etc/one.conf", rt)
    a2 = bridge._lock_file_path("/etc/one.conf", rt)
    b = bridge._lock_file_path("/etc/two.conf", rt)
    assert a1 == a2, "same config produced two different lock paths"
    assert a1 != b, "different configs collided on one lock path"


def test_lock_path_is_stable_across_equivalent_config_paths():
    """A relative and absolute spelling of the same config is one instance."""
    rt = _runtime_dir()
    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, "instance.conf")
        open(cfg, "w").close()
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            relative = bridge._lock_file_path("instance.conf", rt)
        finally:
            os.chdir(cwd)
        absolute = bridge._lock_file_path(cfg, rt)
    assert relative == absolute, (
        "the same config file produced two lock paths depending on how it "
        "was spelled, so one bridge would not see the other"
    )


def main() -> int:
    tests = [
        test_lock_never_lands_in_the_read_only_tools_mount,
        test_taking_the_lock_writes_nothing_into_the_tools_directory,
        test_unwritable_runtime_dir_is_skipped_rather_than_fatal,
        test_separate_configs_get_separate_locks,
        test_lock_path_is_stable_across_equivalent_config_paths,
    ]
    for test in tests:
        test()
        print("PASS: %s" % test.__name__)
    print("\n%d tests passed" % len(tests))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
