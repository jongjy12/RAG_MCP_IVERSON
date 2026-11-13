"""Independent FastMCP STDIO analytics server."""
from fastmcp import FastMCP
from pandas_mcp import analytics

server = FastMCP('Pandas Analytics')
DESCRIPTIONS = {
    'get_dataset_info': 'Inspect dataset and table IDs, row counts, column names/types, source and warnings before choosing a table.',
    'get_column_summary': 'Exact summary of a complete selected column: mean, median, modes, missing counts. Never use RAG for these values.',
    'aggregate_data': 'Exact calculation over all filtered rows. Operations row_count (column may be null), count, distinct_count, sum, mean, median, mode, min, max, survival_rate. Filters are AND {column, operator, value}.',
    'groupby_data': 'Group complete filtered data. aggregations is [{column,operation,alias}]; row_count needs no column. survival_rate returns percentage plus numerator/denominator. Missing groups retained. Sort before limit.',
    'filter_dataset': 'Apply AND conditions ({column,operator,value}) with eq/ne/gt/gte/lt/lte/is_missing/not_missing/contains/in. Return total count and limited records; never execute expressions.',
    'compare_dates': 'Compare actual minus planned dates over matching rows. Positive=late. Return late/on_time/early/missing/invalid counts. Ambiguous dates are invalid.',
}
for name, description in DESCRIPTIONS.items():
    server.tool(name=name, description=description)(getattr(analytics, name))

if __name__ == '__main__':
    server.run(transport='stdio', show_banner=False)

