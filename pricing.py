"""Cost estimates. NO built-in prices: provider prices change, so you supply them.
prices.json: {"model-id": [usd_per_1M_input_tokens, usd_per_1M_output_tokens]}  (or "provider/model-id")."""
from __future__ import annotations

import json
from pathlib import Path


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)  # rough: ~4 chars per token


class Pricing:
    def __init__(self, table: dict | None = None):
        self.table = table or {}

    @classmethod
    def load(cls, path) -> "Pricing":
        p = Path(path) if path else None
        return cls(json.loads(p.read_text()) if p and p.exists() else {})

    def cost(self, model: str, tokens_in: int, tokens_out: int, provider: str = "") -> float | None:
        rate = self.table.get(f"{provider}/{model}") or self.table.get(model)
        if not rate:
            return None  # unknown price: say so, never guess
        return (tokens_in * rate[0] + tokens_out * rate[1]) / 1_000_000
