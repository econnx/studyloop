"""Background jobs: long generation runs in a separate thread while the page stays usable.

The module is imported once per server run, so jobs survive page reruns,
clicks and tab switches.
"""
import threading
import time

_jobs: list[dict] = []
_lock = threading.Lock()


def start(title: str, fn) -> dict:
    """Run fn(progress) in the background. progress(fraction 0..1 or None, message)."""
    job = {"title": title, "status": "running", "progress": 0.0, "message": "",
           "started": time.time(), "finished": None, "error": None, "seen": False}

    def progress(fraction=None, message=None):
        with _lock:
            if fraction is not None:
                job["progress"] = max(0.0, min(1.0, fraction))
            if message:
                job["message"] = message

    def run():
        try:
            fn(progress)
            job["status"] = "done"
        except Exception as e:  # the error is shown in the interface
            job["status"] = "error"
            job["error"] = str(e)
        job["finished"] = time.time()

    with _lock:
        _jobs.append(job)
    threading.Thread(target=run, daemon=True).start()
    return job


def running() -> dict | None:
    return next((j for j in _jobs if j["status"] == "running"), None)


def latest() -> dict | None:
    return _jobs[-1] if _jobs else None
