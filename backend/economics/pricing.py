"""Token prices. Cost is computed at call time and stored on the event row,
so a later price change never rewrites history."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_DEFAULT_PATH = Path(__file__).with_name("pricing.json")


@lru_cache
def _table(path: str) -> dict:
    return json.loads(Path(path).read_text())


def price_table() -> dict:
    return _table(os.environ.get("MODEL_PRICING_PATH", str(_DEFAULT_PATH)))


def is_priced(model: str) -> bool:
    return model in price_table()["models"]


def cost_usd(model: str, tokens_in: int, tokens_out: int = 0) -> float:
    table = price_table()
    price = table["models"].get(model, table["default"])
    return round((tokens_in * price["input"] + tokens_out * price["output"]) / 1_000_000, 8)


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free estimate (about four characters per token for code and English)."""
    return max(1, len(text) // 4)
