"""Small, shared preparation snapshots; never required for ingestion to succeed."""
import time
from config import settings as cfg
from shared import storage


def path(operation_id):
    return cfg.DATA_ROOT / 'progress' / (storage.safe_id(operation_id) + '.json')


def start(operation_id, kind):
    now = time.time()
    storage.atomic_json(path(operation_id), dict(operation_id=operation_id, kind=kind,
                        state='running', stage='Waiting for file', started=now, updated=now))


def read(operation_id):
    return storage.read_json(path(operation_id))


def update(operation_id, stage, state='running'):
    if not operation_id:
        return
    try:
        value = read(operation_id)
        if value['state'] in {'complete', 'failed', 'unknown'} and state == 'running':
            return
        value.update(stage=stage, state=state, updated=time.time())
        storage.atomic_json(path(operation_id), value)
    except (OSError, ValueError):
        pass


def cleanup():
    folder = cfg.DATA_ROOT / 'progress'
    if folder.exists():
        for item in folder.glob('*.json'):
            try:
                if time.time() - item.stat().st_mtime > 7200:
                    item.unlink(missing_ok=True)
            except OSError:
                pass
