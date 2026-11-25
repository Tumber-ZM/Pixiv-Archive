import type { ComponentType } from "react";

import { useUi, type ViewKey } from "../../stores/ui";
import { Authors } from "../../views/Authors";
import { BookmarkSubs } from "../../views/BookmarkSubs";
import { Favorites } from "../../views/Favorites";
import { Gallery } from "../../views/Gallery";
import { Local } from "../../views/Local";
import { Tags } from "../../views/Tags";
import { SettingsPanel } from "../settings/SettingsPanel";
import { Sidebar } from "../sidebar/Sidebar";
import { PageTopbar, isPageSearchView } from "../topbar/PageTopbar";
import { Topbar } from "../topbar/Topbar";

const VIEWS: Record<ViewKey, ComponentType> = {
  gallery: Gallery,
  authors: Authors,
  tags: Tags,
  favorites: Favorites,
  bookmarkSubs: BookmarkSubs,
  local: Local,
};

export function AppShell() {
  const view = useUi((state) => state.view);
  const nestedView = useUi((state) => state.nestedView);
  const View = VIEWS[view];

  return (
    <div className="flex h-full overflow-hidden bg-paper text-text">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        {/* 列表页用页面自己的搜索框顶替全局筛选条；进入子列表后交还 */}
        {isPageSearchView(view) && !nestedView ? <PageTopbar view={view} /> : <Topbar />}
        <main className="min-h-0 flex-1 overflow-y-auto">
          <View />
        </main>
      </div>
      <SettingsPanel />
    </div>
  );
}