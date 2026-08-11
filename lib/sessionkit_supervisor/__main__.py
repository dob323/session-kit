"""Run the fleet supervisor MCP server over standard input/output."""

from .server import main


if __name__ == "__main__":
    raise SystemExit(main())
