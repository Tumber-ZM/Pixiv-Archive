import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, Download, Heart } from "lucide-react";

import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { AuthorRow, RemoteIllust } from "../types/ipc";
import { RemoteThumb } from "./RemoteTagView";

interface Props {
  author: AuthorRow;
  onBack: () => void;
}

/** 作者页的「展示线上全部」：按 Pixiv 用户 ID 分页拉取，拿到一页就展示一页。 */
export function RemoteAuthorView({ author, onBack }: Props) {
  const [items, setItems] = useState<RemoteIllust[]>([]);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [localIds, setLocalIds] = useState<Set<number>>(new Set());
  const [onlyLocal, setOnlyLocal] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);
  const [doneIds, setDoneIds] = useState<Set<number>>(new Set());
  const bumpData = useUi((state) => state.bumpData);
  const r18 = useUi((state) => state.r18);
  const ai = useUi((state) => state.ai);
  const gif = useUi((state) => state.gif);
  const deletedOnly = useUi((state) => state.deletedOnly);

  useEffect(() => {
    let cancelled = false;
    call<number[]>(CMD.listAuthorLocalPixivIds, {
      authorPixivId: author.pixiv_user_id,
    })
      .then((ids) => {
        if (!cancelled) setLocalIds(new Set(ids));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [author.pixiv_user_id]);

  useEffect(() => {
    let cancelled = false;
    setItems([]);
    setInitialLoading(true);
    setLoadingMore(false);
    setError(null);
    setNotice(null);
    setDoneIds(new Set());

    const load = async () => {
      let next: string | null = null;
      let count = 0;
      try {
        for (;;) {
          const result: [RemoteIllust[], string | null] =
            await call<[RemoteIllust[], string | null]>(
              CMD.listPixivAuthorIllustsPage,
              { pixivUserId: author.pixiv_user_id, nextUrl: next },
            );
          const page: RemoteIllust[] = result[0];
          const nextUrl: string | null = result[1];
          if (cancelled) return;
          count += page.length;
          page.sort((a, b) => {
            const compare = (b.create_date || "").localeCompare(
              a.create_date || "",
            );
            return compare === 0 ? b.pixiv_id - a.pixiv_id : compare;
          });
          setItems((prev) => [...prev, ...page]);
          setInitialLoading(false);
          if (!nextUrl || page.length === 0 || count >= 1000) {
            setLoadingMore(false);
            return;
          }
          next = nextUrl;
          setLoadingMore(true);
        }
      } catch (reason: unknown) {
        if (cancelled) return;
        const message = isAppError(reason) ? reason.message : String(reason);
        if (count === 0) setError(message);
        else setNotice(`部分线上作品加载失败：${message}`);
        setLoadingMore(false);
        setInitialLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [author.pixiv_user_id]);

  const visibleItems = useMemo(
    () =>
      items.filter((item) => {
        if (onlyLocal && !localIds.has(item.pixiv_id)) return false;
        const isR18 = item.tags.some(
          (name) => name === "R-18" || name === "R-18G",
        );
        if (r18 === "hide" && isR18) return false;
        if (r18 === "only" && !isR18) return false;
        const isAi =
          item.ai_type === 2 ||
          item.tags.some((name) =>
            /^ai(イラスト|生成|画像)?$/i.test(name.trim()),
          );
        if (ai === "hide" && isAi) return false;
        if (ai === "only" && !isAi) return false;
        if (gif === "hide" && item.is_gif) return false;
        if (gif === "only" && !item.is_gif) return false;
        return true;
      }),
    [items, onlyLocal, localIds, r18, ai, gif],
  );


  const download = async (item: RemoteIllust) => {
    if (busy !== null) return;
    setBusy(item.pixiv_id);
    setNotice(null);
    try {
      await call<number>(CMD.downloadPixivIllust, { pixivId: item.pixiv_id });
      setDoneIds((prev) => new Set(prev).add(item.pixiv_id));
      setLocalIds((prev) => new Set(prev).add(item.pixiv_id));
      bumpData();
      setNotice(`「${item.title || item.pixiv_id}」已下载入库`);
    } catch (reason: unknown) {
      setNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="px-6 py-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className="tri-btn inline-flex items-center gap-1"
          onClick={onBack}
        >
          <ArrowLeft size={12} />
          返回本地作品
        </button>
        <div className="min-w-0">
          <h2 className="truncate text-heading text-text">{author.name}</h2>
          <p className="text-micro text-text-3">
            Pixiv 线上全部作品 · 按发布时间新→旧
            {items.length > 0 &&
              ` · 已加载 ${items.length}${loadingMore ? "…" : ""}`}
            {items.length > 0 && ` · 显示 ${visibleItems.length}`}
          </p>
        </div>
        <button
          type="button"
          className="tri-btn"
          data-active={onlyLocal}
          onClick={() => setOnlyLocal((value) => !value)}
          title="只显示已经下载到本地的作品"
        >
          仅展示本地
        </button>
      </div>

      {deletedOnly && (
        <p className="mt-2 text-micro text-text-3">
          「已隐藏」只适用于本地库；Pixiv 线上结果没有这个状态。
        </p>
      )}

      {initialLoading && items.length === 0 ? (
        <p className="py-10 text-center text-micro text-text-2">
          正在从 Pixiv 获取「{author.name}」的作品，第一页马上显示…
        </p>
      ) : error && items.length === 0 ? (
        <div className="mt-4 rounded-card border border-line bg-surface p-4 text-micro text-text-2">
          <p>Pixiv 线上结果暂时不可用：{error}</p>
          <p className="mt-1 text-text-3">可以返回本地作品，本地标签不受影响。</p>
        </div>
      ) : visibleItems.length === 0 ? (
        <p className="py-10 text-center text-micro text-text-2">
          {items.length === 0
            ? "Pixiv 上没有找到线上作品。"
            : onlyLocal
              ? "这些线上作品都还没有下载到本地。"
              : "当前 R18 / AI / GIF 筛选下没有作品。"}
        </p>
      ) : (
        <>
          <div className="mt-4 grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(210px,1fr))]">
            {visibleItems.map((item) => (
              <div
                key={item.pixiv_id}
                className="overflow-hidden rounded-card border border-line bg-surface"
              >
                <div className="relative aspect-[3/4] bg-black/20">
                  <RemoteThumb
                    url={item.thumbnail}
                    alt={item.title || `Pixiv ${item.pixiv_id}`}
                  />
                  <span className="badge absolute bottom-2 right-2">
                    {item.page_count}P
                  </span>
                  {localIds.has(item.pixiv_id) && (
                    <span className="absolute left-2 top-2 rounded-full bg-black/70 px-1.5 py-0.5 text-micro text-white">
                      本地
                    </span>
                  )}
                  <span
                    className="pixiv-heart"
                    data-active={item.is_favorited}
                    title={
                      item.is_favorited
                        ? "已在 Pixiv 收藏"
                        : "不在 Pixiv 收藏"
                    }
                  >
                    <Heart
                      size={11}
                      fill={item.is_favorited ? "currentColor" : "none"}
                    />
                  </span>
                </div>
                <div className="p-2">
                  <p className="truncate text-label text-text" title={item.title}>
                    {item.title || `Pixiv ${item.pixiv_id}`}
                  </p>
                  <p className="truncate text-micro text-text-3">
                    {item.is_gif ? "GIF · " : ""}
                    {item.page_count}P
                  </p>
                  <button
                    type="button"
                    className="tri-btn tri-btn-accent mt-2 inline-flex w-full items-center justify-center gap-1"
                    disabled={busy !== null || doneIds.has(item.pixiv_id)}
                    onClick={() => void download(item)}
                  >
                    <Download size={11} />
                    {doneIds.has(item.pixiv_id)
                      ? "已入库"
                      : busy === item.pixiv_id
                        ? "下载中…"
                        : "下载"}
                  </button>
                </div>
              </div>
            ))}
          </div>
          {loadingMore && (
            <p className="mt-4 text-center text-micro text-text-3">
              继续加载线上作品…
            </p>
          )}
          {notice && <p className="mt-3 text-micro text-text-2">{notice}</p>}
        </>
      )}
    </div>
  );
}
