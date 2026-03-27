"""Temporal worker for the voice analytics agent."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from .activities import execute_sql, load_schema, model_call, transcribe
from .workflows import VoiceAnalyticsWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TASK_QUEUE = "voice-analytics"


async def main():
    client = await Client.connect(
        "localhost:7233",
        data_converter=pydantic_data_converter,
    )
    logger.info("Connected to Temporal server")

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[VoiceAnalyticsWorkflow],
        activities=[load_schema, transcribe, model_call, execute_sql],
    )

    logger.info("Starting worker on task queue %r", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
