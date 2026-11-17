# AGENTS.md

## 项目定位

这是 AstrBot 的 MCSManager 插件，目标版本为 AstrBot `>=4.29.0`。插件通过 MCSManager 普通用户 API Key 获取可操作实例，并把实例能力暴露为 LLM Tools 与 WebUI 可视化视图。

## 代码结构

- `main.py`：插件入口、Web API、工具调度、权限校验和状态相关逻辑。
- `mcsm_client.py`：MCSManager Panel/Daemon API 客户端。
- `tooling.py`：LLM Tool 的 JSON Schema 与版本兼容适配。
- `state_store.py`：会话绑定和实例权限的持久化。
- `sessions.py`：AstrBot 会话与白名单发现。
- `views/mcsm/`：插件可视化页面，包含蓝图与设置两个 Tab。
- `_conf_schema.json`：AstrBot 插件配置 Schema。

## 开发约束

- 网络请求使用 `aiohttp`，不要使用同步 `requests`。
- 所有工具调用必须校验当前会话的实例绑定和实例权限。
- 调用 `mcsm_send_command` 前必须确认实例 `status == 3`（运行中）；实例未运行时返回结构化错误，而不是发送命令或返回空值。
- 命令输出必须有上限。默认 8000 字符、最大 12000 字符；超长时使用快照与 `mcsm_read_command_output` 分页读取。
- Java 版本检测、安装和切换优先使用实例内 `javm`。
- 不要提交 API Key、Cookie、用户配置、运行时状态文件或命令输出快照。
- 修改 WebUI 时保持 AstrBot bridge 调用方式，不要直接访问 Dashboard Cookie 或父页面 DOM。

## 本地检查

修改后至少执行：

```powershell
python -m py_compile main.py mcsm_client.py tooling.py sessions.py state_store.py
node --check .\views\mcsm\app.js
```

如果环境中有 Ruff：

```powershell
ruff check .
ruff format --check .
```

## 远端环境

当前生产 AstrBot 使用 Docker Compose 部署，插件目录为：

```text
/opt/1panel/apps/astrbot/astrbot/data/plugins/astrbot_plugin_mcsm
```

不要将远端配置、API Key 或运行时数据复制回仓库。