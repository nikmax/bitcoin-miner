# Miner Worker

## Start

```bash
cd worker
cp config.example.json config.json

anpassen: nano config.json
  - Adresse deines Pools inkl. Port -> master_url
  - Deine Bitcoinadresse (wichtig!) -> worker_name

python3 worker.py
```

```
Lokales Dashboard:

```text
http://127.0.0.1:8090
```

Standard-Worker-Dashboard-Login aus Beispielconfig:

```text
worker / change-me
```

Hash erzeugen:

```bash
python3 ../tools/hash_password.py
```
