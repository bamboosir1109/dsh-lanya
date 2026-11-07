"""Scheduled entrypoint; the scoped exporter never searches other workspaces."""
from chat_export import main

if __name__ == "__main__":
    raise SystemExit(main())
