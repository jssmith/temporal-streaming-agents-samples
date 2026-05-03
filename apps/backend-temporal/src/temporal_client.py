"""Shared Temporal client connection helper."""

import os

from dotenv import load_dotenv
from temporalio.client import Client, TLSConfig
from temporalio.contrib.pydantic import pydantic_data_converter

load_dotenv()


async def connect() -> Client:
    """Connect to Temporal, using Cloud config from env vars if present."""
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    api_key = os.environ.get("TEMPORAL_API_KEY")

    tls: TLSConfig | bool = False
    if api_key:
        tls = TLSConfig()

    return await Client.connect(
        address,
        namespace=namespace,
        tls=tls,
        api_key=api_key,
        data_converter=pydantic_data_converter,
    )
