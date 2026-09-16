"""Jev MCP server package."""

import logging
import os

# TypeSafe's SDK honors this environment variable at import time. Keep its
# request/response logging at WARNING so state bodies are never emitted.
os.environ["TYPESAFE_LOG_LEVEL"] = "warning"
logging.getLogger("typesafe_sdk").setLevel(logging.WARNING)

__version__ = "0.2.1"
