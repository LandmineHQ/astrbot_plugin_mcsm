from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

if TYPE_CHECKING:
    from main import MCSMPlugin


class _BoundToolHandler:
    """Callable tool adapter that is immune to AstrBot handler call variants."""

    def __init__(self, plugin: MCSMPlugin, tool_name: str) -> None:
        self.plugin = plugin
        self.tool_name = tool_name

    async def __call__(self, *args: Any, **kwargs: Any) -> str:
        try:
            event = self.plugin._extract_tool_event(args)
        except Exception as exc:
            return self.plugin._tool_error(str(exc))
        kwargs.pop("tool_name", None)
        return await self.plugin.dispatch_tool(event, self.tool_name, **kwargs)


@dataclass
class MCSMTool(FunctionTool[AstrAgentContext]):
    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        event = context.context.event
        if self.handler is None:
            raise RuntimeError(f"MCSM tool {self.name} has no handler")
        return await self.handler(event, **kwargs)


def _schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _number(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "number", "description": description, **extra}


def _boolean(description: str, default: bool | None = None) -> dict[str, Any]:
    value = {"type": "boolean", "description": description}
    if default is not None:
        value["default"] = default
    return value


def _array(description: str, item_type: str = "string") -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": {"type": item_type},
    }


def _object(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "additionalProperties": True,
    }


def _tool_specs() -> list[dict[str, Any]]:
    instance = _string(
        "实例名称、UUID、daemonId:UUID，或绑定的实例 key。仅允许当前会话已授权的实例。"
    )
    return [
        {
            "name": "mcsm_list_instances",
            "description": "列出当前群/会话已授权给机器人的 MCSManager 实例、运行状态和权限。执行任何 MCSM 操作前可先调用。",
            "parameters": _schema(),
        },
        {
            "name": "mcsm_get_instance",
            "description": "读取一个已授权 MCSManager 实例的详细配置、状态、镜像和资源信息。",
            "parameters": _schema({"instance": instance}, ["instance"]),
        },
        {
            "name": "mcsm_get_console",
            "description": "读取已授权实例的最新控制台日志（会去除 ANSI 控制字符并限制返回长度）。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "size": _string("日志截取大小，例如 4KB、20KB；默认 20KB。"),
                    "max_chars": _number(
                        "最多返回字符数，默认 12000，范围 500-40000。"
                    ),
                },
                ["instance"],
            ),
        },
        {
            "name": "mcsm_send_command",
            "description": (
                "向已授权实例的控制台发送一条命令，等待片刻后返回本次命令产生的新控制台输出。适合查询服务器状态、执行 Minecraft 指令或容器命令。"
                "涉及检测、安装或切换 Java 版本时，必须优先使用实例内的 javm：先用 `javm current` 查当前版本、`javm ls` 查已安装版本；切换当前终端用 `javm use <版本>`，设置新终端默认版本用 `javm default <版本>`；查询/安装可用 `javm ls-remote` / `javm install`。不要先直接修改 JAVA_HOME 或替换 JDK 文件。"
                "返回给模型的命令日志总预算默认 8000 字符、最大 12000 字符；超长输出会自动保留头尾摘要并返回 snapshot_id，不会一次返回完整日志。"
                "如果实例不是运行中（status != 3），工具不会发送命令，而是返回 ok=false、status、status_text、sent=false 和可读提示。"
            ),
            "parameters": _schema(
                {
                    "instance": instance,
                    "command": _string("要发送到实例控制台的原样命令。"),
                    "wait_ms": _number(
                        "发送后等待多少毫秒再读取日志，默认 1200，范围 0-10000。"
                    ),
                    "max_chars": _number("最多返回字符数，默认 6000，范围 500-20000。"),
                },
                ["instance", "command"],
            ),
        },
        {
            "name": "mcsm_read_command_output",
            "description": "分页读取 mcsm_send_command 因超长而保存的完整命令输出快照。每页默认 6000 字符，最大 12000 字符。",
            "parameters": _schema(
                {
                    "snapshot_id": _string("mcsm_send_command 返回的 16 位快照 ID。"),
                    "offset": _number(
                        "字符偏移量，从 0 开始；首尾读取使用返回的 next_offset。"
                    ),
                    "limit": _number("本页字符数，默认 6000，范围 512-12000。"),
                },
                ["snapshot_id"],
            ),
        },
        {
            "name": "mcsm_power",
            "description": "启动、停止、重启或强制终止已授权实例。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "action": _string(
                        "操作类型。",
                        enum=["start", "stop", "restart", "kill"],
                    ),
                },
                ["instance", "action"],
            ),
        },
        {
            "name": "mcsm_list_files",
            "description": "分页列出已授权实例目录中的文件和文件夹。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "target": _string("实例内目录路径，默认 /。"),
                    "page": _number("页码，从 0 开始。"),
                    "page_size": _number("每页数量，1-100，默认 50。"),
                    "file_name": _string("可选的文件名过滤关键字。"),
                },
                ["instance"],
            ),
        },
        {
            "name": "mcsm_read_file",
            "description": "读取已授权实例中的文本文件内容，默认最多返回 20000 字符。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "target": _string(
                        "实例内文件的绝对路径，例如 /server.properties。"
                    ),
                    "max_chars": _number(
                        "最多返回字符数，默认 20000，范围 500-100000。"
                    ),
                },
                ["instance", "target"],
            ),
        },
        {
            "name": "mcsm_write_file",
            "description": "把文本内容写入已授权实例中的文件（覆盖写入）。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "target": _string("实例内文件路径。"),
                    "text": _string("写入的完整文本内容。"),
                },
                ["instance", "target", "text"],
            ),
        },
        {
            "name": "mcsm_manage_files",
            "description": "执行文件管理操作：删除、创建目录、新建空文件、复制、移动/重命名、压缩、解压、chmod，或停止 URL 下载任务。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "action": _string(
                        "文件操作。",
                        enum=[
                            "delete",
                            "mkdir",
                            "touch",
                            "copy",
                            "move",
                            "zip",
                            "unzip",
                            "chmod",
                            "stop_download",
                        ],
                    ),
                    "target": _string(
                        "单个目标路径（mkdir/touch/chmod/解压目标等使用）。"
                    ),
                    "source": _string("源文件路径（copy/move/zip/unzip 使用）。"),
                    "destination": _string("目标路径（copy/move 使用）。"),
                    "targets": _array("多个目标路径（delete/zip/chmod 使用）。"),
                    "chmod": _number("Linux 权限数字，例如 755。"),
                    "deep": _boolean("chmod 是否递归应用到目录。", False),
                    "task_id": _string("要停止的下载任务 ID。"),
                },
                ["instance", "action"],
            ),
        },
        {
            "name": "mcsm_file_download_link",
            "description": "为已授权实例中的文件生成一次性下载链接。链接可直接交给用户。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "file_name": _string("实例内文件路径，例如 /logs/latest.log。"),
                },
                ["instance", "file_name"],
            ),
        },
        {
            "name": "mcsm_file_upload_base64",
            "description": "将 Base64 内容作为文件上传到已授权实例。适用于机器人收到的文件、文本或小型二进制内容。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "upload_dir": _string("上传目录，默认 /。"),
                    "file_name": _string("上传后的文件名。"),
                    "content_base64": _string("文件内容的 Base64 编码。最大 8 MiB。"),
                    "overwrite": _boolean("同名文件是否覆盖，默认 true。", True),
                    "unzip": _boolean(
                        "上传后是否让守护进程自动解压，默认 false。", False
                    ),
                },
                ["instance", "file_name", "content_base64"],
            ),
        },
        {
            "name": "mcsm_file_download_from_url",
            "description": "让已授权实例从 URL 在后台下载文件，返回下载任务 ID。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "url": _string("要下载的 http(s) URL。"),
                    "file_name": _string("保存到实例内的路径，例如 /backup/file.zip。"),
                },
                ["instance", "url", "file_name"],
            ),
        },
        {
            "name": "mcsm_file_status",
            "description": "查询已授权实例的文件任务状态，包括 URL 下载任务进度。",
            "parameters": _schema({"instance": instance}, ["instance"]),
        },
        {
            "name": "mcsm_process_config",
            "description": "检查、读取或写入实例工作目录中的进程配置文件（如 server.properties、config.yml）。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "action": _string("操作类型。", enum=["list", "read", "write"]),
                    "files": _array("action=list 时要检查的文件路径列表。"),
                    "file_name": _string("action=read/write 时的文件路径。"),
                    "file_type": _string("可选的配置类型提示。"),
                    "content": _object("action=write 时要写入的配置对象。"),
                },
                ["instance", "action"],
            ),
        },
        {
            "name": "mcsm_async_task",
            "description": "执行实例异步任务，例如启动软件更新，或查询、停止更新任务。",
            "parameters": _schema(
                {
                    "instance": instance,
                    "action": _string(
                        "操作类型。", enum=["start_update", "query", "stop"]
                    ),
                    "task_name": _string("任务类型，默认 update。"),
                    "task_id": _string("查询或停止指定任务时使用。"),
                    "body": _object(
                        "可选的任务请求体。start_update 会自动补充时间戳。"
                    ),
                },
                ["instance", "action"],
            ),
        },
        {
            "name": "mcsm_api",
            "description": "MCSManager 原始 API 兜底工具。可调用官方文档中的面板 API；插件会根据路径自动映射到实例权限分类，并拒绝账号/API Key 管理接口。",
            "parameters": _schema(
                {
                    "instance": _string(
                        "用于权限校验和自动补全 daemonId/uuid 的已授权实例；全局 API 可留空。"
                    ),
                    "method": _string(
                        "HTTP 方法。", enum=["GET", "POST", "PUT", "DELETE", "PATCH"]
                    ),
                    "path": _string(
                        "以 /api/ 开头的面板 API 路径，不要包含 query string。"
                    ),
                    "query": _object("查询参数对象。"),
                    "body": _object("JSON 请求体对象。"),
                },
                ["method", "path"],
            ),
        },
    ]


def build_tools(plugin: MCSMPlugin) -> list[MCSMTool]:
    tools: list[MCSMTool] = []
    for spec in _tool_specs():
        handler = _BoundToolHandler(plugin, spec["name"])
        tools.append(
            MCSMTool(
                name=spec["name"],
                description=spec["description"],
                parameters=spec["parameters"],
                handler=handler,
                handler_module_path=__package__ or "astrbot_plugin_mcsm",
            )
        )
    return tools
