"""
Enhanced LLM utilities for Slack Community Agent.

Supports: Google Gemini, OpenAI, and OpenRouter (multi-provider API).
Streaming is supported for real-time display of model output (e.g. deep researcher thinking).
"""

import asyncio
import os
from typing import List, Dict, Any, Optional, AsyncIterator
from agent.config import Config


async def get_chat_completion(
    messages: List[Dict[str, str]],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None
) -> str:
    """
    Get chat completion from LLM provider.
    Uses a timeout to avoid hanging when the API is slow or unresponsive.

    Args:
        messages: List of message dicts with 'role' and 'content'
        temperature: Temperature for generation
        max_tokens: Maximum tokens in response

    Returns:
        Response text from LLM
    """
    provider = Config.LLM_PROVIDER.lower()
    timeout = Config.LLM_REQUEST_TIMEOUT_SECONDS

    async def _call():
        if provider == "gemini":
            return await _get_gemini_completion(messages, temperature, max_tokens)
        elif provider in ("openai", "openrouter"):
            return await _get_openai_completion(messages, temperature, max_tokens)
        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")

    try:
        return await asyncio.wait_for(_call(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"LLM request timed out after {timeout}s. The API may be slow or unresponsive — try again later."
        )


async def _get_gemini_completion(
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: Optional[int]
) -> str:
    """Get completion from Google Gemini using google.genai SDK."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=Config.GEMINI_API_KEY)

        # Convert role-based messages into a flat prompt string.
        # google.genai supports full Chat sessions too, but a prompt
        # string is simpler and works for all models.
        prompt = "\n\n".join(
            f"{msg['role'].upper()}: {msg['content']}"
            for msg in messages
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens or 2048,
            ),
        )

        return response.text

    except Exception as e:
        raise RuntimeError(f"Gemini API error: {str(e)}")


async def _get_openai_completion(
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: Optional[int]
) -> str:
    """Get completion from OpenAI or OpenRouter."""
    try:
        from openai import AsyncOpenAI

        # Determine API key and model based on provider
        if Config.LLM_PROVIDER.lower() == "openrouter":
            api_key = Config.OPENROUTER_API_KEY
            model = Config.OPENROUTER_MODEL
            base_url = Config.OPENROUTER_BASE_URL
        else:
            api_key = Config.OPENAI_API_KEY
            model = "gpt-4-turbo-preview"
            base_url = None

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            # max_tokens=max_tokens or 2048
        )

        # print("++++++++++Model Output++++++++++")
        # print(response.choices[0].message.content)
        return response.choices[0].message.content

    except Exception as e:
        raise RuntimeError(f"OpenAI/OpenRouter API error: {str(e)}")


async def stream_chat_completion(
    messages: List[Dict[str, str]],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> AsyncIterator[str]:
    """
    Stream chat completion from LLM provider, yielding text chunks as they arrive.

    Yields:
        str: Incremental text deltas. Concatenating all chunks gives the full response.
    """
    provider = Config.LLM_PROVIDER.lower()

    async def _stream():
        if provider == "gemini":
            async for chunk in _stream_gemini(messages, temperature, max_tokens):
                yield chunk
        elif provider in ("openai", "openrouter"):
            async for chunk in _stream_openai(messages, temperature, max_tokens):
                yield chunk
        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")

    async for chunk in _stream():
        yield chunk


async def _stream_gemini(
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: Optional[int],
) -> AsyncIterator[str]:
    """Stream completion from Google Gemini (sync SDK run in thread, chunks via queue)."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=Config.GEMINI_API_KEY)
        prompt = "\n\n".join(
            f"{msg['role'].upper()}: {msg['content']}"
            for msg in messages
        )
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens or 2048,
        )
        queue: asyncio.Queue = asyncio.Queue()
        sentinel = object()

        loop = asyncio.get_event_loop()
        stream_method = getattr(client.models, "generate_content_stream", None)

        def _run_sync_stream():
            try:
                if stream_method is not None:
                    stream = stream_method(
                        model="gemini-2.5-flash",
                        contents=prompt,
                        config=config,
                    )
                    for chunk in stream:
                        text = getattr(chunk, "text", None) or ""
                        if text:
                            loop.call_soon_thread_safe(queue.put_nowait, text)
                else:
                    # Fallback: single completion, yield as one chunk
                    resp = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt,
                        config=config,
                    )
                    if getattr(resp, "text", None):
                        loop.call_soon_thread_safe(queue.put_nowait, resp.text)
            finally:
                loop.call_soon_thread_safe(queue.put_nowait, sentinel)

        import concurrent.futures
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = loop.run_in_executor(executor, _run_sync_stream)
        while True:
            chunk = await queue.get()
            if chunk is sentinel:
                break
            yield chunk
        await fut
    except Exception as e:
        raise RuntimeError(f"Gemini streaming error: {str(e)}")


async def _stream_openai(
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: Optional[int],
) -> AsyncIterator[str]:
    """Stream completion from OpenAI or OpenRouter."""
    try:
        from openai import AsyncOpenAI

        if Config.LLM_PROVIDER.lower() == "openrouter":
            api_key = Config.OPENROUTER_API_KEY
            model = Config.OPENROUTER_MODEL
            base_url = Config.OPENROUTER_BASE_URL
        else:
            api_key = Config.OPENAI_API_KEY
            model = "gpt-4-turbo-preview"
            base_url = None

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and getattr(delta, "content", None):
                yield delta.content
    except Exception as e:
        raise RuntimeError(f"OpenAI/OpenRouter streaming error: {str(e)}")


def get_chat_completion_sync(
    messages: List[Dict[str, str]],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None
) -> str:
    """Synchronous wrapper for get_chat_completion."""
    import asyncio
    return asyncio.run(get_chat_completion(messages, temperature, max_tokens))
