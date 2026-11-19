from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

try:
    from .mcsm_client import MCSMClient, MCSMError, instance_key, normalize_base_url
    from .sessions import discover_sessions, parse_umo
    from .state_store import (
        DEFAULT_PERMISSIONS,
        PERMISSION_DEFINITIONS,
        PluginStateStore,
    )
    from .tooling import build_tools
except ImportError:
    from mcsm_client import MCSMClient, MCSMError, instance_key, normalize_base_url
    from sessions import discover_sessions, parse_umo
    from state_store import (
        DEFAULT_PERMISSIONS,
        PERMISSION_DEFINITIONS,
        PluginStateStore,
    )
    from tooling import build_tools

PLUGIN_NAME = "astrbot_plugin_mcsm"
STATUS_TEXT = {
    -1: "繁忙",
    0: "已停止",
    1: "停止中",
    2: "启动中",
    3: "运行中",
}
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
MAX_LOG_CHARS = 40000
COMMAND_OUTPUT_DEFAULT_CHARS = 8000
COMMAND_OUTPUT_MAX_CHARS = 12000
COMMAND_OUTPUT_MIN_CHARS = 512
COMMAND_SNAPSHOT_TTL_SECONDS = 24 * 60 * 60
COMMAND_SNAPSHOT_MAX_FILES = 100


class MCSMAccessError(Exception):
    pass


class MCSMPlugin(Star):
    name = PLUGIN_NAME

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._data_dir = data_dir
        self._command_output_dir = data_dir / "command_outputs"
        self._terminal_locks: dict[str, asyncio.Lock] = {}
        self._state_store = PluginStateStore(data_dir / "state.json")
        self._client = self._make_client()
        self._register_web_apis()
        self.context.add_llm_tools(*build_tools(self))
        self.logger.info("MCSManager plugin initialized")

    def _make_client(self) -> MCSMClient:
        return MCSMClient(
            str(self.config.get("base_url") or ""),
            str(self.config.get("api_key") or ""),
            verify_ssl=bool(self.config.get("verify_ssl", True)),
            timeout=int(self.config.get("request_timeout", 20) or 20),
        )

    async def _replace_client(self) -> None:
        await self._client.close()
        self._client = self._make_client()

    def _register_web_apis(self) -> None:
        routes = [
            ("/bootstrap", self.page_bootstrap, ["GET"], "MCSM 插件初始化数据"),
            ("/settings/save", self.page_settings_save, ["POST"], "保存 MCSM 连接设置"),
            ("/settings/test", self.page_settings_test, ["POST"], "测试 MCSM 连接"),
            ("/bindings/add", self.page_bindings_add, ["POST"], "绑定实例到会话"),
            (
                "/bindings/remove",
                self.page_bindings_remove,
                ["POST"],
                "解除会话实例绑定",
            ),
            (
                "/bindings/reorder",
                self.page_bindings_reorder,
                ["POST"],
                "保存会话实例顺序",
            ),
            ("/permissions/save", self.page_permissions_save, ["POST"], "保存实例权限"),
        ]
        for suffix, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}{suffix}", handler, methods, description
            )

    def _whitelist(self) -> list[Any]:
        config = self.context.get_config()
        platform_settings = config.get("platform_settings", {}) or {}
        values = platform_settings.get("id_whitelist", [])
        return values if isinstance(values, list) else []

    def _settings_public(self) -> dict[str, Any]:
        return {
            "base_url": str(self.config.get("base_url") or ""),
            "verify_ssl": bool(self.config.get("verify_ssl", True)),
            "request_timeout": int(self.config.get("request_timeout", 20) or 20),
            "has_api_key": bool(str(self.config.get("api_key") or "").strip()),
            "api_key_masked": self._mask_secret(
                str(self.config.get("api_key") or "").strip()
            ),
            "configured": self._client.configured,
        }

    @staticmethod
    def _mask_secret(value: str) -> str:
        if not value:
            return ""
        if len(value) <= 8:
            return "••••••••"
        return f"{value[:4]}••••{value[-4:]}"

    def _public_instance(self, item: dict[str, Any]) -> dict[str, Any]:
        status = item.get("status", 0)
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            status_int = 0
        docker = item.get("docker") if isinstance(item.get("docker"), dict) else {}
        info = item.get("info") if isinstance(item.get("info"), dict) else {}
        config = item.get("config") if isinstance(item.get("config"), dict) else {}
        key = instance_key(item)
        return {
            "key": key,
            "instanceUuid": str(item.get("instanceUuid") or ""),
            "daemonId": str(item.get("daemonId") or ""),
            "nickname": str(
                item.get("nickname") or item.get("instanceUuid") or "未命名实例"
            ),
            "status": status_int,
            "status_text": STATUS_TEXT.get(status_int, "未知"),
            "process_type": str(item.get("processType") or "unknown"),
            "host_ip": str(item.get("hostIp") or ""),
            "remarks": str(item.get("remarks") or ""),
            "ie": str(item.get("ie") or ""),
            "oe": str(item.get("oe") or ""),
            "stopCommand": str(item.get("stopCommand") or ""),
            "endTime": item.get("endTime", 0),
            "lastDatetime": item.get("lastDatetime", 0),
            "docker": {
                "image": str(docker.get("image") or ""),
                "containerName": str(docker.get("containerName") or ""),
                "workingDir": str(docker.get("workingDir") or ""),
                "networkMode": str(docker.get("networkMode") or ""),
                "ports": docker.get("ports", []),
                "memory": docker.get("memory", 0),
                "cpuUsage": docker.get("cpuUsage", 0),
            },
            "info": {
                "mcPingOnline": bool(info.get("mcPingOnline", False)),
                "currentPlayers": info.get("currentPlayers", 0),
                "maxPlayers": info.get("maxPlayers", 0),
                "version": str(info.get("version") or ""),
                "latency": info.get("latency", 0),
                "storageUsage": info.get("storageUsage", 0),
                "storageLimit": info.get("storageLimit", 0),
            },
            "config": {
                "language": str(config.get("language") or ""),
                "outputBufferSize": config.get("outputBufferSize", 0),
                "maxDownloadFromUrlFileCount": config.get(
                    "maxDownloadFromUrlFileCount", 0
                ),
            },
        }

    async def _fetch_dashboard(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self._client.configured:
            return [], {}
        account_data = await self._client.auth_advanced()
        account = {
            "uuid": str(account_data.get("uuid") or ""),
            "userName": str(account_data.get("userName") or ""),
            "permission": account_data.get("permission"),
        }
        raw_instances = account_data.get("instances")
        if not isinstance(raw_instances, list):
            raw_instances = []
        instances = [
            self._public_instance(item)
            for item in raw_instances
            if isinstance(item, dict)
        ]
        instances.sort(
            key=lambda item: (item["status"] != 3, item["nickname"].casefold())
        )
        return instances, account

    async def page_bootstrap(self):
        instances: list[dict[str, Any]] = []
        account: dict[str, Any] = {}
        api_error = ""
        if self._client.configured:
            try:
                instances, account = await self._fetch_dashboard()
            except MCSMError as exc:
                api_error = str(exc)
        sessions = await discover_sessions(
            self.context, self._state_store, self._whitelist()
        )
        return json_response(
            {
                "settings": self._settings_public(),
                "instances": instances,
                "account": account,
                "sessions": sessions,
                "bindings": self._state_store.binding_keys(),
                "binding_permissions": self._state_store.binding_permissions(),
                "permission_definitions": PERMISSION_DEFINITIONS,
                "default_permissions": DEFAULT_PERMISSIONS,
                "api_error": api_error,
            }
        )

    @staticmethod
    def _body_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lower = value.strip().lower()
            if lower in {"1", "true", "yes", "on"}:
                return True
            if lower in {"0", "false", "no", "off"}:
                return False
        return default

    @staticmethod
    def _body_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    async def _settings_payload(self) -> tuple[str, str, bool, int]:
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            payload = {}
        base_url = normalize_base_url(
            payload.get("base_url", self.config.get("base_url"))
        )
        incoming_key = str(payload.get("api_key") or "").strip()
        api_key = incoming_key or str(self.config.get("api_key") or "").strip()
        verify_ssl = self._body_bool(
            payload.get("verify_ssl"), bool(self.config.get("verify_ssl", True))
        )
        timeout = self._body_int(
            payload.get("request_timeout", self.config.get("request_timeout", 20)),
            20,
            5,
            120,
        )
        if not base_url:
            raise ValueError("Base URL 不能为空。")
        if not api_key:
            raise ValueError("API Key 不能为空。")
        return base_url, api_key, verify_ssl, timeout

    async def page_settings_test(self):
        try:
            base_url, api_key, verify_ssl, timeout = await self._settings_payload()
            test_client = MCSMClient(
                base_url,
                api_key,
                verify_ssl=verify_ssl,
                timeout=timeout,
            )
            try:
                account_data = await test_client.auth_advanced()
                raw_instances = account_data.get("instances", [])
                if not isinstance(raw_instances, list):
                    raw_instances = []
                return json_response(
                    {
                        "ok": True,
                        "user_name": str(account_data.get("userName") or ""),
                        "permission": account_data.get("permission"),
                        "instance_count": len(raw_instances),
                        "instances": [
                            str(item.get("nickname") or item.get("instanceUuid") or "")
                            for item in raw_instances
                            if isinstance(item, dict)
                        ],
                    }
                )
            finally:
                await test_client.close()
        except (MCSMError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:
            self.logger.exception("Failed to test MCSManager connection")
            return error_response(f"测试连接失败：{exc}")

    async def page_settings_save(self):
        try:
            base_url, api_key, verify_ssl, timeout = await self._settings_payload()
            self.config["base_url"] = base_url
            self.config["api_key"] = api_key
            self.config["verify_ssl"] = verify_ssl
            self.config["request_timeout"] = timeout
            await self.config.save_config_async()
            await self._replace_client()
            return json_response({"ok": True, "settings": self._settings_public()})
        except (MCSMError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:
            self.logger.exception("Failed to save MCSManager settings")
            return error_response(f"保存设置失败：{exc}")

    async def _validate_instance_key(self, key: str) -> str:
        if not self._client.configured:
            raise MCSMError("请先在设置页填写 Base URL 和 API Key。")
        instances = await self._client.list_instances()
        for item in instances:
            if instance_key(item) == key:
                return key
        raise MCSMError("实例不存在，或当前 API Key 无权操作该实例。")

    async def page_bindings_add(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                payload = {}
            session_key = str(payload.get("session") or "").strip()
            key = str(payload.get("instance_key") or "").strip()
            if not session_key or not key:
                raise ValueError("会话与实例不能为空。")
            if len(session_key) > 512:
                raise ValueError("会话 ID 过长。")
            await self._validate_instance_key(key)
            added = await self._state_store.bind(session_key, key)
            return json_response(
                {
                    "ok": True,
                    "added": added,
                    "bindings": self._state_store.binding_keys(),
                }
            )
        except (MCSMError, ValueError) as exc:
            return error_response(str(exc))

    async def page_bindings_remove(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                payload = {}
            session_key = str(payload.get("session") or "").strip()
            key = str(payload.get("instance_key") or "").strip()
            if not session_key or not key:
                raise ValueError("会话与实例不能为空。")
            removed = await self._state_store.unbind(session_key, key)
            return json_response(
                {
                    "ok": True,
                    "removed": removed,
                    "bindings": self._state_store.binding_keys(),
                }
            )
        except ValueError as exc:
            return error_response(str(exc))

    async def page_bindings_reorder(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                payload = {}
            session_key = str(payload.get("session") or "").strip()
            ordered_keys = payload.get("ordered_keys")
            if not session_key or not isinstance(ordered_keys, list):
                raise ValueError("session 和 ordered_keys 不能为空。")
            if len(ordered_keys) > 500:
                raise ValueError("绑定实例数量异常。")
            normalized = await self._state_store.reorder_bindings(
                session_key, [str(item) for item in ordered_keys]
            )
            return json_response(
                {
                    "ok": True,
                    "ordered_keys": normalized,
                    "bindings": self._state_store.binding_keys(),
                }
            )
        except ValueError as exc:
            return error_response(str(exc))

    async def page_permissions_save(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                payload = {}
            session_key = str(payload.get("session") or "").strip()
            key = str(payload.get("instance_key") or "").strip()
            values = payload.get("permissions")
            if not session_key or not key or not isinstance(values, dict):
                raise ValueError("session、实例 key 与权限对象不能为空。")
            await self._validate_instance_key(key)
            permissions = await self._state_store.set_permissions(
                session_key, key, values
            )
            return json_response(
                {
                    "ok": True,
                    "session": session_key,
                    "instance_key": key,
                    "permissions": permissions,
                }
            )
        except (MCSMError, ValueError) as exc:
            return error_response(str(exc))

    def _session_keys(self, event: AstrMessageEvent) -> list[str]:
        keys: list[str] = []
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            keys.append(umo)
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if group_id:
            keys.append(group_id)
        if umo:
            session_id = parse_umo(umo)["session_id"]
            if session_id:
                keys.append(session_id)
        result: list[str] = []
        for key in keys:
            if key and key not in result:
                result.append(key)
        return result

    def _bound_instance_keys(self, event: AstrMessageEvent) -> list[str]:
        return self._state_store.keys_for_session(self._session_keys(event))

    def _permission_label(self, permission: str) -> str:
        for item in PERMISSION_DEFINITIONS:
            if item["key"] == permission:
                return item["label"]
        return permission

    async def _resolve_instance(
        self,
        event: AstrMessageEvent,
        identifier: str,
        permission: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        bound_keys = self._bound_instance_keys(event)
        if not bound_keys:
            raise MCSMAccessError(
                "当前会话尚未绑定任何 MCSManager 实例。请管理员在插件「蓝图」页拖拽实例完成授权。"
            )
        instances = await self._client.list_instances()
        by_key = {instance_key(item): item for item in instances}
        candidates = [by_key[key] for key in bound_keys if key in by_key]
        if not candidates:
            raise MCSMAccessError(
                "当前会话绑定的实例已不存在，或当前 API Key 已无权访问。请在蓝图页重新绑定。"
            )

        query = str(identifier or "").strip().casefold()
        selected: dict[str, Any] | None = None
        if not query:
            if len(candidates) == 1:
                selected = candidates[0]
            else:
                names = "、".join(
                    str(item.get("nickname") or instance_key(item))
                    for item in candidates
                )
                raise MCSMAccessError(
                    f"当前会话绑定了多个实例，请指定 instance：{names}"
                )
        else:
            exact = [
                item for item in candidates if instance_key(item).casefold() == query
            ]
            if len(exact) == 1:
                selected = exact[0]
            if selected is None:
                matches = []
                for item in candidates:
                    values = {
                        instance_key(item).casefold(),
                        str(item.get("instanceUuid") or "").casefold(),
                        str(item.get("daemonId") or "").casefold(),
                        str(item.get("nickname") or "").casefold(),
                    }
                    if query in values:
                        matches.append(item)
                unique = {instance_key(item): item for item in matches}
                if len(unique) == 1:
                    selected = next(iter(unique.values()))
                elif len(unique) > 1:
                    names = "、".join(
                        str(item.get("nickname") or instance_key(item))
                        for item in unique.values()
                    )
                    raise MCSMAccessError(
                        f"实例名称不唯一，请改用 UUID 或实例 key：{names}"
                    )
        if selected is None:
            raise MCSMAccessError("实例不存在或未授权给当前会话。")

        key = instance_key(selected)
        permissions = self._state_store.permissions_for_any(
            self._session_keys(event), key
        )
        if permission and not permissions.get(permission, False):
            raise MCSMAccessError(
                f"实例「{selected.get('nickname') or key}」未启用「{self._permission_label(permission)}」权限。"
            )
        return selected, key

    @staticmethod
    def _json_tool_result(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)

    def _tool_error(self, message: str, **extra: Any) -> str:
        payload = {"ok": False, "error": message}
        payload.update(extra)
        return self._json_tool_result(payload)

    @staticmethod
    def _extract_tool_event(args: tuple[Any, ...]) -> AstrMessageEvent:
        for value in args:
            if hasattr(value, "unified_msg_origin") and hasattr(value, "get_group_id"):
                return value
            context = getattr(value, "context", None)
            event = getattr(context, "event", None)
            if event is not None and hasattr(event, "unified_msg_origin"):
                return event
        raise MCSMAccessError("无法从工具调用上下文中解析当前消息会话。")

    async def dispatch_tool(
        self, event: AstrMessageEvent, tool_name: str, **kwargs: Any
    ) -> str:
        try:
            if not self._client.configured:
                raise MCSMAccessError(
                    "请先在 MCSManager 插件设置页填写 Base URL 和 API Key。"
                )
            handler = getattr(self, f"_tool_{tool_name.removeprefix('mcsm_')}", None)
            if handler is None:
                raise MCSMAccessError(f"未知工具：{tool_name}")
            result = await handler(event, **kwargs)
            return self._json_tool_result(result)
        except (MCSMAccessError, MCSMError, ValueError) as exc:
            return self._tool_error(str(exc))
        except Exception as exc:
            self.logger.exception("MCSM tool %s failed", tool_name)
            return self._tool_error(f"MCSManager 工具执行失败：{exc}")

    async def _first_authorized_instance(
        self, event: AstrMessageEvent, permission: str
    ) -> tuple[dict[str, Any], str]:
        bound_keys = self._bound_instance_keys(event)
        if not bound_keys:
            raise MCSMAccessError("当前会话尚未绑定任何 MCSManager 实例。")
        instances = await self._client.list_instances()
        by_key = {instance_key(item): item for item in instances}
        for key in bound_keys:
            item = by_key.get(key)
            if item is None:
                continue
            permissions = self._state_store.permissions_for_any(
                self._session_keys(event), key
            )
            if permissions.get(permission, False):
                return item, key
        raise MCSMAccessError(
            f"当前会话没有启用了「{self._permission_label(permission)}」权限的实例。"
        )

    @staticmethod
    def _strip_ansi(value: Any) -> str:
        text = str(value or "")
        text = re.sub(r"\x1B\][^\x07]*(?:\x07|\x1B\\)", "", text)
        text = ANSI_RE.sub("", text)
        return text.replace("\r", "").replace("\x00", "").replace("\x07", "")

    @staticmethod
    def _truncate(value: Any, max_chars: int) -> tuple[str, bool]:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        if len(text) <= max_chars:
            return text, False
        return text[-max_chars:], True

    @staticmethod
    def _command_output_preview(text: str, max_chars: int) -> tuple[str, int, int]:
        total = len(text)
        if total <= max_chars:
            return text, 0, total

        marker = ""
        head_len = 0
        tail_len = 0
        omitted = total
        for _ in range(3):
            marker = f"\n\n... [MCSM 输出已截断：省略 {omitted} / {total} 字符] ...\n\n"
            body_budget = max(16, max_chars - len(marker))
            head_len = max(1, int(body_budget * 0.35))
            tail_len = max(1, body_budget - head_len)
            omitted = max(0, total - head_len - tail_len)

        head = text[:head_len]
        tail = text[-tail_len:]
        omitted = max(0, total - len(head) - len(tail))
        marker = f"\n\n... [MCSM 输出已截断：省略 {omitted} / {total} 字符] ...\n\n"
        preview = head + marker + tail
        if len(preview) > max_chars:
            preview = preview[:max_chars]
            omitted = max(
                0, total - len(head) - max(0, len(preview) - len(head) - len(marker))
            )
        return preview, omitted, total

    @staticmethod
    def _command_snapshot_scope(event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            raise MCSMAccessError("无法解析当前会话，不能保存命令输出快照。")
        return hashlib.sha256(umo.encode("utf-8")).hexdigest()[:16]

    def _save_command_snapshot_sync(self, scope: str, text: str) -> str:
        scope_dir = self._command_output_dir / scope
        scope_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        files: list[Path] = []
        for path in scope_dir.glob("*.log"):
            try:
                if now - path.stat().st_mtime > COMMAND_SNAPSHOT_TTL_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
                files.append(path)
            except OSError:
                continue
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for path in files[COMMAND_SNAPSHOT_MAX_FILES:]:
            path.unlink(missing_ok=True)

        snapshot_id = secrets.token_hex(8)
        (scope_dir / f"{snapshot_id}.log").write_text(text, encoding="utf-8")
        return snapshot_id

    async def _save_command_snapshot(self, event: AstrMessageEvent, text: str) -> str:
        scope = self._command_snapshot_scope(event)
        return await asyncio.to_thread(self._save_command_snapshot_sync, scope, text)

    def _redact_sensitive(self, value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                lower = str(key).lower()
                if any(
                    marker in lower
                    for marker in (
                        "password",
                        "passwd",
                        "apikey",
                        "api_key",
                        "token",
                        "secret",
                    )
                ):
                    result[key] = "***"
                else:
                    result[key] = self._redact_sensitive(item)
            return result
        if isinstance(value, list):
            return [self._redact_sensitive(item) for item in value]
        return value

    def _instance_public_with_permissions(
        self, item: dict[str, Any], event: AstrMessageEvent
    ) -> dict[str, Any]:
        public = self._public_instance(item)
        public["permissions"] = self._state_store.permissions_for_any(
            self._session_keys(event), public["key"]
        )
        return public

    async def _tool_list_instances(self, event: AstrMessageEvent, **_: Any) -> Any:
        bound_keys = self._bound_instance_keys(event)
        instances = await self._client.list_instances()
        by_key = {instance_key(item): item for item in instances}
        bound_instances = [
            self._instance_public_with_permissions(by_key[key], event)
            for key in bound_keys
            if key in by_key
        ]
        stale = [key for key in bound_keys if key not in by_key]
        return {
            "ok": True,
            "session": str(getattr(event, "unified_msg_origin", "") or ""),
            "instances": bound_instances,
            "stale_bindings": stale,
            "message": (
                "当前会话尚未绑定实例。"
                if not bound_keys
                else ("部分绑定实例已不可用。" if stale else "实例列表读取成功。")
            ),
        }

    async def _tool_get_instance(
        self, event: AstrMessageEvent, instance: str = "", **_: Any
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "view")
        public = self._instance_public_with_permissions(selected, event)
        detail: Any = None
        detail_error = ""
        try:
            detail = await self._client.instance_detail(
                str(selected.get("daemonId") or ""),
                str(selected.get("instanceUuid") or ""),
            )
        except MCSMError as exc:
            detail_error = str(exc)
        result: dict[str, Any] = {"ok": True, "instance": public}
        if detail is not None:
            result["detail"] = self._redact_sensitive(detail)
        if detail_error:
            result["detail_error"] = detail_error
        return result

    async def _tool_get_console(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        size: str = "20KB",
        max_chars: int | float | str = 12000,
        **_: Any,
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "console")
        log_size = str(size or "20KB").strip().upper()
        if not re.fullmatch(r"\d+(?:B|KB|MB)?", log_size):
            log_size = "20KB"
        limit = self._body_int(max_chars, 12000, 500, MAX_LOG_CHARS)
        raw = await self._client.output_log(
            str(selected.get("daemonId") or ""),
            str(selected.get("instanceUuid") or ""),
            log_size,
        )
        text = self._strip_ansi(raw)
        tail, truncated = self._truncate(text, limit)
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "size": log_size,
            "truncated": truncated,
            "console": tail,
        }

    def _terminal_lock_for(self, instance_key_value: str) -> asyncio.Lock:
        lock = self._terminal_locks.get(instance_key_value)
        if lock is None:
            lock = asyncio.Lock()
            self._terminal_locks[instance_key_value] = lock
        return lock

    async def _tool_send_command(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        command: str = "",
        wait_ms: int | float | str = 1200,
        max_chars: int | float | str = COMMAND_OUTPUT_DEFAULT_CHARS,
        **_: Any,
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "command")
        try:
            status = int(selected.get("status"))
        except (TypeError, ValueError):
            status = -999
        if status != 3:
            status_text = STATUS_TEXT.get(status, "未知")
            return {
                "ok": False,
                "instance": selected.get("nickname") or key,
                "status": status,
                "status_text": status_text,
                "sent": False,
                "error": f"实例当前为「{status_text}」，命令未发送。",
                "hint": (
                    "实例必须处于运行中（status=3）才能执行命令。"
                    "如需启动，请先调用 mcsm_power(action=start)，"
                    "再查询状态确认运行中后重试。"
                ),
            }
        command_text = str(command or "").strip()
        if not command_text:
            raise ValueError("command 不能为空。")
        if len(command_text) > 8000:
            raise ValueError("命令过长，最多 8000 字符。")

        # A terminal has one input stream. Serialize command writes even when
        # AstrBot invokes multiple copies of this tool concurrently.
        async with self._terminal_lock_for(key):
            return await self._execute_serial_command(
                event, selected, key, command_text, wait_ms, max_chars
            )

    async def _execute_serial_command(
        self,
        event: AstrMessageEvent,
        selected: dict[str, Any],
        key: str,
        command_text: str,
        wait_ms: int | float | str,
        max_chars: int | float | str,
    ) -> dict[str, Any]:
        daemon_id = str(selected.get("daemonId") or "")
        instance_uuid = str(selected.get("instanceUuid") or "")
        before_text = ""
        try:
            before_text = self._strip_ansi(
                await self._client.output_log(daemon_id, instance_uuid, "256KB")
            )
        except MCSMError:
            pass

        await self._client.send_command(daemon_id, instance_uuid, command_text)
        delay = self._body_int(wait_ms, 1200, 0, 10000)
        if delay:
            await asyncio.sleep(delay / 1000)

        limit = self._body_int(
            max_chars,
            COMMAND_OUTPUT_DEFAULT_CHARS,
            COMMAND_OUTPUT_MIN_CHARS,
            COMMAND_OUTPUT_MAX_CHARS,
        )
        after_text = ""
        try:
            after_text = self._strip_ansi(
                await self._client.output_log(daemon_id, instance_uuid, "256KB")
            )
        except MCSMError:
            pass

        delta = self._console_delta(before_text, after_text)
        preview, omitted_chars, total_chars = self._command_output_preview(delta, limit)
        truncated = omitted_chars > 0
        result: dict[str, Any] = {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "command": command_text,
            "sent": True,
            "wait_ms": delay,
            "console_output": preview,
            "returned_chars": len(preview),
            "total_chars": total_chars,
            "omitted_chars": omitted_chars,
            "max_chars": limit,
            "truncated": truncated,
        }
        if truncated:
            snapshot_id = await self._save_command_snapshot(event, delta)
            result["snapshot_id"] = snapshot_id
            result["snapshot_hint"] = (
                "完整输出已保存为临时快照；使用 mcsm_read_command_output 和 "
                "snapshot_id 分页读取，不要一次请求全部日志。"
            )
        if not delta.strip():
            result["note"] = (
                "发送后等待时间内没有检测到新增控制台输出。可增大 wait_ms，"
                "或使用 mcsm_get_console 再读取控制台。"
            )
        return result

    async def _tool_read_command_output(
        self,
        event: AstrMessageEvent,
        snapshot_id: str = "",
        offset: int | float | str = 0,
        limit: int | float | str = 6000,
        **_: Any,
    ) -> Any:
        if not self._bound_instance_keys(event):
            raise MCSMAccessError("当前会话尚未绑定 MCSManager 实例。")
        snapshot = str(snapshot_id or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{16}", snapshot):
            raise ValueError("snapshot_id 格式无效。")
        scope = self._command_snapshot_scope(event)
        path = self._command_output_dir / scope / f"{snapshot}.log"
        if not path.is_file():
            raise MCSMAccessError("命令输出快照不存在或已过期。")

        text = await asyncio.to_thread(
            path.read_text, encoding="utf-8", errors="replace"
        )
        total = len(text)
        start = self._body_int(offset, 0, 0, 10_000_000)
        if start > total:
            start = total
        page_size = self._body_int(limit, 6000, 512, 12000)
        chunk = text[start : start + page_size]
        next_offset = start + len(chunk)
        return {
            "ok": True,
            "snapshot_id": snapshot,
            "offset": start,
            "limit": page_size,
            "returned_chars": len(chunk),
            "total_chars": total,
            "next_offset": next_offset if next_offset < total else None,
            "has_more": next_offset < total,
            "content": chunk,
        }

    @staticmethod
    def _console_delta(before: str, after: str) -> str:
        if not before:
            return after
        if after.startswith(before):
            return after[len(before) :]
        limit = min(len(before), len(after))
        prefix = 0
        while prefix < limit and before[prefix] == after[prefix]:
            prefix += 1
        return after[prefix:]

    async def _tool_power(
        self, event: AstrMessageEvent, instance: str = "", action: str = "", **_: Any
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "power")
        action = str(action or "").strip().lower()
        daemon_id = str(selected.get("daemonId") or "")
        instance_uuid = str(selected.get("instanceUuid") or "")
        methods = {
            "start": self._client.open_instance,
            "stop": self._client.stop_instance,
            "restart": self._client.restart_instance,
            "kill": self._client.kill_instance,
        }
        if action not in methods:
            raise ValueError("action 必须是 start、stop、restart 或 kill。")
        result = await methods[action](daemon_id, instance_uuid)
        await asyncio.sleep(0.8)
        status = None
        try:
            instances = await self._client.list_instances()
            for item in instances:
                if instance_key(item) == key:
                    status = self._public_instance(item)
                    break
        except MCSMError:
            pass
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "action": action,
            "result": result,
            "status": status,
        }

    async def _tool_list_files(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        target: str = "/",
        page: int | float | str = 0,
        page_size: int | float | str = 50,
        file_name: str = "",
        **_: Any,
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "file_read")
        page_number = self._body_int(page, 0, 0, 1000000)
        size = self._body_int(page_size, 50, 1, 100)
        data = await self._client.files_list(
            str(selected.get("daemonId") or ""),
            str(selected.get("instanceUuid") or ""),
            str(target or "/"),
            page_number,
            size,
            str(file_name or "").strip(),
        )
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "target": target or "/",
            "data": data,
        }

    async def _tool_read_file(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        target: str = "",
        max_chars: int | float | str = 20000,
        **_: Any,
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "file_read")
        path = str(target or "").strip()
        if not path:
            raise ValueError("target 不能为空。")
        data = await self._client.file_read(
            str(selected.get("daemonId") or ""),
            str(selected.get("instanceUuid") or ""),
            path,
        )
        limit = self._body_int(max_chars, 20000, 500, 100000)
        text, truncated = self._truncate(data, limit)
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "target": path,
            "truncated": truncated,
            "content": text,
        }

    async def _tool_write_file(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        target: str = "",
        text: str = "",
        **_: Any,
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "file_write")
        path = str(target or "").strip()
        content = str(text or "")
        if not path:
            raise ValueError("target 不能为空。")
        if len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("写入内容超过 1 MiB 限制。")
        result = await self._client.file_write(
            str(selected.get("daemonId") or ""),
            str(selected.get("instanceUuid") or ""),
            path,
            content,
        )
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "target": path,
            "written_bytes": len(content.encode("utf-8")),
            "result": result,
        }

    async def _tool_manage_files(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        action: str = "",
        target: str = "",
        source: str = "",
        destination: str = "",
        targets: list[Any] | None = None,
        chmod: int | float | str = 755,
        deep: bool = False,
        task_id: str = "",
        **_: Any,
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "file_write")
        daemon_id = str(selected.get("daemonId") or "")
        instance_uuid = str(selected.get("instanceUuid") or "")
        action = str(action or "").strip().lower()
        paths = [str(item).strip() for item in (targets or []) if str(item).strip()]
        single = str(target or "").strip()
        src = str(source or "").strip()
        dst = str(destination or "").strip()

        if action == "delete":
            if not paths and single:
                paths = [single]
            if not paths:
                raise ValueError("delete 需要 target 或 targets。")
            result = await self._client.files_delete(daemon_id, instance_uuid, paths)
        elif action == "mkdir":
            if not single:
                raise ValueError("mkdir 需要 target。")
            result = await self._client.files_mkdir(daemon_id, instance_uuid, single)
        elif action == "touch":
            if not single:
                raise ValueError("touch 需要 target。")
            result = await self._client.files_touch(daemon_id, instance_uuid, single)
        elif action in {"copy", "move"}:
            if not src or not dst:
                raise ValueError(f"{action} 需要 source 和 destination。")
            pairs = [[src, dst]]
            if action == "copy":
                result = await self._client.files_copy(daemon_id, instance_uuid, pairs)
            else:
                result = await self._client.files_move(daemon_id, instance_uuid, pairs)
        elif action == "zip":
            if not src or not paths:
                raise ValueError(
                    "zip 需要 source（压缩包路径）和 targets（要压缩的路径数组）。"
                )
            result = await self._client.files_compress(
                daemon_id,
                instance_uuid,
                action="zip",
                source=src,
                targets=paths,
            )
        elif action == "unzip":
            if not src or not single:
                raise ValueError(
                    "unzip 需要 source（压缩包路径）和 target（解压目录）。"
                )
            result = await self._client.files_compress(
                daemon_id,
                instance_uuid,
                action="unzip",
                source=src,
                targets=[single],
            )
        elif action == "chmod":
            if not paths and single:
                paths = [single]
            if not paths:
                raise ValueError("chmod 需要 target 或 targets。")
            mode = self._body_int(chmod, 755, 0, 7777)
            result = await self._client.files_chmod(
                daemon_id,
                instance_uuid,
                targets=paths,
                chmod=mode,
                deep=bool(deep),
            )
        elif action == "stop_download":
            download_id = str(task_id or single).strip()
            if not download_id:
                raise ValueError("stop_download 需要 task_id。")
            result = await self._client.stop_download_from_url(
                daemon_id, instance_uuid, download_id
            )
        else:
            raise ValueError(f"不支持的文件操作：{action}")

        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "action": action,
            "result": result,
        }

    async def _tool_file_download_link(
        self, event: AstrMessageEvent, instance: str = "", file_name: str = "", **_: Any
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "file_read")
        path = str(file_name or "").strip()
        if not path:
            raise ValueError("file_name 不能为空。")
        url = await self._client.file_download_url(
            str(selected.get("daemonId") or ""),
            str(selected.get("instanceUuid") or ""),
            path,
        )
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "file_name": path,
            "download_url": url,
            "expires": "一次性链接，下载后失效",
        }

    async def _tool_file_upload_base64(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        upload_dir: str = "/",
        file_name: str = "",
        content_base64: str = "",
        overwrite: bool = True,
        unzip: bool = False,
        **_: Any,
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "file_write")
        safe_name = str(file_name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("file_name 必须是有效文件名。")
        try:
            content = base64.b64decode(str(content_base64 or ""), validate=True)
        except Exception as exc:
            raise ValueError("content_base64 不是有效的 Base64 内容。") from exc
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("上传内容超过 8 MiB 限制。")
        result = await self._client.upload_bytes(
            str(selected.get("daemonId") or ""),
            str(selected.get("instanceUuid") or ""),
            upload_dir=str(upload_dir or "/"),
            file_name=safe_name,
            content=content,
            overwrite=bool(overwrite),
            unzip=bool(unzip),
        )
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "upload_dir": str(upload_dir or "/"),
            "file_name": safe_name,
            "uploaded_bytes": len(content),
            "result": result,
        }

    async def _tool_file_download_from_url(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        url: str = "",
        file_name: str = "",
        **_: Any,
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "file_write")
        source_url = str(url or "").strip()
        target_path = str(file_name or "").strip()
        if not source_url.startswith(("http://", "https://")):
            raise ValueError("url 必须是 http:// 或 https:// 地址。")
        if not target_path:
            raise ValueError("file_name 不能为空。")
        task_id = await self._client.download_from_url(
            str(selected.get("daemonId") or ""),
            str(selected.get("instanceUuid") or ""),
            source_url,
            target_path,
        )
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "task_id": task_id,
            "target": target_path,
            "message": "后台下载任务已创建，可用 mcsm_file_status 查询进度。",
        }

    async def _tool_file_status(
        self, event: AstrMessageEvent, instance: str = "", **_: Any
    ) -> Any:
        selected, key = await self._resolve_instance(event, instance, "file_read")
        data = await self._client.files_status(
            str(selected.get("daemonId") or ""),
            str(selected.get("instanceUuid") or ""),
        )
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "data": data,
        }

    async def _tool_process_config(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        action: str = "",
        files: list[Any] | None = None,
        file_name: str = "",
        file_type: str = "",
        content: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        action = str(action or "").strip().lower()
        permission = "file_read" if action in {"list", "read"} else "file_write"
        selected, key = await self._resolve_instance(event, instance, permission)
        daemon_id = str(selected.get("daemonId") or "")
        instance_uuid = str(selected.get("instanceUuid") or "")
        name = str(file_name or "").strip()
        if action == "list":
            paths = [str(item).strip() for item in (files or []) if str(item).strip()]
            if not paths:
                raise ValueError("action=list 需要 files 数组。")
            data = await self._client.process_config_list(
                daemon_id, instance_uuid, paths
            )
        elif action == "read":
            if not name:
                raise ValueError("action=read 需要 file_name。")
            data = await self._client.process_config_get(
                daemon_id, instance_uuid, name, str(file_type or "")
            )
            data = self._redact_sensitive(data)
        elif action == "write":
            if not name:
                raise ValueError("action=write 需要 file_name。")
            if content is None:
                raise ValueError("action=write 需要 content 对象。")
            data = await self._client.process_config_put(
                daemon_id,
                instance_uuid,
                name,
                content,
                str(file_type or ""),
            )
        else:
            raise ValueError("action 必须是 list、read 或 write。")
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "action": action,
            "data": data,
        }

    async def _tool_async_task(
        self,
        event: AstrMessageEvent,
        instance: str = "",
        action: str = "",
        task_name: str = "update",
        task_id: str = "",
        body: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        action = str(action or "").strip().lower()
        permission = "view" if action == "query" else "power"
        selected, key = await self._resolve_instance(event, instance, permission)
        request_body: dict[str, Any] = (
            copy.deepcopy(body) if isinstance(body, dict) else {}
        )
        if action == "start_update":
            request_body.setdefault("time", int(time.time() * 1000))
        data = await self._client.async_task(
            str(selected.get("daemonId") or ""),
            str(selected.get("instanceUuid") or ""),
            action=action,
            task_name=str(task_name or "update"),
            task_id=str(task_id or ""),
            body=request_body,
        )
        return {
            "ok": True,
            "instance": selected.get("nickname") or key,
            "action": action,
            "task_name": task_name or "update",
            "data": data,
        }

    @staticmethod
    def _classify_api_permission(method: str, path: str) -> str:
        method = method.upper()
        path = path.rstrip("/") or "/api"
        if path.startswith("/api/files"):
            if path in {"/api/files/list", "/api/files/status"}:
                return "file_read"
            if path == "/api/files/download":
                return "file_read"
            if path == "/api/files/upload":
                return "file_write"
            if path == "/api/files/" and method in {"GET", "PUT"}:
                return "file_write"
            return "file_write"
        if path.startswith("/api/protected_instance"):
            if path == "/api/protected_instance/command":
                return "command"
            if path == "/api/protected_instance/outputlog":
                return "console"
            if path == "/api/protected_instance/process_config/list":
                return "file_read"
            if (
                path == "/api/protected_instance/process_config/file"
                and method == "GET"
            ):
                return "file_read"
            if (
                path == "/api/protected_instance/process_config/file"
                and method == "PUT"
            ):
                return "file_write"
            if path == "/api/protected_instance/query_asynchronous":
                return "view"
            if path == "/api/protected_instance/instance_update":
                return "admin"
            if path in {
                "/api/protected_instance/open",
                "/api/protected_instance/stop",
                "/api/protected_instance/restart",
                "/api/protected_instance/kill",
                "/api/protected_instance/asynchronous",
                "/api/protected_instance/stop_asynchronous",
                "/api/protected_instance/install_instance",
            }:
                return "power"
            return "admin"
        if path.startswith("/api/instance"):
            if method == "GET":
                return "view"
            return "admin"
        if path.startswith("/api/service"):
            return "admin"
        if path.startswith("/api/overview"):
            return "admin"
        if path.startswith("/api/environment"):
            return "admin"
        return "admin"

    @staticmethod
    def _is_instance_scoped_api(path: str) -> bool:
        return path.startswith(("/api/files", "/api/protected_instance"))

    async def _tool_api(
        self,
        event: AstrMessageEvent,
        method: str = "GET",
        path: str = "",
        instance: str = "",
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        method = str(method or "").strip().upper()
        api_path = str(path or "").strip()
        if method not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
            raise ValueError("method 必须是 GET、POST、PUT、DELETE 或 PATCH。")
        if not api_path.startswith("/api/") or "://" in api_path or "?" in api_path:
            raise ValueError("path 必须是以 /api/ 开头且不含 query string 的路径。")
        if ".." in api_path.split("/"):
            raise ValueError("path 不允许包含 ..。")
        if api_path == "/api/auth" or api_path.startswith("/api/auth/"):
            raise MCSMAccessError(
                "出于安全原因，通用 API 工具禁止访问账号与 API Key 管理接口。"
            )

        permission = self._classify_api_permission(method, api_path)
        params = dict(query) if isinstance(query, dict) else {}
        payload: Any = dict(body) if isinstance(body, dict) else body

        if self._is_instance_scoped_api(api_path):
            selected, key = await self._resolve_instance(event, instance, permission)
            params["daemonId"] = str(selected.get("daemonId") or "")
            params["uuid"] = str(selected.get("instanceUuid") or "")
            instance_name = selected.get("nickname") or key
        else:
            authorized, key = await self._first_authorized_instance(event, permission)
            instance_name = authorized.get("nickname") or key
            if instance:
                _selected, _key = await self._resolve_instance(
                    event, instance, permission
                )

        if api_path == "/api/protected_instance/command":
            async with self._terminal_lock_for(key):
                response = await self._client.request_panel(
                    method,
                    api_path,
                    params=params,
                    body=payload,
                )
        else:
            response = await self._client.request_panel(
                method,
                api_path,
                params=params,
                body=payload,
            )
        return {
            "ok": True,
            "instance": instance_name,
            "permission": permission,
            "response": response,
        }

    @filter.on_llm_request()
    async def inject_mcsm_policies(
        self, event: AstrMessageEvent, req: ProviderRequest | None = None
    ) -> None:
        """Inject MCSM-specific Java and bounded-output policies."""
        if req is None or not self._bound_instance_keys(event):
            return
        java_policy = (
            '<mcsm_java_policy priority="high">\n'
            "当用户要求检测、查看、安装或切换 MCSManager 实例的 Java 版本时，必须优先使用实例内的 javm（Java Version Manager），不要先直接执行 java -version、修改 JAVA_HOME 或替换 JDK 文件。\n"
            "推荐流程：\n"
            "1. javm current：查看当前正在使用的 Java 版本。\n"
            "2. javm ls：查看已安装的 Java 版本。\n"
            "3. javm use <版本>：切换当前终端会话使用的 Java 版本。\n"
            "4. javm default <版本>：设置新终端会话的默认 Java 版本。\n"
            "5. javm ls-remote / javm install：查询或安装 Java 版本。\n"
            "如果 javm 不存在或执行失败，再回退到 java -version 或常规排查，并向用户说明回退原因。\n"
            "</mcsm_java_policy>"
        )
        output_policy = (
            '<mcsm_output_policy priority="high">\n'
            "mcsm_send_command 单次返回的日志总预算默认 8000 字符，硬上限 12000 字符。\n"
            "超长命令输出会保留头尾摘要并附 snapshot_id；需要完整内容时使用 mcsm_read_command_output 分页读取，每页默认 6000、最大 12000 字符。\n"
            "不要尝试在一条回复中完整输出超长日志，也不要假设未显示部分为空。\n"
            "</mcsm_output_policy>"
        )
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        additions = [
            policy
            for policy in (java_policy, output_policy)
            if policy not in system_prompt
        ]
        if not additions:
            return
        req.system_prompt = (system_prompt + "\n\n" + "\n\n".join(additions)).strip()

    @filter.command("mcsm")
    async def mcsm_status_command(self, event: AstrMessageEvent):
        """查看当前会话已绑定的 MCSManager 实例。"""
        if not self._client.configured:
            yield event.plain_result(
                "MCSManager 尚未配置，请管理员在插件「设置」页填写 Base URL 与 API Key。"
            )
            return
        try:
            bound_keys = self._bound_instance_keys(event)
            if not bound_keys:
                yield event.plain_result(
                    "当前会话尚未绑定 MCSManager 实例，请在插件「蓝图」页完成授权。"
                )
                return
            instances = await self._client.list_instances()
            by_key = {instance_key(item): item for item in instances}
            lines = ["当前会话可操作的 MCSManager 实例："]
            for key in bound_keys:
                item = by_key.get(key)
                if item is None:
                    lines.append(f"- {key}（已失效）")
                    continue
                status = self._public_instance(item)["status_text"]
                permissions = self._state_store.permissions_for_any(
                    self._session_keys(event), key
                )
                enabled = [
                    item_def["label"]
                    for item_def in PERMISSION_DEFINITIONS
                    if permissions.get(item_def["key"])
                ]
                lines.append(
                    f"- {item.get('nickname') or key} [{status}] 权限：{'、'.join(enabled) or '无'}"
                )
            yield event.plain_result("\n".join(lines))
        except (MCSMError, MCSMAccessError) as exc:
            yield event.plain_result(f"读取 MCSManager 实例失败：{exc}")

    async def terminate(self) -> None:
        await self._client.close()
