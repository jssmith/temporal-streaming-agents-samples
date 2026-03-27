"""Non-Temporal voice analytics agent — simple async loop.

Usage:
    cd voice-terminal
    uv run python -m src.main_simple
"""

import asyncio
import logging
import sys

from .agent import TurnResult, run_turn
from .audio import AudioPlayer, print_audio_devices, record_until_silence
from .display import (
    print_banner,
    print_error,
    print_interrupted,
    print_listening,
    print_response,
    print_tool_call,
    print_transcript,
)
from .transcribe import transcribe
from .tts import tts_stream

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print_banner()
    print_audio_devices()

    conversation: list[dict] = []
    response_id: str | None = None
    player = AudioPlayer()

    while True:
        print_listening()

        try:
            audio_bytes = await record_until_silence()
        except KeyboardInterrupt:
            print("\nGoodbye!")
            break

        if not audio_bytes:
            continue

        # Transcribe
        try:
            text = await transcribe(audio_bytes)
        except Exception as e:
            logger.exception("Transcription failed")
            print_error(f"Transcription failed: {e}")
            continue

        if not text.strip():
            continue

        print_transcript(text)

        # Start playback infrastructure
        player.start()
        player.start_speech_detection()
        interrupted = False

        # Sentence callback: stream TTS and enqueue audio
        async def on_sentence(sentence: str) -> None:
            nonlocal interrupted
            if interrupted:
                return
            try:
                async for chunk in tts_stream(sentence):
                    if player.speech_detected:
                        interrupted = True
                        player.interrupt()
                        return
                    player.enqueue(chunk)
            except Exception:
                logger.exception("TTS failed for sentence")

        # Run agent turn
        try:
            result: TurnResult = await run_turn(
                message=text,
                conversation=conversation,
                previous_response_id=response_id,
                on_sentence=on_sentence,
                on_tool_call=print_tool_call,
            )
            response_id = result.response_id
            print_response(result.response_text)
        except asyncio.CancelledError:
            print_interrupted()
            interrupted = True
        except Exception as e:
            logger.exception("Agent turn failed")
            print_error(f"Agent error: {e}")
            player.stop()
            continue

        if not interrupted:
            # Wait for playback to finish (or interruption)
            await player.wait_until_done()
            if player.speech_detected:
                print_interrupted()

        player.stop()


def main_entry() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main_entry()
