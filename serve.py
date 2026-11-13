"""Entrypoint used by the aggregator for its process-lifetime STDIO servers."""
import sys

if __name__ == '__main__':
    if sys.argv[1] == 'rag':
        from rag_mcp.rag_server import server, ingestion
        ingestion.invalidate_old_indexes()
    elif sys.argv[1] == 'pandas':
        from pandas_mcp.pandas_server import server
    else:
        raise SystemExit('Choose pandas or rag.')
    server.run(transport='stdio',show_banner=False)

