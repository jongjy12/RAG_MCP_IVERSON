"""Local demo settings. Load keys without exposing their values."""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env', override=False)
load_dotenv(ROOT.parent / '.env', override=False)

DATA_ROOT = Path(os.getenv('DEMO_DATA_ROOT', str(ROOT / 'runtime'))).resolve()
STAGING = DATA_ROOT / 'temp_downloads'
UPLOADS = DATA_ROOT / 'uploads'
MAX_BYTES = int(os.getenv('DEMO_MAX_FILE_MB', '10')) * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ROWS = 50_000
MAX_CELLS = 500_000
MAX_CHUNKS = int(os.getenv('DEMO_MAX_CHUNKS', '10000'))
INGEST_TIMEOUT_SECONDS = int(os.getenv('DEMO_INGEST_TIMEOUT_SECONDS', '300'))
MAX_DATASETS = int(os.getenv('DEMO_MAX_DATASETS', '3'))
EMBEDDING_MODEL = os.getenv('EMBEDDING_MODEL', 'sentence-transformers/all-MiniLM-L6-v2')
AGGREGATOR_PORT = int(os.getenv('DEMO_AGGREGATOR_PORT', '8000'))
AGGREGATOR_URL = f'http://127.0.0.1:{AGGREGATOR_PORT}/mcp'
FLASK_PORT = int(os.getenv('DEMO_FLASK_PORT', '5000'))
TAVILY_URL = os.getenv('TAVILY_API_MCP_URL', os.getenv('TAVILY_MCP_URL', 'https://mcp.tavily.com/mcp/'))
TAVILY_KEY = os.getenv('TAVILY_API_KEY', '')
LLM_KEY = os.getenv('OPENROUTER_API_KEY', '')
LLM_URL = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')
LLM_MODEL = os.getenv('MODEL', os.getenv('OPENROUTER_MODEL', 'openrouter/free'))

