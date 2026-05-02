"""Speech-to-text via OpenAI Whisper API."""

import openai


async def transcribe(audio_bytes: bytes) -> str:
    """Transcribe WAV audio bytes to text using Whisper."""
    client = openai.AsyncOpenAI()
    transcript = await client.audio.transcriptions.create(
        model="whisper-1",
        file=("audio.wav", audio_bytes, "audio/wav"),
    )
    return transcript.text
