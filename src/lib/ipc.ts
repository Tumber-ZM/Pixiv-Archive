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
  getAuthorAvatar: "get_author_avatar",
  refreshAuthorAvatars: "refresh_author_avatars",
  listBookmarkSubs: "list_bookmark_subs",
  addBookmarkSub: "add_bookmark_sub",
  removeBookmarkSub: "remove_bookmark_sub",
  toggleBookmarkSubAuto: "toggle_bookmark_sub_auto",
  previewBookmarkSub: "preview_bookmark_sub",
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
  checkBookmarkSub: "check_bookmark_sub",
  checkAllBookmarkSubs: "check_all_bookmark_subs",
  syncArtworks: "sync_artworks",
  regenerateThumbnails: "regenerate_thumbnails",
  repairIncompleteImages: "repair_incomplete_images",
  takeStartupNotice: "take_startup_notice",
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