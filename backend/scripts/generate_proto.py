#!/usr/bin/env python3
"""Regenerate the Python wire bindings from ``proto/``.

    python scripts/generate_proto.py          # regenerate in place
    python scripts/generate_proto.py --check   # fail if the tree is stale

The output is **checked in**. Installing or running the Controller must not
require a protobuf compiler: this is a control plane people deploy on a home
lab box, and "install protoc first" is a barrier out of all proportion to a
file that changes a few times a year. `--check` is what keeps the checked-in
copy honest, and it is a test rather than a hook so it runs where everything
else does.

Requires ``grpcio-tools``, which is a dev dependency for the same reason.
"""

from __future__ import annotations

import argparse
import filecmp
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
PROTO_ROOT = REPO / "proto"
OUT_ROOT = BACKEND / "src"

#: A compiled FileDescriptorSet, for the Agent's build.
#:
#: `prost-build` normally shells out to `protoc`. Feeding it a descriptor set
#: instead means building the Agent needs nothing but a Rust toolchain -- the
#: same property the checked-in Python bindings give the Controller, and it
#: matters more here: the Agent is the thing people cross-compile for five
#: architectures, and a protoc dependency would be in every one of those
#: toolchains.
DESCRIPTOR_SET = REPO / "agent" / "proto" / "agent.bin"

#: Every ``.proto`` in the tree. Listed by glob rather than by name so adding
#: one to the contract does not also require editing the build.
SOURCES = sorted(PROTO_ROOT.rglob("*.proto"))


#: What protoc itself produces, and therefore the only thing worth diffing.
#: The `__init__.py` shims below are ours, not its, and comparing them would
#: have `--check` report the Controller's own package init as stale output.
GENERATED = ("*_pb2.py", "*_pb2.pyi")


def generate(out_root: Path, descriptor_set: Path | None = None) -> None:
    from grpc_tools import protoc

    out_root.mkdir(parents=True, exist_ok=True)
    arguments = [
        "protoc",
        f"--proto_path={PROTO_ROOT}",
        f"--python_out={out_root}",
        f"--pyi_out={out_root}",
        *(str(path) for path in SOURCES),
    ]
    if descriptor_set is not None:
        descriptor_set.parent.mkdir(parents=True, exist_ok=True)
        arguments.insert(1, f"--descriptor_set_out={descriptor_set}")
        arguments.insert(1, "--include_imports")
    if protoc.main(arguments) != 0:
        raise SystemExit("protoc failed")

    # protoc emits package directories without __init__.py, so the generated
    # modules would be importable only by accident of namespace packages.
    # Making them explicit keeps `bystack` a regular package and keeps the
    # wheel build (which lists `packages = ["src/bystack"]`) picking them up.
    #
    # Never overwrites. The topmost directory here is `bystack` itself, which
    # is the Controller's own package and already has an `__init__.py` worth
    # rather more than an empty file.
    for directory in _package_dirs(out_root):
        init = directory / "__init__.py"
        if not init.exists():
            init.write_text(_INIT_DOC if _holds_generated(directory) else "")


def _package_dirs(out_root: Path) -> list[Path]:
    """Every directory on the path to a generated module, outermost first."""
    found: dict[Path, None] = {}
    for source in SOURCES:
        package = source.relative_to(PROTO_ROOT).parent
        for depth in range(1, len(package.parts) + 1):
            found[out_root / Path(*package.parts[:depth])] = None
    return list(found)


def _holds_generated(directory: Path) -> bool:
    return any(directory.glob(pattern) for pattern in GENERATED)


_INIT_DOC = '''"""Generated wire bindings. Do not edit.

Regenerate with ``python scripts/generate_proto.py`` after changing the
``.proto``; ``tests/test_wire.py`` fails if this directory drifts from it.
"""
'''


def check() -> int:
    """Regenerate into a temporary tree and compare, without touching the repo."""
    with tempfile.TemporaryDirectory() as temporary:
        candidate = Path(temporary)
        descriptor = candidate / "_descriptor" / "agent.bin"
        generate(candidate, descriptor)

        stale: list[str] = []
        if not DESCRIPTOR_SET.exists() or not filecmp.cmp(
            descriptor, DESCRIPTOR_SET, shallow=False
        ):
            stale.append(str(DESCRIPTOR_SET.relative_to(REPO)))
        for pattern in GENERATED:
            for produced in sorted(candidate.rglob(pattern)):
                relative = produced.relative_to(candidate)
                committed = OUT_ROOT / relative
                if not committed.exists() or not filecmp.cmp(produced, committed, shallow=False):
                    stale.append(str(relative))

        # A generated module that is not importable is as broken as one that is
        # out of date, and the failure surfaces far from here.
        for directory in _package_dirs(candidate):
            relative = directory.relative_to(candidate)
            if not (OUT_ROOT / relative / "__init__.py").exists():
                stale.append(f"{relative}/__init__.py (missing)")

    if stale:
        print("generated bindings are stale:", file=sys.stderr)
        for path in stale:
            print(f"  {path}", file=sys.stderr)
        print("\nrun: python scripts/generate_proto.py", file=sys.stderr)
        return 1

    print(f"bindings are current ({len(SOURCES)} proto file(s))")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail if regeneration would change anything"
    )
    args = parser.parse_args(argv)

    if not SOURCES:
        raise SystemExit(f"no .proto files under {PROTO_ROOT}")

    if args.check:
        return check()

    generate(OUT_ROOT, DESCRIPTOR_SET)
    print(f"generated {len(SOURCES)} proto file(s) into {OUT_ROOT}")
    print(f"wrote the agent's descriptor set to {DESCRIPTOR_SET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
