import json
import re
import time
from typing import Dict, List, Optional

import requests

from .config import Settings

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_EXHAUSTION_MARKERS = (
    "insufficient", "credit", "quota", "balance", "billing",
    "payment required", "exceeded your", "out of funds",
)


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    pass


def _looks_exhausted(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _EXHAUSTION_MARKERS)


class FeatherlessClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.endpoint = f"{settings.featherless_base}/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {settings.featherless_key}",
            "Content-Type": "application/json",
        }
        self.calls_made = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @property
    def tokens_used(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def usage(self) -> Dict:
        return {
            "calls": self.calls_made,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.tokens_used,
        }

    def complete(self, messages: List[Dict[str, str]], max_tokens: int = 700,
                 temperature: float = 0.25, retries: int = 2) -> str:
        body = {
            "model": self.settings.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        delay = 2.0
        last_error = None

        for _ in range(retries):
            try:
                response = requests.post(self.endpoint, headers=self.headers,
                                         json=body, timeout=45)

                if response.status_code in (401, 402, 403):
                    raise LLMUnavailable(
                        f"featherless returned {response.status_code}: "
                        f"{response.text[:200]}")

                if response.status_code == 429 and _looks_exhausted(response.text):
                    raise LLMUnavailable(
                        f"featherless credits appear exhausted: {response.text[:200]}")

                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = f"http {response.status_code}: {response.text[:200]}"
                    time.sleep(delay)
                    delay *= 2
                    continue

                response.raise_for_status()
                payload = response.json()

                if _looks_exhausted(json.dumps(payload.get("error", {}))):
                    raise LLMUnavailable(f"featherless error: {str(payload)[:200]}")

                choices = payload.get("choices")
                if not choices:
                    last_error = f"no choices in response: {str(payload)[:200]}"
                    time.sleep(delay)
                    delay *= 2
                    continue

                usage = payload.get("usage", {})
                self.calls_made += 1
                self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
                self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)

                return choices[0]["message"]["content"]

            except LLMUnavailable:
                raise
            except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(delay)
                delay *= 2

        raise LLMError(f"featherless request failed: {last_error}")

    def complete_json(self, messages: List[Dict[str, str]], max_tokens: int = 700) -> Optional[Dict]:
        text = self.complete(messages, max_tokens=max_tokens)
        match = _JSON_RE.search(text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    def healthcheck(self) -> bool:
        try:
            self.complete([{"role": "user", "content": "Reply with: ok"}],
                          max_tokens=5, retries=1)
            return True
        except LLMUnavailable:
            raise
        except LLMError:
            return False
