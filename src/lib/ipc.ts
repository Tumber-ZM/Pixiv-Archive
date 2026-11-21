import { invoke } from "@tauri-apps/api/core";

import type { AppError } from "../types/ipc";

/** command 名唯一真源。T08 用 scripts/check-ipc.mjs 与 Rust 侧求差。 */
export const CMD = {
  listArtworks: "list_artworks",
  searchArtworks: "search_artworks",
  getArtwork: "get_artwork",
  getStats: "get_stats",
  deleteArtwork: "delete_artwork",
  batchDeleteArtworks: "batch_delete_artworks",
  listFavorites: "list_favorites",
  createFavorite: "create_favorite",
  deleteFavorite: "delete_favorite",
  addToFavorite: "add_to_favorite",
  removeFromFavorite: "remove_from_favorite",
  batchRemoveFromFavorite: "batch_remove_from_favorite",
  listAuthors: "list_authors",
  listTags: "list_tags",
  listTagPreviews: "list_tag_previews",
  listAuthorTags: "list_author_tags",
  listAuthorLocalPixivIds: "list_author_local_pixiv_ids",
  listTagCombos: "list_tag_combos",
  saveTagCombo: "save_tag_combo",
  renameTagCombo: "rename_tag_combo",
  deleteTagCombo: "delete_tag_combo",
  toggleTagComboPin: "toggle_tag_combo_pin",
  getAuthorAvatar: "get_author_avatar",
  refreshAuthorAvatars: "refresh_author_avatars",
  listSubs: "list_subs",
  addSub: "add_sub",
  removeSub: "remove_sub",
  toggleSubAuto: "toggle_sub_auto",
  resetSubBaseline: "reset_sub_baseline",
  previewSub: "preview_sub",
  pixivFavoritesStatus: "pixiv_favorites_status",
  syncPixivFavorites: "sync_pixiv_favorites",
  searchPixivAuthors: "search_pixiv_authors",
  searchPixivIllusts: "search_pixiv_illusts",
  listPixivAuthorIllusts: "list_pixiv_author_illusts",
  listPixivAuthorIllustsPage: "list_pixiv_author_illusts_page",
  searchPixivTags: "search_pixiv_tags",
  fetchRemoteThumbnail: "fetch_remote_thumbnail",
  downloadPixivIllust: "download_pixiv_illust",
  scanDirectory: "scan_directory",
  cancelScan: "cancel_scan",
  scanStatus: "scan_status",
  openInFolder: "open_in_folder",
  pickDirectory: "pick_directory",
  defaultScanRoot: "default_scan_root",
  pickFile: "pick_file",
  dataSizes: "data_sizes",
  exportDataBundle: "export_data_bundle",
  importDataBundle: "import_data_bundle",
  relinkImages: "relink_images",
  getSettings: "get_settings",
  saveSettings: "save_settings",
  syncMetadata: "sync_metadata",
  checkSub: "check_sub",
  checkAllSubs: "check_all_subs",
  repairIncompleteImages: "repair_incomplete_images",
  syncArtworks: "sync_artworks",
  regenerateThumbnails: "regenerate_thumbnails",
  importLegacy: "import_legacy",
  appInfo: "app_info",
  checkUpdate: "check_update",
} as const;

/**
 * `invoke` 抛出来的不是 `Error` 实例而是 `{ code, message }` 对象，
 * 这里统一归一，调用方只需要 catch `AppError`。
 */
export async function call<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  try {
    return await invoke<T>(cmd, args);
  } catch (error) {
    if (error && typeof error === "object" && "code" in error) {
      throw error as AppError;
    }
    throw { code: "INTERNAL", message: String(error) } satisfies AppError;
  }
}

export function isAppError(value: unknown): value is AppError {
  return !!value && typeof value === "object" && "code" in value && "message" in value;
}