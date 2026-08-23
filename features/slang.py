from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT


class SlangDictionary:
    def __init__(self, config: dict[str, Any]) -> None:
        self.path = PROJECT_ROOT / config["slang"]["dict_path"]
        self.prompt_max_chars = max(
            0, int(config["slang"].get("prompt_max_chars", 800))
        )
        self.entries = self._load()

    def apply(self, text: str) -> str:
        result = text
        # Prefer specific pronunciations when one entry contains another.
        ordered = sorted(self.entries.items(), key=lambda item: len(item[0]), reverse=True)
        for source, target in ordered:
            if source:
                result = result.replace(source, str(target))
        return result

    def initial_prompt_addition(self) -> str:
        if not self.entries:
            return ""
        prefix = "유행어/특수 발음 사전: "
        pairs = [
            f"{source}={target}"
            for source, target in sorted(
                self.entries.items(), key=lambda item: len(item[0]), reverse=True
            )
        ]
        prompt = prefix + ", ".join(pairs)
        if self.prompt_max_chars <= 0:
            return ""
        if len(prompt) <= self.prompt_max_chars:
            return prompt
        return prompt[: self.prompt_max_chars].rsplit(", ", 1)[0]

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
