"""Test isolation chirurgical du wrapper GeminiChatProvider en prod.

Bypass StreamHandler + RetryPolicy + CircuitBreaker + Orchestrator.
Appelle direct `provider.stream_chat(request)`.

Si ça yield des chunks → bug dans une couche au-dessus (orchestrator
ou retry ou streaming).
Si ça hang → bug dans le provider NEXYA lui-même.
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, "/app")

from app.ai.providers import ChatCompletionRequest, ChatMessage  # noqa: E402
from app.ai.providers.gemini import GeminiChatProvider  # noqa: E402
from app.ai.tools.base import get_tool_registry  # noqa: E402
from app.ai.tools.planner_tools import register_planner_tools  # noqa: E402


async def main() -> None:
    print("Building GeminiChatProvider...", flush=True)
    provider = GeminiChatProvider()
    print(f"Provider: name={provider.name} default_model={provider.default_model}", flush=True)

    register_planner_tools()
    tools_payload = get_tool_registry().build_openai_tools()
    print(f"Tools registered: {len(tools_payload)} tools", flush=True)
    print("", flush=True)

    # Payload MIMICKED de /chat/stream prod : avec tools et disable_thinking
    request = ChatCompletionRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(role="user", content="Bonjour"),
        ],
        system_prompt="Tu es NEXYA, un assistant IA. Réponds simplement.",
        temperature=0.7,
        max_tokens=2048,
        tools=tools_payload,
        extra={"disable_thinking": True},
    )

    print(
        f"Request: model={request.model} messages={len(request.messages)} tools={request.tools}",
        flush=True,
    )
    print("", flush=True)
    print("Starting stream_chat...", flush=True)

    t0 = time.time()
    chunks = 0
    full_text = ""
    finish_reason = None

    try:
        async for chunk in provider.stream_chat(request):
            elapsed = time.time() - t0
            chunks += 1
            text = chunk.delta or ""
            full_text += text
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            print(
                f"[T+{elapsed:.2f}s] chunk#{chunks} delta={text[:40]!r} finish={chunk.finish_reason} usage={chunk.usage}",
                flush=True,
            )
            if elapsed > 30:
                print("TIMEOUT 30s — break", flush=True)
                break
    except Exception as exc:  # noqa: BLE001
        print(f"EXCEPTION: {type(exc).__name__}: {exc}", flush=True)
        import traceback

        traceback.print_exc()

    elapsed = time.time() - t0
    print("", flush=True)
    print(
        f"DONE in {elapsed:.2f}s, chunks={chunks}, finish={finish_reason}, full_text={full_text!r}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
