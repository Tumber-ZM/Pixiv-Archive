import { ArtworkList } from "../components/wall/ArtworkList";

/** 画廊视图：无额外固定筛选，所有条件都由顶栏与侧栏控制。 */
export function Gallery() {
  return <ArtworkList remoteSearch />;
}