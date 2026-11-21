import { useEffect, useRef, useState } from "react";
import {
  Bookmark,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Pin,
  Plus,
  RotateCcw,
  Save,
  X,
} from "lucide-react";

import { ContextMenu, type MenuItem } from "../components/common/ContextMenu";
import { Dialog } from "../components/dialog/Dialog";
import { ArtworkList } from "../components/wall/ArtworkList";
import { CMD, call, isAppError } from "../lib/ipc";
import { useUi } from "../stores/ui";
import type { FavoriteSyncStatus, RemoteTag, TagCombo, TagRow } from "../types/ipc";
import { RemoteTagView } from "./RemoteTagView";
import { TagPreviewGrid } from "./TagPreviewGrid";

const SEARCH_DEBOUNCE_MS = 300;
const TAG_ROW_HEIGHT = 36;
// 本地标签初始固定 1 行，避免异步加载完成后把下面的图片区顶下去
const DEFAULT_ROWS = 1;
const MORE_ROWS = 4;
type LocalRowsMode = "collapsed" | "all";

/** 当前正在看的标签。跳转只需要名字，不必带完整 TagRow。 */
interface CurrentTag {
  name: string;
  label: string;
}


export function Tags() {
  const q = useUi((state) => state.q);
  const [rows, setRows] = useState<TagRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<CurrentTag | null>(null);
  const [remoteCurrent, setRemoteCurrent] = useState<RemoteTag | null>(null);

  const [onlyCollected, setOnlyCollected] = useState(false);
  const [onlySubscribed, setOnlySubscribed] = useState(false);
  const [favoriteStatus, setFavoriteStatus] = useState<FavoriteSyncStatus | null>(null);
  const favoritesSynced = (favoriteStatus?.count ?? 0) > 0;

  const [combos, setCombos] = useState<TagCombo[]>([]);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  // 顶栏黄色常驻；active=true 表示这次随机还在“预览覆盖”状态，false 表示只是候选
  const [randomActive, setRandomActive] = useState(false);
  // 通过搜索/随机得到的标签：置顶显示，方便继续操作；手动翻到的选中标签留在原位
  const [priorityTags, setPriorityTags] = useState<string[]>([]);
  const [comboName, setComboName] = useState("");
  const [comboBusy, setComboBusy] = useState(false);
  const [comboError, setComboError] = useState<string | null>(null);
  const [combosOpen, setCombosOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [activeComboId, setActiveComboId] = useState<number | null>(null);
  const [comboMenu, setComboMenu] = useState<{
    x: number;
    y: number;
    combo: TagCombo;
  } | null>(null);
  const [renameTarget, setRenameTarget] = useState<TagCombo | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<TagCombo | null>(null);
  const [expandedCombos, setExpandedCombos] = useState<Set<number>>(new Set());

  const [localMode, setLocalMode] = useState<LocalRowsMode>("collapsed");
  const [totalRows, setTotalRows] = useState(0);
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
  const randomTag = useUi((state) => state.randomTag);
  const tagHistory = useUi((state) => state.tagHistory);
  const pushTagHistory = useUi((state) => state.pushTagHistory);

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
        count: row.count,
      });
      pushTagHistory({
        name: row.name,
        label: row.translated_name ?? row.name,
      });
      setRandomActive(true);
    };
    window.addEventListener("pa-tags-random", handler);
    return () => window.removeEventListener("pa-tags-random", handler);
  }, [rows, selectedTags, setRandomTag, pushTagHistory]);

  // 顶栏黄色按钮：点击加入 / 取消随机标签；黄色本身常驻
  useEffect(() => {
    const handler = () => {
      if (!randomTag) return;
      const adding = !selectedTags.includes(randomTag.name);
      setRandomActive(false);
      setSelectedTags((prev) =>
        adding
          ? [...prev, randomTag.name]
          : prev.filter((tag) => tag !== randomTag.name),
      );
      setPriorityTags((prev) =>
        adding
          ? prev.includes(randomTag.name)
            ? prev
            : [randomTag.name, ...prev]
          : prev.filter((tag) => tag !== randomTag.name),
      );
    };
    window.addEventListener("pa-tags-toggle-random", handler);
    return () => window.removeEventListener("pa-tags-toggle-random", handler);
  }, [randomTag, selectedTags]);

  // Pixiv 标签默认只显示一行
  useEffect(() => {
    const el = remoteBoxRef.current;
    if (!el) {
      setRemoteHasOverflow(false);
      return undefined;
    }
    const update = () => {
      setRemoteHasOverflow(el.scrollHeight > TAG_ROW_HEIGHT + 1);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [remoteTags, remoteExpanded, remoteFor, remoteLoading, remoteError]);

  useEffect(() => {
    setRemoteExpanded(false);
  }, [q]);

  // 标签按钮云总共有多少行（用于决定「展开 / 展开（全部）」）
  useEffect(() => {
    const el = tagBoxRef.current;
    if (!el) {
      setTotalRows(0);
      return undefined;
    }
    const update = () => {
      setTotalRows(Math.max(1, Math.ceil(el.scrollHeight / TAG_ROW_HEIGHT)));
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [rows, priorityTags, randomTag, localMode, q, loading, error]);

  const labelFor = (name: string) => {
    const row = rows.find((item) => item.name === name);
    return row?.translated_name ?? name;
  };

  const removeTag = (name: string) => {
    const next = selectedTags.filter((tag) => tag !== name);
    if (next.length === 0) setActiveComboId(null);
    setSelectedTags(next);
    setPriorityTags((prev) => prev.filter((tag) => tag !== name));
    setRandomActive(false);
  };

  const handleTagClick = (name: string) => {
    const historyLabel = tagHistory.find((item) => item.name === name)?.label;
    pushTagHistory({
      name,
      label:
        randomTag?.name === name
          ? randomTag.label
          : historyLabel ?? labelFor(name),
    });
    if (randomTag?.name === name) {
      // 黄色随机标签：点击加入组合并变蓝；再点取消，黄色候选保留
      setRandomActive(false);
      const adding = !selectedTags.includes(name);
      setSelectedTags((prev) =>
        prev.includes(name) ? prev.filter((tag) => tag !== name) : [...prev, name],
      );
      setPriorityTags((prev) =>
        adding
          ? prev.includes(name)
            ? prev
            : [name, ...prev]
          : prev.filter((tag) => tag !== name),
      );
      return;
    }
    // 点其他标签只切换这个标签，不再清掉顶栏的随机标签
    setRandomActive(false);
    const adding = !selectedTags.includes(name);
    setSelectedTags((prev) =>
      prev.includes(name) ? prev.filter((tag) => tag !== name) : [...prev, name],
    );
    setPriorityTags((prev) => {
      if (!adding) return prev.filter((tag) => tag !== name);
      // 搜索状态点中的标签置顶；手动翻到的保持在原位
      if (q.trim() && !prev.includes(name)) return [...prev, name];
      return prev;
    });
  };

  const applyCombo = (combo: TagCombo) => {
    setSelectedTags([...combo.tags]);
    setPriorityTags([]);
    setRandomActive(false);
    setActiveComboId(combo.id);
    setCombosOpen(false);
    setComboError(null);
  };

  const saveCombo = async () => {
    if (!selectedTags.length || comboBusy) return false;
    setComboBusy(true);
    setComboError(null);
    try {
      const combo = await call<TagCombo>(CMD.saveTagCombo, {
        name: comboName.trim() || null,
        tags: selectedTags,
      });
      setComboName("");
      setActiveComboId(combo.id);
      await loadCombos();
      return true;
    } catch (reason: unknown) {
      setComboError(isAppError(reason) ? reason.message : String(reason));
      return false;
    } finally {
      setComboBusy(false);
    }
  };

  const focusTagSearch = () => {
    const input = document.querySelector<HTMLInputElement>("header input");
    input?.focus();
    tagBoxRef.current?.scrollIntoView({ block: "nearest" });
  };

  const addRemoteTagToCombo = (tag: RemoteTag) => {
    setRandomActive(false);
    setSelectedTags((prev) =>
      prev.includes(tag.name) ? prev : [...prev, tag.name],
    );
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

  if (remoteCurrent) {
    return (
      <RemoteTagView
        tag={remoteCurrent}
        onBack={() => setRemoteCurrent(null)}
        onAdd={() => addRemoteTagToCombo(remoteCurrent)}
        inCombo={selectedTags.includes(remoteCurrent.name)}
      />
    );
  }

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
  const galleryTags =
    randomActive && randomTag ? [randomTag.name] : selectedTags;
  const rowByName = new Map(rows.map((row) => [row.name, row]));
  const priorityNames: string[] = [];
  if (randomTag) priorityNames.push(randomTag.name);
  for (const name of priorityTags) {
    if (!priorityNames.includes(name)) priorityNames.push(name);
  }
  const prioritySet = new Set(priorityNames);
  const priorityRows: TagRow[] = priorityNames.map((name, index) => {
    const row = rowByName.get(name);
    if (row) return row;
    return {
      id: -100000 - index,
      name,
      translated_name: randomTag?.name === name ? randomTag.label : null,
      count: randomTag?.name === name ? randomTag.count : 0,
      cover_path: null,
      pixiv_favorited: false,
    };
  });
  const displayRows = [
    ...priorityRows,
    ...rows.filter((row) => !prioritySet.has(row.name)),
  ];

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
              : "还没同步 Pixiv 收藏，先按订阅前就有的作品筛选"
          }
          onClick={() => setOnlyCollected((value) => !value)}
        >
          仅看 Pixiv 收藏
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

        {/* 已保存的标签组合：和筛选同一行，宽度随内容增长 */}
        <div className="combo-box relative flex w-fit max-w-full flex-wrap items-center gap-2 rounded-card border border-line bg-surface px-2">
          <button
            type="button"
            className="order-1 flex items-center gap-1.5 text-label text-text"
            onClick={() => setCombosOpen((value) => !value)}
            title={combosOpen ? "收起已保存的标签组合" : "展开已保存的标签组合"}
          >
            <Bookmark size={13} />
            已保存的标签组合
            {combosOpen ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
          </button>
          <span className="order-2 h-4 w-px bg-line" aria-hidden />
          {selectedTags.length === 0 ? (
            <span className="order-4 text-micro text-text-3">未选择标签</span>
          ) : (
            selectedTags.map((tag) => (
              <button
                key={tag}
                type="button"
                className="tag-pick order-4"
                data-active="true"
                title={`点击取消选择 ${tag}`}
                onClick={() => removeTag(tag)}
              >
                <span className="tag-pick-label"># {labelFor(tag)}</span>
                <X size={10} />
              </button>
            ))
          )}
          <button
            type="button"
            className="tag-pick order-5"
            onClick={focusTagSearch}
            title="到标签区搜索并添加标签"
          >
            <Plus size={10} />
            添加标签
          </button>
          <div className="order-3 flex items-center gap-2">
            <button
              type="button"
              className="order-2 inline-flex items-center gap-1 text-micro text-text-3 hover:text-text disabled:opacity-40"
              disabled={selectedTags.length === 0}
              onClick={() => {
                setSelectedTags([]);
                setPriorityTags([]);
                setRandomActive(false);
                setActiveComboId(null);
              }}
            >
              清空
              <RotateCcw size={11} />
            </button>
            <div className="relative order-1">
              <button
                type="button"
                className="tri-btn tri-btn-accent inline-flex items-center gap-1"
                disabled={selectedTags.length === 0 || comboBusy}
                onClick={() => setSaveOpen((value) => !value)}
                title="把当前标签保存成组合"
              >
                <Save size={12} />
                保存为组合
                <ChevronDown size={10} />
              </button>
              {saveOpen && (
                <div className="absolute right-0 top-full z-30 mt-1 w-[240px] rounded-card border border-line bg-surface p-2 shadow-lg">
                  <input
                    className="line-input"
                    placeholder="组合名称（可留空）"
                    value={comboName}
                    maxLength={30}
                    autoFocus
                    onChange={(event) => setComboName(event.currentTarget.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !comboBusy) {
                        void saveCombo().then((ok) => {
                          if (ok) setSaveOpen(false);
                        });
                      }
                    }}
                  />
                  <div className="mt-2 flex justify-end gap-2">
                    <button
                      type="button"
                      className="tri-btn"
                      onClick={() => setSaveOpen(false)}
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      className="tri-btn tri-btn-accent"
                      disabled={comboBusy}
                      onClick={async () => {
                        const ok = await saveCombo();
                        if (ok) setSaveOpen(false);
                      }}
                    >
                      保存
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        {comboError && (
          <p className="order-6 w-full text-micro text-danger">{comboError}</p>
        )}
        </div>

        {tagHistory.length > 0 && (
          <div className="flex shrink-0 items-center gap-2">
            <span className="text-micro text-text-3">历史标签</span>
            {tagHistory.map((item) => {
              const selected = selectedTags.includes(item.name);
              return (
                <button
                  key={item.name}
                  type="button"
                  className="tag-pick"
                  data-active={selected}
                  onClick={() => handleTagClick(item.name)}
                  title={`历史标签 #${item.label}`}
                >
                  <span className="tag-pick-label"># {item.label}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>

        {combosOpen && (
          <div className="mt-2 border-t border-line pt-2">
            {combos.length === 0 ? (
              <p className="text-micro text-text-3">
                还没有保存的标签组合。选好标签后点「保存为组合」。
              </p>
            ) : (
              <div className="flex flex-wrap gap-2">
                {combos.map((combo) => {
              const expanded = expandedCombos.has(combo.id);
              const active = activeComboId === combo.id;
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
        )}

      {/* 标签按钮云 */}
      <div className="mt-5">
        <div className="flex h-7 items-center gap-2">
          <span className="section-label">标签</span>
          {!searching && totalRows > DEFAULT_ROWS && (
            <button
              type="button"
              className="section-more"
              onClick={() =>
                setLocalMode(localMode === "all" ? "collapsed" : "all")
              }
              title={
                localMode === "all" ? "收起本地标签" : "展开全部本地标签"
              }
            >
              {localMode === "all" ? (
                <>
                  <ChevronUp size={10} />
                  收起
                </>
              ) : (
                <>
                  <ChevronDown size={10} />
                  {totalRows > MORE_ROWS ? "展开（全部）" : "展开"}
                </>
              )}
            </button>
          )}
          <span className="min-w-0 truncate text-micro text-text-3">
            {q.trim()
              ? `搜索「${q.trim()}」的本地结果 · ${rows.length} 个`
              : `按本地数量排序 · ${rows.length} 个`}
          </span>
          {searching && (
            <span className="ml-auto shrink-0 text-micro text-text-3">
              全部搜索结果
            </span>
          )}
        </div>

        {error ? (
          <div
            className="flex h-9 items-center overflow-hidden text-micro text-text-2"
            title={error}
          >
            <span className="truncate">加载失败：{error}</span>
          </div>
        ) : loading ? (
          <div className="flex h-9 items-center text-micro text-text-2">
            加载中…
          </div>
        ) : rows.length === 0 ? (
          <div className="flex h-9 items-center text-micro text-text-2">
            {q.trim()
              ? "没有找到标签，换个关键词或调整筛选条件"
              : "还没有标签，先同步元数据"}
          </div>
        ) : (
          <>
            <div
              ref={tagBoxRef}
              className={`flex flex-wrap content-start gap-2 ${searching || localMode === "all" ? "" : "overflow-hidden"}`}
              style={
                searching || localMode === "all"
                  ? undefined
                  : {
                      maxHeight: DEFAULT_ROWS * TAG_ROW_HEIGHT,
                      minHeight:
                        localMode === "collapsed"
                          ? TAG_ROW_HEIGHT
                          : undefined,
                    }
              }
            >
              {displayRows.map((row) => {
                const label = row.translated_name ?? row.name;
                const selected = selectedTags.includes(row.name);
                const isRandom = randomTag?.name === row.name;
                const randomVisual =
                  isRandom && !selected
                    ? randomActive
                      ? "true"
                      : "dim"
                    : undefined;
                return (
                  <button
                    key={row.id}
                    type="button"
                    className="tag-pick"
                    data-active={selected}
                    data-random={randomVisual}
                    aria-pressed={selected}
                    onClick={() => handleTagClick(row.name)}
                    title={
                      isRandom
                        ? `随机标签 #${label}：点击加入 / 取消组合`
                        : `点击选中 / 取消 #${label}（${row.count} 件）`
                    }
                  >
                    <span className="tag-pick-label"># {label}</span>
                    {row.count > 0 && (
                      <span className="tag-pick-count">{row.count}</span>
                    )}
                  </button>
                );
              })}
            </div>
          </>
        )}
      </div>

      {/* Pixiv 上的标签：固定一行高度，异步返回时不会顶动下面的图片区 */}
      <div className="mt-4">
        <div className="flex h-7 items-center gap-2">
          <span className="section-label">Pixiv 的标签</span>
          {!remoteLoading &&
            !remoteError &&
            remoteTags.length > 0 &&
            (remoteHasOverflow || remoteExpanded) && (
              <button
                type="button"
                className="section-more"
                onClick={() => setRemoteExpanded((value) => !value)}
                title={remoteExpanded ? "收起 Pixiv 标签" : "展开 Pixiv 标签"}
              >
                {remoteExpanded ? (
                  <>
                    <ChevronUp size={10} />
                    收起
                  </>
                ) : (
                  <>
                    <ChevronDown size={10} />
                    展开
                  </>
                )}
              </button>
            )}
        </div>
        {remoteError ? (
          <div
            className="flex h-9 items-center overflow-hidden text-micro text-text-2"
            title={remoteError}
          >
            <span className="truncate">无信息</span>
          </div>
        ) : remoteLoading || remoteFor !== q.trim() ? (
          <div className="flex h-9 items-center text-micro text-text-2">
            获取中…
          </div>
        ) : remoteTags.length === 0 ? (
          <div className="flex h-9 items-center text-micro text-text-2">
            无信息
          </div>
        ) : (
          <div
            ref={remoteBoxRef}
            className={`flex flex-wrap content-start gap-2 ${remoteExpanded ? "" : "overflow-hidden"}`}
            style={
              remoteExpanded
                ? undefined
                : {
                    maxHeight: TAG_ROW_HEIGHT,
                    minHeight: TAG_ROW_HEIGHT,
                  }
            }
          >
            {remoteTags.map((tag) => {
              const label = tag.translated_name ?? tag.name;
              return (
                <button
                  key={tag.name}
                  type="button"
                  className="tag-pick"
                  data-source="pixiv"
                  onClick={() => setRemoteCurrent(tag)}
                  title={`查看 Pixiv 线上作品 #${label}`}
                >
                  <span className="tag-pick-label"># {label}</span>
                  {tag.count > 0 && (
                    <span className="tag-pick-count">{tag.count}</span>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* 没选标签时展示可视化标签图；选标签后展示作品；顶栏「默认状态」可切回全部作品 */}
      <div className="-mx-6 mt-5 border-t border-line pt-4">
        {galleryTags.length > 0 ? (
          <ArtworkList
            ignoreQuery
            heading={
              randomActive && randomTag
                ? `随机标签：# ${randomTag.label}`
                : `已选 ${selectedTags.length} 个标签`
            }
            extraQuery={{ tags_all: galleryTags }}
          />
        ) : showAllTagsDefault ? (
          <ArtworkList ignoreQuery heading="全部作品" />
        ) : (
          <TagPreviewGrid
            q={q}
            onlyCollected={onlyCollected}
            favoritesSynced={favoritesSynced}
            onlySubscribed={onlySubscribed}
            selectedTags={selectedTags}
            onToggle={handleTagClick}
          />
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
