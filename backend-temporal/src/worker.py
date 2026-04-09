"""Temporal worker for the analytics agent."""

import asyncio
import logging

from temporalio.worker import Worker

from . import temporal_client
from .activities import execute_tool, load_schema, model_call
from .workflows import AnalyticsWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    client = await temporal_client.connect()
    logger.info("Connected to Temporal at %s", client.service_client.config.target_host)

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
