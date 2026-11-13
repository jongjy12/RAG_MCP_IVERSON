# Local demo interface

Flask is loopback-only. Requests from the UI use the browser session cookie.
Mutations reject cross-origin browser requests. Responses are JSON except
the HTML page and CSV/XLSX attachments. Errors have `{ "error": "message" }` with
400 for validation, 409 for busy operations, 413 for request size, 503 for an
unavailable local/provider service, and 504 for unknown completion. A 504
includes `unknown_completion: true` and must not be automatically retried.

- `GET /`: UI.
- `POST /preparation/start`: JSON `{kind: upload|inspect|use|download}`; returns
  a server-issued `operation_id`, owned by the browser session. Pass it as an
  optional field to the corresponding existing preparation POST (multipart for
  upload). Each ID is single-use. Existing callers can omit it.
- `GET /preparation/<operation_id>`: owned session only; returns `state`
  (`running`, `complete`, `failed`, `unknown`), actual `stage`, and Unix
  `started`/`updated` timestamps. Read-only polling never invokes RAG or retries
  ingestion. Records expire after two hours. Download preparation does not index.
- `GET /datasets/preview?dataset_id=...&table_id=...`: requires the currently
  selected dataset and a valid explicit table ID. Calls Pandas `filter_dataset`
  with limit 10; returns `records`, `total_matches`, `source_rows`, and table/
  dataset IDs. Uses the same normalized data as analysis.
- `GET /health`: MCP local server connectivity and Tavily configuration presence.
- `GET /datasets`: `{datasets: [...], selected_dataset_id: string|null}`.
  Dataset metadata includes `dataset_id`, `name`, `filename`, `format`, `rows`,
  `columns`, `tables`, `warnings`, `source`, `chunks`, and `status`.
- `POST /datasets/select`: JSON `{dataset_id}`; returns `{dataset}` only if its
  RAG index is live. Failure preserves the previous selection.
- `POST /upload`: multipart field `file` (CSV/XLSX/XLS); returns `{dataset}`
  after the shared RAG ingestion tool completes and selects that dataset.
- `POST /dataset-search`: JSON `{topic, limit: 1..10}`; returns
  `{search_id, candidates, total_search_hits, warnings}`. Candidates have
  server-issued IDs and source/download metadata. Selection lasts one hour.
  `availability` is `page_unverified` or `direct_unverified`; a direct-looking
  link does not guarantee access. Descriptions are cleaned supplied text, with
  a neutral fallback where no useful description is available.
- `POST /dataset-search/inspect`: JSON `{search_id, candidate_id, download_id?}`;
  resolves a dataset page or inspects a direct ZIP/XLSX/DATA file. It returns
  either server-issued download choices or `{inspection_id, source_size_bytes,
  source_format, files}`. File choices include IDs, names, formats, byte sizes,
  previews and suggested header handling. The UI inspects the selected candidate
  before performing import/download so it can show actual file sizes.
  Text files also include `header_detection` (`likely_present`, `likely_absent`,
  `uncertain`), `header_message`, and `header` (`present`, `absent`, or null).
  `row_count` counts raw source rows. Subtract one only for `present`.
  An uncertain header has no default; the UI requires an explicit choice and
  expands the preview. Both choices remain available as overrides. Inspected
  text export/import always requires `header: present|absent` server-side.
  Kaggle dataset pages use the official Kaggle client with server-side
  `KAGGLE_API_TOKEN`, downloading a bounded ZIP before inspection. The token is
  never accepted from or returned to the browser. Missing/rejected tokens,
  denied access or consent requirements, timeouts, rate limits, and oversized
  downloads return descriptive errors. Dataset provenance records the canonical
  Kaggle page and dataset reference, never temporary signed URLs or credentials.
- `POST /dataset-search/cancel-inspection`: JSON `{inspection_id}`; removes
  the session's temporary inspection files. Inspections expire after 30 minutes.
- `POST /dataset-search/use`: JSON `{search_id, candidate_id, inspection_id?,
  file_id?, header?}`; stages the chosen validated CSV or XLSX and calls RAG
  MCP. Header is `present` or `absent` for inspected text files. Existing direct
  CSV requests remain supported. Only server-issued selections are accepted;
  callers cannot provide a replacement URL or filesystem path.
- `POST /dataset-search/download`: same selection payload; returns the chosen
  CSV or XLSX attachment. It does not change the active dataset or invoke RAG.
- `POST /ask`: JSON `{question, dataset_id?}`; rejects an ID different from
  the session's selected dataset. Returns `{answer, dataset_id, dataset_name,
  tool_calls}`. Each trace has `tool`, `arguments`, and `result`.
- `GET /chat/history`: `{dataset_id, messages}` for the selected dataset.

MCP is exposed through one Streamable HTTP `/mcp` endpoint. The aggregator
forwards the five Pandas tools, `compare_dates`, and the three RAG tools to
long-lived STDIO subprocesses. It exposes Tavily Search for application-led
discovery and `demo_status` for health. Only the eight read-only Pandas/RAG
tools are provided to the chat model. Actual schemas are discoverable with
MCP `tools/list`; tools reject unsupported columns, operators and limits.

Ingestion uses `DEMO_MAX_CHUNKS` (code fallback 10,000; this workspace sets
15,000) and `DEMO_INGEST_TIMEOUT_SECONDS` (code fallback 300; workspace 900).
Only `rag_load_dataset` receives the longer MCP deadline. Outer Flask/MCP
waits include preparation/forwarding grace periods. Timeouts retain the
unknown-completion response and must not cause automatic ingestion retries.

