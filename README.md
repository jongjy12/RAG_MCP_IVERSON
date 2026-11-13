# Dataset Analyst demo

From this folder, run:

```powershell
python run.py
```

The launcher reuses the existing `PythonProject/.venv` when plain Python lacks
the dependencies. Alternatively, use an activated environment with
`pip install -r requirements.txt`. Open http://127.0.0.1:5000. The launcher starts
Flask and the MCP aggregator; the aggregator owns the Pandas and RAG STDIO
subprocesses. Stop with Ctrl+C. Do not use Flask's automatic reloader.

Settings are read from `demo/.env`, then the existing `FinalProject/.env` without
overwriting environment variables. Keep keys there, never in source files.
Chat uses `OPENROUTER_API_KEY` and `MODEL` (default `openrouter/free`);
dataset discovery uses `TAVILY_API_KEY` and `TAVILY_API_MCP_URL`.
`OPENROUTER_BASE_URL`, `EMBEDDING_MODEL`, `DEMO_FLASK_PORT`,
`DEMO_AGGREGATOR_PORT`, and `DEMO_DATA_ROOT` can override local defaults.
Local uploads do not require either provider key. The embedding model is
loaded locally on the first ingestion; initial use needs a cached model or
network access. Set `DEMO_OFFLINE_MODE=1` to require the local model cache.

Upload CSV/XLSX/XLS with the file picker or drag and drop. Or open **Find
dataset online**, search, and select a result. The result count can be set from
1 to 10 (default 5). Inspect the selected result to see the downloaded source
size and each supported file's size before importing or saving it. ZIP results
show both the archive size and each contained data file. Choose a file if needed
and confirm whether text files contain a header. Both flows stage a managed
file and call `rag_load_dataset` through the aggregator.
**Download file only** exports without indexing. The selected dataset displays
its name, row count, table column names, status, and parser warnings.
Preparation displays actual stages and elapsed time, with separate failure and
unknown-completion states. Status polling reads small shared snapshots without
calling the busy RAG tool. Expand **Preview selected dataset** to view the first
10 normalized rows and choose a table/sheet. Three schema-based question buttons
fill the chat input for editing, without an extra LLM call. File sizes use decimal
B/KB/MB/GB; upload limits continue to use MiB.
After a successful export, the search dialog shows a download receipt with the
file type, downloaded size, and CSV row/column counts when available; it clearly
states that the file was not added to the active dataset or indexed.

The RAG server extracts tables once and saves normalized records alongside
the original file under `runtime/uploads/<dataset_id>/`. Pandas calculates
against those same normalized records. FAISS indexes stay in the RAG process;
re-upload after restarting. Sources and exact tool evidence are available
under each chat answer. Chat histories are separate for each selected dataset.

Demo defaults: 10 MiB per source, 50,000 source rows, 500,000 cells, 100 MiB
expanded XLSX content, 10,000 searchable chunks, three resident datasets,
up to ten RAG passages, 100 displayed records/groups, and eight tool calls
per question. Large files are rejected, never silently sampled. Ambiguous
tables/projects or percentage scales require clarification. XLS conversion
preserves values, dates, percentage formats and merged regions; formulas
use stored values. Styled empty workbook tails do not count as data.

This workspace sets `DEMO_MAX_CHUNKS=15000` in `FinalProject/.env` to support
the complete Amazon Sales CSV (1,465 rows, 16 columns, 13,017 text chunks).
The code fallback remains 10,000 when no override is present. Restart `run.py`
after changing the setting so Flask, the aggregator, and RAG load the same
allowance. All rows/columns and the existing chunking model are retained;
larger indexes take more embedding time and memory. To roll back, set
`DEMO_MAX_CHUNKS=10000` and restart. Existing indexes are kept only in memory,
so restarting requires re-importing your demo datasets.

The local workspace also sets `DEMO_INGEST_TIMEOUT_SECONDS=900`. A large
import has up to 15 minutes in the RAG MCP call; the outer MCP/Flask waits
include a grace period for forwarding the result (and an initial public CSV
download). Other MCP operations keep their existing deadlines. This setting
does not make embedding faster or trigger automatic retries. Its code fallback
is 300 seconds; restart the full launcher when changing it.

Measured on this machine on 2026-11-07: the complete Amazon CSV imported
through Flask -> HTTP aggregator -> STDIO RAG -> FAISS in 452.51 seconds
(7 minutes 33 seconds), with the model already loaded by a small baseline
upload. All 13,017 chunks, 1,465 rows and 16 columns were retained. The sampled
peak working-set sum across the test client, aggregator and all RAG/Pandas
descendants was 1,654.9 MiB (about 1.62 GiB); this includes the model and test
infrastructure, not just the additional dataset. Health checks remained
successful and completed within 0.709 seconds. Download-only left the old
selection intact; retrieval, exact Pandas row counts, failed-upload selection
preservation and temporary cleanup passed. Cold model loading, downloads,
other machines and background load can add time; prepare this dataset ahead
of a live demo.

Kaggle dataset pages use authenticated API downloads rather than HTML link
discovery. Set `KAGGLE_API_TOKEN` in `FinalProject/.env` (or `demo/.env`) and
restart `run.py`. Install the updated requirements in the app's Python
environment; the downloader uses the official `kagglesdk` client. Select a Kaggle
result and click **Find files** to download its ZIP and choose a supported file.
An isolated worker enforces a 180-second timeout and the same download size
limit as public files. ZIP extraction uses the shared limits and validation.
Missing or rejected tokens and denied permissions have specific messages;
dataset-specific consent must be completed on Kaggle. Tokens stay server-side.
Temporary downloads are removed after failure, expiry, cancellation, or after
the selected file has been copied for ingestion/download-only export.

Local UI contract (see `api-contract.md`): `/upload`, `/datasets`,
`/datasets/select`, `/dataset-search`, `/dataset-search/inspect`,
`/dataset-search/cancel-inspection`, `/dataset-search/use`,
`/dataset-search/download`, `/ask`, `/chat/history`, and `/health`.
MCP endpoint: http://127.0.0.1:8000/mcp.

Checks from this folder:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q
```

Tests use isolated temporary storage. Local calculations and validation are
checked independently of providers; integration checks explicitly identify
mocked versus live dependencies. No private workbooks are copied into fixtures.

Dataset discovery ranks topic matches first, then favors direct data files and
dataset repositories over tutorials. Source URLs remain available for inspection;
unverified links are labeled accordingly. Descriptions are cleaned excerpts of
supplied search content, with a neutral fallback rather than generated facts.

Inspected CSV/DATA files compare their first row with a bounded sample of later
rows. Likely headerless files keep the first record and get generated names.
Ambiguous files (including text-only tables, missing first-row values, and
whole-number column labels that could be data) require an explicit header choice;
the preview opens and download/import stay disabled until a choice is made.
Either detected choice can be overridden. XLSX and manual upload retain their
existing parsing rules. Header detection is a heuristic: review the preview.

The analyst prompt requires evidence for numerical/descriptive findings and
distinguishes association from causation. It must acknowledge when the data
cannot explain a difference. This reduces unsupported explanations; it is not
a guarantee that model output is always correct. Repeatable checks:

```powershell
node tests/test_header_ui.cjs
node tests/test_markdown.cjs
# Optional live model evaluation on fixed public aggregates; uses provider credits:
..\..\.venv\Scripts\python.exe tests/evaluate_grounding.py --live
```

`tests/ui_quality_fixture.py` serves a disposable browser fixture on port 15001
with fixed search/files, fake embeddings, and separate cookies/storage. It tests
UI behavior without touching the running demo; it does not verify live providers.

Optional integration check with local provider fixtures (no live credits):

```powershell
postman simulation run postman/simulations/providers.sim.yaml --port 4010 --no-history
# In another terminal, from this same folder:
$env:DEMO_INTEGRATION='1'
..\..\.venv\Scripts\python.exe -m pytest -q tests --basetemp runtime/test-integration
```

The source CSV download is redirected to the fixture server by the integration
test only. Public-source URL validation stays enabled in the application.

Optional live Amazon ingestion benchmark (uses the configured Kaggle account):

```powershell
..\..\.venv\Scripts\python.exe -u tests/benchmark_amazon_ingestion.py
```

This downloads the real dataset, tests export-only and full RAG ingestion,
then checks exact Pandas row counts, retrieval, health responsiveness, and
selection preservation on a failed upload. It runs a separate aggregator on a
free port with disposable storage. Discovery uses a fixed candidate to avoid
Tavily/LLM calls; those provider interactions are covered by the simulation
suite. The Windows benchmark records import time and the peak working-set sum
of its owned processes in `runtime/chunk_extension_benchmark.json` and removes
its temporary datasets when finished.


### Chat web search

Type `/websearch <question>` in chat to search through the existing Tavily MCP connection and receive an answer with source links. It works without a selected dataset. `/websearch` alone displays usage help. Queries are limited to 500 characters and one basic search (up to five results) per turn. Requires the existing Tavily and OpenRouter configuration.

Ordinary chat questions continue to use dataset tools only. With a dataset selected, explicitly ask to compare it with web context if both are needed; calculations and web context are distinguished in the answer. Web search does not download or import datasets. Use Find dataset online for ingestion. Sources are bounded search excerpts, not guaranteed full-page content. Chat without a dataset has a separate session history.
