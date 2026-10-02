"""内存后台任务管理：扫描 / 同步在后台线程执行，前端轮询进度，支持取消。

任务状态流转：running → done | error | cancelled
"""
import threading
import time
import traceback
import uuid

_lock = threading.Lock()
_jobs = {}
_active = {"thread": None}
_MAX_JOBS = 50


class Job:
    def __init__(self, job_id, kind):
        self.job_id = job_id
        self.kind = kind
        self.cancel_event = threading.Event()
        self.state = {
            "job_id": job_id,
            "kind": kind,
            "status": "running",
            "phase": "init",
            "current": 0,
            "total": None,
            "message": "准备中…",
            "result": None,
            "error": None,
            "created_at": time.time(),
        }

    def update(self, phase=None, current=None, total=None, message=None):
        if phase is not None:
            self.state["phase"] = phase
        if current is not None:
            self.state["current"] = current
        if total is not None:
            self.state["total"] = total
        if message is not None:
            self.state["message"] = message

    def set_status(self, status):
        self.state["status"] = status

    def set_error(self, error):
        self.state["error"] = error

    def cancel(self):
        self.cancel_event.set()
        if self.state["status"] == "running":
            self.set_status("cancelled")

    def snapshot(self):
        return dict(self.state)


def get(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return job.snapshot() if job else None


def cancel(job_id):
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job.cancel()
        return job.snapshot() if job else None


def is_busy():
    with _lock:
        t = _active["thread"]
        return t is not None and t.is_alive()


def start(kind, fn):
    """启动后台任务。fn 接收一个 Job 实例。返回 (job_id, error)。

    检查占用与注册新任务必须在同一把锁内完成，否则并发请求会同时通过
    is_alive 检查，导致两个任务并行运行、互相覆盖 _active。
    """
    with _lock:
        active = _active["thread"]
        if active is not None and active.is_alive():
            return None, "已有任务进行中，请等待或停止当前任务"
        job = Job(uuid.uuid4().hex[:12], kind)
        _jobs[job.job_id] = job
        # 防止任务字典无限增长
        if len(_jobs) > _MAX_JOBS:
            for k in list(_jobs.keys())[: len(_jobs) - _MAX_JOBS]:
                _jobs.pop(k, None)

        def run():
            try:
                fn(job)
                if job.state["status"] == "running":
                    job.set_status("done")
            except Exception as e:
                job.set_status("error")
                job.set_error({
                    "code": "INTERNAL",
                    "message": str(e),
                    "detail": traceback.format_exc(),
                })
                job.update(message="任务执行出错")
            finally:
                with _lock:
                    if _active["thread"] is thread:
                        _active["thread"] = None

        thread = threading.Thread(target=run, name=f"job-{kind}", daemon=True)
        _active["thread"] = thread

    thread.start()
    return job.job_id, None
