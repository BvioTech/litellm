# Claude Opus 5 Bedrock Application Inference Profile 修复记录

## 目标

让 Claude Code 通过 LiteLLM 调用 AWS Bedrock Claude Opus 5 时，自动把旧版 thinking 参数转换为 Opus 5 要求的 adaptive thinking，同时保持 Opus 4.5 等旧模型行为不变

目标请求转换如下

```json
{
  "thinking": {
    "type": "enabled",
    "budget_tokens": 4096
  }
}
```

转换为

```json
{
  "thinking": {
    "type": "adaptive"
  },
  "output_config": {
    "effort": "high"
  }
}
```

## 问题现象

Claude Code 仍发送兼容旧模型的 `thinking.type=enabled` 和 `budget_tokens`，但 Bedrock 上的 Opus 5 只接受 `thinking.type=adaptive`，并要求使用 `output_config.effort` 控制推理强度

典型错误为

```text
"thinking.type.enabled" is not supported for this model. Use
"thinking.type.adaptive" and "output_config.effort" to control thinking behavior
```

## 根因

实际 deployment 使用 AWS Bedrock application inference profile ARN，例如

```text
arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/abcdef123456
```

这种 ARN 不包含 `anthropic`、`claude` 或 `opus-5`，LiteLLM 无法仅从 ARN 判断底层模型能力

Router 对外暴露的 `model_name: claude-opus-5` 只是用户可自定义的模型组名称，可能包含多个不同 deployment，不能作为可靠的能力判断依据。底层模型必须通过 deployment 的 `model_info.base_model` 显式声明

## 分支策略

旧的 `pr-32983` 分支基于较早的 staging，当时已经落后上游 `litellm_internal_staging` 1090 个提交，不适合作为新的交付基线

本次从最新的上游 `litellm_internal_staging` 创建新分支，并手工迁移最小修复行为，避免直接合并陈旧分支和无关 Invoke 改动

```text
分支: litellm_opus_5_latest_staging
基线提交: 4d543245
修复提交: cd932ae2a81ea082d442b8cdc40e376a95eda8cc
```

远程分支为 `origin/litellm_opus_5_latest_staging`

该分支相对目标 staging 为 ahead 1、behind 0

## 修复实现

### 1. Router 传递底层模型信息

`litellm/router.py` 从选中的 deployment 读取顶层 `model_info`，并写入调用参数

```python
model_info = deployment.get("model_info", {}).copy()
kwargs["model_info"] = model_info
```

这是 LiteLLM 原有机制，本次修复直接复用，不根据模型组名称猜测底层模型

### 2. Completion 主调用链读取 base model

`litellm/main.py` 优先读取直接传入的 `base_model`，否则读取 `model_info.base_model`，再把结果传给 `get_optional_params`

```python
base_model = kwargs.get("base_model") or model_info.get("base_model")
```

### 3. Bedrock Converse 使用 base model 判断能力

`litellm/utils.py` 只在 Bedrock `converse` 或 `converse_like` 路径执行转换

```python
if bedrock_route in ("converse", "converse_like"):
    capability_model = base_model or model
```

application profile ARN 无法识别能力时，`capability_model` 使用配置的真实底层模型 ID

### 4. 将 legacy thinking 转换为 adaptive thinking

`litellm/llms/anthropic/chat/transformation.py` 中的共享转换函数先查询模型能力表

只有满足以下条件才转换

- 模型能力表声明 `supports_adaptive_thinking: true`
- `thinking` 是字典
- `thinking.type` 等于 `enabled`

函数根据原来的 `budget_tokens` 映射 `low`、`medium`、`high` 或 `xhigh`，然后生成

```python
optional_params["thinking"] = {"type": "adaptive"}
optional_params["output_config"] = {"effort": effort}
```

如果调用方已经设置 `output_config.effort`，转换使用 `setdefault` 保留调用方的值

### 5. Bedrock Converse 透传 output_config

`litellm/llms/bedrock/chat/converse_transformation.py` 使用内部 `_output_config_model` 标记保存能力判断使用的真实模型

该标记不会发送给 Bedrock，只用于确认底层是 Anthropic 模型、归一化 effort，并把参数放入 `additionalModelRequestFields`

最终请求结构为

```json
{
  "additionalModelRequestFields": {
    "thinking": {
      "type": "adaptive"
    },
    "output_config": {
      "effort": "high"
    }
  }
}
```

## 正确配置

`model_info` 必须与 `litellm_params` 同级，并且 `base_model` 必须填写 application profile 背后的真实 Bedrock 模型 ID

```yaml
model_list:
  - model_name: claude-opus-5
    litellm_params:
      model: bedrock/arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/abcdef123456
    model_info:
      base_model: global.anthropic.claude-opus-5
```

以下配置层级错误，Router 不会把它作为 deployment 的 `model_info` 传递

```yaml
litellm_params:
  model_info:
    base_model: global.anthropic.claude-opus-5
```

以下值也不建议使用

```yaml
model_info:
  base_model: claude-opus-5
```

它只是公开别名，不包含明确的 Bedrock Anthropic provider 信息。当前 Converse 透传逻辑需要能够把 base model 解析为 `anthropic` 模型，因此应使用实际的 Bedrock 模型 ID

## 兼容性边界

修复不会无条件转换所有模型

- Opus 5 等声明 `supports_adaptive_thinking: true` 的模型执行转换
- Opus 4.5 等旧模型继续保留 `thinking.type=enabled` 和 `budget_tokens`
- 已经发送 `thinking.type=adaptive` 的请求保持不变
- 调用方显式设置的 `output_config.effort` 不会被覆盖
- 显式使用 `bedrock/invoke/...` 不在本次修复范围内
- 模型组包含多个 deployment 时，每个 opaque application profile deployment 都必须设置正确的 `model_info.base_model`

没有根据 `model_name: claude-opus-5` 自动推断能力，因为模型组名称可以任意命名，也可能指向不同代际的模型。使用 deployment 级别的真实 base model 可以避免破坏旧模型

## Docker 构建路径

仓库根目录 `Dockerfile` 使用当前构建上下文中的源码

```dockerfile
COPY . .
RUN uv sync --frozen --no-default-groups --no-editable ...
```

因此，以该分支仓库根目录作为 build context 时，修复代码会进入镜像

不要使用 `docker/build_from_pip/Dockerfile.build_from_pip` 验证本次修复。该文件安装发布到 PyPI 的固定 LiteLLM 版本，不会安装当前分支源码

构建期间 Rust 报出的 `No space left on device` 属于远程构建环境存储问题，与 Opus 5 参数转换逻辑无关

## 验证结果

代码侧已经验证以下行为

| 场景 | 最终 thinking | 最终 output_config |
| --- | --- | --- |
| opaque ARN，无 base model | 保留 `enabled` | 无 |
| opaque ARN，Opus 4.5 base model | 保留 `enabled` | 无 |
| opaque ARN，`global.anthropic.claude-opus-5` | `adaptive` | `effort: high` |

相关 Messages、Converse、base model 和路由测试共 77 项通过，格式、编译检查及 `make pre-commit` 通过

线上最小请求对比结果

- 直接发送 `thinking.type=enabled` 时返回 400
- 直接发送 `thinking.type=adaptive` 和 `output_config.effort=low` 时返回 200

该结果说明 Bedrock Opus 5 本身工作正常，当前线上失败发生在 LiteLLM 没有执行 legacy 到 adaptive 的转换

## 线上排查顺序

1. 确认远程构建检出的提交为 `cd932ae2a81ea082d442b8cdc40e376a95eda8cc`
2. 确认使用仓库根目录 `Dockerfile`，并以该仓库根目录作为 build context
3. 确认模型配置中的 `model_info` 与 `litellm_params` 同级
4. 确认 `base_model` 是真实的 Bedrock 模型 ID，例如 `global.anthropic.claude-opus-5`
5. 确认路由是 `converse` 或 `converse_like`，没有显式使用 `bedrock/invoke/...`
6. 如果模型组有多个 deployment，逐个确认 base model 配置
7. 比较正在运行的 Pod `imageID` 与 registry manifest digest，不能只比较可变 tag
8. 确认所有旧 Pod 都已完成滚动替换

## 当前状态

代码修复已经提交并推送，且本地转换测试通过

线上代理仍然对 legacy `thinking.type=enabled` 返回 400，因此生产环境尚未验证修复生效。根据当前证据，优先检查 deployment 的 `model_info.base_model`，其次检查运行中的镜像 digest 和旧 Pod，不能仅凭错误判断镜像没有包含代码
