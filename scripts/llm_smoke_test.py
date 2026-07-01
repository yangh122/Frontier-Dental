"""Quick smoke test: confirm the LLM fallback client can reach the provider
and return parsed JSON. Run from the repo root:

    ./.venv/Scripts/python.exe scripts/llm_smoke_test.py
"""

from __future__ import annotations

from src.config import Config
from src.llm.client import LLMClient


def main() -> None:
    cfg = Config.load("config.yaml")
    llm = LLMClient(cfg)

    print(f"provider = {llm.provider}")
    print(f"model    = {llm.model}")
    print(f"available = {llm.available}")
    if llm._disabled_reason:
        print(f"DISABLED: {llm._disabled_reason}")
        return

    result = llm.extract_json(
        system="You extract product data. Reply with a single JSON object.",
        user=(
            "Extract fields from this text as JSON with keys "
            "'name', 'material', 'sizes': "
            "'Nitrile exam gloves, powder-free, sizes S/M/L.'"
        ),
    )
    print(f"llm_calls = {llm.calls}")
    print("response:", result)

    if result is None:
        print("\n=> Call returned None (see logs/crawl.log for the reason).")
    else:
        print("\n=> LLM works: got parsed JSON back.")


if __name__ == "__main__":
    main()
