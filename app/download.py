import os
import re
import time
import shutil
from urllib.parse import urlparse
from dotenv import load_dotenv
from app import paths
from app.pixiv import (
    get_pixiv_client,
    _build_session_with_referer,
    apply_image_mirror,
    PixivAuthError,
    PixivNetworkError,
    PixivError,
)

load_dotenv(paths.ENV_FILE)

REQUEST_TIMEOUT = 30
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_name(name, fallback="untitled"):
    name = _ILLEGAL_CHARS.sub("_", (name or "").strip())
    name = name.rstrip(". ") or fallback
    return name[:150]


def _sleep_interruptible(delay_ms, cancel_event=None):
    """分段限速睡眠（0.1s 粒度），期间可被取消。返回是否被取消。"""
    remain = delay_ms / 1000.0
    while remain > 1e-9:
        if cancel_event is not None and cancel_event.is_set():
            return True
        time.sleep(0.1 if remain >= 0.1 else remain)
        remain -= 0.1
    return False


def check_bookmark_subscription(client, session, pixiv_user_id, last_pid, source_dir,
                                delay_ms=800, progress_callback=None, cancel_event=None,
                                max_pages=15):
    """检查订阅的公开收藏列表：只下载比 last_pid 更新的作品。

    收藏列表项不含 bookmark_data.id（App API 特性），故以「作品 PID」为增量游标：
    列表按收藏时间倒序，用 next_url 的 max_bookmark_id 逐页向后翻，遇到 PID <=
    last_pid 或整页作品都已入库即停止——稳态下每次检查只拉一页，不逐个比对历史 ID。
    本轮无失败时把游标推进到所见最大 PID；有失败则保持原游标（下轮整体重试，
    靠已入库集合与文件级去重避免重复下载）。auth 错误向上抛由编排层中断。
    落盘目录按每条收藏自身的画师分组，命名规则与扫描器兼容。
    """
    from app.database import get_db

    result = {
        "pixiv_user_id": pixiv_user_id,
        "new_found": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "max_pid_seen": last_pid,
        "details": [],
    }
    threshold = last_pid or 0

    with get_db() as conn:
        known_pids = {r[0] for r in conn.execute("SELECT pixiv_id FROM artworks").fetchall()}

    collected = []
    seen_pids = set()
    max_bookmark_id = None
    hit_boundary = False
    for _ in range(max_pages):
        if cancel_event is not None and cancel_event.is_set():
            break
        illusts, next_url = client.list_user_bookmarks(
            pixiv_user_id, max_bookmark_id=max_bookmark_id
        )
        if not illusts:
            break
        page_all_known = True
        for item in illusts:
            pid = item.get("id", 0)
            if pid > (result["max_pid_seen"] or 0):
                result["max_pid_seen"] = pid
            if not pid or pid <= threshold:
                hit_boundary = True
                continue
            if pid in known_pids or pid in seen_pids:
                continue
            page_all_known = False
            seen_pids.add(pid)
            collected.append(item)
        if hit_boundary or page_all_known or not next_url:
            break
        max_bookmark_id = client.parse_next_qs(next_url).get("max_bookmark_id")
        if not max_bookmark_id:
            break

    result["new_found"] = len(collected)
    collected.sort(key=lambda x: x.get("id", 0))  # 旧→新，游标推进更稳

    for idx, illust in enumerate(collected):
        if cancel_event is not None and cancel_event.is_set():
            break
        pixiv_id = illust.get("id", 0)
        author_name = (illust.get("user") or {}).get("name", "") or str(pixiv_id)
        if progress_callback:
            progress_callback(
                "download", idx + 1, len(collected),
                f"下载收藏作品 {idx + 1}/{len(collected)}（PID {pixiv_id}）",
            )
        if delay_ms > 0:
            if _sleep_interruptible(delay_ms, cancel_event):
                break

        author_root = os.path.join(source_dir, _safe_name(author_name))
        status, err = download_single_illust(session, illust, author_root, author_name)
        if status in ("downloaded", "skipped"):
            result[status] += 1
        else:
            result["failed"] += 1
            result["details"].append(
                {"pixiv_id": pixiv_id, "status": "failed", "error": err}
            )

    # 有失败项时不推进游标（下轮重试；已成功的靠 known_pids/文件存在去重）
    if result["failed"] > 0:
        result["max_pid_seen"] = threshold

    return result


def _resolve_dest(author_root, author_name, pixiv_id, title, page_count):
    """计算单作品目标目录与文件名前缀（单页/多页规则，与扫描入库命名一致）。"""
    dest_dir = author_root
    prefix = f"[{_safe_name(author_name)}] {pixiv_id}"
    if page_count > 1:
        dest_dir = os.path.join(
            author_root, f"[{_safe_name(author_name)}] {_safe_name(title)}"
        )
        prefix = f"[{_safe_name(title)}] {pixiv_id}_p"
    return dest_dir, prefix


def download_single_illust(session, illust, author_root, author_name):
    """下载单个 illust 的全部页（.part 原子写）。

    返回 (status, err_msg)：status 为 'downloaded' / 'skipped'；
    认证类错误以 PixivAuthError 抛出（供上层中断整批）；其它失败以 ('failed', msg) 返回。
    """
    pixiv_id = illust.get("id", 0)
    title = illust.get("title", "")
    page_count = max(1, illust.get("page_count", 1))
    urls = _extract_original_urls(illust)
    dest_dir, _prefix = _resolve_dest(author_root, author_name, pixiv_id, title, page_count)

    if _work_exists(dest_dir, _prefix):
        return "skipped", ""

    try:
        for p, url in enumerate(urls):
            if not url:
                continue
            ext = _url_ext(url) or ".jpg"
            if page_count > 1:
                filename = f"[{_safe_name(title)}] {pixiv_id}_p{p}{ext}"
            else:
                filename = f"[{_safe_name(author_name)}] {pixiv_id}{ext}"
            dest = os.path.join(dest_dir, filename)
            if os.path.exists(dest):
                continue
            os.makedirs(dest_dir, exist_ok=True)
            # 先写 .part 再原子改名：中断/超时不会留下半截文件，
            # 避免扫描时把残损 JPEG 当成完整作品入库
            tmp = dest + ".part"
            try:
                with session.get(url, timeout=REQUEST_TIMEOUT, stream=True) as resp:
                    if resp.status_code != 200:
                        raise PixivNetworkError(f"HTTP {resp.status_code}")
                    with open(tmp, "wb") as f:
                        shutil.copyfileobj(resp.raw, f)
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        return "downloaded", ""
    except PixivAuthError:
        raise
    except PixivError as e:
        return "failed", str(e)
    except Exception as e:
        return "failed", str(e)


def _extract_original_urls(illust):
    pages = illust.get("meta_pages") or []
    if pages:
        return [
            apply_image_mirror(p.get("image_urls", {}).get("original", ""))
            for p in pages
            if p.get("image_urls", {}).get("original")
        ]
    single = illust.get("meta_single_page", {}).get("original_image_url", "")
    if single:
        return [apply_image_mirror(single)]
    return []


def _url_ext(url):
    path = urlparse(url).path
    return os.path.splitext(path)[1].lower()


def _work_exists(dest_dir, prefix):
    """目录中已有文件名以 prefix 开头（任意扩展名）即视为已存在。"""
    if not os.path.isdir(dest_dir):
        return False
    try:
        for name in os.listdir(dest_dir):
            if name.startswith(prefix):
                return True
    except Exception:
        return False
    return False
