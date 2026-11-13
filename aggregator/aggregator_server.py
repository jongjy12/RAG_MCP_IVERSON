"""One HTTP endpoint backed by two process-lifetime STDIO connections."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastmcp import Client, FastMCP
from fastmcp.client.transports import PythonStdioTransport, StreamableHttpTransport

from config import settings as cfg
from shared import storage

clients = {}


def unpack(result):
    if getattr(result, 'is_error', False):
        raise ValueError(' '.join(getattr(c, 'text', '') for c in result.content))
    data = result.data
    if data is not None:
        return data.model_dump(mode='json') if hasattr(data, 'model_dump') else data
    if result.structured_content is not None:
        structured = result.structured_content
        return structured.get('result', structured)
    for item in result.content:
        if getattr(item, 'text', None):
            try: return json.loads(item.text)
            except ValueError: return {'text': item.text}
    return {}


@asynccontextmanager
async def lifespan(server):
    async with AsyncExitStack() as stack:
        for label in ['pandas', 'rag']:
            child_env = {key: value for key, value in os.environ.items()
                         if key.startswith(('HF_', 'HUGGINGFACE_', 'TRANSFORMERS_', 'TOKENIZERS_'))}
            child_env.update(DEMO_DATA_ROOT=str(cfg.DATA_ROOT), EMBEDDING_MODEL=cfg.EMBEDDING_MODEL,
                             DEMO_MAX_FILE_MB=str(cfg.MAX_BYTES//1024//1024),
                             DEMO_MAX_CHUNKS=str(cfg.MAX_CHUNKS), DEMO_MAX_DATASETS=str(cfg.MAX_DATASETS),
                             DEMO_OFFLINE_MODE=os.getenv('DEMO_OFFLINE_MODE','0'))
            transport = PythonStdioTransport(cfg.ROOT/'serve.py', args=[label], cwd=str(cfg.ROOT),
                                             python_cmd=sys.executable, keep_alive=True, env=child_env)
            clients[label] = await stack.enter_async_context(Client(transport, timeout=300, init_timeout=60))
        try:
            yield {}
        finally:
            clients.clear()


server = FastMCP('Data Analytics Aggregator', lifespan=lifespan, mask_error_details=False)


async def call(label, name, arguments):
    try:
        # Larger imports get their own budget; retrieval and analytics retain
        # the client's existing 300-second deadline.
        options = {'timeout': cfg.INGEST_TIMEOUT_SECONDS} if name == 'rag_load_dataset' else {}
        return unpack(await clients[label].call_tool(name, arguments, **options))
    except Exception:
        # Only invalidate on a dead connection, never on a validation/tool error.
        try: await clients[label].list_tools()
        except Exception:
            if label == 'rag':
                for info in storage.catalog():
                    if info.get('status')=='ready':
                        info['status']='needs_reingestion'
                        storage.atomic_json(storage.dataset_dir(info['dataset_id'])/'dataset.json', info)
        raise


@server.tool
async def get_dataset_info(dataset_id: str) -> dict:
    """Inspect table IDs, schema, dimensions, roles and source before analysis."""
    return await call('pandas','get_dataset_info',locals())


@server.tool
async def get_column_summary(dataset_id: str, column: str, table_id: str | None = None) -> dict:
    """Exact complete-column statistics including mean, median, modes and missing values."""
    return await call('pandas','get_column_summary',locals())


@server.tool
async def aggregate_data(dataset_id: str, operation: str, column: str | None = None,
                         filters: list[dict] | None = None, table_id: str | None = None) -> dict:
    """Exact aggregation: row_count (no column), count, distinct_count, sum, mean, median, mode, min, max, survival_rate. AND filters use {column,operator,value}."""
    return await call('pandas','aggregate_data',locals())


@server.tool
async def groupby_data(dataset_id: str, group_by: list[str], aggregations: list[dict],
                       filters: list[dict] | None = None, table_id: str | None = None,
                       sort_by: str | None = None, descending: bool = True, limit: int = 100) -> dict:
    """Group all matching rows. aggregations=[{column,operation,alias}]; row_count needs no column. survival_rate gives percent/numerator/denominator. Missing groups retained; sort before limit."""
    return await call('pandas','groupby_data',locals())


@server.tool
async def filter_dataset(dataset_id: str, conditions: list[dict] | None = None,
                         columns: list[str] | None = None, limit: int = 100, table_id: str | None = None) -> dict:
    """AND filters {column,operator,value}: eq/ne/gt/gte/lt/lte/is_missing/not_missing/contains/in. Return exact total and limited records."""
    return await call('pandas','filter_dataset',locals())


@server.tool
async def compare_dates(dataset_id: str, planned_date_column: str, actual_date_column: str,
                        filters: list[dict] | None = None, limit: int = 100, table_id: str | None = None) -> dict:
    """Compare actual minus planned dates; positive days are late. Return late/on_time/early/missing/invalid counts."""
    return await call('pandas','compare_dates',locals())


@server.tool
async def rag_load_dataset(source_ref: str, dataset_name: str | None = None) -> dict:
    """Application-only managed file ingestion into normalized tables and FAISS."""
    return await call('rag','rag_load_dataset',locals())


@server.tool
async def rag_get_dataset_info(dataset_id: str) -> dict:
    """Get live FAISS readiness and metadata. Missing indexes require re-ingestion."""
    return await call('rag','rag_get_dataset_info',locals())


@server.tool
async def rag_search_dataset(dataset_id: str, query: str, top_k: int = 5,
                             table_id: str | None = None, filters: list[dict] | None = None) -> dict:
    """Retrieve descriptive passages with source provenance. Resolve table/project scope first, using {column,operator,value} AND filters before top-k. Not a statistics tool."""
    return await call('rag','rag_search_dataset',locals())


@server.tool
async def tavily_search(query: str, max_results: int = 5) -> dict:
    """Web search and dataset discovery through hosted Tavily MCP. Never indexes files."""
    if not cfg.TAVILY_KEY:
        raise ValueError('Dataset search needs TAVILY_API_KEY. Local upload and analysis remain available.')
    if not query.strip() or len(query)>500 or not 1<=max_results<=5:
        raise ValueError('Use a query of 1-500 characters and 1-5 results.')
    from web_search_mcp import search_helpers as helper
    try:
        transport = StreamableHttpTransport(cfg.TAVILY_URL, headers={'Authorization':f'Bearer {cfg.TAVILY_KEY}'})
        async with Client(transport,timeout=35,init_timeout=15) as remote:
            tools = await remote.list_tools()
            tool = next((t for t in tools if t.name.lower().replace('_','-')=='tavily-search'),None)
            if tool is None: raise ValueError('Tavily MCP does not expose Search.')
            args = helper._bounded_mcp_search_arguments({'query':query,'max_results':max_results,'search_depth':'basic','topic':'general'},tool)
            result = await remote.call_tool(tool.name,args)
            # Retain all evidence for the prototype's structured/formatted parser.
            return {'structuredContent':result.structured_content,
                    'content':[c.model_dump(mode='json') for c in result.content]}
    except Exception as exc:
        raise ValueError('Tavily dataset search is unavailable. Check the configured endpoint/key or try local upload.') from exc


@server.tool
async def demo_status() -> dict:
    """Application health check for the two long-lived local MCP subprocesses."""
    status = {}
    for label, client in clients.items():
        try:
            await client.list_tools()
            status[label] = True
        except Exception: status[label] = False
    return {'local_servers':status,'tavily_configured':bool(cfg.TAVILY_KEY)}


if __name__ == '__main__':
    server.run(transport='http',host='127.0.0.1',port=cfg.AGGREGATOR_PORT,path='/mcp',show_banner=False)

