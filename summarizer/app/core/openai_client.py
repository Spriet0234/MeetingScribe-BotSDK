import json, httpx
from app.core.config import settings

class OpenAICompatClient:
    def __init__(self, model: str | None = None):
        self.base = settings.OPENAI_BASE_URL.rstrip("/")
        self.model = model or settings.MODEL
        self.key = settings.OPENAI_API_KEY

    async def chat(self, system: str, user: str, max_tokens=1200, temperature=0.2) -> str:
        url = f"{self.base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.key: headers["Authorization"] = f"Bearer {self.key}"
        payload = {
            "model": self.model,
            "messages": [{"role":"system","content":system},
                         {"role":"user","content":user}],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        async with httpx.AsyncClient(timeout=120) as cx:
            r = await cx.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        return data["choices"][0]["message"]["content"]

