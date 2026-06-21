"""Test SDK Gemini direct DEPUIS le container prod.

Confirme/infirme si Gemini est accessible depuis Hetzner — bypass complet
du wrapper NEXYA (orchestrator/retry/streaming).

Si ce script reçoit des chunks → wrapper est le coupable.
Si ce script hang → connectivité réseau Hetzner ↔ Gemini, problème infra.
"""

from __future__ import annotations

import asyncio
import os
import time

from google import genai


async def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: GEMINI_API_KEY vide dans le container")
        return

    print(f"GEMINI_API_KEY: {api_key[:10]}...{api_key[-4:]} (len {len(api_key)})", flush=True)
    print(f"GEMINI_USE_VERTEX: {os.environ.get('GEMINI_USE_VERTEX', 'NOT_SET')}", flush=True)
    print("", flush=True)

    client = genai.Client(api_key=api_key)
    print("Client created. Starting stream...", flush=True)

    t0 = time.time()
    chunks_received = 0
    full_text = ""

    try:
        stream = await client.aio.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents="Say hi in 5 words.",
        )
        async for chunk in stream:
            elapsed = time.time() - t0
            chunks_received += 1
            text = chunk.text or ""
            full_text += text
            print(
                f"[T+{elapsed:.2f}s] CHUNK #{chunks_received}: {text!r}",
                flush=True,
            )
            if chunks_received >= 5:
                break
    except Exception as exc:  # noqa: BLE001
        print(f"EXCEPTION: {type(exc).__name__}: {exc}", flush=True)

    elapsed = time.time() - t0
    print("", flush=True)
    print(f"DONE in {elapsed:.2f}s, chunks={chunks_received}, full_text={full_text!r}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
