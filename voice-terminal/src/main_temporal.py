"""Temporal voice analytics agent — terminal client.

Usage:
    # Terminal 1: Start worker
    uv run python -m src.worker

    # Terminal 2: Start client
    uv run python -m src.main_temporal
"""

import asyncio
import base64
import logging
import sys
import uuid

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from .audio import AudioPlayer, print_audio_devices, record_until_silence
from .display import (
    print_banner,
    print_interrupted,
    print_listening,
    print_response,
    print_tool_call,
    print_transcript,
)
from .types import (
    AckAudioInput,
    PollAudioInput,
    PollAudioResult,
    StartTurnInput,
    VoiceWorkflowState,
)
from .workflows import VoiceAnalyticsWorkflow

logger = logging.getLogger(__name__)

TASK_QUEUE = "voice-analytics"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print_banner()
    print("  (Temporal mode)")
    print_audio_devices()

    # Connect to Temporal
    client = await Client.connect(
        "localhost:7233",
        data_converter=pydantic_data_converter,
    )

    session_id = f"voice-{uuid.uuid4().hex[:8]}"

    # Start workflow
    handle = await client.start_workflow(
        VoiceAnalyticsWorkflow.run,
        VoiceWorkflowState(),
        id=session_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started workflow %s", session_id)

    player = AudioPlayer()

    try:
        while True:
            print_listening()

            try:
                audio_bytes = await record_until_silence()
            except KeyboardInterrupt:
                break

            if not audio_bytes:
                continue

            audio_b64 = base64.b64encode(audio_bytes).decode()

            # Send audio to workflow
            await handle.signal(
                VoiceAnalyticsWorkflow.start_turn,
                StartTurnInput(audio_base64=audio_b64),
            )

            # Poll for results
            player.start()
            player.start_speech_detection()
            last_seen = 0
            interrupted = False

            while True:
                if player.speech_detected and not interrupted:
                    interrupted = True
                    player.interrupt()
                    await handle.signal(VoiceAnalyticsWorkflow.interrupt)
                    break

                result: PollAudioResult = await handle.execute_update(
                    VoiceAnalyticsWorkflow.poll_audio,
                    PollAudioInput(last_seen_index=last_seen),
                )

                if result.transcript and last_seen == 0:
                    print_transcript(result.transcript)

                for chunk_b64 in result.audio_chunks:
                    pcm = base64.b64decode(chunk_b64)
                    player.enqueue(pcm)

                last_seen = result.next_index

                # Ack consumed chunks so workflow can discard them
                if result.audio_chunks:
                    await handle.signal(
                        VoiceAnalyticsWorkflow.ack_audio,
                        AckAudioInput(through_index=last_seen),
                    )

                if result.turn_complete:
                    if result.response_text:
                        print_response(result.response_text)
                    for tc in result.tool_calls:
                        print_tool_call(
                            tc.get("name", ""),
                            tc.get("arguments", {}),
                            tc.get("result", {}),
                        )
                    break

            if not interrupted:
                await player.wait_until_done()
                if player.speech_detected:
                    print_interrupted()

            player.stop()

    finally:
        await handle.signal(VoiceAnalyticsWorkflow.close_session)
        print("\nGoodbye!")


def main_entry() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main_entry()
