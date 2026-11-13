"""Explicit chat commands and bounded web evidence, without dataset filtering."""
import re
from types import SimpleNamespace
from urllib.parse import urlsplit


def parse_command(question):
    match = re.match(r'^/websearch(?:\s+|$)', question.strip(), re.I)
    if not match:
        return False, question
    query = question.strip()[match.end():].strip()
    if len(query) > 500:
        raise ValueError('Keep your web search question within 500 characters.')
    return True, query


def normalize_sources(payload):
    from web_search_mcp.search_helpers import _parse_tavily_mcp_result
    if not isinstance(payload, dict):
        return []
    response = SimpleNamespace(
        structuredContent=payload.get('structuredContent', payload),
        content=[SimpleNamespace(text=b.get('text', '')) for b in payload.get('content', []) if isinstance(b, dict)])
    records = _parse_tavily_mcp_result(response)
    sources, seen = [], set()
    for record in records:
        url = str(record.get('url') or '').strip()
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid or url in seen:
            continue
        seen.add(url)
        sources.append({'title':str(record.get('title') or parsed.hostname)[:250], 'url':url,
                        'content':str(record.get('content') or record.get('raw_content') or '')[:3500]})
        if len(sources) == 5:
            break
    return sources
