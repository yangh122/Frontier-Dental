"""Resumability via a simple append-only checkpoint of completed product URLs.

If a run dies halfway (crash, network, Ctrl-C), the next run skips URLs already
persisted and continues. Cheap and durable — good enough for a POC, and the
README notes the production upgrade path (queue + DB state).
"""

from __future__ import annotations

from pathlib import Path


class Checkpoint:
    def __init__(self, path: str = ".checkpoints/done.txt"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._done: set[str] = set()
        if self.path.exists():
            self._done = {
                line.strip()
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }

    def is_done(self, url: str) -> bool:
        return url in self._done

    def mark(self, url: str) -> None:
        if url in self._done:
            return
        self._done.add(url)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(url + "\n")

    def __len__(self) -> int:
        return len(self._done)
