"""Small in-process event bus used by the gallery UI."""
import json
import logging
import queue
import threading
import time

log = logging.getLogger("pixiv_archive.events")

_lock = threading.Lock()
_subscribers = set()
_seq = 0


def publish(event_type, payload=None):
    global _seq
    event = {
        "seq": 0,
        "type": event_type,
        "time": time.time(),
        "payload": payload or {},
    }
    with _lock:
        _seq += 1
        event["seq"] = _seq
        subscribers = list(_subscribers)

    dropped = 0
    for subscriber in subscribers:
        try:
            subscriber.put_nowait(event)
        except queue.Full:
            dropped += 1
    if subscribers:
        log.info("event published: type=%s subscribers=%d dropped=%d", event_type, len(subscribers), dropped)
    else:
        log.info("event published with no active subscribers: type=%s", event_type)


def stream():
    subscriber = queue.Queue(maxsize=100)
    with _lock:
        _subscribers.add(subscriber)
        count = len(_subscribers)
    log.info("sse client connected; subscribers=%d", count)
    try:
        yield _format_event("connected", {"time": time.time()})
        while True:
            try:
                event = subscriber.get(timeout=15)
                yield _format_event(event["type"], event)
            except queue.Empty:
                yield ": keepalive\n\n"
    except GeneratorExit:
        raise
    finally:
        with _lock:
            _subscribers.discard(subscriber)
            count = len(_subscribers)
        log.info("sse client disconnected; subscribers=%d", count)


def _format_event(event_type, data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {payload}\n\n"
