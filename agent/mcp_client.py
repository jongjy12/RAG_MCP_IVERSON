"""Bridge synchronous Flask requests to one owned asynchronous MCP client loop."""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Thread

from fastmcp import Client
from config import settings as cfg
from aggregator.aggregator_server import unpack


class UnknownCompletion(RuntimeError):
    pass


class Bridge:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self.loop.run_forever,daemon=True,name='mcp-client-loop')
        self.thread.start()

    def run(self, coroutine, timeout=320):
        future = asyncio.run_coroutine_threadsafe(coroutine,self.loop)
        # Do not cancel remote ingestion when the browser/request stops waiting.
        return future.result(timeout=timeout)

    async def call_async(self, name, arguments):
        from fastmcp.exceptions import ToolError
        try:
            # Leave time for the aggregator to return the ingestion outcome.
            timeout = cfg.INGEST_TIMEOUT_SECONDS + 20 if name == 'rag_load_dataset' else 300
            async with Client(cfg.AGGREGATOR_URL,timeout=timeout,init_timeout=15) as client:
                return unpack(await client.call_tool(name,arguments))
        except (ValueError, ToolError):
            raise
        except Exception as exc:
            if name == 'rag_load_dataset':
                raise UnknownCompletion('Ingestion completion is unknown. Do not retry; check the application status or restart it.') from exc
            raise

    def call(self,name,arguments):
        return self.run(self.call_async(name,arguments))

    def tools(self):
        async def discover():
            async with Client(cfg.AGGREGATOR_URL,timeout=30) as client:
                return await client.list_tools()
        return self.run(discover(),timeout=45)

