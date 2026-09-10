# LiteLLM Bvio 升级评估

评估日期：2026-09-09；分支交付记录更新：2026-09-10

## 结论

`litellm-Bvio` 的源码基线升级到官方稳定版 v1.100.0，保留原有补丁并补充 API 适配。本次已完成独立工作区试 rebase、本地兼容验证和复审，交付范围为源码分支更新

| 对象 | 固定版本 |
| --- | --- |
| 功能补丁原始末端 | `7fa0606f854c394c7868f1634368edab07d0d2d6` |
| 升级前远端 Bvio 末端 | `f5e7f4ca4ed4c7537c1971fb9797c48b38d104f0` |
| 原上游基线 | `0e9cd9893e`，包版本 1.98.0 |
| 本次目标 | `v1.100.0`，`e4f25265704e2b2c6cf6e81be2e4c5cffff896f4` |
| 读取时的官方 main | `328a5f5d6024c673c4d5e37bad8dab17ab8e79ee` |
| 交付分支 | `litellm-Bvio` |

官方 [v1.100.0](https://github.com/BerriAI/litellm/releases/tag/v1.100.0) 于 9 月 6 日发布，是检查时的最新稳定版。另有 v1.101.0-rc.1；main 比选定稳定 tag 还有 2,128 个提交，因此本次选择稳定版作为候选基线

从原上游基线到 v1.100.0，有 2,956 个新增提交、5,023 个变化路径。计数关闭 Git 重命名检测，属于树差异计数

## 与当前使用有关的更新

| 范围 | 更新及价值 |
| --- | --- |
| Anthropic / Bedrock thinking | reasoning_effort 请求带 summarized thinking；使用 provider 返回的 thinking token 数；Claude 4.6 保留显式 legacy 预算；预算按 max_tokens 限制 |
| 流式响应 | 工具起始块及时发送、空 thinking 块处理、signature-only thinking 块输出、慢上游的 SSE 心跳、Messages 流式错误及 fallback 处理 |
| 缓存计费与日志 | Bedrock Converse 区分 1 小时与 5 分钟缓存写入；缓存计费修复；Request Logs 增加缓存命中及 session 观察能力 |
| 错误处理 | 预期 4xx 不再大量消耗日志 worker CPU；保留 provider 4xx traceback；改进 Messages 错误序列化及中断请求的部分用量记录 |
| 其他网关能力 | Responses previous_response_id 桥接上下文、更多 provider 与模型表更新、MCP OAuth 与工具参数转发、管理界面和自动路由改进 |

上述更新见 [v1.99.0](https://github.com/BerriAI/litellm/releases/tag/v1.99.0) 和 [v1.100.0](https://github.com/BerriAI/litellm/releases/tag/v1.100.0) 发布记录。关键行为还对照了目标 tag 的实际源码

流式 fallback 的增强不意味着未配置 fallback 的模型组会自动获得备用模型；心跳也不能替代缺失的终止事件

## Bvio 补丁适配结果

原有五个提交均已重放。保留范围仍集中于五个源码文件：`utils.py`、Bedrock Converse transformation、Anthropic adapter transformation、adapter streaming iterator、Anthropic 类型

| 冲突位置 | 合并方式 |
| --- | --- |
| adapter streaming iterator | 保留上游工具起始块及时 flush，叠加 Bvio 可见输出诊断 |
| adapter transformation | 保留上游其他 Claude provider 的 effort 能力分发，叠加 Bedrock 缺少 thinking 时的 effort 转发 |
| Anthropic 类型 | 保留上游 thinking_tokens 详情类型，加入 Bvio refusal 结束原因 |

原样重放后，测试发现新版 effort mapper 新增必填 max_tokens。适配后传入映射后的 maxTokens，并更新 summarized thinking 的精确测试预期

独立复审又复现并修正了旧补丁的三个边界：Claude Opus 4.6 的 effort 归一化、非 Claude Bedrock 模型被错误加入 Anthropic thinking、零参数工具调用被误报为空回复。相应测试覆盖了 ARN 和真实模型 ID、原生字段优先级、调用方对象不被修改、GPT-OSS / Nova 2，以及同步和异步工具流

v1.100.0 仍没有覆盖 Bvio 的全部需求，尤其是 ARN 的能力识别和无 thinking 时的 effort 转发。读取时的 main 已新增 [2063c29f5d](https://github.com/BerriAI/litellm/commit/2063c29f5d95f8dd00eef3fd7dfcbc1df05787b8)，对直接模型 ID 的 Converse legacy thinking 增加转换；该挂载点仍使用 model 原串，不能据此认定已解决隐藏模型信息的 application profile ARN

## 后续构建提交合入

交付前重新读取远端，确认其比原功能补丁末端多四个 Docker 构建提交，已按最终行为合入：

| 原提交 | 处理 |
| --- | --- |
| `be37a1f510` / `d33543175d` | 后一个提交覆盖前一个 npm cache 设置，三个 UI builder 均保留最终的 `RUN npm ci --prefer-offline` |
| `0347d6d213` | 三个主构建文件使用 `UV_PYTHON_DOWNLOADS=never`，所有 uv 构建和安装命令显式选择 `/usr/bin/python3.13`，结合上游固定的 APK Python 3.13 |
| `f5e7f4ca4e` | glibc 2.44 修复目的已被上游更新覆盖，六个 Dockerfile 保留 v1.100.0 的 `e624c5d5e42382ce7165ddafcbbf8e6769a24cbd02ea6114b880b05ae5ba2a8d` 基础镜像 digest |

固定的 uv 0.11.7 将 `UV_PYTHON_DOWNLOADS=0` 和 `never` 均解析为禁止下载；使用 `never` 与远端修复保持一致。Docker 配置通过静态检查和独立复审，本机 Docker daemon 未运行，尚未进行镜像构建或启动验证

## 验证证据

Bedrock Chat 与 Anthropic Adapters 两个目录：740 tests，0 failures，0 errors。原有 Bvio 测试随上游行为适配，并新增 token 上限、4.6 归一化、非 Claude 和零参数工具的回归覆盖

测试运行时确认导入的是 v1.100.0 重放并适配后的源码。测试使用现有 Python 3.13 虚拟环境，设置 LITELLM_LOCAL_MODEL_COST_MAP=True 固定模型表；这验证了适配后的 Python 代码，没有验证全新 lockfile 环境和新 Rust 构建

另以注入的 HTTP 客户端捕获最终 Converse 请求，不访问 AWS：验证 Chat Completions 的 legacy enabled、effort-only、reasoning_effort=max，以及 Anthropic Messages 的 legacy enabled、effort-only、adaptive + max。六个请求捕获均通过，实际请求体同时包含 adaptive thinking 和期望 effort

Ruff 的 E9/F63/F7/F82 检查和 git diff --check 通过。本次未运行全仓 make check；完整构建与依赖环境验证仍属于发布阶段

独立 persona-judge 复审结论为 accept。结论限于标准接口的本地兼容验证

额外输入边界：Messages 桥直接接收 OpenAI 风格 reasoning_effort=max 时，裸 ARN 在升级前后均被归一为 high；使用 Anthropic 原生 output_config.effort=max 的路径已验证。该既有输入边界未扩展为本次 handler 改动

## 发布前尚未覆盖的工作

需要使用完整 checkout 的根 Dockerfile 完成构建和启动验证。本次代码验证使用稀疏检出，未构建镜像

需要检查实际数据库的迁移状态并演练升级。原基线到 v1.100.0 涉及 12 个变化的迁移 SQL 文件，包含 spend log 时间字段、预算窗口、worker heartbeat、shadow eval 等结构变化。本次未连接或修改数据库

需要使用实际配置执行真实 Bedrock 的文本、工具调用、thinking 和流式请求验收，并核对镜像 digest / 源码 SHA。本次离线结果不能确定当前线上 enabled 报错的根因，也未证明线上 0.0.6 镜像包含哪个提交

镜像构建、镜像推送和线上部署不在本次源码分支交付范围内
