"""Event-driven folder watcher for automatic scans."""
import logging
import os
import threading
import time

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    from watchdog.observers.polling import PollingObserver
except Exception:
    FileSystemEventHandler = None
    Observer = None
    PollingObserver = None

from app.scanner import IMAGE_EXTENSIONS
from app.events import publish

log = logging.getLogger("pixiv_archive.watcher")


class _ImageChangeHandler(FileSystemEventHandler):
    def __init__(self, schedule_scan):
        self._schedule_scan = schedule_scan

    def on_created(self, event):
        self._maybe_schedule(event)

    def on_moved(self, event):
        self._maybe_schedule(event)

    def on_modified(self, event):
        self._maybe_schedule(event)

    def _maybe_schedule(self, event):
        if getattr(event, "is_directory", False):
            return
        path = getattr(event, "dest_path", "") or getattr(event, "src_path", "")
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            publish("watch_event", {"path": path, "event_type": event.event_type})
            self._schedule_scan(path, event.event_type)


class FolderWatcher:
    def __init__(self, scan_callback, debounce_seconds=30):
        self._scan_callback = scan_callback
        self._debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._observer = None
        self._timer = None
        self._paths = []
        self._last_event = None
        self._last_scan_at = None
        self._last_error = None
        self._pending = False

    def start(self, paths):
        if Observer is None:
            log.warning("auto watch unavailable: watchdog is not installed")
            return {"ok": False, "error": "watchdog 未安装"}

        paths = _normalize_paths(paths)
        self._debounce_seconds = _watch_debounce_seconds()
        with self._lock:
            self.stop_locked()
            if not paths:
                log.info("auto watch not started: no source directories configured")
                return {"ok": False, "error": "没有可监看的图片目录"}

            use_polling = (os.getenv("AUTO_WATCH_POLLING", "") or "").strip() == "1"
            observer_cls = PollingObserver if use_polling and PollingObserver is not None else Observer
            observer = observer_cls(timeout=60) if use_polling else observer_cls()
            if use_polling:
                log.warning("auto watch polling mode enabled; this may keep disks awake")
            handler = _ImageChangeHandler(self.schedule_scan)
            watched = []
            for path in paths:
                if not os.path.isdir(path):
                    log.warning("auto watch skipped missing directory: %s", path)
                    continue
                observer.schedule(handler, path, recursive=True)
                watched.append(path)

            if not watched:
                log.warning("auto watch not started: no accessible directories from %s", paths)
                return {"ok": False, "error": "图片目录不存在或不可访问"}

            observer.daemon = True
            try:
                observer.start()
            except Exception as e:
                self._last_error = str(e)
                log.exception("auto watch failed to start")
                return {"ok": False, "error": str(e)}
            self._observer = observer
            self._paths = watched
            self._last_error = None
            log.info("auto watch started for %d director%s: %s",
                     len(watched), "y" if len(watched) == 1 else "ies", watched)
            return {"ok": True, "paths": list(watched), "debounce_seconds": self._debounce_seconds}

    def stop(self):
        with self._lock:
            self.stop_locked()

    def stop_locked(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._observer is not None:
            log.info("auto watch stopping")
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None
        self._paths = []
        self._pending = False

    def restart(self, paths):
        log.info("auto watch restarting")
        return self.start(paths)

    def schedule_scan(self, path=None, event_type=None):
        with self._lock:
            if self._observer is None:
                log.info("auto watch ignored event because watcher is stopped: %s", path or "")
                return
            if self._timer is not None:
                self._timer.cancel()
            now = time.time()
            self._last_event = {
                "path": path or "",
                "event_type": event_type or "reschedule",
                "time": now,
            }
            self._pending = True
            self._timer = threading.Timer(self._debounce_seconds, self._run_scan)
            self._timer.daemon = True
            self._timer.start()
            log.info("auto watch event queued (%s): %s; scan in %ss",
                     event_type or "reschedule", path or "", self._debounce_seconds)
            publish(
                "watch_scan_queued",
                {"path": path or "", "event_type": event_type or "reschedule", "debounce_seconds": self._debounce_seconds},
            )

    def _run_scan(self):
        with self._lock:
            self._timer = None
            self._pending = False
            self._last_scan_at = time.time()
        log.info("auto watch debounce elapsed; requesting scan")
        publish("watch_scan_requested", {})
        try:
            self._scan_callback()
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            log.exception("auto watch scan callback failed")

    def status(self):
        with self._lock:
            return {
                "available": Observer is not None,
                "running": self._observer is not None,
                "paths": list(self._paths),
                "mode": "polling" if isinstance(self._observer, PollingObserver) else "events",
                "debounce_seconds": self._debounce_seconds,
                "pending": self._pending,
                "last_event": dict(self._last_event) if self._last_event else None,
                "last_scan_at": self._last_scan_at,
                "last_error": self._last_error,
            }


def _normalize_paths(paths):
    seen = set()
    result = []
    for path in paths or []:
        path = (path or "").strip()
        if not path:
            continue
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        seen.add(norm)
        result.append(path)
    return result


def _watch_debounce_seconds():
    raw = (os.getenv("AUTO_WATCH_DEBOUNCE_SECONDS", "") or "").strip()
    if raw.isdigit():
        return max(1, min(int(raw), 300))
    return 30
