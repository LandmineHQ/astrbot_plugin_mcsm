# AstrBot MCSManager 插件

将 MCSManager 面板接入 AstrBot，支持：

- 自动发现当前 API Key 可操作的实例；
- 在 WebUI「蓝图」页把实例授权给具体群会话 / SID；
- 为单个实例设置机器人可用的能力；
- 通过 LLM Tools 操作实例：状态、控制台、命令、启停、文件管理、上传下载、进程配置和异步任务；
- 提供通用 MCSManager API 兜底工具。

## 安装

把本目录放到 AstrBot 的：

```text
data/plugins/astrbot_plugin_mcsm/
```

然后在 AstrBot WebUI 中重载插件。

## 配置

打开插件页面「设置」：

- **Base URL**：MCSManager 面板根地址，例如 `https://mcsm.example.com`，不要填写 `/api`；
- **API Key**：MCSManager 账号 API Key。API Key 继承账号权限，普通账号只会发现已分配给它的实例；
- **校验 SSL**：面板或守护进程使用自签名证书时可关闭；
- **请求超时**：HTTP 请求超时时间。

点击 API Key 右侧的铅笔按钮会使用当前表单值发起一次 `/api/auth?advanced=1` 请求，确认 API Key 是否有效。

> 如果上传/下载使用的 MCSManager 守护进程地址是自签名证书，需要关闭「校验 SSL」；否则面板 API 可用，但文件上传与下载链接会因证书校验失败。

## Java 版本操作策略

只要当前会话绑定了 MCSManager 实例，插件就会在 LLM 请求中注入一条额外提示：

- 检测当前 Java 版本时优先使用 `javm current`；
- 查看已安装版本优先使用 `javm ls`；
- 切换当前终端版本使用 `javm use <版本>`；
- 设置新终端默认版本使用 `javm default <版本>`；
- 查询或安装版本使用 `javm ls-remote` / `javm install`。

只有 `javm` 不存在或执行失败时，才回退到 `java -version` 或常规排查。这样可避免模型直接修改 `JAVA_HOME` 或替换实例内 JDK 文件。

## 命令日志上限

`mcsm_send_command` 会在发送前检查实例状态。实例不是 `status=3`（运行中）时不会发送命令，而是返回 `ok=false`、`status`、`status_text`、`sent=false` 和启动提示。

实例终端只有一个输入通道，不能并行写命令。插件对同一实例使用异步锁串行执行；LLM 也不应在同一轮并行调用多个 `mcsm_send_command`，多条命令应等待前一条完成，或合并为一条。

实例终端只有一个输入通道，不能并行写命令。插件对同一实例使用异步锁串行执行；LLM 也不应在同一轮并行调用多个 `mcsm_send_command`，多条命令应等待前一条完成，或合并为一条。

所有文件路径均相对实例工作目录；`/` 表示工作目录本身，例如 `/cy3-3.14.2-linux-server/` 表示工作目录下的子目录。

`mcsm_send_command` 不会无限返回控制台内容：

- 默认单次最多返回 8,000 字符；
- 模型显式请求时最多 12,000 字符；
- 超出预算时保留头尾摘要，并返回 `snapshot_id`；
- 完整输出保存到插件数据目录，24 小时后自动过期；
- 使用 `mcsm_read_command_output`，通过 `snapshot_id`、`offset` 和 `limit` 分页读取，每页默认 6,000、最大 12,000 字符。

8,000 字符大致相当于 2,000-4,000 个中英文混合 token，足以覆盖常见异常栈和启动失败信息，同时避免一次占满上下文。

## 蓝图授权

1. 左侧展示当前 Key 可操作的实例；
2. 点击实例卡片右上角齿轮，设置该实例允许的工具能力；
3. 从右侧选择一个群 / SID；
4. 把左侧实例拖入激活区域，即完成该群对该实例的授权；
5. 已绑定的会话会排在 SID 下拉列表前面，并显示圆角状态灯。

机器人运行时只会看到当前会话绑定的实例。没有绑定、实例已被移出 MCSManager 账号权限，或对应能力未开启时，工具会拒绝执行。

## 默认权限

新实例默认开启：

- 查看实例
- 读取控制台
- 读取文件

默认关闭：

- 发送指令
- 启停控制
- 写入文件
- 高级管理

管理员可以在蓝图页按实例调整。

## Tools

常用工具：

- `mcsm_list_instances`
- `mcsm_get_instance`
- `mcsm_get_console`
- `mcsm_send_command`
- `mcsm_read_command_output`
- `mcsm_power`
- `mcsm_list_files`
- `mcsm_read_file`
- `mcsm_write_file`
- `mcsm_manage_files`
- `mcsm_file_download_link`
- `mcsm_file_upload_base64`
- `mcsm_file_download_from_url`
- `mcsm_file_status`
- `mcsm_process_config`
- `mcsm_async_task`
- `mcsm_api`

`mcsm_api` 允许访问 `/api/` 下的 MCSManager 面板 API，但不允许访问账号与 API Key 管理接口；它会根据路径和操作类型映射到实例权限。

## 安全说明

- API Key 只保存在插件配置中，不会回显到 WebUI；设置页仅显示掩码。
- 插件通过 AstrBot 的插件 Web API bridge 访问后端，不把 Dashboard Cookie 暴露给 iframe。
- 工具调用会同时校验：当前会话和实例是否绑定、实例是否仍在当前 API Key 权限内、实例权限是否开启。
- 通用 API 工具不会越过实例权限分类；`admin` 权限需要显式开启。

## 参考

- [MCSManager 官方文档](https://docs.mcsmanager.com/)
- [AstrBot 插件可视化视图](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)

本项目基于 MCSManager 普通用户 API Key 方式设计，管理员 API Key 同样可用，但插件仍按绑定和权限执行。