"""Retired unscoped memory writer. No import or command here opens a database.

The old DEFAULTS were placeholders and must never become real memories. Model
writes use memento. Operator maintenance uses memory_mutations.py with an exact
owner selection, a reviewed request JSON, preview first and explicit --apply.
"""
import argparse
import sys


def add(*args, **kwargs):
    raise RuntimeError("Unscoped seed writes are retired; use memory_mutations.py")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--add")
    parser.add_argument("--track")
    parser.add_argument("--from-json")
    parser.add_argument("--list", action="store_true")
    parser.parse_args(argv)
    print("seed_memory.py is retired; no database was opened. "
          "Use memory_mutations.py add --db EXISTING_DB --request REVIEWED_JSON "
          "to preview; add --apply only after review. "
          "Request selection must explicitly contain track/scope/workspace_key/agent_key. "
          "Read entries through the scoped memory management view.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
