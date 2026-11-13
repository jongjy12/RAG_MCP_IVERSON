"""Search-result normalization and safe CSV downloads adapted from prototype/main.py."""
from __future__ import annotations
import html
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote,urljoin,urlsplit,urlunsplit
import httpx
MAX_TAVILY_RESULTS = 5
MAX_FIXED_SEARCH_QUERIES = 2
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_REDIRECTS = 5
class DatasetSearchError(ValueError): pass
class DatasetDownloadError(ValueError): pass


def _mcp_tool_properties(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(mode="json", by_alias=True)
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties", {})
    return properties if isinstance(properties, dict) else {}

def _dataset_search_plan(topic: str) -> list[dict[str, Any]]:
    """Build three fast broad and source-focused searches without LLM fan-out."""
    topic = " ".join(topic.split())
    plans = [
        {"query": topic, "search_depth": "basic"},
        {"query": f"{topic} dataset CSV or XLSX download", "search_depth": "basic"},
        {
            "query": f"{topic} CSV or XLSX dataset GitHub Kaggle OpenML Hugging Face UCI",
            "search_depth": "basic",
            "include_domains": [
                "github.com",
                "raw.githubusercontent.com",
                "kaggle.com",
                "openml.org",
                "huggingface.co",
                "archive.ics.uci.edu",
                "data.gov",
                "zenodo.org",
                "figshare.com",
            ],
        },
    ]

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for plan in plans[:MAX_FIXED_SEARCH_QUERIES]:
        query = " ".join(str(plan["query"]).split())[:390]
        key = re.sub(r"[^a-z0-9]+", " ", query.lower()).strip()
        if not query or key in seen:
            continue
        seen.add(key)
        unique.append({**plan, "query": query})
    return unique

def _bounded_mcp_search_arguments(
    arguments: dict[str, Any],
    tool: Any,
) -> dict[str, Any]:
    """Build a supported MCP call with a broad per-query result count."""
    properties = _mcp_tool_properties(tool)
    bounded = {
        key: value
        for key, value in arguments.items()
        if key == "query" or key in properties
    }

    if "max_results" in properties:
        field_schema = properties.get("max_results", {})
        try:
            schema_maximum = int(field_schema.get("maximum", MAX_TAVILY_RESULTS))
            schema_minimum = int(field_schema.get("minimum", 1))
        except (TypeError, ValueError):
            schema_maximum, schema_minimum = MAX_TAVILY_RESULTS, 1
        maximum = max(1, min(MAX_TAVILY_RESULTS, schema_maximum))
        minimum = max(1, min(schema_minimum, maximum))
        bounded["max_results"] = max(minimum, maximum)

    if "search_depth" in properties:
        requested_depth = str(arguments.get("search_depth", "basic"))
        field_schema = properties.get("search_depth", {})
        allowed_depths = field_schema.get("enum") if isinstance(field_schema, dict) else None
        if not isinstance(allowed_depths, list) or requested_depth in allowed_depths:
            bounded["search_depth"] = requested_depth

    if "include_domains" in properties:
        domains = arguments.get("include_domains")
        if isinstance(domains, list) and domains:
            bounded["include_domains"] = domains

    if "exact_match" in properties:
        # Exact phrase filtering can lose alternative names and file variants.
        bounded["exact_match"] = False
    if "topic" in properties:
        topic_schema = properties.get("topic", {})
        allowed_topics = topic_schema.get("enum") if isinstance(topic_schema, dict) else None
        if not isinstance(allowed_topics, list) or "general" in allowed_topics:
            bounded["topic"] = "general"
    return bounded

def _parse_formatted_tavily_results(text: str) -> list[dict[str, Any]]:
    """Parse Tavily MCP's human-readable result blocks (Title/ID/URL/Content)."""
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    active_field: str | None = None
    labels = {
        "Title": "title",
        "ID": "id",
        "URL": "url",
        "Content": "content",
        "Raw Content": "raw_content",
        "Score": "score",
        "Favicon": "favicon",
    }

    def flush() -> None:
        nonlocal current, active_field
        if isinstance(current.get("url"), str) and current["url"].startswith(("http://", "https://")):
            records.append(current)
        current = {}
        active_field = None

    for line in text.splitlines():
        matched_label = None
        value = ""
        for label, field_name in labels.items():
            prefix = f"{label}:"
            if line.startswith(prefix):
                matched_label = field_name
                value = line[len(prefix):].strip()
                break

        if matched_label == "title":
            if current.get("url"):
                flush()
            current["title"] = value
            active_field = None
        elif matched_label == "url":
            if current.get("url"):
                flush()
            current["url"] = value
            active_field = None
        elif matched_label in {"id", "content", "raw_content", "score", "favicon"}:
            current[matched_label] = value
            active_field = matched_label if matched_label in {"content", "raw_content"} else None
        elif active_field:
            current[active_field] = f"{current.get(active_field, '')}\n{line}".strip()

    flush()
    return records

def _records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json", by_alias=True)
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict) and isinstance(item.get("url"), str)]
        for key in ("data", "result", "structuredContent"):
            nested = payload.get(key)
            if nested is not None:
                records = _records_from_payload(nested)
                if records:
                    return records
    elif isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict) and isinstance(item.get("url"), str)]
    return []

def _parse_tavily_mcp_result(result: Any) -> list[dict[str, Any]]:
    if getattr(result, "isError", False):
        raise DatasetSearchError("Tavily MCP reported a search tool error.")

    structured = getattr(result, "structuredContent", None)
    records = _records_from_payload(structured) if structured is not None else []
    if records:
        return records

    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            if hasattr(block, "model_dump"):
                text = json.dumps(block.model_dump(mode="json", by_alias=True), ensure_ascii=False)
            else:
                continue
        if text.startswith("Tavily API error:"):
            raise DatasetSearchError("Tavily MCP reported a search tool error.")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        records = _records_from_payload(payload) if payload is not None else []
        if not records:
            records = _parse_formatted_tavily_results(text)
        if records:
            return records
    return []

def _canonical_url(raw_url: Any) -> str:
    if not isinstance(raw_url, str):
        return ""
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
    )

def _merge_search_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for order, record in enumerate(records):
        url = record.get("url")
        key = _canonical_url(url)
        if not key:
            continue
        normalized = {
            "title": str(record.get("title") or "").strip(),
            "url": str(url).strip(),
            "content": str(record.get("content") or record.get("raw_content") or "").strip(),
            "_query_order": int(record.get("_query_order", order)),
        }
        score = record.get("score")
        try:
            if score is not None:
                normalized["_tavily_score"] = float(score)
        except (TypeError, ValueError):
            pass

        previous = merged.get(key)
        if previous is None:
            merged[key] = normalized
            continue
        if len(normalized["content"]) > len(previous.get("content", "")):
            previous["content"] = normalized["content"]
        if not previous.get("title") and normalized["title"]:
            previous["title"] = normalized["title"]
        if normalized["_query_order"] < previous.get("_query_order", order):
            previous["_query_order"] = normalized["_query_order"]
        if normalized.get("_tavily_score", -1) > previous.get("_tavily_score", -1):
            if "_tavily_score" in normalized:
                previous["_tavily_score"] = normalized["_tavily_score"]
    return list(merged.values())

def _extract_csv_links(*texts: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for text in texts:
        for match in re.findall(r"https?://[^\s<>\"']+", html.unescape(text or ""), re.IGNORECASE):
            candidate = match.rstrip(".,;:!?)]}")
            download_url = direct_file_url(candidate)
            if download_url and download_url not in seen:
                seen.add(download_url)
                links.append((candidate, download_url))
    return links

def _source_label(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    known_sources = {
        "github.com": "GitHub",
        "raw.githubusercontent.com": "GitHub",
        "kaggle.com": "Kaggle",
        "openml.org": "OpenML",
        "huggingface.co": "Hugging Face",
        "archive.ics.uci.edu": "UCI",
        "data.gov": "Data.gov",
        "zenodo.org": "Zenodo",
        "figshare.com": "Figshare",
    }
    for domain, label in known_sources.items():
        if host == domain or host.endswith(f".{domain}"):
            return label
    return host or "Unknown source"

def _datasets_from_search_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    for record in records:
        page_url = str(record.get("url") or "").strip()
        title = str(record.get("title") or "").strip()
        content = str(record.get("content") or "").strip()
        direct_links = _extract_csv_links(page_url, content)
        if not title:
            title = _candidate_file_name(direct_links[0][1] if direct_links else page_url)

        shared = {
            "name": title,
            "description": content[:1200] or None,
            "source": _source_label(page_url),
            "url": page_url,
            "format": file_format(direct_links[0][1]) if direct_links else None,
            "_query_order": record.get("_query_order", 0),
        }
        if "_tavily_score" in record:
            shared["_tavily_score"] = record["_tavily_score"]

        if not direct_links:
            datasets.append({**shared, "download_url": ""})
            continue

        for found_url, download_url in direct_links:
            name = title
            if len(direct_links) > 1:
                name = f"{title} — {_candidate_file_name(download_url)}"
            datasets.append(
                {
                    **shared,
                    "name": name,
                    "download_url": download_url,
                    "_source_file_url": found_url,
                }
            )
    return datasets

class _DescriptionText(HTMLParser):
    # Unknown tag-like text may be a comparison such as x<y and y>0.
    _html_tags = frozenset('a abbr article b blockquote body br code dd div dl dt em footer h1 h2 h3 h4 h5 h6 head header hr html i img li main ol p pre section small span strong sub sup table tbody td th thead tr u ul'.split())

    _void_tags = frozenset({'br', 'hr', 'img'})

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.closing_tags=set(re.findall(r'</([a-z][a-z0-9]*)\s*>',source,re.I))
        self.closing_tags={tag.lower() for tag in self.closing_tags}
        self.open_tags={}

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        # HTMLParser reads "<b and b>" as a tag with bare attributes.
        # Require explicit attribute values and a closing tag for non-void
        # markup; ambiguous source text is safer to display unchanged.
        markup=(tag in self._html_tags and all(value is not None for _,value in attrs)
                and (tag in self._void_tags or tag in self.closing_tags))
        self.parts.append(' ' if markup else self.get_starttag_text())
        if markup and tag not in self._void_tags:
            self.open_tags[tag]=self.open_tags.get(tag,0)+1

    def handle_endtag(self, tag):
        markup=self.open_tags.get(tag,0)>0
        self.parts.append(' ' if markup else f'</{tag}>')
        if markup: self.open_tags[tag]-=1

    def handle_startendtag(self, tag, attrs):
        markup=tag in self._html_tags and all(value is not None for _,value in attrs)
        self.parts.append(' ' if markup else self.get_starttag_text())


def clean_description(value: Any) -> str:
    """Extract readable supplied text without generating new dataset facts."""
    text=str(value or '')
    text=re.sub(r'\[([^\]]+)\]\([^)]+\)',r'\1',text)
    parser=_DescriptionText(text)
    parser.feed(text)
    parser.close()
    text=''.join(parser.parts)
    # Download-history Markdown tables and site widgets aren't dataset descriptions.
    text=re.split(r'\|\s*date\s*\|\s*downloads\s*\|',text,flags=re.I)[0]
    text=re.sub(r'\b(?:keyboard_arrow_down|keyboardarrowdown|vpn_key|vpnkey|grid_3x3|grid3x3|text_format|textformat|calendar_view_week|calendarviewweek|get_app|getapp|fullscreen|expand_more|more_vert)\b',' ',text,flags=re.I)
    text=re.sub(r'(?:Sorry, something went wrong\.?|Uh oh!?|There was an error while loading\.?|Please reload this page\.?|You can.t perform that action at this time\.?)',' ',text,flags=re.I)
    text=re.sub(r'\b(?:Download Course|MIT OpenCourseWare search|GIVE NOW|about ocw|help & faqs|contact us|Repository navigation|Collapse file tree|Expand file tree|Search this repository|Copy path|Raw view|Sort|Detail Compact|Not specified Tags Internet)\b',' ',text,flags=re.I)
    # Drop complete navigation/comment sections while retaining earlier source facts.
    text=re.split(r'(?:Latest commit|History History|\b\w+ commented (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|\|\s*---)',text,flags=re.I)[0]
    text=re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '',text)
    text=re.sub(r'(\*\*|__|~~)(.+?)\1',r'\2',text)
    text=' '.join(text.split()).strip(' |')
    if len(text)<25 or not re.search(r'\b(?:data(?:set)?s?|rows?|columns?|samples?|records?|features?|csv|xlsx|zip|measurements?|products?|reviews?)\b',text,re.I):
        return 'Description unavailable. Inspect available files to review this source.'
    return text[:600].rsplit(' ',1)[0]+'…' if len(text)>600 else text


def _dataset_quality(row: dict[str, Any]) -> int:
    url=str(row.get('url') or '')
    title=str(row.get('name') or '').lower()
    if re.search(r'\b(?:tutorial|how to|importing and viewing|discussion|stackoverflow)\b',title) or 'gist.github.com' in url:
        return 0
    if direct_file_url(row.get('download_url')) or direct_file_url(url): return 3
    if _source_label(url) in {'UCI','Kaggle','OpenML','Hugging Face','Data.gov','Zenodo','Figshare'}: return 2
    return 1


def _rank_dataset_rows(rows: list[dict[str, Any]], topic: str) -> list[dict[str, Any]]:
    tokens = [token for token in re.findall(r"[a-z0-9]+", topic.lower()) if len(token) > 1]

    def sort_key(row: dict[str, Any]) -> tuple[int, int, float, int]:
        searchable = " ".join(
            str(row.get(field) or "") for field in ("name", "description", "url", "download_url")
        ).lower()
        matches = sum(1 for token in tokens if token in searchable)
        try:
            tavily_score = float(row.get("_tavily_score", -1))
        except (TypeError, ValueError):
            tavily_score = -1.0
        try:
            query_order = int(row.get("_query_order", 0))
        except (TypeError, ValueError):
            query_order = 0
        return matches, _dataset_quality(row), tavily_score, -query_order

    return sorted(rows, key=sort_key, reverse=True)

def direct_csv_url(raw: Any) -> str | None:
    """Mirror the n8n Prepare Candidates directCsvUrl() function."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    path = parsed.path
    lowered_path = path.lower()
    query = parsed.query.lower()
    if host in {"gist.github.com", "gist.githubusercontent.com"}:
        return None
    if lowered_path.endswith(".ipynb"):
        return None

    looks_like_csv = (
        lowered_path.endswith(".csv")
        or "format=csv" in query
        or "output=csv" in query
        or ".csv" in query
    )
    if not looks_like_csv:
        return None

    if host in {"github.com", "www.github.com"}:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 5 or parts[2] != "blob":
            return None
        raw_path = "/".join([parts[0], parts[1], *parts[3:]])
        return f"https://raw.githubusercontent.com/{raw_path}"
    return value

def file_format(raw: Any) -> str | None:
    if not isinstance(raw, str): return None
    parsed = urlsplit(raw)
    path, query = parsed.path.lower(), parsed.query.lower()
    # Kaggle's ?select=some.csv identifies a preview on a page, not a direct file.
    if parsed.hostname in {'kaggle.com','www.kaggle.com'} and path.startswith('/datasets/'): return None
    if path.endswith('.xlsx'): return 'XLSX'
    if path.endswith('.zip'): return 'ZIP'
    if path.endswith('.data'): return 'DATA'
    if path.endswith('.csv') or 'format=csv' in query or 'output=csv' in query or '.csv' in query: return 'CSV'
    return None

def direct_file_url(raw: Any) -> str | None:
    if not isinstance(raw, str): return None
    value=raw.strip(); parsed=urlsplit(value)
    if parsed.scheme.lower() not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password: return None
    if parsed.hostname.lower() in {'gist.github.com','gist.githubusercontent.com'} or not file_format(value): return None
    if parsed.hostname.lower() in {'github.com','www.github.com'}:
        parts=[part for part in parsed.path.split('/') if part]
        if len(parts)<5 or parts[2]!='blob': return None
        return 'https://raw.githubusercontent.com/'+'/'.join([parts[0],parts[1],*parts[3:]])
    return value

def _candidate_file_name(download_url: str) -> str:
    try:
        filename = unquote(urlsplit(download_url.split("#", 1)[0]).path.rsplit("/", 1)[-1])
    except Exception:
        filename = "dataset.csv"
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(char for char in filename if char >= " " and char not in '<>:"|?*')
    return filename or "dataset.csv"

def prepare_candidates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Mirror the n8n splitOut + Prepare Candidates transformation."""
    candidates: list[dict[str, str]] = []
    rejected = 0
    seen: set[str] = set()
    usable_rows = [row for row in rows if isinstance(row, dict) and row]

    for dataset in usable_rows:
        download_url = direct_file_url(dataset.get("download_url")) or direct_file_url(
            dataset.get("url")
        )
        page_url = str(dataset.get('url') or '').strip()
        parsed = urlsplit(page_url)
        description = ' '.join(str(dataset.get(k) or '') for k in ('name','description','source')).lower()
        is_page = (parsed.scheme.lower() in {'http','https'} and bool(parsed.hostname) and
                   not parsed.username and not parsed.password and
                   ('dataset' in description or _source_label(page_url) in {'UCI','Kaggle','OpenML','Hugging Face','Data.gov','Zenodo','Figshare'}))
        if not download_url and not is_page:
            rejected += 1
            continue
        key=download_url or page_url
        parts=urlsplit(key)
        dedup_key=urlunsplit((parts.scheme.lower(),parts.netloc.lower(),parts.path.rstrip('/'),parts.query,''))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        candidate_id = f"c{len(candidates) + 1}"
        name = str(dataset.get("name") or key)
        source = str(dataset.get("source") or "Unknown source")
        candidate = {
            "id": candidate_id,
            "name": name,
            "description": clean_description(dataset.get("description")),
            "availability": 'direct_unverified' if download_url else 'page_unverified',
            "source": source,
            "format": file_format(download_url) or 'PAGE',
            "page_url": str(dataset.get("url") or ""),
            "download_url": download_url or '',
            "file_name": _candidate_file_name(download_url) if download_url else '',
            "label": f"[{candidate_id}] {name} - {source} ({file_format(download_url) or 'PAGE'})",
        }
        candidates.append(candidate)

    return {
        "candidates": candidates,
        "count": len(candidates),
        "total_results": len(usable_rows),
        "rejected_count": rejected,
        "form_fields": [
            {
                "fieldLabel": "Dataset",
                "fieldType": "radio",
                "requiredField": True,
                "fieldOptions": [{"option": item["label"]} for item in candidates],
            }
        ],
    }

def _is_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise DatasetDownloadError("Only public HTTP or HTTPS URLs can be downloaded.")
    if parsed.username or parsed.password:
        raise DatasetDownloadError("URLs containing credentials cannot be downloaded.")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        (".local", ".internal", ".lan")
    ):
        raise DatasetDownloadError("Local or private network URLs are not allowed.")

    try:
        literal_address = ipaddress.ip_address(hostname)
        if not literal_address.is_global:
            raise DatasetDownloadError("Local or private network URLs are not allowed.")
        addresses = {literal_address}
    except ValueError:
        try:
            records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise DatasetDownloadError("The source host could not be resolved.") from None
        addresses = set()
        for record in records:
            address = record[4][0].split("%", 1)[0]
            try:
                addresses.add(ipaddress.ip_address(address))
            except ValueError:
                continue

    if not addresses or any(not address.is_global for address in addresses):
        raise DatasetDownloadError("The source host must resolve to public IP addresses.")

async def download_csv(url: str, settings: Settings | None = None) -> bytes:
    """Download the selected CSV with the n8n node's 60-second timeout."""
    settings = settings or Settings.from_env()
    current_url = url
    headers = {
        "User-Agent": "TavilyDatasetSearch/1.0",
        "Accept": "text/csv, application/csv, application/octet-stream, */*",
    }

    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers=headers,
    ) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            _is_public_url(current_url)
            try:
                async with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise DatasetDownloadError(
                                "The source returned an invalid redirect."
                            )
                        if redirect_count == MAX_REDIRECTS:
                            raise DatasetDownloadError(
                                "The source exceeded the redirect limit."
                            )
                        current_url = urljoin(current_url, location)
                        continue

                    if response.status_code >= 400:
                        raise DatasetDownloadError(
                            f"The source returned HTTP {response.status_code}."
                        )
                    if "text/html" in response.headers.get("content-type", "").lower():
                        raise DatasetDownloadError("The selected link returned an HTML page.")

                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > settings.max_download_bytes:
                                raise DatasetDownloadError(
                                    "The CSV exceeds the configured download size limit."
                                )
                        except ValueError:
                            pass

                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > settings.max_download_bytes:
                            raise DatasetDownloadError(
                                "The CSV exceeds the configured download size limit."
                            )
                    return bytes(content)
            except DatasetDownloadError:
                raise
            except httpx.TimeoutException:
                raise DatasetDownloadError("The CSV download timed out.") from None
            except httpx.HTTPError:
                raise DatasetDownloadError("The CSV download request failed.") from None
    raise DatasetDownloadError("The source exceeded the redirect limit.")
