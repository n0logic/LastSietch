#!/usr/bin/env python3
import importlib.util
import json
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location('base_restore', Path(__file__).with_name('restore.py'))
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


if __name__ == '__main__':
    try:
        if os.geteuid() != 0 or len(sys.argv) != 1 or os.environ.get('SSH_ORIGINAL_COMMAND'):
            raise ValueError('restricted restore probe')
        raw = sys.stdin.buffer.read(restore.vault.MAX_BYTES + 1)
        if len(raw) > restore.vault.MAX_BYTES:
            raise ValueError('snapshot exceeds bound')
        # Catalog-only schema check first: a different-layout archive (v1 against 1.5.3)
        # names tables the target lacks, so the full preflight would error instead of block.
        schema = restore.vault.exporter.sql_read(restore.build_schema_check(raw).decode())
        if json.loads(schema).get('typed_schema_match') is not True:
            result = schema
        else:
            result = restore.vault.exporter.sql_read(restore.build_preflight(raw).decode())
        if len(result) > 65536:
            raise ValueError('probe result exceeds bound')
        sys.stdout.buffer.write(result)
    except Exception:
        print('{"error":"restore_preflight_unavailable"}', file=sys.stderr)
        raise SystemExit(1)
