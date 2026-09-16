"""Run the Jev MCP server over stdio."""

from .server import mcp


def main() -> None:
    """Start the MCP server using the official stdio transport."""

    mcp.run("stdio")


if __name__ == "__main__":
    main()
