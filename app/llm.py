from typing import Dict, List

import httpx


class LLMError(Exception):
    pass


class LLMClient:
    """Async client for Ollama Cloud using the native /api/chat endpoint.

    Native, not OpenAI-compat (/v1). Note on reasoning: with think:true the
    models' hidden thinking counts against num_predict and often consumes the
    entire budget (empty answers, long waits). We run with think:false and a
    strict no-reasoning prompt instead - fast (~2s) and reliable.
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0, think: bool = False):
        self.model = model
        self.think = think
        host = base_url.rstrip("/")
        if host.endswith("/v1"):
            host = host[: -len("/v1")]
        self._client = httpx.AsyncClient(
            base_url=host,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=15.0),
        )

    async def chat(self, messages: List[Dict[str, str]], temperature: float = 0.8, max_tokens: int = None) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "think": self.think,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens or 2048,
            },
        }
        try:
            resp = await self._client.post("/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError(f"{self.model} timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Network error calling {self.model}: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(f"{self.model} API error {resp.status_code}: {resp.text[:300]}")

        try:
            message = resp.json()["message"]
        except (KeyError, ValueError) as exc:
            raise LLMError(f"Unexpected response from {self.model}: {resp.text[:300]}") from exc

        content = (message.get("content") or "").strip()
        if not content:
            raise LLMError(f"{self.model} returned no answer (only reasoning)")
        return content

    async def aclose(self) -> None:
        await self._client.aclose()