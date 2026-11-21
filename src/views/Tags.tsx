import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Pin,
  Save,
  Tag as TagIcon,
  X,
} from "lucide-react";

import { EmptyState } from "../components/common/EmptyState";
import { ContextMenu, type MenuItem } from "../components/common/ContextMenu";
import { Dialog } from "../components/dialog/Dialog";
import { ArtworkList } from "../components/wall/ArtworkList";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { FavoriteSyncStatus, RemoteTag, TagCombo, TagRow } from "../types/ipc";

const SEARCH_DEBOUNCE_MS = 300;
const TAG_ROW_HEIGHT = 36;
const DEFAULT_ROWS = 2;
const MORE_ROWS = 4;

/** 当前正在看的标签。跳转只需要名字，不必带完整 TagRow。 */
interface CurrentTag {
  name: string;
  label: string;
}

function sameTags(a: string[], b: string[]) {
  return a.length === b.length && a.every((tag) => b.includes(tag));
}

export function Tags() {
  const q = useUi((state) => state.q);
  const [rows, setRows] = useState<TagRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<CurrentTag | null>(null);

  const [onlyCollected, setOnlyCollected] = useState(false);
  const [onlySubscribed, setOnlySubscribed] = useState(false);
  const [favoriteStatus, setFavoriteStatus] = useState<FavoriteSyncStatus | null>(null);
  const favoritesSynced = (favoriteStatus?.count ?? 0) > 0;

  const [combos, setCombos] = useState<TagCombo[]>([]);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [comboName, setComboName] = useState("");
  const [comboBusy, setComboBusy] = useState(false);
  const [comboError, setComboError] = useState<string | null>(null);
  const [comboMenu, setComboMenu] = useState<{
    x: number;
    y: number;
    combo: TagCombo;
  } | null>(null);
  const [renameTarget, setRenameTarget] = useState<TagCombo | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<TagCombo | null>(null);
  const [expandedCombos, setExpandedCombos] = useState<Set<number>>(new Set());

  const [visibleRows, setVisibleRows] = useState(DEFAULT_ROWS);
  const [hasOverflow, setHasOverflow] = useState(false);
  const tagBoxRef = useRef<HTMLDivElement>(null);

  const [remoteTags, setRemoteTags] = useState<RemoteTag[]>([]);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [remoteError, setRemoteError] = useState<string | null>(null);
  const [remoteFor, setRemoteFor] = useState("");
  const [remoteExpanded, setRemoteExpanded] = useState(false);
  const [remoteHasOverflow, setRemoteHasOverflow] = useState(false);
  const remoteBoxRef = useRef<HTMLDivElement>(null);

  const showAllTagsDefault = useUi((state) => state.showAllTagsDefault);
  const setRandomTag = useUi((state) => state.setRandomTag);

  const pendingTag = useUi((state) => state.pendingTag);
  const consumePending = useUi((state) => state.consumePending);
  const dataEpoch = useUi((state) => state.dataEpoch);

  useEffect(() => {
    if (!pendingTag) return;
    setCurrent({ name: pendingTag, label: pendingTag });
    consumePending();
  }, [pendingTag, consumePending]);

  // Pixiv 收藏概况：同步过才按收藏筛选，没同步时退回「订阅前就有」的口径
  useEffect(() => {
    let cancelled = false;
    call<FavoriteSyncStatus>(CMD.pixivFavoritesStatus)
      .then((status) => {
        if (!cancelled) setFavoriteStatus(status);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [dataEpoch]);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      setLoading(true);
      call<TagRow[]>(CMD.listTags, {
        q: q.trim() || undefined,
        favoritedOnly: onlyCollected && favoritesSynced ? true : undefined,
        collectedOnly: onlyCollected && !favoritesSynced ? true : undefined,
        subscribedOnly: onlySubscribed ? true : undefined,
      })
        .then((data) => {
          if (cancelled) return;
          setRows(data);
          setError(null);
        })
        .catch((reason: unknown) => {
          if (cancelled) return;
          setError(isAppError(reason) ? reason.message : String(reason));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [q, dataEpoch, onlyCollected, onlySubscribed, favoritesSynced]);

  // 换搜索词 / 筛选条件时回到 2 行
  useEffect(() => {
    setVisibleRows(DEFAULT_ROWS);
  }, [q, onlyCollected, onlySubscribed]);

  const loadCombos = async () => {
    try {
      const data = await call<TagCombo[]>(CMD.listTagCombos);
      setCombos(data);
    } catch (reason: unknown) {
      setComboError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  useEffect(() => {
    void loadCombos();
  }, []);

  // Pixiv 上的标签：有搜索词时聚合远端作品标签，没词时取热门标签；失败只提示
  useEffect(() => {
    let cancelled = false;
    const word = q.trim();
    const timer = window.setTimeout(() => {
      setRemoteLoading(true);
      setRemoteError(null);
      call<RemoteTag[]>(CMD.searchPixivTags, { word })
        .then((tags) => {
          if (cancelled) return;
          setRemoteTags(tags);
          setRemoteFor(word);
        })
        .catch((reason: unknown) => {
          if (cancelled) return;
          setRemoteError(isAppError(reason) ? reason.message : String(reason));
          setRemoteTags([]);
          setRemoteFor(word);
        })
        .finally(() => {
          if (!cancelled) setRemoteLoading(false);
        });
    }, 450);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [q]);

  // 顶栏「随机标签」：从当前标签列表随机挑一个并勾选
  useEffect(() => {
    const handler = () => {
      if (rows.length === 0) return;
      const candidates = rows.filter((row) => !selectedTags.includes(row.name));
      const pool = candidates.length > 0 ? candidates : rows;
      const row = pool[Math.floor(Math.random() * pool.length)];
      setRandomTag({
        name: row.name,
        label: row.translated_name ?? row.name,
      });
      setSelectedTags((prev) =>
        prev.includes(row.name) ? prev : [...prev, row.name],
      );
    };
    window.addEventListener("pa-tags-random", handler);
    return () => window.removeEventListener("pa-tags-random", handler);
  }, [rows, selectedTags, setRandomTag]);

  // Pixiv 标签默认只显示一行
  useEffect(() => {
    const el = remoteBoxRef.current;
    if (!el) return undefined;
    const update = () => {
      setRemoteHasOverflow(el.scrollHeight > TAG_ROW_HEIGHT + 1);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [remoteTags, remoteExpanded, remoteFor]);

  useEffect(() => {
    setRemoteExpanded(false);
  }, [q]);

  // 标签按钮云是否有被 2 行裁掉的内容
  useEffect(() => {
    const el = tagBoxRef.current;
    if (!el) return undefined;
    const update = () => {
      setHasOverflow(
        !q.trim() && el.scrollHeight > visibleRows * TAG_ROW_HEIGHT + 1,
      );
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [rows, visibleRows, q]);

  const labelFor = (name: string) => {
    const row = rows.find((item) => item.name === name);
    return row?.translated_name ?? name;
  };

  const toggleTag = (name: string) => {
    setSelectedTags((prev) =>
      prev.includes(name) ? prev.filter((tag) => tag !== name) : [...prev, name],
    );
  };

  const applyCombo = (combo: TagCombo) => {
    setSelectedTags([...combo.tags]);
    setComboError(null);
  };

  const saveCombo = async () => {
    if (!selectedTags.length || comboBusy) return;
    setComboBusy(true);
    setComboError(null);
    try {
      await call<TagCombo>(CMD.saveTagCombo, {
        name: comboName.trim() || null,
        tags: selectedTags,
      });
      setComboName("");
      await loadCombos();
    } catch (reason: unknown) {
      setComboError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setComboBusy(false);
    }
  };

  const togglePin = async (combo: TagCombo) => {
    setComboMenu(null);
    try {
      await call<TagCombo>(CMD.toggleTagComboPin, { id: combo.id });
      await loadCombos();
    } catch (reason: unknown) {
      setComboError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  const confirmRename = async () => {
    if (!renameTarget || !renameValue.trim() || comboBusy) return;
    setComboBusy(true);
    try {
      await call<TagCombo>(CMD.renameTagCombo, {
        id: renameTarget.id,
        name: renameValue.trim(),
      });
      setRenameTarget(null);
      setRenameValue("");
      await loadCombos();
    } catch (reason: unknown) {
      setComboError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setComboBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget || comboBusy) return;
    setComboBusy(true);
    try {
      await call<boolean>(CMD.deleteTagCombo, { id: deleteTarget.id });
      setDeleteTarget(null);
      await loadCombos();
    } catch (reason: unknown) {
      setComboError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setComboBusy(false);
    }
  };

  if (current) {
    return (
      <ArtworkList
        extraQuery={{ tag: current.name }}
        heading={`# ${current.label}`}
        onBack={() => setCurrent(null)}
      />
    );
  }

  const searching = Boolean(q.trim());

  const comboMenuItems: MenuItem[] = comboMenu
    ? [
        {
          label: comboMenu.combo.pinned ? "取消置顶" : "置顶",
          onSelect: () => void togglePin(comboMenu.combo),
        },
        {
          label: "重命名",
          onSelect: () => {
            setRenameTarget(comboMenu.combo);
            setRenameValue(comboMenu.combo.name);
            setComboMenu(null);
          },
        },
        {
          label: "删除",
          danger: true,
          onSelect: () => {
            setDeleteTarget(comboMenu.combo);
            setComboMenu(null);
          },
        },
      ]
    : [];

  return (
    <div className="px-6 py-4">
      {/* 筛选 */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-micro text-text-3">筛选</span>
        <button
          type="button"
          className="tri-btn"
          data-active={onlyCollected}
          title={
            favoritesSynced
              ? "只看用在 Pixiv 收藏作品上的标签"
              : "还没同步收藏，先按订阅前就有的作品筛选"
          }
          onClick={() => setOnlyCollected((value) => !value)}
        >
          仅看收藏
        </button>
        <button
          type="button"
          className="tri-btn"
          data-active={onlySubscribed}
          title="只看用在已订阅作者作品上的标签"
          onClick={() => setOnlySubscribed((value) => !value)}
        >
          仅看已订阅
        </button>
        {!loading && !error && (
          <span className="text-micro text-text-3">共 {rows.length} 个标签</span>
        )}
        <span className="text-micro text-text-3">
          {favoritesSynced
            ? `Pixiv 收藏 ${favoriteStatus?.count ?? 0} 件`
            : "Pixiv 收藏未同步（按订阅前作品算）"}
        </span>
        {(onlyCollected || onlySubscribed) && (
          <button
            type="button"
            className="text-micro text-text-3 hover:text-text"
            onClick={() => {
              setOnlyCollected(false);
              setOnlySubscribed(false);
            }}
          >
            清除筛选
          </button>
        )}
      </div>

      {/* 标签组合：紧跟在筛选后面，保持单行高度 */}
      <div className="mt-2">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className="text-micro text-text-3"
            title="勾选下方标签组成组合；保存后右键可重命名 / 删除 / 置顶"
          >
            标签组合
          </span>
          <button
            type="button"
            className="tri-btn tri-btn-accent inline-flex items-center gap-1"
            disabled={selectedTags.length === 0 || comboBusy}
            onClick={saveCombo}
            title="把当前勾选的标签保存成组合"
          >
            <Save size={12} />
            保存该组标签
          </button>
          <div className="w-[160px]">
            <input
              className="line-input"
              placeholder="组合名称（可留空）"
              value={comboName}
              maxLength={30}
              onChange={(event) => setComboName(event.currentTarget.value)}
            />
          </div>
          {selectedTags.length > 0 && (
            <button
              type="button"
              className="text-micro text-text-3 hover:text-text"
              onClick={() => setSelectedTags([])}
            >
              清空
            </button>
          )}
          <span className="text-micro text-text-3">此次组合：</span>
          {selectedTags.length === 0 ? (
            <span className="text-micro text-text-3">未选择标签</span>
          ) : (
            selectedTags.map((tag) => (
              <span key={tag} className="combo-tag" title={tag}>
                #{labelFor(tag)}
                <button
                  type="button"
                  className="combo-tag-x"
                  aria-label={`移除 ${tag}`}
                  onClick={() => toggleTag(tag)}
                >
                  <X size={10} />
                </button>
              </span>
            ))
          )}
        </div>
        {comboError && <p className="mt-1 text-micro text-danger">{comboError}</p>}

        {combos.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {combos.map((combo) => {
              const expanded = expandedCombos.has(combo.id);
              const active = sameTags(selectedTags, combo.tags);
              return (
                <div
                  key={combo.id}
                  className="combo-card w-[240px] rounded-btn border border-line bg-surface"
                  data-active={active}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    setComboMenu({ x: event.clientX, y: event.clientY, combo });
                  }}
                >
                  <div className="flex items-center gap-1 px-1 py-1">
                    <button
                      type="button"
                      className="icon-btn"
                      aria-label={expanded ? "收起组合" : "展开组合"}
                      onClick={() =>
                        setExpandedCombos((prev) => {
                          const next = new Set(prev);
                          if (next.has(combo.id)) next.delete(combo.id);
                          else next.add(combo.id);
                          return next;
                        })
                      }
                    >
                      {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                    <button
                      type="button"
                      className="min-w-0 flex-1 text-left"
                      onClick={() => applyCombo(combo)}
                      title={`选用这组标签：${combo.tags.join("、")}`}
                    >
                      <span className="flex items-center gap-1 text-label text-text">
                        {combo.pinned && (
                          <Pin size={10} className="shrink-0 text-accent" />
                        )}
                        <span className="truncate">{combo.name}</span>
                      </span>
                      <span className="block truncate text-micro text-text-3">
                        {combo.tags.length} 个标签 ·{" "}
                        {combo.tags.map((tag) => labelFor(tag)).join(" · ")}
                      </span>
                    </button>
                  </div>
                  {expanded && (
                    <div className="flex flex-wrap gap-1 border-t border-line px-2 py-2">
                      {combo.tags.map((tag) => (
                        <span key={tag} className="combo-tag" title={tag}>
                          #{labelFor(tag)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* 标签按钮云 */}
      <div className="mt-5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-micro text-text-3">标签</span>
          <span className="text-micro text-text-3">
            {q.trim()
              ? `搜索「${q.trim()}」的本地结果 · ${rows.length} 个`
              : `按本地数量排序 · ${rows.length} 个`}
          </span>
        </div>

        {error ? (
          <EmptyState title="加载失败" hint={error} />
        ) : loading ? (
          <p className="py-6 text-center text-micro text-text-2">加载中…</p>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={TagIcon}
            title="没有找到标签"
            hint={q.trim() ? "换个关键词，或调整筛选条件" : "先同步元数据，或调整筛选条件"}
          />
        ) : (
          <>
            <div
              ref={tagBoxRef}
              className={`mt-2 flex flex-wrap gap-2 ${searching ? "" : "overflow-hidden"}`}
              style={searching ? undefined : { maxHeight: visibleRows * TAG_ROW_HEIGHT }}
            >
              {rows.map((row) => {
                const label = row.translated_name ?? row.name;
                const selected = selectedTags.includes(row.name);
                return (
                  <div key={row.id} className="tag-pick" data-active={selected}>
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={() => toggleTag(row.name)}
                      aria-label={`选择标签 ${label}`}
                    />
                    <button
                      type="button"
                      className="tag-pick-open"
                      onClick={() => toggleTag(row.name)}
                      title={`勾选 / 取消 #${label}（${row.count} 件）`}
                    >
                      <span className="tag-pick-label"># {label}</span>
                      <span className="tag-pick-count">{row.count}</span>
                    </button>
                  </div>
                );
              })}
            </div>
            {!searching && (hasOverflow || visibleRows > DEFAULT_ROWS) && (
              <div className="mt-2 flex items-center gap-3 text-micro">
                {hasOverflow && (
                  <button
                    type="button"
                    className="text-text-3 hover:text-text"
                    onClick={() => setVisibleRows((value) => value + MORE_ROWS)}
                  >
                    展开更多（+{MORE_ROWS} 行）
                  </button>
                )}
                {visibleRows > DEFAULT_ROWS && (
                  <button
                    type="button"
                    className="text-text-3 hover:text-text"
                    onClick={() => setVisibleRows(DEFAULT_ROWS)}
                  >
                    收起
                  </button>
                )}
              </div>
            )}
          </>
        )}
      </div>

      {/* Pixiv 上的标签 */}
      <div className="mt-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-micro text-text-3">Pixiv 的标签</span>
          {remoteLoading && <span className="text-micro text-text-3">获取中…</span>}
        </div>
        {remoteError ? (
          <p className="mt-2 text-micro text-text-2">
            Pixiv 标签暂不可用：{remoteError}。本地标签不受影响。
          </p>
        ) : remoteLoading ? (
          <p className="mt-2 text-micro text-text-2">
            正在从 Pixiv 获取{q.trim() ? `「${q.trim()}」的` : "热门"}标签…
          </p>
        ) : remoteFor === q.trim() && remoteTags.length > 0 ? (
          <>
            <div
              ref={remoteBoxRef}
              className={`mt-2 flex flex-wrap gap-2 ${remoteExpanded ? "" : "overflow-hidden"}`}
              style={
                remoteExpanded ? undefined : { maxHeight: TAG_ROW_HEIGHT }
              }
            >
              {remoteTags.map((tag) => {
                const selected = selectedTags.includes(tag.name);
                const label = tag.translated_name ?? tag.name;
                return (
                  <div
                    key={tag.name}
                    className="tag-pick"
                    data-active={selected}
                    data-source="pixiv"
                  >
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={() => toggleTag(tag.name)}
                      aria-label={`选择 Pixiv 标签 ${label}`}
                    />
                    <button
                      type="button"
                      className="tag-pick-open"
                      onClick={() => toggleTag(tag.name)}
                      title={`勾选 / 取消 Pixiv 标签 #${label}`}
                    >
                      <span className="tag-pick-label"># {label}</span>
                      {tag.count > 0 && (
                        <span className="tag-pick-count">{tag.count}</span>
                      )}
                    </button>
                  </div>
                );
              })}
            </div>
            {(remoteHasOverflow || remoteExpanded) && (
              <button
                type="button"
                className="mt-2 text-micro text-text-3 hover:text-text"
                onClick={() => setRemoteExpanded((value) => !value)}
              >
                {remoteExpanded ? "收起" : "展开更多"}
              </button>
            )}
          </>
        ) : remoteFor === q.trim() ? (
          <p className="mt-2 text-micro text-text-2">Pixiv 上也没有找到相关标签。</p>
        ) : null}
      </div>

      {/* 图片区：勾选标签时按组合过滤；没勾选时看「默认状态」开关 */}
      <div className="-mx-6 mt-5 border-t border-line pt-4">
        {selectedTags.length > 0 || showAllTagsDefault ? (
          <ArtworkList
            ignoreQuery
            heading={
              selectedTags.length > 0
                ? `已选 ${selectedTags.length} 个标签`
                : "全部作品"
            }
            extraQuery={
              selectedTags.length > 0 ? { tags_all: selectedTags } : undefined
            }
          />
        ) : (
          <div className="px-6 py-10 text-center text-micro text-text-3">
            默认状态：不展示全部。勾选上面的标签后显示对应作品。
          </div>
        )}
      </div>

      {comboMenu && (
        <ContextMenu
          x={comboMenu.x}
          y={comboMenu.y}
          items={comboMenuItems}
          onClose={() => setComboMenu(null)}
        />
      )}

      <Dialog
        open={renameTarget !== null}
        title="重命名标签组合"
        confirmLabel="保存"
        busy={comboBusy}
        confirmDisabled={!renameValue.trim()}
        onCancel={() => setRenameTarget(null)}
        onConfirm={confirmRename}
      >
        <div className="w-full">
          <input
            className="line-input"
            value={renameValue}
            maxLength={30}
            onChange={(event) => setRenameValue(event.currentTarget.value)}
          />
        </div>
      </Dialog>

      <Dialog
        open={deleteTarget !== null}
        title="删除标签组合"
        danger
        confirmLabel="删除"
        busy={comboBusy}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={confirmDelete}
      >
        <p>
          将删除组合「{deleteTarget?.name}」。只删除这个组合，不影响标签和作品。
        </p>
      </Dialog>
    </div>
  );
}
