# pyproject.toml (or requirements.txt)
# pydantic-ai>=0.0.28  (or latest)
# openai>=1.40
# httpx>=0.27

import asyncio
import base64
import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel

VLLM_BASE_URL = "https://your-vllm-host.example.com/v1"  # vLLM OpenAI-compatible server (/v1) 
MODEL_ID = "your-model-name"  # e.g., "meta-llama/Llama-3.1-8B-Instruct"

# If your vLLM gateway requires Basic Auth:
BASIC_USER = "username"
BASIC_PASS = "password"
_basic_token = base64.b64encode(f"{BASIC_USER}:{BASIC_PASS}".encode()).decode()

# Create an httpx AsyncClient that injects the auth header on every request.
http_client = httpx.AsyncClient(
    headers={
        "Authorization": f"Basic {_basic_token}",
        # You can add gateway-required headers here, e.g. X-Org, etc.
    },
    timeout=60.0,
)

# Build an OpenAI-compatible async client targeting vLLM.
# Note: some OpenAI clients still expect an api_key, even for local/proxied endpoints.
openai_client = AsyncOpenAI(
    base_url=VLLM_BASE_URL,
    api_key="not-used-or-dummy",   # required by the SDK, ignored by many proxies
    http_client=http_client,       # <- ensures our Basic Auth header is sent
)

# Plug the OpenAI-style client into Pydantic-AI's OpenAIModel:
model = OpenAIModel(
    model_name=MODEL_ID,
    client=openai_client,
)

# Define a tiny agent. You can grow this with tools, structured outputs, etc.
agent = Agent(
    model=model,
    system_prompt=(
        "You are a concise assistant. Keep answers brief unless asked to go deeper."
    ),
)

async def main():
    # Basic chat
    result = await agent.run("Give me three bullet points explaining transformers.")
    print(result.data)        # the final string/text
    
    # If you want the raw traces or usage:
    # print(result.usage)     # token accounting if the model returns it
    # print(result.messages)  # conversation state

asyncio.run(main())

# def ask(prompt: str) -> str:
#     return asyncio.run(agent.run(prompt)).data

# if __name__ == "__main__":
#     print(ask("Summarize attention in two sentences."))