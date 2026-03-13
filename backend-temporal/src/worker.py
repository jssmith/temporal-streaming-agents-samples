"""Temporal worker for the analytics agent."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from .activities import execute_tool, load_schema, model_call
from .workflows import AnalyticsWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    client = await Client.connect(
        "localhost:7233",
        data_converter=pydantic_data_converter,
    )
    logger.info("Connected to Temporal server")

    worker = Worker(
        client,
        task_queue="analytics-agent",
        workflows=[AnalyticsWorkflow],
        activities=[load_schema, model_call, execute_tool],
    )

    logger.info("Starting worker on task queue 'analytics-agent'")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
