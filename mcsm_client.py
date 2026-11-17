from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import aiohttp

JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None


class MCSMError(Exception):
    """A user-facing MCSManager API error."""


def instance_key(instance: dict[str, Any]) -> str:
    daemon_id = str(instance.get("daemonId") or "").strip()
    instance_uuid = str(instance.get("instanceUuid") or "").strip()
    return f"{daemon_id}:{instance_uuid}"


def normalize_base_url(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MCSMError("Base URL 必须是完整的 http:// 或 https:// 地址。")
    if parsed.username or parsed.password:
        raise MCSMError("Base URL 不能包含用户名或密码。")
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
    )


def daemon_http_base(addr: Any) -> str:
    value = str(addr or "").strip().rstrip("/")
    if not value:
        raise MCSMError("MCSManager 未返回可用的守护进程地址。")
    if value.startswith("wss://"):
        value = "https://" + value[len("wss://") :]
    elif value.startswith("ws://"):
        value = "http://" + value[len("ws://") :]
    elif not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value.rstrip("/")


def _message_from_error(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        for key in ("message", "msg", "error"):
            if data.get(key):
                return str(data[key])
        if data.get("data"):
            return _message_from_error(data["data"], fallback)
    if isinstance(data, str) and data.strip():
        return data.strip()
    if data is not None:
        try:
            return json.dumps(data, ensure_ascii=False)
        except TypeError:
            return str(data)
    return fallback


class MCSMClient:
    """Small async client for the MCSManager panel and daemon APIs."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify_ssl: bool = True,
        timeout: int = 20,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_key = str(api_key or "").strip()
        self.verify_ssl = bool(verify_ssl)
        self.timeout = max(5, min(int(timeout or 20), 120))
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "AstrBot-MCSM-Plugin/0.1",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _url(self, path: str) -> str:
        if not self.base_url:
            raise MCSMError("尚未配置 MCSManager Base URL。")
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def _safe_text(self, text: str) -> str:
        if self.api_key:
            return text.replace(self.api_key, "***")
        return text

    async def request_panel(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: JSONValue = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise MCSMError("尚未配置 MCSManager Base URL 或 API Key。")

        query = {str(k): str(v) for k, v in (params or {}).items() if v is not None}
        query["apikey"] = self.api_key
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = (
            None
            if body is None
            else json.dumps(body, ensure_ascii=False).encode("utf-8")
        )
        session = await self._get_session()
        try:
            async with session.request(
                method.upper(),
                self._url(path),
                params=query,
                data=payload,
                headers=headers,
                ssl=self.verify_ssl,
            ) as response:
                raw = await response.text()
                try:
                    data = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    data = {"status": response.status, "data": raw}
                if response.status >= 400:
                    raise MCSMError(
                        self._safe_text(
                            _message_from_error(
                                data, f"MCSManager 返回 HTTP {response.status}"
                            )
                        )
                    )
                if isinstance(data, dict):
                    status = data.get("status")
                    if status is not None and str(status) != "200":
                        raise MCSMError(
                            self._safe_text(
                                _message_from_error(
                                    data, f"MCSManager 请求失败：{status}"
                                )
                            )
                        )
                    return data
                return {"status": response.status, "data": data}
        except asyncio.TimeoutError as exc:
            raise MCSMError(f"MCSManager 请求超时（{self.timeout} 秒）。") from exc
        except aiohttp.ClientError as exc:
            raise MCSMError(self._safe_text(f"连接 MCSManager 失败：{exc}")) from exc

    async def request_data(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: JSONValue = None,
    ) -> Any:
        payload = await self.request_panel(method, path, params=params, body=body)
        return payload.get("data")

    async def auth_advanced(self) -> dict[str, Any]:
        data = await self.request_data("GET", "/api/auth", params={"advanced": "1"})
        return data if isinstance(data, dict) else {}

    async def list_instances(self) -> list[dict[str, Any]]:
        data = await self.auth_advanced()
        instances = data.get("instances")
        if not isinstance(instances, list):
            return []
        return [item for item in instances if isinstance(item, dict)]

    async def instance_detail(self, daemon_id: str, instance_uuid: str) -> Any:
        return await self.request_data(
            "GET",
            "/api/instance",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
        )

    async def open_instance(self, daemon_id: str, instance_uuid: str) -> Any:
        return await self.request_data(
            "GET",
            "/api/protected_instance/open",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
        )

    async def stop_instance(self, daemon_id: str, instance_uuid: str) -> Any:
        return await self.request_data(
            "GET",
            "/api/protected_instance/stop",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
        )

    async def restart_instance(self, daemon_id: str, instance_uuid: str) -> Any:
        return await self.request_data(
            "GET",
            "/api/protected_instance/restart",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
        )

    async def kill_instance(self, daemon_id: str, instance_uuid: str) -> Any:
        return await self.request_data(
            "GET",
            "/api/protected_instance/kill",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
        )

    async def send_command(
        self, daemon_id: str, instance_uuid: str, command: str
    ) -> Any:
        return await self.request_data(
            "GET",
            "/api/protected_instance/command",
            params={
                "daemonId": daemon_id,
                "uuid": instance_uuid,
                "command": command,
            },
        )

    async def output_log(
        self, daemon_id: str, instance_uuid: str, size: str = "20KB"
    ) -> Any:
        return await self.request_data(
            "GET",
            "/api/protected_instance/outputlog",
            params={
                "daemonId": daemon_id,
                "uuid": instance_uuid,
                "size": size,
            },
        )

    async def process_config_list(
        self, daemon_id: str, instance_uuid: str, files: list[str]
    ) -> Any:
        return await self.request_data(
            "POST",
            "/api/protected_instance/process_config/list",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"files": files},
        )

    async def process_config_get(
        self,
        daemon_id: str,
        instance_uuid: str,
        file_name: str,
        file_type: str = "",
    ) -> Any:
        params = {
            "daemonId": daemon_id,
            "uuid": instance_uuid,
            "fileName": file_name,
        }
        if file_type:
            params["type"] = file_type
        return await self.request_data(
            "GET", "/api/protected_instance/process_config/file", params=params
        )

    async def process_config_put(
        self,
        daemon_id: str,
        instance_uuid: str,
        file_name: str,
        content: JSONValue,
        file_type: str = "",
    ) -> Any:
        params = {
            "daemonId": daemon_id,
            "uuid": instance_uuid,
            "fileName": file_name,
        }
        if file_type:
            params["type"] = file_type
        return await self.request_data(
            "PUT",
            "/api/protected_instance/process_config/file",
            params=params,
            body=content,
        )

    async def files_list(
        self,
        daemon_id: str,
        instance_uuid: str,
        target: str = "/",
        page: int = 0,
        page_size: int = 50,
        file_name: str = "",
    ) -> Any:
        params: dict[str, Any] = {
            "daemonId": daemon_id,
            "uuid": instance_uuid,
            "target": target,
            "page": max(0, page),
            "page_size": max(1, min(page_size, 100)),
        }
        if file_name:
            params["file_name"] = file_name
        return await self.request_data("GET", "/api/files/list", params=params)

    async def files_status(self, daemon_id: str, instance_uuid: str) -> Any:
        return await self.request_data(
            "GET",
            "/api/files/status",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
        )

    async def file_read(self, daemon_id: str, instance_uuid: str, target: str) -> Any:
        return await self.request_data(
            "PUT",
            "/api/files/",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"target": target},
        )

    async def file_write(
        self, daemon_id: str, instance_uuid: str, target: str, text: str
    ) -> Any:
        return await self.request_data(
            "PUT",
            "/api/files/",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"target": target, "text": text},
        )

    async def files_mkdir(self, daemon_id: str, instance_uuid: str, target: str) -> Any:
        return await self.request_data(
            "POST",
            "/api/files/mkdir",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"target": target},
        )

    async def files_touch(self, daemon_id: str, instance_uuid: str, target: str) -> Any:
        return await self.request_data(
            "POST",
            "/api/files/touch",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"target": target},
        )

    async def files_delete(
        self, daemon_id: str, instance_uuid: str, targets: list[str]
    ) -> Any:
        return await self.request_data(
            "DELETE",
            "/api/files",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"targets": targets},
        )

    async def files_copy(
        self, daemon_id: str, instance_uuid: str, targets: list[list[str]]
    ) -> Any:
        return await self.request_data(
            "POST",
            "/api/files/copy",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"targets": targets},
        )

    async def files_move(
        self, daemon_id: str, instance_uuid: str, targets: list[list[str]]
    ) -> Any:
        return await self.request_data(
            "PUT",
            "/api/files/move",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"targets": targets},
        )

    async def files_compress(
        self,
        daemon_id: str,
        instance_uuid: str,
        *,
        action: str,
        source: str,
        targets: list[str],
        code: str = "utf-8",
    ) -> Any:
        return await self.request_data(
            "POST",
            "/api/files/compress",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={
                "type": 1 if action == "zip" else 2,
                "code": code or "utf-8",
                "source": source,
                "targets": targets if action == "zip" else targets[0],
            },
        )

    async def files_chmod(
        self,
        daemon_id: str,
        instance_uuid: str,
        *,
        targets: list[str],
        chmod: int,
        deep: bool = False,
    ) -> Any:
        if len(targets) == 1:
            return await self.request_data(
                "PUT",
                "/api/files/chmod",
                params={"daemonId": daemon_id, "uuid": instance_uuid},
                body={"target": targets[0], "chmod": chmod, "deep": deep},
            )
        return await self.request_data(
            "PUT",
            "/api/files/chmod_batch",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"targets": targets, "chmod": chmod, "deep": deep},
        )

    async def file_download_passport(
        self, daemon_id: str, instance_uuid: str, file_name: str
    ) -> dict[str, Any]:
        data = await self.request_data(
            "GET",
            "/api/files/download",
            params={
                "daemonId": daemon_id,
                "uuid": instance_uuid,
                "file_name": file_name,
            },
        )
        if not isinstance(data, dict):
            raise MCSMError("MCSManager 未返回有效的下载凭证。")
        return data

    async def file_download_url(
        self, daemon_id: str, instance_uuid: str, file_name: str
    ) -> str:
        passport = await self.file_download_passport(
            daemon_id, instance_uuid, file_name
        )
        base = daemon_http_base(passport.get("addr"))
        password = str(passport.get("password") or "")
        if not password:
            raise MCSMError("MCSManager 未返回下载密码。")
        return f"{base}/download/{quote(password, safe='')}/{quote(file_name.lstrip('/'), safe='/')}"

    async def file_upload_passport(
        self, daemon_id: str, instance_uuid: str, upload_dir: str
    ) -> dict[str, Any]:
        data = await self.request_data(
            "POST",
            "/api/files/upload",
            params={
                "daemonId": daemon_id,
                "uuid": instance_uuid,
                "upload_dir": upload_dir,
            },
        )
        if not isinstance(data, dict):
            raise MCSMError("MCSManager 未返回有效的上传凭证。")
        return data

    async def upload_bytes(
        self,
        daemon_id: str,
        instance_uuid: str,
        *,
        upload_dir: str,
        file_name: str,
        content: bytes,
        overwrite: bool = True,
        unzip: bool = False,
        code: str = "utf-8",
    ) -> str:
        passport = await self.file_upload_passport(daemon_id, instance_uuid, upload_dir)
        base = daemon_http_base(passport.get("addr"))
        password = str(passport.get("password") or "")
        if not password:
            raise MCSMError("MCSManager 未返回上传密码。")
        upload_url = f"{base}/upload/{quote(password, safe='')}"
        query = {
            "overwrite": "true" if overwrite else "false",
            "unzip": "true" if unzip else "false",
        }
        if unzip and code:
            query["code"] = code
        form = aiohttp.FormData()
        form.add_field(
            "file",
            content,
            filename=Path(file_name).name,
            content_type="application/octet-stream",
        )
        session = await self._get_session()
        try:
            async with session.post(
                upload_url,
                params=query,
                data=form,
                ssl=self.verify_ssl,
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise MCSMError(
                        self._safe_text(
                            _message_from_error(
                                text, f"守护进程返回 HTTP {response.status}"
                            )
                        )
                    )
                return text.strip() or "OK"
        except asyncio.TimeoutError as exc:
            raise MCSMError(f"上传文件超时（{self.timeout} 秒）。") from exc
        except aiohttp.ClientConnectorCertificateError as exc:
            raise MCSMError(
                "上传文件失败：守护进程证书校验失败。"
                "如果面板使用自签名证书，请在插件设置中关闭「校验 SSL」后重试。"
            ) from exc
        except aiohttp.ClientError as exc:
            raise MCSMError(self._safe_text(f"上传文件失败：{exc}")) from exc

    async def download_from_url(
        self,
        daemon_id: str,
        instance_uuid: str,
        url: str,
        file_name: str,
    ) -> Any:
        return await self.request_data(
            "POST",
            "/api/files/download_from_url",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"url": url, "file_name": file_name},
        )

    async def stop_download_from_url(
        self, daemon_id: str, instance_uuid: str, task_id: str
    ) -> Any:
        return await self.request_data(
            "POST",
            "/api/files/download_from_url_stop",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
            body={"taskId": task_id},
        )

    async def async_task(
        self,
        daemon_id: str,
        instance_uuid: str,
        *,
        action: str,
        task_name: str,
        task_id: str = "",
        body: JSONValue = None,
    ) -> Any:
        if action == "start_update":
            return await self.request_data(
                "POST",
                "/api/protected_instance/asynchronous",
                params={
                    "daemonId": daemon_id,
                    "uuid": instance_uuid,
                    "task_name": task_name or "update",
                },
                body=body if body is not None else {},
            )
        if action == "query":
            payload = dict(body) if isinstance(body, dict) else {}
            if task_id:
                payload["taskId"] = task_id
            return await self.request_data(
                "POST",
                "/api/protected_instance/query_asynchronous",
                params={
                    "daemonId": daemon_id,
                    "uuid": instance_uuid,
                    "task_name": task_name or "update",
                },
                body=payload,
            )
        if action == "stop":
            payload = {"taskId": task_id} if task_id else {}
            return await self.request_data(
                "POST",
                "/api/protected_instance/stop_asynchronous",
                params={"daemonId": daemon_id, "uuid": instance_uuid},
                body=payload,
            )
        raise MCSMError(f"不支持的异步任务操作：{action}")
