#!/usr/bin/env python3
import json, os, socket, struct, subprocess, time, hashlib, threading, uuid
from pathlib import Path
import requests
from flask import Flask, jsonify, render_template_string, request, Response

import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))
from shared.kernels.opencl_sha256 import OPENCL_KERNEL
from shared.config import load_json_config, save_json_config
from shared.protocol import API_REGISTER, API_JOB, API_HEARTBEAT, API_FOUND, API_STATUS, STATUS_IDLE, STATUS_MINING, STATUS_REGISTERED, STATUS_ERROR, STATUS_FOUND
API_WORKER_STATUS = "/api/worker/status"
API_WORKER_HISTORY = "/api/worker/history"

CONFIG_PATH = Path(os.environ.get("MINER_WORKER_CONFIG", "config.json"))
app = Flask(__name__)
state_lock = threading.Lock()
WORKER_STOP = threading.Event()
THERMAL_PAUSE = threading.Event()
GLOBAL_HASHER = None
HEARTBEAT_SEQ = 0
BENCH_STATE = {"running": False, "last_result": None, "history": []}
LOCAL_STATE = {
    "worker_id": None, "name": None, "backend": None, "gpu_device": None, "master_url": None,
    "master_running": False, "status": "starting", "job_id": None, "height": None, "extranonce": None,
    "hashrate_hs": 0.0, "verified_hashrate_hs": 0.0, "cluster_hashrate_hs": 0.0, "share_percent": 0.0,
    "total_hashes": 0, "nonce": None, "batch_size": None, "local_size": None,
    "completed_batches": 0, "last_interval_hashes": 0, "last_interval_seconds": 0.0,
    "gpu_metrics": [], "max_gpu_temp_c": None, "thermal_stop": False,
    "last_error": None, "last_update": None, "logs": [],
    "block_history": [], "history_summary": {},
}

def local_log(msg):
    line = {"ts": time.strftime("%H:%M:%S"), "msg": str(msg)}
    with state_lock:
        LOCAL_STATE.setdefault("logs", []).append(line)
        LOCAL_STATE["logs"] = LOCAL_STATE["logs"][-200:]
    print(str(msg))

def update_local(**kwargs):
    with state_lock:
        LOCAL_STATE.update(kwargs)
        LOCAL_STATE["last_update"] = time.strftime("%H:%M:%S")


def load_config():
    return load_json_config(CONFIG_PATH)


def ensure_worker_id(c):
    """Dauerhafte technische Worker-ID erzeugen und in config.json speichern."""
    wid = str(c.get("worker_id") or "").strip()
    if wid:
        return wid
    wid = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
    try:
        c2 = dict(c)
        c2["worker_id"] = wid
        save_json_config(CONFIG_PATH, c2)
        c.update(c2)
        local_log(f"Neue permanente worker_id erzeugt und gespeichert: {wid}")
    except Exception as e:
        local_log(f"Konnte worker_id nicht speichern, nutze temporär {wid}: {e}")
    return wid


def worker_dashboard_auth_ok():
    try:
        c = load_config()
    except Exception:
        c = {}
    if not c.get("worker_dashboard_auth_enabled", False):
        return True
    auth = request.authorization
    if not auth:
        return False
    expected_user = str(c.get("worker_dashboard_user", "worker"))
    expected_hash = str(c.get("worker_dashboard_password_hash", ""))
    got_hash = hashlib.sha256((auth.password or "").encode("utf-8")).hexdigest()
    return secrets_compare(auth.username or "", expected_user) and secrets_compare(got_hash, expected_hash)

def secrets_compare(a, b):
    # kleine lokale Variante ohne zusätzlichen Import; Länge egal, keine sensiblen Timings im LAN-Dashboard.
    return str(a) == str(b)

def require_worker_dashboard_auth(fn):
    def wrapper(*args, **kwargs):
        if worker_dashboard_auth_ok():
            return fn(*args, **kwargs)
        return Response("Login erforderlich", 401, {"WWW-Authenticate": 'Basic realm="Miner Worker Dashboard"'})
    wrapper.__name__ = fn.__name__
    return wrapper

def sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()

def gpu_metrics():
    try:
        out = subprocess.check_output([
            "nvidia-smi",
            "--query-gpu=index,name,temperature.gpu,utilization.gpu,power.draw,pstate,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ], text=True, timeout=3)
        gpus = []
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 8:
                gpus.append({"index": p[0], "name": p[1], "temp_c": p[2], "util_percent": p[3], "power_w": p[4], "pstate": p[5], "mem_used_mb": p[6], "mem_total_mb": p[7]})
        return gpus
    except Exception:
        return []



def hottest_gpu_temp(metrics):
    temps = []
    for g in metrics or []:
        try:
            temps.append(float(g.get("temp_c")))
        except Exception:
            pass
    return max(temps) if temps else None

def safety_check(c, metrics=None):
    """Thermal guard with hysteresis.

    Wichtig: Ein Temperaturereignis setzt NICHT mehr WORKER_STOP.
    Der Worker pausiert nur, meldet 0 H/s an den Master und nimmt die Arbeit
    automatisch wieder auf, wenn die Karte ausreichend abgekühlt ist.
    """
    if metrics is None:
        metrics = gpu_metrics()
    max_temp = float(c.get("max_gpu_temp_c", 80))
    resume_temp = float(c.get("thermal_resume_temp_c", max_temp - 6))
    poll_seconds = float(c.get("thermal_poll_seconds", 3))
    temp = hottest_gpu_temp(metrics)

    if temp is not None and temp >= max_temp:
        if not THERMAL_PAUSE.is_set():
            local_log(f"Thermal pause: GPU {temp:.0f}°C >= Limit {max_temp:.0f}°C")
        THERMAL_PAUSE.set()

    if THERMAL_PAUSE.is_set():
        if temp is not None and temp <= resume_temp:
            THERMAL_PAUSE.clear()
            local_log(f"Thermal resume: GPU {temp:.0f}°C <= Resume {resume_temp:.0f}°C")
        else:
            msg = f"Thermal pause aktiv: GPU {temp if temp is not None else '?'}°C, Resume <= {resume_temp:.0f}°C"
            update_local(status="thermal_pause", thermal_stop=True, last_error=msg, gpu_metrics=metrics,
                         max_gpu_temp_c=max_temp, hashrate_hs=0.0, verified_hashrate_hs=0.0,
                         last_interval_hashes=0, last_interval_seconds=0.0)
            time.sleep(min(max(poll_seconds, 1.0), 10.0))
            return False

    update_local(gpu_metrics=metrics, max_gpu_temp_c=max_temp, thermal_stop=False)
    return not WORKER_STOP.is_set()

def auth_headers(c):
    return {"Authorization": "Bearer " + c["worker_token"]}

def _json_response(resp, url):
    """Robust JSON parser for Master API responses.

    Older versions crashed with JSONDecodeError when Flask returned an
    HTML error page, an empty response, or a proxy/network message. This
    turns that into a clear Worker error while keeping the process alive.
    """
    text = resp.text or ""
    content_type = resp.headers.get("content-type", "")
    if resp.status_code >= 400:
        snippet = text.strip().replace("\n", " ")[:240]
        raise RuntimeError(f"Master API HTTP {resp.status_code} bei {url}: {snippet or 'leere Antwort'}")
    try:
        return resp.json()
    except ValueError:
        snippet = text.strip().replace("\n", " ")[:240]
        raise RuntimeError(
            f"Master API lieferte kein JSON bei {url} "
            f"(content-type={content_type or '-'}): {snippet or 'leere Antwort'}"
        )

def post(c, path, payload, timeout=30):
    url = c["master_url"].rstrip("/") + path
    resp = requests.post(url, json=payload, headers=auth_headers(c), timeout=timeout)
    return _json_response(resp, url)

def get_master_status(c, timeout=5):
    try:
        # Worker dürfen die Dashboard-Login-Daten des Masters nicht kennen.
        # Deshalb holen sie Cluster-Hashrate/Running-Status über die
        # token-geschützte Worker-Status-API.
        url = c["master_url"].rstrip("/") + API_WORKER_STATUS
        resp = requests.get(url, headers=auth_headers(c), timeout=timeout)
        return _json_response(resp, url)
    except Exception as e:
        # Nur als Info speichern; nicht die lokale Hashrate/GPU-Werte zerstören.
        update_local(last_error=f"Master-Clusterstatus nicht erreichbar: {e}")
        return None

def get_master_history(c, worker_id, name=None, timeout=8):
    try:
        url = c["master_url"].rstrip("/") + API_WORKER_HISTORY
        payload = {"worker_id": worker_id, "name": name or c.get("worker_name", worker_id), "limit": int(c.get("worker_history_limit", 100))}
        resp = requests.post(url, json=payload, headers=auth_headers(c), timeout=timeout)
        return _json_response(resp, url)
    except Exception as e:
        update_local(last_error=f"Master-Historie nicht erreichbar: {e}")
        return None


def select_opencl_gpu(cl, platform_filter="", device_filter=""):
    candidates = []
    pf = platform_filter.lower()
    df = device_filter.lower()
    for pidx, platform in enumerate(cl.get_platforms()):
        try:
            devices = platform.get_devices(device_type=cl.device_type.GPU)
        except Exception:
            continue
        for didx, device in enumerate(devices):
            ptxt = f"{platform.name} {platform.vendor}".lower()
            dtxt = f"{device.name} {device.vendor}".lower()
            if pf and pf not in ptxt:
                continue
            if df and df not in dtxt:
                continue
            candidates.append((platform, device, pidx, didx))
    if not candidates:
        raise RuntimeError("Keine OpenCL-GPU gefunden")
    for item in candidates:
        if "nvidia" in f"{item[1].name} {item[1].vendor}".lower():
            return item
    return candidates[0]

class GpuHasher:
    def __init__(self, batch_size=262144, platform_filter="nvidia", device_filter="", local_size=256):
        import numpy as np
        import pyopencl as cl
        self.np = np
        self.cl = cl
        self.batch_size = int(batch_size)
        self.local_size = int(local_size or 0)
        self.platform, self.device, self.platform_index, self.device_index = select_opencl_gpu(cl, platform_filter, device_filter)
        self.ctx = cl.Context([self.device])
        self.queue = cl.CommandQueue(self.ctx)
        self.prg = cl.Program(self.ctx, OPENCL_KERNEL).build()
        mf = cl.mem_flags
        self.prefix_buf = cl.Buffer(self.ctx, mf.READ_ONLY, size=76)
        self.target_buf = cl.Buffer(self.ctx, mf.READ_ONLY, size=32)
        self.result_buf = cl.Buffer(self.ctx, mf.READ_WRITE, size=8)
        self.result = np.zeros(2, dtype=np.uint32)
        self.zero_result = np.zeros(2, dtype=np.uint32)
        self.target_words = np.zeros(8, dtype=np.uint32)

    @property
    def device_name(self):
        return f"{self.device.name.strip()} / {self.platform.name.strip()}"

    def prepare_target(self, target_hex: str):
        raw = bytes.fromhex(target_hex.rjust(64, "0"))
        vals = [int.from_bytes(raw[i*4:(i+1)*4], "big") for i in range(8)]
        self.target_words[:] = vals
        self.cl.enqueue_copy(self.queue, self.target_buf, self.target_words).wait()

    def scan_batch(self, header_prefix76: bytes, start_nonce: int, count: int):
        cl = self.cl
        prefix = self.np.frombuffer(header_prefix76, dtype=self.np.uint8)
        cl.enqueue_copy(self.queue, self.prefix_buf, prefix)
        cl.enqueue_copy(self.queue, self.result_buf, self.zero_result)
        if self.local_size and count >= self.local_size:
            global_size = ((int(count) + self.local_size - 1) // self.local_size) * self.local_size
            local = (self.local_size,)
        else:
            global_size = int(count)
            local = None
        self.prg.mine_headers(
            self.queue,
            (int(global_size),),
            local,
            self.prefix_buf,
            self.np.uint32(start_nonce),
            self.np.uint32(count),
            self.target_buf,
            self.result_buf,
        )
        cl.enqueue_copy(self.queue, self.result, self.result_buf).wait()
        if int(self.result[0]) != 0:
            return int(self.result[1])
        return None

def make_hasher(c):
    if not c.get("use_gpu", True):
        return None, "cpu", None
    try:
        h = GpuHasher(
            batch_size=int(c.get("gpu_batch_size", 262144)),
            platform_filter=str(c.get("gpu_platform_substring", "nvidia")),
            device_filter=str(c.get("gpu_device_substring", "")),
            local_size=int(c.get("gpu_local_size", 256)),
        )
        return h, "opencl", h.device_name
    except Exception as e:
        print("GPU nicht verfügbar, CPU-Fallback:", e)
        return None, "cpu", None

def register(c, worker_id, backend, gpu_device):
    payload = {"worker_id": worker_id, "name": c.get("worker_name", worker_id), "backend": backend, "gpu_device": gpu_device, "gpus": gpu_metrics(), "batch_size": c.get("gpu_batch_size"), "local_size": c.get("gpu_local_size", 256)}
    res = post(c, API_REGISTER, payload)
    if not res.get("ok"):
        raise RuntimeError(res)
    update_local(status=STATUS_REGISTERED, gpu_metrics=payload["gpus"], batch_size=payload["batch_size"], local_size=payload["local_size"])

def heartbeat(c, worker_id, backend, job_id=None, hashrate=0.0, total_hashes=0, nonce=None, status=STATUS_MINING,
              completed_batches=0, last_interval_hashes=0, last_interval_seconds=0.0):
    global HEARTBEAT_SEQ
    HEARTBEAT_SEQ += 1
    metrics = gpu_metrics()
    if not safety_check(c, metrics):
        status = "thermal_pause" if not WORKER_STOP.is_set() else "local_stop"
        hashrate = 0.0
        last_interval_hashes = 0
        last_interval_seconds = 0.0
    if status not in (STATUS_MINING, STATUS_FOUND):
        hashrate = 0.0
        verified = 0.0
    else:
        verified = (float(last_interval_hashes) / float(last_interval_seconds)) if float(last_interval_seconds or 0) > 0 else 0.0
    payload = {
        "worker_id": worker_id, "name": c.get("worker_name", worker_id), "backend": backend, "job_id": job_id, "template_id": None,
        "heartbeat_seq": HEARTBEAT_SEQ,
        "hashrate_hs": hashrate, "verified_hashrate_hs": verified, "total_hashes": total_hashes, "nonce": nonce,
        "completed_batches": completed_batches, "last_interval_hashes": int(last_interval_hashes or 0),
        "last_interval_seconds": float(last_interval_seconds or 0.0),
        "gpu_metrics": metrics, "status": status,
        "batch_size": c.get("gpu_batch_size"), "local_size": c.get("gpu_local_size", 256),
    }
    res = post(c, API_HEARTBEAT, payload, timeout=10)
    cluster = get_master_status(c)
    cluster_hr = float((cluster or {}).get("total_hashrate_hs") or 0.0)
    share = (float(verified or hashrate) / cluster_hr * 100.0) if cluster_hr > 0 else 0.0
    update_local(master_running=bool(res.get("running")), status=status, job_id=job_id,
                 hashrate_hs=float(hashrate or 0.0), verified_hashrate_hs=float(verified or 0.0), cluster_hashrate_hs=cluster_hr,
                 share_percent=share, total_hashes=int(total_hashes or 0), nonce=nonce,
                 completed_batches=int(completed_batches or 0), last_interval_hashes=int(last_interval_hashes or 0),
                 last_interval_seconds=float(last_interval_seconds or 0.0),
                 gpu_metrics=metrics, batch_size=c.get("gpu_batch_size"), local_size=c.get("gpu_local_size", 256))
    return res

def mine_job_cpu(job, c, worker_id, backend):
    prefix = bytes.fromhex(job["header_prefix_hex"])
    target = int(job["target_hex"], 16)
    nonce = 0
    batch = int(c.get("cpu_batch_size", 50000))
    total = 0
    last = time.time()
    last_total = 0
    while nonce <= 0xffffffff and not WORKER_STOP.is_set():
        if not safety_check(c):
            r = heartbeat(c, worker_id, backend, job["job_id"], 0.0, total, nonce, status="thermal_pause",
                          completed_batches=0, last_interval_hashes=0, last_interval_seconds=0.0)
            if not r.get("running") or r.get("template_id") != job.get("template_id"):
                return None, total
            last, last_total = time.time(), total
            continue
        end = min(0x100000000, nonce + batch)
        for n in range(nonce, end):
            h = sha256d(prefix + struct.pack("<I", n))
            if int.from_bytes(h[::-1], "big") < target:
                found_total = total + (n - nonce) + 1
                now = time.time()
                interval_hashes = max(1, found_total - last_total)
                interval_seconds = max(0.001, now - last)
                hr = interval_hashes / interval_seconds
                try:
                    heartbeat(c, worker_id, backend, job["job_id"], hr, found_total, n, status=STATUS_FOUND,
                              completed_batches=0, last_interval_hashes=interval_hashes, last_interval_seconds=interval_seconds)
                except Exception as e:
                    local_log(f"Heartbeat bei Fund fehlgeschlagen: {e}")
                return n, found_total
        done = end - nonce
        nonce = end
        total += done
        now = time.time()
        if now - last >= 2:
            interval_hashes = total - last_total
            interval_seconds = max(0.001, now - last)
            hr = interval_hashes / interval_seconds
            r = heartbeat(c, worker_id, backend, job["job_id"], hr, total, nonce, completed_batches=0, last_interval_hashes=interval_hashes, last_interval_seconds=interval_seconds)
            if not r.get("running") or r.get("template_id") != job.get("template_id"):
                return None, total
            last, last_total = now, total
    return None, total

def mine_job_gpu(job, c, worker_id, backend, hasher):
    prefix = bytes.fromhex(job["header_prefix_hex"])
    hasher.prepare_target(job["target_hex"])
    nonce = 0
    total = 0
    completed_batches = 0
    last = time.time()
    last_total = 0
    last_batches = 0
    while nonce <= 0xffffffff and not WORKER_STOP.is_set():
        if not safety_check(c):
            r = heartbeat(c, worker_id, backend, job["job_id"], 0.0, total, nonce, status="thermal_pause",
                          completed_batches=completed_batches, last_interval_hashes=0, last_interval_seconds=0.0)
            if not r.get("running") or r.get("template_id") != job.get("template_id"):
                return None, total
            last, last_total = time.time(), total
            continue
        count = min(hasher.batch_size, 0x100000000 - nonce)
        found = hasher.scan_batch(prefix, nonce, count)
        nonce += count
        total += count
        completed_batches += 1
        if found is not None:
            now = time.time()
            interval_hashes = max(1, total - last_total)
            interval_seconds = max(0.001, now - last)
            hr = interval_hashes / interval_seconds
            try:
                heartbeat(c, worker_id, backend, job["job_id"], hr, total, found, status=STATUS_FOUND,
                          completed_batches=completed_batches, last_interval_hashes=interval_hashes, last_interval_seconds=interval_seconds)
            except Exception as e:
                local_log(f"Heartbeat bei Fund fehlgeschlagen: {e}")
            return found, total
        now = time.time()
        if now - last >= 2:
            interval_hashes = total - last_total
            interval_seconds = max(0.001, now - last)
            hr = interval_hashes / interval_seconds
            r = heartbeat(c, worker_id, backend, job["job_id"], hr, total, nonce, completed_batches=completed_batches, last_interval_hashes=interval_hashes, last_interval_seconds=interval_seconds)
            if not r.get("running") or r.get("template_id") != job.get("template_id"):
                return None, total
            last, last_total = now, total
    return None, total


def run_local_benchmark(duration_seconds=15, batch_size=None):
    """Lokaler Verifikations-Benchmark: zählt nur vollständig abgeschlossene Nonce-Bereiche."""
    global BENCH_STATE, GLOBAL_HASHER
    c = load_config()
    duration_seconds = max(3, int(duration_seconds or c.get("benchmark_seconds", 15)))
    old_stop = WORKER_STOP.is_set()
    if old_stop:
        return {"ok": False, "error": "Worker ist lokal gestoppt; Prozess neu starten."}
    with state_lock:
        BENCH_STATE["running"] = True
        BENCH_STATE["last_result"] = None
    prefix = b"\x00" * 76
    start = time.perf_counter()
    deadline = start + duration_seconds
    total = 0
    batches = 0
    backend = LOCAL_STATE.get("backend") or "cpu"
    device = LOCAL_STATE.get("gpu_device")
    try:
        if GLOBAL_HASHER is not None:
            h = GLOBAL_HASHER
            if batch_size:
                # nicht dauerhaft ändern; nur kleinere Testmenge pro Durchlauf verwenden
                bsz = int(batch_size)
            else:
                bsz = int(getattr(h, "batch_size", c.get("gpu_batch_size", 262144)))
            h.prepare_target("00" * 32)
            nonce = 0
            while time.perf_counter() < deadline and not WORKER_STOP.is_set():
                count = min(bsz, 0x100000000 - nonce)
                h.scan_batch(prefix, nonce, count)
                nonce = (nonce + count) & 0xffffffff
                total += count
                batches += 1
        else:
            bsz = int(batch_size or c.get("cpu_batch_size", 50000))
            nonce = 0
            while time.perf_counter() < deadline and not WORKER_STOP.is_set():
                end = nonce + bsz
                for n in range(nonce, end):
                    sha256d(prefix + struct.pack("<I", n & 0xffffffff))
                total += bsz
                batches += 1
                nonce = end & 0xffffffff
        elapsed = max(0.001, time.perf_counter() - start)
        res = {
            "ok": True,
            "backend": backend,
            "gpu_device": device,
            "duration_seconds": elapsed,
            "requested_seconds": duration_seconds,
            "completed_batches": batches,
            "verified_nonces": total,
            "verified_hashrate_hs": total / elapsed,
            "batch_size": int(batch_size or (getattr(GLOBAL_HASHER, "batch_size", c.get("gpu_batch_size", c.get("cpu_batch_size", 50000))) if GLOBAL_HASHER is not None else c.get("cpu_batch_size", 50000))),
            "note": "Berechnung aus vollständig abgeschlossenen Nonce-Bereichen; unabhängig von der normalen Worker-Heartbeat-Anzeige."
        }
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    with state_lock:
        BENCH_STATE["running"] = False
        BENCH_STATE["last_result"] = res
        BENCH_STATE.setdefault("history", []).append({"ts": time.strftime("%H:%M:%S"), **res})
        BENCH_STATE["history"] = BENCH_STATE["history"][-20:]
    return res


WORKER_HTML = r"""
<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Miner Worker Final 1.2</title><style>
body{font-family:system-ui,Arial;background:#0b1020;color:#e8eefc;margin:0}header{padding:16px 22px;background:#111936;border-bottom:1px solid #26345f}.wrap{padding:18px;max-width:1100px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card{background:#111936;border:1px solid #26345f;border-radius:14px;padding:14px;margin-bottom:12px}.label{color:#9fb0d0;font-size:.85rem}.value{font-size:1.5rem;font-weight:700}.ok{color:#34d399}.bad{color:#fb7185}.warn{color:#fbbf24}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #26345f;text-align:left}.bar{height:12px;background:#26345f;border-radius:8px;overflow:hidden}.bar>span{display:block;height:12px;background:#34d399}.small{font-size:.85rem;color:#9fb0d0}.log{height:190px;overflow:auto;background:#0b1020;border-radius:10px;padding:10px}.pill{display:inline-block;border-radius:999px;padding:2px 8px;background:#26345f;font-size:.8rem}a{color:#93c5fd}button{background:#2563eb;color:white;border:0;border-radius:10px;padding:10px 14px;margin-right:8px;cursor:pointer}.danger{background:#dc2626}.warnbtn{background:#d97706}
</style></head><body><header><b>Miner Worker Final 1.2</b> <span id="state"></span></header><div class="wrap">
<div class="card"><button onclick="fetch('/start',{method:'POST'}).then(r=>r.json()).then(x=>alert(x.message||JSON.stringify(x)))">Start / Resume</button> <button onclick="fetch('/stop',{method:'POST'}).then(r=>r.json()).then(x=>alert(x.message||JSON.stringify(x)))" class="warnbtn">Stop Mining</button> <button onclick="if(confirm('Worker-Prozess wirklich beenden?')) fetch('/quit',{method:'POST'}).then(r=>r.json()).then(x=>alert(x.message||JSON.stringify(x)))" class="danger">Quit</button> <button onclick="fetch('/benchmark',{method:'POST'}).then(r=>r.json()).then(x=>alert(JSON.stringify(x,null,2)))">Lokalen Kurztest starten</button><p class="small">Stop hält nur das Rechnen lokal an. Start nimmt denselben Worker wieder auf. Quit beendet den Prozess.</p></div>
<div class="grid"><div class="card"><div class="label">Eigene Hashrate</div><div id="hr" class="value">-</div></div><div class="card"><div class="label">Cluster-Hashrate</div><div id="chr" class="value">-</div></div><div class="card"><div class="label">Mein Anteil</div><div id="share" class="value">-</div></div><div class="card"><div class="label">Status</div><div id="st" class="value">-</div></div></div>
<div class="card"><h3>Anteil am Cluster</h3><div class="bar"><span id="sharebar" style="width:0%"></span></div><p class="small">Zeigt den Anteil dieses Workers an der aktuell vom Master gemeldeten Gesamt-Hashrate.</p></div>
<div class="card"><h3>Safety / Verifikation</h3><table><tbody id="verify"></tbody></table></div>
<div class="card"><h3>Worker</h3><table><tbody id="info"></tbody></table></div>
<div class="card"><h3>GPU</h3><table><thead><tr><th>Name</th><th>Temp</th><th>Load</th><th>Power</th><th>P-State</th><th>VRAM</th></tr></thead><tbody id="gpu"></tbody></table></div>
<div class="card"><h3>Meine Block-Beteiligungen</h3><p class="small">Vom Master per worker_token geladen; das Worker-Dashboard kennt keine Master-Login-Daten. <button onclick="loadHistory()">Aktualisieren</button></p><div id="hist_summary" class="small"></div><table><thead><tr><th>Height</th><th>Ende</th><th>Worker Hashes</th><th>Cluster Hashes</th><th>Anteil</th><th>Paid</th></tr></thead><tbody id="history"></tbody></table></div>
<div class="card"><h3>Log</h3><div id="logs" class="log"></div></div>
<div class="card"><h3>Fehler</h3><pre id="err"></pre></div>
</div><script>
function esc(x){return String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fmt(n){n=Number(n||0); if(n>1e9)return (n/1e9).toFixed(2)+' GH/s'; if(n>1e6)return (n/1e6).toFixed(2)+' MH/s'; if(n>1e3)return (n/1e3).toFixed(2)+' kH/s'; return n.toFixed(0)+' H/s'}
function fmtHash(n){n=Number(n||0); if(n>1e12)return (n/1e12).toFixed(2)+' TH'; if(n>1e9)return (n/1e9).toFixed(2)+' GH'; if(n>1e6)return (n/1e6).toFixed(2)+' MH'; if(n>1e3)return (n/1e3).toFixed(2)+' kH'; return n.toFixed(0)}
async function loadHistory(){try{let h=await(await fetch('/api/history')).json();let rows=h.blocks||[];let sm=h.summary||{};document.getElementById('hist_summary').textContent=`Blöcke: ${sm.block_count||0} · Gesamt eigener Beitrag: ${fmtHash(sm.worker_verified_hashes||0)} · Ø Anteil: ${Number(sm.overall_share_percent||0).toFixed(2)}%`;document.getElementById('history').innerHTML=rows.map(b=>`<tr><td>${esc(b.height)}</td><td>${esc(b.ended_at||'')}</td><td>${fmtHash(b.worker_verified_hashes)}</td><td>${fmtHash(b.cluster_verified_hashes)}</td><td>${Number(b.worker_share_percent||0).toFixed(2)}%</td><td>${esc(b.paid||'no')}</td></tr>`).join('')||'<tr><td colspan="6">Noch keine Beteiligungen gefunden.</td></tr>'}catch(e){document.getElementById('hist_summary').textContent='Historie nicht erreichbar: '+e}}

async function tick(){let s=await (await fetch('/api/status')).json();document.getElementById('state').innerHTML=s.master_running?'<span class="ok">● MASTER RUNNING</span>':'<span class="warn">● MASTER STOPPED/UNBEKANNT</span>';document.getElementById('hr').textContent=fmt(s.hashrate_hs);document.getElementById('chr').textContent=fmt(s.cluster_hashrate_hs);document.getElementById('share').textContent=(s.share_percent||0).toFixed(1)+'%';document.getElementById('sharebar').style.width=Math.max(0,Math.min(100,s.share_percent||0))+'%';document.getElementById('st').textContent=s.status||'-';document.getElementById('verify').innerHTML=`<tr><td>Verifizierte Hashrate</td><td><b>${fmt(s.verified_hashrate_hs||0)}</b></td></tr><tr><td>Letztes Intervall</td><td>${Number(s.last_interval_hashes||0).toLocaleString()} Nonces in ${Number(s.last_interval_seconds||0).toFixed(3)}s</td></tr><tr><td>Abgeschlossene GPU-Batches</td><td>${Number(s.completed_batches||0).toLocaleString()}</td></tr><tr><td>Temperatur-Limit</td><td>${esc(s.max_gpu_temp_c)}°C ${s.thermal_stop?'<span class="bad">THERMAL STOP</span>':''}</td></tr>`;document.getElementById('info').innerHTML=`<tr><td>Name</td><td><b>${esc(s.name)}</b></td></tr><tr><td>Worker-ID</td><td>${esc(s.worker_id)}</td></tr><tr><td>Master</td><td>${esc(s.master_url)}</td></tr><tr><td>Backend</td><td>${esc(s.backend)}</td></tr><tr><td>GPU</td><td>${esc(s.gpu_device)}</td></tr><tr><td>Job</td><td>${esc(s.job_id)} · Height ${esc(s.height)} · ExtraNonce ${esc(s.extranonce)}</td></tr><tr><td>Nonce</td><td>${esc(s.nonce)}</td></tr><tr><td>Hashes</td><td>${Number(s.total_hashes||0).toLocaleString()}</td></tr><tr><td>Tuning</td><td><span class="pill">Batch ${esc(s.batch_size)}</span> <span class="pill">Local ${esc(s.local_size)}</span></td></tr><tr><td>Letztes Update</td><td>${esc(s.last_update)}</td></tr>`;document.getElementById('gpu').innerHTML=(s.gpu_metrics||[]).map(g=>`<tr><td>${esc(g.name)}</td><td>${esc(g.temp_c)}°C</td><td>${esc(g.util_percent)}%</td><td>${esc(g.power_w)}W</td><td>${esc(g.pstate)}</td><td>${esc(g.mem_used_mb)}/${esc(g.mem_total_mb)} MB</td></tr>`).join('')||'<tr><td colspan="6">Keine GPU-Metriken</td></tr>';document.getElementById('logs').innerHTML=(s.logs||[]).slice(-100).map(l=>`<div><span class="small">${esc(l.ts)}</span> ${esc(l.msg)}</div>`).join('');let lg=document.getElementById('logs');lg.scrollTop=lg.scrollHeight;document.getElementById('err').textContent=s.last_error||''}
setInterval(tick,1000);tick();loadHistory();setInterval(loadHistory,15000);</script></body></html>
"""


@app.route("/api/benchmark", methods=["GET"])
@require_worker_dashboard_auth
def local_benchmark_status():
    with state_lock:
        return jsonify(json.loads(json.dumps(BENCH_STATE)))

@app.route("/benchmark", methods=["POST", "GET"])
@require_worker_dashboard_auth
def local_benchmark_start():
    try:
        c = load_config()
        seconds = int(c.get("benchmark_seconds", 15))
        batch = c.get("benchmark_batch_size")
    except Exception:
        seconds = 15
        batch = None
    if BENCH_STATE.get("running"):
        return jsonify({"ok": False, "error": "Benchmark läuft bereits"}), 409
    def runner():
        local_log("Lokaler Verifikations-Benchmark gestartet")
        res = run_local_benchmark(seconds, batch)
        local_log(f"Benchmark fertig: {res}")
    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"ok": True, "message": "Benchmark gestartet", "seconds": seconds, "batch_size": batch})

@app.route("/")
@require_worker_dashboard_auth
def local_dashboard():
    return render_template_string(WORKER_HTML)

@app.route("/api/history")
@require_worker_dashboard_auth
def local_history():
    try:
        c = load_config()
        wid = LOCAL_STATE.get("worker_id") or c.get("worker_id")
        name = LOCAL_STATE.get("name") or c.get("worker_name", wid)
        hist = get_master_history(c, wid, name=name)
        if hist and hist.get("ok"):
            update_local(block_history=hist.get("blocks", []), history_summary=hist.get("summary", {}))
            return jsonify(hist)
        return jsonify(hist or {"ok": False, "error": "Keine Historie vom Master erhalten"}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/status")
@require_worker_dashboard_auth
def local_status():
    with state_lock:
        out = dict(LOCAL_STATE)
        out["benchmark"] = json.loads(json.dumps(BENCH_STATE))
        return jsonify(out)

@app.route("/start", methods=["POST", "GET"])
@require_worker_dashboard_auth
def local_start():
    WORKER_STOP.clear()
    update_local(status="resuming", hashrate_hs=0.0, verified_hashrate_hs=0.0, last_interval_hashes=0, last_interval_seconds=0.0, last_error=None)
    local_log("Lokaler Worker-Start/Resume aktiviert")
    return jsonify({"ok": True, "message": "Worker rechnet wieder, sobald der Master einen Job liefert."})

@app.route("/stop", methods=["POST", "GET"])
@require_worker_dashboard_auth
def local_stop():
    # Stop bedeutet nur: lokal nicht weiterrechnen. Dashboard und Master-Verbindung bleiben erhalten.
    WORKER_STOP.set()
    update_local(status="local_stop", hashrate_hs=0.0, verified_hashrate_hs=0.0, last_interval_hashes=0, last_interval_seconds=0.0)
    local_log("Lokales Mining gestoppt. Worker-Prozess bleibt aktiv.")
    return jsonify({"ok": True, "message": "Mining lokal gestoppt. Mit Start/Resume weiterrechnen oder Quit zum Beenden."})

@app.route("/quit", methods=["POST", "GET"])
@require_worker_dashboard_auth
def local_quit():
    WORKER_STOP.set()
    update_local(status="quitting", hashrate_hs=0.0, verified_hashrate_hs=0.0, last_interval_hashes=0, last_interval_seconds=0.0)
    local_log("Worker-Quit angefordert. Prozess wird beendet.")
    def delayed_exit():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=delayed_exit, daemon=True).start()
    return jsonify({"ok": True, "message": "Worker-Prozess wird beendet."})

def start_worker_dashboard(c):
    if not c.get("worker_dashboard_enabled", True):
        return
    host = c.get("worker_dashboard_host", "127.0.0.1")
    port = int(c.get("worker_dashboard_port", 8090))
    def run():
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    threading.Thread(target=run, daemon=True).start()
    local_log(f"Worker-Dashboard: http://{host}:{port}")


def main():
    WORKER_STOP.clear()
    c = load_config()
    worker_id = ensure_worker_id(c)
    global GLOBAL_HASHER
    hasher, backend, gpu_device = make_hasher(c)
    GLOBAL_HASHER = hasher
    update_local(worker_id=worker_id, name=c.get("worker_name", worker_id), backend=backend, gpu_device=gpu_device, master_url=c.get("master_url"), batch_size=c.get("gpu_batch_size"), local_size=c.get("gpu_local_size", 256))
    start_worker_dashboard(c)
    local_log(f"Worker: {worker_id} Backend: {backend} GPU: {gpu_device}")
    register(c, worker_id, backend, gpu_device)
    total_all = 0
    while True:
        try:
            res = post(c, API_JOB, {"worker_id": worker_id, "name": c.get("worker_name", worker_id)}, timeout=30)
            if not res.get("ok"):
                update_local(status=STATUS_ERROR, last_error=str(res))
                local_log(f"Job Fehler: {res}")
                time.sleep(5)
                continue
            if not res.get("running"):
                heartbeat(c, worker_id, backend, status=STATUS_IDLE)
                update_local(status=STATUS_IDLE, master_running=False, hashrate_hs=0.0, verified_hashrate_hs=0.0, last_interval_hashes=0, last_interval_seconds=0.0)
                local_log("Master gestoppt. Warte...")
                time.sleep(5)
                continue
            if WORKER_STOP.is_set():
                try:
                    heartbeat(c, worker_id, backend, status="local_stop")
                except Exception:
                    pass
                update_local(status="local_stop", master_running=bool(res.get("running")), hashrate_hs=0.0, verified_hashrate_hs=0.0, last_interval_hashes=0, last_interval_seconds=0.0)
                time.sleep(2)
                continue
            job = res["job"]
            update_local(status=STATUS_MINING, job_id=job["job_id"], height=job.get("height"), extranonce=job.get("extranonce"), master_running=True)
            local_log(f"Job {job['job_id']} Height {job['height']} ExtraNonce {job['extranonce']}")
            if hasher:
                found_nonce, tries = mine_job_gpu(job, c, worker_id, backend, hasher)
            else:
                found_nonce, tries = mine_job_cpu(job, c, worker_id, backend)
            total_all += tries
            if found_nonce is not None:
                update_local(status=STATUS_FOUND, nonce=found_nonce)
                local_log(f"GEFUNDEN {found_nonce}")
                out = post(c, API_FOUND, {"worker_id": worker_id, "name": c.get("worker_name", worker_id), "job_id": job["job_id"], "nonce": found_nonce}, timeout=120)
                local_log(f"Submit: {out}")
                if out.get("benchmark"):
                    time.sleep(0.2)
                else:
                    time.sleep(10)
        except KeyboardInterrupt:
            print("Stop.")
            return
        except Exception as e:
            update_local(status=STATUS_ERROR, last_error=str(e))
            local_log(f"Worker Fehler: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
