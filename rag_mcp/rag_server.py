from fastmcp import FastMCP
from rag_mcp import ingestion

server = FastMCP('Dataset RAG')
server.tool(description='Application-only ingestion. Resolve a managed source_ref, extract normalized tables once, save original and index in FAISS. Return ready dataset metadata.')(ingestion.rag_load_dataset)
server.tool(description='Inspect readiness, column names, table IDs, source and parser warnings for an indexed dataset.')(ingestion.rag_get_dataset_info)
server.tool(description='Retrieve descriptive passages in an explicit table and validated AND row/project scope before top-k. Never calculate statistics from passages. Resolve ambiguous tables with dataset info.')(ingestion.rag_search_dataset)

if __name__ == '__main__':
    ingestion.invalidate_old_indexes()
    server.run(transport='stdio', show_banner=False)

