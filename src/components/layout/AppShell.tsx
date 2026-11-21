import type { ComponentType } from "react";

import { useUi, type ViewKey } from "../../stores/ui";
import { Authors } from "../../views/Authors";
import { Subs } from "../../views/Subs";
import { Favorites } from "../../views/Favorites";
import { Gallery } from "../../views/Gallery";
import { Local } from "../../views/Local";
import { Tags } from "../../views/Tags";
import { SettingsPanel } from "../settings/SettingsPanel";
import { Sidebar } from "../sidebar/Sidebar";
import { Topbar } from "../topbar/Topbar";

const VIEWS: Record<ViewKey, ComponentType> = {
  gallery: Gallery,
  authors: Authors,
  tags: Tags,
  favorites: Favorites,
  subs: Subs,
  local: Local,
};

export function AppShell() {
  const view = useUi((state) => state.view);
  const View = VIEWS[view];

  return (
    <div className="flex h-full overflow-hidden bg-paper text-text">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <main className="min-h-0 flex-1 overflow-y-auto">
          <View />
        </main>
      </div>
      <SettingsPanel />
    </div>
  );
}