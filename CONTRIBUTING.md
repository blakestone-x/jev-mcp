# Contributing

Install the development extras, then run:

```powershell
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pyright
python -m pytest
```

Tests never touch the network. Use the in-memory transport fixtures for SDK
requests and do not call a live service from a test.

Policy thresholds change only in `jev_mcp/questions.py`, and every threshold
change must include or update a focused test that demonstrates the boundary.
Keep tool inputs typed, validate external data at the boundary, and avoid
logging request state or credentials.
