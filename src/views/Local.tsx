import { ArtworkList } from "../components/wall/ArtworkList";

/**
 * 本地作品：文件名里拿不到 Pixiv 作品 ID、入库时派生了负数 ID 的图片。
 * 画廊与作者/标签/收藏夹视图都会把这些过滤掉，只有这里能看到。
 */
export function Local() {
  return <ArtworkList extraQuery={{ scope: "local" }} />;
}