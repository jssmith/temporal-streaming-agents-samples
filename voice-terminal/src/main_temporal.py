"""Temporal voice analytics agent — terminal client.

Usage:
    # Terminal 1: Start worker
    uv run python -m src.worker

    # Terminal 2: Start client
    uv run python -m src.main_temporal
"""

import asyncio
import base64
import json
import logging
import sys
import uuid

from temporalio.client import Client
from temporalio.contrib.pubsub import PubSubClient
from temporalio.contrib.pydantic import pydantic_data_converter

from .audio import AudioPlayer, print_audio_devices, record_until_silence
from .display import (
    print_banner,
    print_interrupted,
    print_listening,
    print_response,
    print_status,
    print_tool_call,
    print_transcript,
)
from .types import (
    AUDIO_TOPIC,
    EVENTS_TOPIC,
    StartTurnInput,
    VoiceWorkflowState,
)
from .workflows import VoiceAnalyticsWorkflow

logger = logging.getLogger(__name__)

TASK_QUEUE = "voice-analytics"


async def _consume_turn(
    pubsub: PubSubClient,
    player: AudioPlayer,
    from_offset: int,
) -> None:
    """Subscribe to pub/sub and process audio + events for one turn.

    Processes items until TURN_COMPLETE or cancellation.
    """
    async for item in pubsub.subscribe(
        topics=[AUDIO_TOPIC, EVENTS_TOPIC],
        from_offset=from_offset,
    ):
        if item.topic == AUDIO_TOPIC:
            payload = json.loads(item.data)
            pcm = base64.b64decode(payload["audio_base64"])
            player.enqueue(pcm)

        elif item.topic == EVENTS_TOPIC:
            event = json.loads(item.data)
            event_type = event.get("type")
            data = event.get("data", {})

            if event_type == "TRANSCRIPT":
                print_transcript(data.get("text", ""))

            elif event_type == "TOOL_CALL":
                print_tool_call(
                    data.get("name", ""),
                    data.get("arguments", {}),
                    data.get("result", {}),
                )

            elif event_type == "STATUS":
                print_status(data.get("text", ""))

            elif event_type == "RESPONSE_TEXT":
                print_response(data.get("text", ""))

            elif event_type == "TURN_COMPLETE":
                return


async def _drain_to_turn_complete(
    pubsub: PubSubClient,
    from_offset: int,
) -> None:
    """Drain pub/sub events until TURN_COMPLETE, discarding everything.

    After interruption, the old turn's activity may still be publishing
    audio chunks. We must advance past the old TURN_COMPLETE before
    subscribing for the next turn, otherwise the stale TURN_COMPLETE
    would cause the next subscription to exit immediately.
    """
    async for item in pubsub.subscribe(
        topics=[EVENTS_TOPIC],
        from_offset=from_offset,
    ):
        event = json.loads(item.data)
        if event.get("type") == "TURN_COMPLETE":
            return


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

    pubsub = PubSubClient.create(client, workflow_id=session_id)
    player = AudioPlayer()
    last_offset = 0
    drain_task: asyncio.Task[None] | None = None

    try:
        while True:
            print_listening()

            try:
                audio_bytes = await record_until_silence()
            except KeyboardInterrupt:
                break

            if not audio_bytes:
                logger.info("No speech detected, listening again")
                continue

            # If a drain from a previous interruption is still running,
            # wait for it to finish before starting the next turn.
            if drain_task is not None:
                print_status("Waiting for previous turn to finish...")
                await drain_task
                last_offset = await pubsub.get_offset()
                await handle.signal(
                    VoiceAnalyticsWorkflow.truncate,
                    last_offset,
                )
                drain_task = None

            audio_b64 = base64.b64encode(audio_bytes).decode()

            # Send audio to workflow
            await handle.signal(
                VoiceAnalyticsWorkflow.start_turn,
                StartTurnInput(audio_base64=audio_b64),
            )

            # Subscribe to pub/sub for this turn's audio + events
            player.start()
            player.start_speech_detection()
            interrupted = False

            consume_task = asyncio.create_task(
                _consume_turn(pubsub, player, last_offset)
            )

            # Watch for speech interruption while consuming
            while not consume_task.done():
                await asyncio.sleep(0.05)
                if player.speech_detected and not interrupted:
                    interrupted = True
                    player.interrupt()
                    consume_task.cancel()
                    await handle.signal(VoiceAnalyticsWorkflow.interrupt)
                    break

            try:
                await consume_task
            except asyncio.CancelledError:
                pass

            if interrupted:
                # The old turn's activity is still running. Drain past
                # its TURN_COMPLETE in the background while the user
                # records their next question.
                drain_task = asyncio.create_task(
                    _drain_to_turn_complete(pubsub, last_offset)
                )
            else:
                # Normal completion — advance offset and truncate now.
                last_offset = await pubsub.get_offset()
                await handle.signal(
                    VoiceAnalyticsWorkflow.truncate,
                    last_offset,
                )

                await player.wait_until_done()
                if player.speech_detected:
                    print_interrupted()

            player.stop()

    finally:
        if drain_task is not None:
            drain_task.cancel()
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
