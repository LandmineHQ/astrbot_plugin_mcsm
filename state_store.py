from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

PERMISSION_DEFINITIONS: list[dict[str, str]] = [
    {
        "key": "view",
        "label": "查看实例",
        "description": "读取实例状态与详细配置。",
    },
    {
        "key": "console",
        "label": "读取控制台",
        "description": "读取实例控制台日志与输出。",
    },
    {
        "key": "command",
        "label": "发送指令",
        "description": "向实例控制台发送命令。",
    },
    {
        "key": "power",
        "label": "启停控制",
        "description": "启动、停止、重启、强制终止和更新实例。",
    },
    {
        "key": "file_read",
        "label": "读取文件",
        "description": "列出、读取、下载实例文件和配置。",
    },
    {
        "key": "file_write",
        "label": "写入文件",
        "description": "写入、上传、删除、移动、压缩和 chmod。",
    },
    {
        "key": "admin",
        "label": "高级管理",
        "description": "允许通过通用 API 执行实例管理、镜像管理等高风险操作。",
    },
]

DEFAULT_PERMISSIONS: dict[str, bool] = {
    "view": True,
    "console": True,
    "command": False,
    "power": False,
    "file_read": True,
    "file_write": False,
    "admin": False,
}

PERMISSION_KEYS = {item["key"] for item in PERMISSION_DEFINITIONS}
CURRENT_STATE_VERSION = 2


def _default_state() -> dict[str, Any]:
    return {
        "version": CURRENT_STATE_VERSION,
        "bindings": {},
        "binding_permissions": {},
    }


def _normalize_permission_values(values: Any) -> dict[str, bool]:
    merged = copy.deepcopy(DEFAULT_PERMISSIONS)
    if isinstance(values, dict):
        for key in PERMISSION_KEYS:
            if isinstance(values.get(key), bool):
                merged[key] = values[key]
    return merged


class PluginStateStore:
    """Persisted bindings and permissions scoped by session and instance."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._state, migrated = self._load()
        if migrated:
            self._write_state_sync(self._state)

    def _load(self) -> tuple[dict[str, Any], bool]:
        raw: dict[str, Any] = {}
        migrated = False
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
                    migrated = (
                        raw.get("version") != CURRENT_STATE_VERSION
                        or "instance_permissions" in raw
                        or "binding_permissions" not in raw
                    )
        except Exception:
            # Keep the plugin usable if a state file was partially corrupted.
            raw = {}
        return self._normalize(raw), migrated

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        state = _default_state()

        bindings = raw.get("bindings")
        if isinstance(bindings, dict):
            normalized_bindings: dict[str, list[str]] = {}
            for session, values in bindings.items():
                if not isinstance(session, str) or not session:
                    continue
                if not isinstance(values, list):
                    continue
                keys: list[str] = []
                for value in values:
                    key = str(value or "").strip()
                    if key and key not in keys:
                        keys.append(key)
                normalized_bindings[session] = keys
            state["bindings"] = normalized_bindings

        normalized_permissions: dict[str, dict[str, dict[str, bool]]] = {}
        raw_permissions = raw.get("binding_permissions")
        if isinstance(raw_permissions, dict):
            for session, instance_values in raw_permissions.items():
                if not isinstance(session, str) or not session:
                    continue
                if not isinstance(instance_values, dict):
                    continue
                for instance_key, values in instance_values.items():
                    key = str(instance_key or "").strip()
                    if not key:
                        continue
                    normalized_permissions.setdefault(session, {})[key] = (
                        _normalize_permission_values(values)
                    )

        # v1 stored one global permission set per instance. Migrate that set to
        # every session currently bound to the instance.
        legacy_permissions = raw.get("instance_permissions")
        if isinstance(legacy_permissions, dict):
            for session, instance_keys in state["bindings"].items():
                for instance_key in instance_keys:
                    legacy = legacy_permissions.get(instance_key)
                    if not isinstance(legacy, dict):
                        continue
                    normalized_permissions.setdefault(session, {}).setdefault(
                        instance_key, _normalize_permission_values(legacy)
                    )

        state["binding_permissions"] = normalized_permissions
        return state

    def _write_state_sync(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.path)

    async def save(self) -> None:
        async with self._lock:
            self._write_state_sync(self._state)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    def binding_keys(self) -> dict[str, list[str]]:
        return copy.deepcopy(self._state["bindings"])

    def binding_permissions(self) -> dict[str, dict[str, dict[str, bool]]]:
        return copy.deepcopy(self._state["binding_permissions"])

    def keys_for_session(self, session_keys: list[str]) -> list[str]:
        result: list[str] = []
        bindings = self._state["bindings"]
        for session_key in session_keys:
            for instance_key in bindings.get(session_key, []):
                if instance_key not in result:
                    result.append(instance_key)
        return result

    def session_binding_count(self, session_keys: list[str]) -> int:
        return len(self.keys_for_session(session_keys))

    async def bind(self, session_key: str, instance_key: str) -> bool:
        session_key = str(session_key or "").strip()
        instance_key = str(instance_key or "").strip()
        if not session_key or not instance_key:
            raise ValueError("会话与实例均不能为空。")
        async with self._lock:
            values = self._state["bindings"].setdefault(session_key, [])
            if instance_key in values:
                return False
            values.append(instance_key)
            await self._save_unlocked()
            return True

    async def reorder_bindings(
        self, session_key: str, ordered_keys: list[str]
    ) -> list[str]:
        session_key = str(session_key or "").strip()
        if not session_key:
            raise ValueError("会话不能为空。")
        async with self._lock:
            existing = self._state["bindings"].get(session_key, [])
            if not existing:
                return []
            existing_set = set(existing)
            normalized: list[str] = []
            for value in ordered_keys:
                key = str(value or "").strip()
                if key in existing_set and key not in normalized:
                    normalized.append(key)
            for key in existing:
                if key not in normalized:
                    normalized.append(key)
            self._state["bindings"][session_key] = normalized
            await self._save_unlocked()
            return list(normalized)

    async def unbind(self, session_key: str, instance_key: str) -> bool:
        session_key = str(session_key or "").strip()
        instance_key = str(instance_key or "").strip()
        async with self._lock:
            values = self._state["bindings"].get(session_key)
            if not values or instance_key not in values:
                return False
            values.remove(instance_key)
            if not values:
                self._state["bindings"].pop(session_key, None)
            await self._save_unlocked()
            return True

    def permissions_for(self, session_key: str, instance_key: str) -> dict[str, bool]:
        stored = (
            self._state["binding_permissions"]
            .get(str(session_key or ""), {})
            .get(str(instance_key or ""))
        )
        return _normalize_permission_values(stored)

    def permissions_for_any(
        self, session_keys: list[str], instance_key: str
    ) -> dict[str, bool]:
        for session_key in session_keys:
            stored = (
                self._state["binding_permissions"]
                .get(session_key, {})
                .get(instance_key)
            )
            if isinstance(stored, dict):
                return _normalize_permission_values(stored)
        return copy.deepcopy(DEFAULT_PERMISSIONS)

    async def set_permissions(
        self, session_key: str, instance_key: str, permissions: dict[str, Any]
    ) -> dict[str, bool]:
        session_key = str(session_key or "").strip()
        instance_key = str(instance_key or "").strip()
        if not session_key or not instance_key:
            raise ValueError("会话与实例均不能为空。")
        merged = self.permissions_for(session_key, instance_key)
        for key in PERMISSION_KEYS:
            if isinstance(permissions.get(key), bool):
                merged[key] = permissions[key]
        async with self._lock:
            self._state["binding_permissions"].setdefault(session_key, {})[
                instance_key
            ] = merged
            await self._save_unlocked()
        return copy.deepcopy(merged)

    async def _save_unlocked(self) -> None:
        self._write_state_sync(self._state)
