# Miner Worker Final 1.4

Kompatibilitäts-Update für Master 3.0.

## Änderungen

- Worker sendet `worker_name` jetzt nicht nur beim Register, sondern auch bei:
  - Job-Anfrage
  - Heartbeat
  - Blockfund
- Dadurch bleibt der Anzeigename stabil, auch wenn der Master neu gestartet wird, während Worker weiterlaufen.
- Permanente `worker_id` bleibt technische Identität.
- Der Master lehnt weiterhin zweite aktive Instanzen mit gleichem `worker_name` ab.

## Start

```bash
cd miner_worker_final_1_4/worker
cp config.example.json config.json
python3 worker.py
```

Lokales Dashboard:

```text
http://WORKER-IP:8090
```
