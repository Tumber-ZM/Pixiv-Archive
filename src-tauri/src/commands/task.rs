use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use tauri::State;

/// 任务槽：扫描 / 同步 / 下载 / 头像刷新各自一把锁。
///
/// 同步和下载可以同时跑（共享网络限速器而不是共享锁），同一类任务仍然串行，
/// 避免两个同步任务把同一批作品写两遍。
#[derive(Default)]
pub struct TaskCancel(pub Mutex<HashMap<&'static str, Arc<AtomicBool>>>);

impl TaskCancel {
    /// 占位并返回取消标志；同类任务已在跑时返回 None。
    pub fn begin(&self, kind: &'static str) -> Option<Arc<AtomicBool>> {
        let mut guard = self.0.lock().ok()?;
        if guard.contains_key(kind) {
            return None;
        }
        let flag = Arc::new(AtomicBool::new(false));
        guard.insert(kind, flag.clone());
        Some(flag)
    }

    pub fn end(&self, kind: &'static str) {
        if let Ok(mut guard) = self.0.lock() {
            guard.remove(kind);
        }
    }

    pub fn is_running(&self) -> bool {
        self.0
            .lock()
            .map(|guard| !guard.is_empty())
            .unwrap_or(false)
    }

    /// 正在跑的任务类型，前端据此显示进度浮窗。
    pub fn running(&self) -> Vec<String> {
        self.0
            .lock()
            .map(|guard| guard.keys().map(|key| key.to_string()).collect())
            .unwrap_or_default()
    }

    /// 取消全部在跑的任务：前端只有一个取消按钮，语义就是「都停下」。
    pub fn request_cancel(&self) -> bool {
        if let Ok(guard) = self.0.lock() {
            if guard.is_empty() {
                return false;
            }
            for flag in guard.values() {
                flag.store(true, Ordering::Relaxed);
            }
            return true;
        }
        false
    }
}

/// 取消当前所有长任务（扫描 / 同步 / 订阅下载都适用）。没有任务在跑时返回 false。
#[tauri::command]
pub fn cancel_task(state: State<'_, TaskCancel>) -> bool {
    state.request_cancel()
}

#[tauri::command]
pub fn task_running(state: State<'_, TaskCancel>) -> bool {
    state.is_running()
}

/// 返回正在跑的任务类型列表（可能同时有 `sync` 和 `download`）。
#[tauri::command]
pub fn task_kinds(state: State<'_, TaskCancel>) -> Vec<String> {
    state.running()
}