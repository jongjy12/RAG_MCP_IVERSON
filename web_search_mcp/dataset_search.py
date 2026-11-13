from __future__ import annotations

import time
from types import SimpleNamespace

from config import settings as cfg
from rag_mcp.ingestion import validate_csv
from web_search_mcp import search_helpers as helper


async def search(bridge, topic: str, limit: int = 5):
    if not isinstance(topic,str) or not topic.strip() or len(topic)>300:
        raise ValueError('Enter a dataset topic of 1-300 characters.')
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=10:
        raise ValueError('Choose 1-10 results.')
    records, warnings, succeeded = [], [], 0
    for index, plan in enumerate(helper._dataset_search_plan(topic)):
        try:
            data = await bridge.call_async('tavily_search',{'query':plan['query'],'max_results':5})
            result = SimpleNamespace(structuredContent=data.get('structuredContent'),
                                     content=[SimpleNamespace(**item) for item in data.get('content',[])])
            parsed = helper._parse_tavily_mcp_result(result)
            records.extend({**item,'_query_order':index} for item in parsed)
            succeeded += 1
        except Exception:
            warnings.append(f'Search pass {index+1} failed; results from the other pass are retained.')
    if not succeeded:
        raise ValueError('Dataset search is unavailable. Check Tavily configuration or use local upload.')
    merged=helper._merge_search_records(records)
    datasets=helper._datasets_from_search_records(merged)
    ranked=helper._rank_dataset_rows(datasets,topic)
    prepared=helper.prepare_candidates(ranked)
    candidates=prepared['candidates'][:limit]
    # Never let a link parsed from snippets become a browser script URL.
    for candidate in candidates:
        candidate['url'] = candidate.get('page_url', '')
        if not str(candidate.get('url','')).startswith(('http://','https://')):
            candidate['url']=candidate['download_url']
    return {'candidates':candidates,'total_search_hits':len(merged),'warnings':warnings}


async def download(candidate):
    settings = SimpleNamespace(max_download_bytes=cfg.MAX_BYTES)
    content=await helper.download_csv(candidate['download_url'],settings)
    exported,warnings=validate_csv(content)
    return exported,warnings

