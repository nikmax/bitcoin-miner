# Miner Worker Final 1.5.4

Maintenance-Release.

## Neu

- Worker crasht nicht mehr bei Master API 401/403 oder temporär unerreichbarem Master.
- Fehler wird im Worker-Dashboard unter `last_error` angezeigt.
- Registrierung wird automatisch wiederholt.
- Config im Worker-Dashboard editierbar: `http://WORKER:8090/config`.
- CPU-Fallback und Nonce-Range-Debugging bleiben enthalten.

## Start

```bash
cd worker
cp config.example.json config.json
python3 worker.py
```

Standard Worker-Dashboard-Login: `worker / change-me`.
