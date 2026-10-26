"""FRX background load generator (Blueprint v3.0, Section 6.2). Standard library only.

Each replica:
  * API loop: GET ConfigMap frx-load-rate at api_per_s (also how it learns the current rate);
  * exec loop: starts /bin/true at exec_per_s.
It never touches target namespaces. It prints one JSON status line per second to stdout.
"""
import json
import ssl
import subprocess
import threading
import time
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
URL = "https://kubernetes.default.svc/api/v1/namespaces/frx-load/configmaps/frx-load-rate"
state = {"exec_per_s": 0.0, "api_per_s": 1.0, "level": "?", "api_ok": 0, "api_err": 0, "execs": 0}
lock = threading.Lock()


def api_loop():
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    while True:
        t0 = time.monotonic()
        try:
            token = open(f"{SA}/token").read().strip()
            req = urllib.request.Request(URL, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
                data = json.load(r).get("data", {})
            with lock:
                state["exec_per_s"] = float(data.get("exec_per_s", 0))
                state["api_per_s"] = max(1.0, float(data.get("api_per_s", 1)))
                state["level"] = data.get("level", "?")
                state["api_ok"] += 1
        except Exception:
            with lock:
                state["api_err"] += 1
        with lock:
            period = 1.0 / state["api_per_s"]
        time.sleep(max(0.0, period - (time.monotonic() - t0)))


def exec_loop():
    while True:
        with lock:
            rate = state["exec_per_s"]
        if rate <= 0:
            time.sleep(0.2)
            continue
        t0 = time.monotonic()
        subprocess.run(["/bin/true"], check=False)
        with lock:
            state["execs"] += 1
        time.sleep(max(0.0, 1.0 / rate - (time.monotonic() - t0)))


if __name__ == "__main__":
    threading.Thread(target=api_loop, daemon=True).start()
    threading.Thread(target=exec_loop, daemon=True).start()
    while True:
        time.sleep(1)
        with lock:
            print(json.dumps(dict(state, t=time.time())), flush=True)
