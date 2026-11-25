import { useState } from "react";
import { Search } from "lucide-react";

import { CMD, call, isAppError } from "../../lib/ipc";
import { useUi, type PageSearchKey, type ViewKey } from "../../stores/ui";

const PLACEHOLDERS: Record<PageSearchKey, string> = {
  authors: "搜索作者…",
  tags: "搜索标签…",
  favorites: "搜索本地收藏…",
  bookmarkSubs: "搜索订阅…",
};

/** 该视图的顶栏是否由页面自己的搜索框接管 */
export function isPageSearchView(view: ViewKey): view is PageSearchKey {
  return view === "authors" || view === "tags" || view === "favorites" || view === "bookmarkSubs";
}

/** 作者页顶栏右侧的「更新作者头像」：拉取结果靠 dataEpoch 让作者列表自动重取。 */
function RefreshAvatarsButton() {
  const [refreshing, setRefreshing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const setScanTask = useUi((state) => state.setScanTask);
  const bumpData = useUi((state) => state.bumpData);

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setNotice(null);
    setScanTask({ current: 0, total: 0, imported: 0, message: "更新作者头像…", kind: "subs" });
    try {
      const updated = await call<number>(CMD.refreshAuthorAvatars);
      bumpData();
      setNotice(`已更新 ${updated} 个头像`);
      setTimeout(() => setNotice(null), 4000);
    } catch (reason: unknown) {
      setNotice(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setRefreshing(false);
      setScanTask(null);
    }
  };

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        className="tri-btn shrink-0"
        onClick={refresh}
        disabled={refreshing}
        title="用作者 ID 从 Pixiv 重新获取并覆盖本地头像"
      >
        {refreshing ? "更新中…" : "更新作者头像"}
      </button>
      {notice && <span className="text-micro text-text-2">{notice}</span>}
    </div>
  );
}

/** 作者 / 标签 / 本地收藏 / 订阅页的顶栏：放该页自己的搜索框，替代全局筛选条 */
export function PageTopbar({ view }: { view: PageSearchKey }) {
  const value = useUi((state) => state.pageSearch[view]);
  const setPageSearch = useUi((state) => state.setPageSearch);
  const placeholder = PLACEHOLDERS[view];

  return (
    <header className="flex h-topbar shrink-0 items-center gap-3 border-b border-line bg-paper px-6">
      <div className="flex w-[320px] items-center gap-2">
        <Search size={16} className="shrink-0 text-text-3" />
        <input
          className="line-input"
          placeholder={placeholder}
          value={value}
          onChange={(event) => setPageSearch(view, event.currentTarget.value)}
          aria-label={placeholder}
        />
      </div>
      {view === "authors" && <RefreshAvatarsButton />}
    </header>
  );
}