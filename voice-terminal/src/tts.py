"""Text-to-speech via OpenAI TTS API with streaming support."""

import re
from collections.abc import AsyncIterator

import openai


async def tts_stream(text: str) -> AsyncIterator[bytes]:
    """Stream TTS audio for the given text. Yields PCM chunks (24kHz, 16-bit mono)."""
    client = openai.AsyncOpenAI()
    async with client.audio.speech.with_streaming_response.create(
        model="tts-1",
        voice="alloy",
        input=text,
        response_format="pcm",
    ) as response:
        async for chunk in response.iter_bytes(chunk_size=4096):
            yield chunk


async def tts_full(text: str) -> bytes:
    """Generate TTS audio for the full text. Returns complete PCM bytes."""
    chunks = []
    async for chunk in tts_stream(text):
        chunks.append(chunk)
    return b"".join(chunks)


# Sentence boundary pattern: split on sentence-ending punctuation followed by space
_SENTENCE_BOUNDARY = re.compile(r'(?<=[.!?])\s+')
# Minimum characters before we flush a sentence (avoids tiny fragments)
_MIN_SENTENCE_LEN = 20


def split_sentences(text: str) -> list[str]:
    """Split text into sentence-sized chunks for incremental TTS."""
    parts = _SENTENCE_BOUNDARY.split(text)
    sentences = []
    current = ""
    for part in parts:
        current += (" " if current else "") + part
        if len(current) >= _MIN_SENTENCE_LEN:
            sentences.append(current.strip())
            current = ""
    if current.strip():
        if sentences:
            sentences[-1] += " " + current.strip()
        else:
            sentences.append(current.strip())
    return sentences
