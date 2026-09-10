# litellm-Bvio 分支维护说明

## 当前基线

`litellm-Bvio` 基于官方稳定版 `v1.100.0`（`e4f25265704e2b2c6cf6e81be2e4c5cffff896f4`，2026-09-06 发布）

本次升级重放原有五个功能提交，补充 API 适配与回归测试，并合入远端截至 `f5e7f4ca4ed4c7537c1971fb9797c48b38d104f0` 的后续构建修复。具体比较与验证记录见 [升级评估](BVIO_UPGRADE_AUDIT.md)

镜像构建使用完整 checkout 的仓库根 `Dockerfile`，构建上下文需要包含 UI、Rust 和其他构建目录。保留 v1.100.0 的 glibc 2.44 基础镜像与 Python 3.13；UI 安装不挂载 npm cache，uv 禁止托管 Python 下载并显式使用 `/usr/bin/python3.13`

## 自动构建与镜像发布

[Docker Build and Publish](.github/workflows/docker-publish.yml) 在每次 push 到 `litellm-Bvio` 时构建根 Dockerfile，并推送 `linux/amd64` 镜像到 ECR。分支标签为 `litellm-bvio`，固定提交标签为 `sha-<完整提交 SHA>`。同分支的新 push 会取消尚未完成的旧构建，构建缓存保存在 GitHub Actions

在仓库 Settings > Secrets and variables > Actions 配置以下 Repository variables：

| 变量 | 内容 |
| --- | --- |
| `AWS_REGION` | 目标 ECR 所在区域 |
| `AWS_ROLE_ARN` | GitHub Actions 通过 OIDC 承担的 IAM role |
| `ECR_REGISTRY_ID` | 目标 ECR 的 12 位 AWS 账号 ID |
| `ECR_REPOSITORY` | ECR repository 名称，例如 `violoop/litellm` |

IAM role 的信任策略需允许本仓库 `litellm-Bvio` 分支的 GitHub OIDC 身份，权限需覆盖目标 repository 的镜像推送与读取，以及 `ecr:GetAuthorizationToken`。目标 repository 需预先存在；跨账号推送还需目标 repository policy 授权

飞书通知使用 Repository secret `FEISHU_WEBHOOK`，构建成功或失败后发送结果、提交和 Actions 链接；成功时同时发送镜像地址与 digest。未设置该 Secret 时跳过通知。Webhook 不写入源码；通知失败不改变镜像构建结果

Actions summary 保存固定提交镜像地址和 digest，可据此选择部署版本。手动触发时选择 `litellm-Bvio` 分支；工作流进入仓库默认分支后，GitHub 才会显示 `workflow_dispatch` 入口

## 保留的行为

以下结论以 `v1.100.0` 的实际代码为准

| 功能 | 本分支处理 |
| --- | --- |
| 内容过滤结束原因 | 将 `content_filter` 映射为 Anthropic `refusal`，保留相应类型 |
| 空回复诊断 | 完成流没有可见文本或工具调用时记录一次 warning，包含模型、结束原因、输出 token 数 |
| ARN 下的 adaptive thinking | application inference profile 不因无法解析模型能力而被降级为固定 thinking 预算 |
| 旧 thinking 参数 | 通过 `model_info.base_model` 识别真实模型；需要 adaptive 的模型执行转换，Claude 4.6 保留上游接受的显式 legacy 预算 |
| 高 effort 与关闭 thinking 的冲突 | 保留既有 Bvio 策略：`xhigh` / `max` 所需的 thinking 缺失或关闭时启用 adaptive；`high` 及以下尊重显式 disabled |
| 缺少 thinking 时的 effort | Anthropic Messages 桥仍转发 Bedrock 原生 `output_config.effort` |
| OpenAI 风格 reasoning_effort | 在 Claude 的真实 base model 上恢复档位，保留 Opus 5 的 `max` 和调用方显式原生字段 |
| Bedrock 账号访问拒绝 | 精确识别 Anthropic 账号拒绝错误，首次失败即冷却该 deployment，并在现有重试预算内选择健康候选 |

## Bedrock 账号访问拒绝与选路

Router 对 Bedrock 的 `BadRequestError` / `PermissionDeniedError` 提取上游 JSON 顶层 `message`，使用完整匹配识别 `Access to Anthropic models is not allowed for this account.`。规则容忍大小写、空白和句末句点差异，普通参数错误及其他 provider 保持既有处理

命中后立即将失败的 deployment 加入 cooldown，时长沿用既有 `cooldown_time`；`disable_cooldowns` 和 deployment 的 `cooldown_time: 0` 仍可停用冷却。冷却按 deployment ID 生效，共用 AWS 账号的其他 deployment 不会被批量修改

当同一模型组还有健康候选时，当前请求可继续重试，受原有 `num_retries` / retry policy 约束。`num_retries: 0` 会保留错误并冷却失败的 deployment，后续请求可跳过它；没有健康候选时返回错误。该规则不会遍历超出重试预算的所有账号，也不会自动创建跨模型组 fallback

本地 HTTP 服务回归覆盖 Chat Completions 和 Anthropic Messages 的流式及非流式入口：两个 deployment 连续返回该 400 后，同一请求从第三个获得回复；普通 400 不触发冷却或重试；禁用重试与全部候选被拒绝时正常终止。流式成功响应使用真实 Bedrock EventStream 编码，验证回复文本和 Messages 终止事件仅输出一次。相关冷却、重试与加权 failover 回归共 170 passed；未进行真实 AWS 或线上验收

## v1.100.0 适配

上游 `_translate_reasoning_effort_to_anthropic` 新增必填 `max_tokens`。本分支传入映射后的 `maxTokens`，使 `max_completion_tokens` 优先级和 legacy thinking 预算上限保持一致

上游的 `reasoning_effort` 映射现在生成 `thinking.display: summarized`。本分支保留此行为，调用方显式指定的原生字段仍优先

Claude 的重新映射只作用于真实 Bedrock base model 解析为 `anthropic.*` 的部署，GPT-OSS 和 Nova 2 保留各自的 reasoning 参数

本分支复用上游 `normalize_bedrock_opus_output_config_effort`。例如 Claude Opus 4.6 的 `xhigh` 按上游规则归一为 `max`；对原生 `output_config` 的归一化在 provider-specific 参数合并后执行，并先复制字典以保护调用方对象

空回复诊断将已排队的 `tool_use` 起始块计为可见输出，因此零参数工具调用也能被正确识别。同步、异步和 thinking 转工具调用均有回归用例

## 模型配置

每条 application inference profile deployment 都应声明与真实模型匹配的 Bedrock `base_model`，并将 `model_info` 放在 `litellm_params` 的同一层

```yaml
model_list:
  - model_name: claude-opus-5
    litellm_params:
      model: bedrock/arn:aws:bedrock:us-east-2:ACCOUNT:application-inference-profile/PROFILE
      aws_region_name: us-east-2
    model_info:
      base_model: us.anthropic.claude-opus-5
```

此类 ARN 会自动选择 Converse。`base_model` 用于模型能力识别和相关元信息，实际调用目标仍由 `litellm_params.model` 指定。`us.anthropic.claude-opus-5` 和 `global.anthropic.claude-opus-5` 都能识别 Opus 5 的 thinking 能力

Anthropic Messages 请求使用 `output_config.effort`；OpenAI Chat Completions 请求可使用 `reasoning_effort`。把 OpenAI 风格 `reasoning_effort=max` 直接交给 Messages 桥时，裸 ARN 在升级前后都会被上游归一为 `high`，此输入边界未在本次扩展处理

## 验证与发布边界

已对适配后的源码运行 Bedrock Chat 和 Anthropic Adapters 两个目录的测试：740 passed，0 failed。使用既有 Python 3.13 虚拟环境和固定的本地模型表，未重新构建 Rust 扩展或安装整套新 lockfile 环境

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest \
  tests/test_litellm/llms/bedrock/chat/ \
  tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/ \
  -q --tb=short
```

发布还需完整镜像构建、数据库迁移演练和真实 Bedrock 请求验收。v1.100.0 更新了 Docker 基础镜像并固定使用 Python 3.13；从原基线比较涉及 12 个数据库迁移文件，实际待执行项以部署数据库的迁移记录为准

本记录覆盖源码兼容验证，线上 `thinking.type.enabled` 报错仍需通过实际镜像版本与请求日志核对
