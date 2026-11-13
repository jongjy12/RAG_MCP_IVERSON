"""Validated calculations over complete normalized tables; no eval or SQL."""
from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd

from shared import storage

OPS = {'row_count', 'count', 'distinct_count', 'sum', 'mean', 'median', 'mode', 'min', 'max', 'survival_rate'}
FILTER_OPS = {'eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'is_missing', 'not_missing', 'contains', 'in'}


def missing(series):
    return series.isna() | series.map(lambda v: isinstance(v, str) and not v.strip())


def require_column(frame, column):
    if column not in frame.columns:
        raise ValueError(f'Unknown column {column!r}. Available columns: {list(frame.columns)}')


def filtered(frame, conditions=None):
    if conditions is not None and not isinstance(conditions, list):
        raise ValueError('Filters must be a list of AND conditions.')
    if len(conditions or []) > 20:
        raise ValueError('Use at most 20 filter conditions.')
    mask = pd.Series(True, index=frame.index)
    for condition in conditions or []:
        if not isinstance(condition, dict):
            raise ValueError('Each filter needs column, operator, and value.')
        column, operator, value = condition.get('column'), condition.get('operator'), condition.get('value')
        require_column(frame, column)
        if operator not in FILTER_OPS:
            raise ValueError(f'Unsupported filter operator. Use {sorted(FILTER_OPS)}')
        series = frame[column]
        blanks = missing(series)
        if operator == 'is_missing':
            part = blanks
        elif operator == 'not_missing':
            part = ~blanks
        elif operator == 'contains':
            if not isinstance(value, str):
                raise ValueError('contains requires a string.')
            part = series.astype('string').str.contains(value, case=False, regex=False, na=False) & ~blanks
        elif operator == 'in':
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError('in requires a list of at most 100 values.')
            part = series.isin(value) & ~blanks
        else:
            if value is None:
                raise ValueError('Use is_missing/not_missing for missing values.')
            if operator in {'gt', 'gte', 'lt', 'lte'}:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError('Numeric comparisons require a numeric value.')
                series = pd.to_numeric(series, errors='coerce')
            elif pd.api.types.is_bool_dtype(series):
                if not isinstance(value, bool):
                    raise ValueError('Boolean columns require true or false.')
            elif pd.api.types.is_numeric_dtype(series):
                if not isinstance(value, (int, float)):
                    raise ValueError('Numeric columns require numeric filter values.')
            if operator == 'eq': part = series.eq(value)
            elif operator == 'ne': part = series.ne(value)
            elif operator == 'gt': part = series.gt(value)
            elif operator == 'gte': part = series.ge(value)
            elif operator == 'lt': part = series.lt(value)
            else: part = series.le(value)
            part = part & ~blanks
        mask &= part.fillna(False)
    return frame.loc[mask]


def calculate(frame, column, operation):
    if operation not in OPS:
        raise ValueError(f'Unsupported operation. Use {sorted(OPS)}')
    if operation == 'row_count':
        return {'value': len(frame), 'included': len(frame), 'missing': 0, 'invalid': 0}
    require_column(frame, column)
    series = frame[column]
    blanks = missing(series)
    valid = series[~blanks]
    meta = {'included': len(valid), 'missing': int(blanks.sum()), 'invalid': 0}
    if operation == 'count': return {'value': len(valid), **meta}
    if operation == 'distinct_count': return {'value': int(valid.nunique()), **meta}
    if operation == 'mode': return {'value': storage.json_safe(valid.mode().tolist()), **meta}
    numeric = pd.to_numeric(valid, errors='coerce').replace([float('inf'), float('-inf')], float('nan'))
    if operation == 'survival_rate':
        numeric = numeric.where(numeric.isin([0, 1]))
    meta['invalid'] = int(numeric.isna().sum())
    numeric = numeric.dropna()
    meta['included'] = len(numeric)
    if operation == 'survival_rate':
        numerator = int(numeric.sum())
        return {'value': numerator / len(numeric) * 100 if len(numeric) else None,
                'numerator': numerator, 'denominator': len(numeric), 'unit': 'percent', **meta}
    value = getattr(numeric, operation)() if len(numeric) else None
    return storage.json_safe({'value': value, **meta})


def get_dataset_info(dataset_id: str):
    info = storage.manifest(dataset_id)
    return {k: v for k, v in info.items() if k not in {'original_path'}}


def get_column_summary(dataset_id: str, column: str, table_id: str | None = None):
    frame, table = storage.load_table(dataset_id, table_id)
    require_column(frame, column)
    series = frame[column]
    valid = series[~missing(series)]
    result = {'dataset_id': dataset_id, 'table_id': table['id'], 'column': column,
              'rows': len(frame), 'count': len(valid), 'missing': len(frame)-len(valid),
              'dtype': str(series.dtype), 'distinct': int(valid.nunique()), 'mode': storage.json_safe(valid.mode().tolist())}
    numeric = pd.to_numeric(valid, errors='coerce')
    if pd.api.types.is_numeric_dtype(series) or (len(valid) and numeric.notna().all()):
        for operation in ['mean', 'median', 'min', 'max', 'sum']:
            result[operation] = calculate(frame, column, operation)['value']
    result['warnings'] = percentage_warning(table, column)
    return storage.json_safe(result)


def percentage_warning(table, column):
    if column and ('%' in column or 'completion' in column.casefold()) and column not in table.get('percentage_columns', []):
        return ['Percentage scale is not established by formatting or explicit percent strings. Confirm whether numeric values use 0-1 or 0-100 before interpreting them as percentages.']
    return []


def aggregate_data(dataset_id: str, operation: str, column: str | None = None,
                   filters: list[dict] | None = None, table_id: str | None = None):
    frame, table = storage.load_table(dataset_id, table_id)
    selected = filtered(frame, filters)
    return {'dataset_id': dataset_id, 'table_id': table['id'], 'operation': operation,
            'column': column, 'filters': filters or [], 'matched_rows': len(selected),
            **calculate(selected, column, operation), 'warnings': percentage_warning(table, column)}


def groupby_data(dataset_id: str, group_by: list[str], aggregations: list[dict],
                 filters: list[dict] | None = None, table_id: str | None = None,
                 sort_by: str | None = None, descending: bool = True, limit: int = 100):
    if not 1 <= limit <= 100:
        raise ValueError('limit must be 1 through 100.')
    frame, table = storage.load_table(dataset_id, table_id)
    if not isinstance(group_by, list) or not 1 <= len(group_by) <= 4 or len(set(group_by)) != len(group_by):
        raise ValueError('group_by must contain 1-4 distinct columns.')
    if not isinstance(aggregations, list) or not 1 <= len(aggregations) <= 10:
        raise ValueError('Supply 1-10 aggregations: {column, operation, alias}.')
    for column in group_by: require_column(frame, column)
    aliases = set(group_by)
    specs = []
    for spec in aggregations:
        operation = spec.get('operation')
        column = spec.get('column')
        alias = spec.get('alias') or (operation if operation == 'row_count' else f'{column}_{operation}')
        if not isinstance(alias, str) or alias in aliases or operation not in OPS:
            raise ValueError('Use unique aggregation aliases and supported operations.')
        if operation != 'row_count': require_column(frame, column)
        aliases.add(alias)
        specs.append((column, operation, alias))
    selected = filtered(frame, filters).copy()
    for column in group_by:
        selected[column] = selected[column].astype(object).where(~missing(selected[column]), None)
    records = []
    for keys, group in selected.groupby(group_by, dropna=False, sort=False):
        if not isinstance(keys, tuple): keys = (keys,)
        record = dict(zip(group_by, keys))
        for column, operation, alias in specs:
            result = calculate(group, column, operation)
            record[alias] = result['value']
            record[alias + '_included'] = result['included']
            record[alias + '_missing'] = result['missing']
            record[alias + '_invalid'] = result['invalid']
            if operation == 'survival_rate':
                record[alias + '_numerator'] = result['numerator']
                record[alias + '_denominator'] = result['denominator']
        records.append(storage.json_safe(record))
    if sort_by:
        if sort_by not in aliases: raise ValueError('sort_by must be a group column or aggregation alias.')
        # Keep missing sort values last in either direction.
        present = [r for r in records if r.get(sort_by) is not None]
        absent = [r for r in records if r.get(sort_by) is None]
        try: present.sort(key=lambda r: r[sort_by], reverse=descending)
        except TypeError: present.sort(key=lambda r: str(r[sort_by]), reverse=descending)
        records = present + absent
    return {'dataset_id': dataset_id, 'table_id': table['id'], 'filters': filters or [],
            'matched_rows': len(selected), 'total_groups': len(records), 'truncated': len(records)>limit,
            'records': records[:limit], 'missing_group_label': 'Unassigned/Unknown (null)',
            'warnings': list(dict.fromkeys(w for c, _, _ in specs for w in percentage_warning(table, c)))}


def filter_dataset(dataset_id: str, conditions: list[dict] | None = None,
                   columns: list[str] | None = None, limit: int = 100, table_id: str | None = None):
    if not 1 <= limit <= 100: raise ValueError('limit must be 1 through 100.')
    frame, table = storage.load_table(dataset_id, table_id)
    selected = filtered(frame, conditions)
    if columns:
        for column in columns: require_column(frame, column)
        selected = selected[columns]
    return storage.json_safe({'dataset_id': dataset_id, 'table_id': table['id'], 'total_matches': len(selected),
                              'truncated': len(selected)>limit, 'records': selected.head(limit).to_dict(orient='records'),
                              'source_rows': [table['source_rows'][i] for i in selected.head(limit).index]})


def parse_date(value):
    if value is None or pd.isna(value) or (isinstance(value, str) and not value.strip()):
        return None, 'missing'
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).normalize(), None
    text = str(value).strip()
    # ISO is explicit; day/month is accepted only when one component exceeds 12.
    formats = []
    if re.match(r'^\d{4}-\d{2}-\d{2}', text):
        try: return pd.Timestamp(datetime.fromisoformat(text)).normalize(), None
        except ValueError: return None, 'invalid'
    match = re.fullmatch(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', text)
    if match:
        first, second, _ = map(int, match.groups())
        if first <= 12 and second <= 12 and first != second: return None, 'invalid'
        sep = '/' if '/' in text else '-'
        formats = [f'%d{sep}%m{sep}%Y'] if first > 12 or first == second else [f'%m{sep}%d{sep}%Y']
    for fmt in formats:
        try: return pd.Timestamp(datetime.strptime(text, fmt)), None
        except ValueError: pass
    return None, 'invalid'


def compare_dates(dataset_id: str, planned_date_column: str, actual_date_column: str,
                  filters: list[dict] | None = None, limit: int = 100, table_id: str | None = None):
    if not 1 <= limit <= 100: raise ValueError('limit must be 1 through 100.')
    frame, table = storage.load_table(dataset_id, table_id)
    for column in [planned_date_column, actual_date_column]: require_column(frame, column)
    selected = filtered(frame, filters)
    counts = dict(late=0, on_time=0, early=0, missing=0, invalid=0)
    records = []
    for index, row in selected.iterrows():
        planned, p_error = parse_date(row[planned_date_column])
        actual, a_error = parse_date(row[actual_date_column])
        days = None
        if 'invalid' in (p_error, a_error): status = 'invalid'
        elif p_error or a_error: status = 'missing'
        else:
            days = (actual-planned).days
            status = 'late' if days>0 else 'early' if days<0 else 'on_time'
        counts[status] += 1
        if len(records)<limit:
            records.append({'source_row': table['source_rows'][index], 'planned': row[planned_date_column],
                            'actual': row[actual_date_column], 'days_difference': days, 'status': status})
    return storage.json_safe({'dataset_id': dataset_id, 'table_id': table['id'], 'matched_rows': len(selected),
                              **counts, 'records': records, 'truncated': len(selected)>limit,
                              'date_convention': 'Typed Excel dates or ISO; ambiguous slash/dash dates are invalid. Positive days means late.'})

