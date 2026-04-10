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
from temporalio.service import RPCError, RPCStatusCode

from .audio import AudioPlayer, print_audio_devices, record_until_silence
from .display import (
    print_banner,
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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print_banner()
    print("  (Temporal mode)")
    print_audio_devices()

    client = await Client.connect(
        "localhost:7233",
        data_converter=pydantic_data_converter,
    )

    session_id = f"voice-{uuid.uuid4().hex[:8]}"
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

    try:
        while True:
            # 1. LISTEN
            print_listening()
            try:
                audio_bytes = await record_until_silence()
            except KeyboardInterrupt:
                break
            if not audio_bytes:
                logger.info("No speech detected, listening again")
                continue

            # 2. SEND to workflow
            audio_b64 = base64.b64encode(audio_bytes).decode()
            await handle.signal(
                VoiceAnalyticsWorkflow.start_turn,
                StartTurnInput(audio_base64=audio_b64),
            )

            # 3. RECEIVE events + audio until TURN_COMPLETE
            player.start()
            async for item in pubsub.subscribe(
                topics=[AUDIO_TOPIC, EVENTS_TOPIC],
                from_offset=last_offset,
            ):
                last_offset = item.offset + 1

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
                    elif event_type == "STATUS":
                        print_status(data.get("text", ""))
                    elif event_type == "TOOL_CALL":
                        print_tool_call(
                            data.get("name", ""),
                            data.get("arguments", {}),
                            data.get("result", {}),
                        )
                    elif event_type == "RESPONSE_TEXT":
                        print_response(data.get("text", ""))
                    elif event_type == "TURN_COMPLETE":
                        break

            # 4. WAIT for playback to finish
            await player.wait_until_done()
            player.stop()

    finally:
        try:
            await handle.signal(VoiceAnalyticsWorkflow.close_session)
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                logger.info("Workflow already completed")
            else:
                raise
        print("\nGoodbye!")


def main_entry() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main_entry()
