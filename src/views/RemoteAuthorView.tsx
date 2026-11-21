import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, Download } from "lucide-react";

import { CMD, call, isAppError } from "../lib/ipc";
import { SORT_LABELS, useUi } from "../stores/ui";
import type { AuthorRow, RemoteIllust } from "../types/ipc";
import { RemoteThumb } from "./RemoteTagView";

function seededShuffle<T>(items: T[], seed: number): T[] {
  const list = [...items];
  let state = seed >>> 0 || 1;
  for (let i = list.length - 1; i > 0; i -= 1) {
    state = (state * 1664525 + 1013904223) >>> 0;
    const j = state % (i + 1);
    [list[i], list[j]] = [list[j], list[i]];
  }
  return list;
}

interface Props {
  author: AuthorRow;
  onBack: () => void;
}

/** 作者页的「展示线上全部」：按 Pixiv 用户 ID 拉取线上作品。 */
export function RemoteAuthorView({ author, onBack }: Props) {
  const [items, setItems] = useState<RemoteIllust[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [done, setDone] = useState<Set<number>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  const bumpData = useUi((state) => state.bumpData);
  const r18 = useUi((state) => state.r18);
  const ai = useUi((state) => state.ai);
  const gif = useUi((state) => state.gif);
  const deletedOnly = useUi((state) => state.deletedOnly);
  const sort = useUi((state) => state.sort);
  const order = useUi((state) => state.order);
  const shuffleToken = useUi((state) => state.shuffleToken);

  useEffect(() => {
    let cancelled = false;
    setItems([]);
    setLoading(true);
    setError(null);
    setNotice(null);
    setDone(new Set());
    call<RemoteIllust[]>(CMD.listPixivAuthorIllusts, {
      pixivUserId: author.pixiv_user_id,
    })
      .then((list) => {
        if (!cancelled) setItems(list);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(isAppError(reason) ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [author.pixiv_user_id]);

  const visibleItems = useMemo(
    () =>
      items.filter((item) => {
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
    [items, r18, ai, gif],
  );

  const orderedItems = useMemo(() => {
    if (sort === "random") {
      return seededShuffle(
        visibleItems,
        (shuffleToken || 1) + (order === "asc" ? 1 : 0),
      );
    }
    const list = [...visibleItems];
    list.sort((a, b) => {
      if (sort === "title") return a.title.localeCompare(b.title, "ja");
      if (sort === "pixiv_id") return a.pixiv_id - b.pixiv_id;
      if (sort === "create_date") {
        const compare = (a.create_date || "").localeCompare(b.create_date || "");
        return compare === 0 ? a.pixiv_id - b.pixiv_id : compare;
      }
      return 0;
    });
    if (sort !== "first_seen" && order === "asc") list.reverse();
    if (sort === "first_seen" && order === "asc") list.reverse();
    return list;
  }, [visibleItems, sort, order, shuffleToken]);

  const download = async (item: RemoteIllust) => {
    if (busy !== null) return;
    setBusy(item.pixiv_id);
    setNotice(null);
    try {
      await call<number>(CMD.downloadPixivIllust, { pixivId: item.pixiv_id });
      setDone((prev) => new Set(prev).add(item.pixiv_id));
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
            Pixiv 线上全部作品 ·{" "}
            {sort === "first_seen" ? "线上发布时间顺序" : SORT_LABELS[sort]}
            {sort !== "random" ? (order === "asc" ? " ↑" : " ↓") : ""}
            {items.length > 0 && ` · 显示 ${visibleItems.length}/${items.length}`}
          </p>
        </div>
      </div>

      {deletedOnly && (
        <p className="mt-2 text-micro text-text-3">
          「已隐藏」只适用于本地库；Pixiv 线上结果没有这个状态。
        </p>
      )}

      {loading ? (
        <p className="py-10 text-center text-micro text-text-2">
          正在从 Pixiv 获取「{author.name}」的全部作品…
        </p>
      ) : error ? (
        <div className="mt-4 rounded-card border border-line bg-surface p-4 text-micro text-text-2">
          <p>Pixiv 线上结果暂时不可用：{error}</p>
          <p className="mt-1 text-text-3">可以返回本地作品，本地标签不受影响。</p>
        </div>
      ) : visibleItems.length === 0 ? (
        <p className="py-10 text-center text-micro text-text-2">
          {items.length === 0
            ? "Pixiv 上没有找到线上作品。"
            : "当前 R18 / AI / GIF 筛选下没有作品。"}
        </p>
      ) : (
        <>
          <div className="mt-4 grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(210px,1fr))]">
            {orderedItems.map((item) => (
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
                    disabled={busy !== null || done.has(item.pixiv_id)}
                    onClick={() => void download(item)}
                  >
                    <Download size={11} />
                    {done.has(item.pixiv_id)
                      ? "已入库"
                      : busy === item.pixiv_id
                        ? "下载中…"
                        : "下载"}
                  </button>
                </div>
              </div>
            ))}
          </div>
          {notice && <p className="mt-3 text-micro text-text-2">{notice}</p>}
        </>
      )}
    </div>
  );
}
