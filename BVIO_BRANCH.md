# litellm-Bvio 分支维护说明

## 这个分支是什么

`litellm-Bvio` = **官方 `upstream/main` + 上游没有的补丁**。后续构建镜像用这个分支。

当前基线 `0e9cd9893e`（`upstream/main`，2026-08-11，version 1.98.0）。

设计原则：凡是官方已经修的，一律用官方版本，本分支不留自研实现。只保留官方确实没有、而我们排障必需的东西。

补丁分两组：**空回复诊断**（补丁 1-2，互相独立）和 **thinking/effort 链路**（补丁 3-6，一起才完整）。

> **部署前必读**：thinking 那组补丁依赖 `model_info.base_model`。没有它，ARN 的能力判定无法完成，Bedrock 会回 400。见「部署时必须改的配置」。

## 为什么不能直接打官方 release 镜像

我们依赖的三个 Bedrock 修复只在 `main` 上，还没进任何正式发布：

| 官方提交 | 内容 | v1.96.2（最新正式） | v1.97.0-rc.1 | main |
| --- | --- | --- | --- | --- |
| `1018d18e6b` | mixed chunk 按 payload 种类拆分 (#35289) | 已含 | 已含 | 已含 |
| `0f41365c34` | ARN 的 `output_config` effort 转发（只覆盖一半，见补丁 3） | 未含 | 未含 | 已含 |
| `929ee52b87` | adaptive thinking 过 `/v1/messages` 桥 | 未含 | 未含 | 已含 |
| `363d56f917` | per-deployment `keepalive_seconds` (#34423) | 未含 | 未含 | 已含 |

打 `v1.96.2` 或 `v1.97.0-rc.1`，Opus 5 的 effort 参数会被丢掉 —— 也就是当初 `cd932ae2a8` 要解决的问题会复发。

**等 v1.97.0 / v1.98.0 正式发布后**，重新核对这三个提交是否进入 tag。若都进了，本分支可以 rebase 到该 tag，不必再跟 `main` 这条滚动线。

## 已被官方取代、本分支不再携带的自研补丁

这些提交仍保留在 `litellm_sse_keepalive` 和 `litellm-Bvio-pre-upstream` 分支上以备查，但**不再进入构建**。

| 原自研 | 官方对应 | 为什么用官方的 |
| --- | --- | --- |
| `cd932ae2a8` support Opus 5 application profiles | 部分被 `0f41365c34` 取代 | 官方方案更简单：ARN 藏了底层模型，本地判定不了，于是原样转发 `output_config` 交给 Bedrock 校验。我们那套 `_output_config_model` 传递管道不再需要。但官方只补到 `_transform_request_helper` 这一层，`map_openai_params` 和 `reasoning_effort` 两条路仍是空的，由补丁 3-4 补上 |
| `2614be0301` emit SSE keepalives | `363d56f917` | 官方支持 per-deployment 配置，粒度更细。注意配置项变了，见下节 |
| `litellm-fork` 工作区里未提交的 adaptive 门控（3 文件 62 行） | 无 | 它改的是 invoke 路由的 `output_config` strip 门和一个已被上游重写的 converse 分支。实测：在其所在的 1.93.0 树上带着这 62 行跑 ARN，`thinking:{adaptive}` 仍被降级，`output_config` 整个丢失。锚点变量 `output_config_model` 在 `upstream/main` 已不存在。**可以丢弃** —— 但注意 `pr-32983` 里**已提交**的那部分不能丢，见补丁 4 |
| 本次曾自研的 `_CombinedChunkSplitter` reasoning/content 拆分 | `1018d18e6b` | 官方实现更彻底：按三类 payload 拆（reasoning / text / tool_calls），从头重建 `Delta` 而非 deepcopy 清字段，并额外处理了四个边缘情况 —— tool_calls 被拆成两个同 id 的块、multi-choice 丢次要 choices、tool 参数续传被拆断、无 signature 的 thinking 被收集两次 |

**一处曾经写错的判断，已纠正。** 本文档早期版本写「官方从未采用我们那套机制，grep `_translate_legacy_thinking_for_adaptive_model` 全部为 0 处」—— 错的。官方**采用了这个函数**，实现与我们的逐字一致（同样的预算阈值、同样的 `setdefault` 语义），但只接到 native Anthropic Messages 路由上。ARN 走 converse 路由，碰不到。这个误判导致 `pr-32983` 里那 9 行 `utils.py` 挂载点被当成冗余丢掉，线上表现为 violoop 报 `"thinking.type.enabled" is not supported`。补丁 4 就是把它接回来。

## 部署时必须改的配置

### 1. `model_info.base_model` 是 thinking 链路的硬依赖

application inference profile ARN 里没有任何模型信息，`base_model` 是唯一能让 litellm 解析出真实能力的入口，而 `get_optional_params` 是唯一能拿到它的层（补丁 4 就挂在那里）。

```yaml
model_list:
  - model_name: claude-opus-5
    litellm_params:
      model: bedrock/converse/arn:aws:bedrock:ap-southeast-1:...:application-inference-profile/xxx
    model_info:
      base_model: global.anthropic.claude-opus-5     # 必填，不是可选的成本统计字段
```

线上当前用的就是 `global.anthropic.claude-opus-5`，实测能力探测全部命中：

```
'global.anthropic.claude-opus-5' in litellm.model_cost   -> True
get_bedrock_base_model(...)                              -> 'anthropic.claude-opus-5'
_is_adaptive_thinking_model / xhigh / output_config      -> True / True / True
```

**删掉这一行的后果**：legacy `thinking:{type:"enabled"}` 会原样发到 Bedrock，回 400。测试里有 `test_without_base_model_the_legacy_shape_survives` 把这个依赖钉住。

### 2. keepalive 配置位置变了

keepalive 从我们的全局开关换成了官方的按 deployment 配置。**不改配置的话 keepalive 会静默失效。**

```
旧（bvio 2614be0301）：litellm.sse_keepalive_interval_seconds   全局
新（官方 363d56f917）：litellm_params.keepalive_seconds         每个 deployment，或请求体
```

官方实现在 `keepalive_seconds > 0` 时才启用，每 N 秒发 `: ping\n\n` SSE 注释帧。

## 本分支保留的补丁

上游没有，也没有相关 PR。

### 1. `content_filter` 映射为 `refusal`

`litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py`
`_translate_openai_finish_reason_to_anthropic` 增加 `content_filter → refusal` 分支。

`litellm/types/llms/anthropic.py`

```python
AnthropicFinishReason = Literal[
    "end_turn", "max_tokens", "stop_sequence", "tool_use", "refusal"
]
```

官方仍是三分支加兜底 `end_turn`，类型里也没有 `refusal`。

为什么必须改：`end_turn` 语义是"模型自然说完"，`content_filter` 是被外部拦断。内容为空时两者在客户端看来完全一样，于是

- Claude Code 判断不出该不该重试，注入 `[no visible output]` 再发一次，第二次注定同样被拦，白烧一次调用
- violoop-device 的 `core/ai/providers/anthropic.js` 会走到 `recordSuccess()` 并返回 `success: true, content: ''`，把一次拒绝记成成功样本，污染 provider 健康统计

空内容加 `end_turn` 至少对应三种情况，压成一个值后无法区分：被内容过滤（该提示被拒、不重试）、思考吃满 `max_tokens` 正文未开始（该提高上限）、模型确实无话可说（正常收尾）。

### 2. 零可见内容时打一条 warning

`litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py`

- 实例标志 `emitted_visible_delta` / `warned_no_visible_output`
- `_visible_delta_gate()`：包装官方的 `_delta_has_content()`，返回值语义不变，额外记录是否曾发出 `text_delta` / `input_json_delta`。四个原调用点（同步两处、异步两处）改调它。`_delta_has_content` 保持 staticmethod 不动，官方测试不受影响
- `_warn_if_no_visible_output()`：在 `_augment_message_delta_usage()` 开头调用。该方法是所有最终 `message_delta` 的共同出口（覆盖同步与异步全部路径），单点接入即可，靠 `warned_no_visible_output` 保证一条流只打一次

```
Anthropic Adapter - stream produced no client-visible content block;
the client will render an empty reply
(model=..., stop_reason=..., output_tokens=..., last_block_type=...)
```

为什么必须有：Anthropic 客户端只渲染 `text` 和 `tool_use` 块。Bedrock Converse 返回的 reasoning 只有 signature、没有明文（实测 6/6 请求 `thinking_delta` 恒为 0），所以一个正文被拦或正文未开始的回合，到客户端就是一个空 `thinking` 块加一个非零 `output_tokens`，两侧都不报错。而 `output_tokens` 本身包含 reasoning tokens，单看它无法区分成因 —— 定位这个问题时正是因为缺这条日志，只能靠 `output_tokens` 反推，中途推错过一次。

## thinking / effort 链路（补丁 3-6）

这四个补丁解决同一件事，缺一不可。先把事实摆清楚，因为排查过程中我们在两个方向上都判断错过。

### Bedrock 的实际契约

AWS 官方文档 [Adaptive thinking](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html)：

- Claude Opus 5 **支持** adaptive thinking，**不需要 beta header**
- `thinking.type: "enabled"` + `budget_tokens` 在 Opus 4.6 / Sonnet 4.6 上**已废弃**；在 Opus 4.7、Mythos 5、Fable 5 上**直接 400**
- 只支持 adaptive 的那些模型上，`thinking.type: "disabled"` 也会 400

线上实测到的两条 Bedrock 报错，正好是这条契约的两个方向：

```
"thinking.type.enabled" is not supported for this model.
Use "thinking.type.adaptive" and "output_config.effort" to control thinking behavior.

output_config.effort 'xhigh' is not supported when thinking is disabled on this model.
Use effort 'high' or below, or enable thinking.
```

结论一句话：**这条链上任何地方都不该发 `enabled`；能开思考就用 `adaptive`。**

曾经写错、已纠正的两个判断：

1. 「Bedrock 不认 adaptive」—— 错。被 AWS 文档和一条真实录制的 Converse 请求（pydantic-ai 测试 cassette，发 `{"thinking":{"type":"adaptive"},"output_config":{"effort":"high"}}` 成功返回 reasoningContent）双重否证
2. 「`model_info.base_model` 对 thinking 判定无效」—— 对 upstream 成立，但正是补丁 4 的挂载点让它生效，见上文部署配置

### 根因：能力判定依赖模型价格表，ARN 里没有模型信息

```
_is_adaptive_thinking_model(ARN)                           -> False
_is_adaptive_thinking_model('global.anthropic.claude-opus-5') -> True
BedrockModelInfo.get_base_model(ARN)                       -> 'ymo9b3n37s88'
```

于是三道能力盲的门各自产出错误形状：

| | 位置 | 误判后果 | 谁修 |
| --- | --- | --- | --- |
| 门 1 | `map_openai_params`（`param == "thinking"`） | `thinking:{adaptive}` 降级成 `{enabled, budget_tokens:2048}` | 补丁 3 |
| 门 2 | `_transform_request_helper` | `output_config` 被丢弃 | 官方 `0f41365c34` |
| 门 3 | `_handle_reasoning_effort_parameter` | `reasoning_effort` 产出 legacy 预算、不产出 `output_config` | 补丁 4 兜住 |

**这些 bug 不是 1.98.0 引入的。** `litellm-Bvio-pre-upstream`（1.95.0）门 1 处逐字相同，1.93.0 亦然。1.95.0 上之所以不报错，是因为 `output_config` 压根没发出门 —— 代价是客户端设的 effort 一直被忽略。

### 3. ARN 下 adaptive 不被降级

`converse_transformation.py` `map_openai_params`，门 1 的降级条件追加一项：

```python
and not is_bedrock_application_inference_profile_arn(model)
```

没配 `base_model` 时这是唯一的防线；配了 `base_model` 时补丁 4 也能修回来，但那条路会从 `budget_tokens:2048` 反推出 `effort:medium`，而客户端本来可能没指定 effort。保留本补丁可以让 adaptive 请求原样通过，不凭空造出一个档位。

### 4. legacy thinking 在 converse 路由上翻译为 adaptive

`litellm/utils.py` `get_optional_params`，bedrock 分支末尾挂上官方那个函数：

```python
if bedrock_route in ("converse", "converse_like"):
    AnthropicMessagesConfig._translate_legacy_thinking_for_adaptive_model(
        model=base_model or model, optional_params=optional_params, custom_llm_provider="bedrock")
```

这是本组的核心。位置选在这里有两个不可替代的理由：

- `base_model` 只在这一层可见（`map_openai_params` 永远只拿到 `litellm_params.model` 原串）
- 它跑在 `map_openai_params` **之后**，所以能顺手修掉门 1 和门 3 刚刚合成出来的 legacy 预算

一处覆盖三种来源：客户端直接发 `enabled`（violoop）、门 1 降级出的 `enabled`、门 3 从 `reasoning_effort` 产出的 `enabled`。函数内部用 `setdefault` 写 effort，所以客户端原本的档位不会被预算反推值覆盖。

### 5. effort 高于 high 时开启 adaptive

`converse_transformation.py` `_enable_thinking_for_high_effort`，在 `_transform_request_helper` 组装请求体时调用。

`xhigh` / `max` 在 thinking 关闭时会被 Bedrock 拒。客户端确实会发这个组合 —— [claude-code#79798](https://github.com/anthropics/claude-code/issues/79798) 和 [#76689](https://github.com/anthropics/claude-code/issues/76689)：`alwaysThinkingEnabled: true` 没被翻译成 `thinking:{type:"adaptive"}`，会话静默地不带思考跑，effort 到 xhigh 时硬 400。那两个 issue 是**直连 Anthropic、无任何网关**复现的，所以这是客户端 bug，不是 litellm 的；但网关必须挡，否则 violoop 一样会中。

处理方式：effort 档位是更具体的信号，所以开启 adaptive 而不是压档。`high` 及以下不动，`disabled` 保持 `disabled`。只有 `adaptive` 能开启思考 —— `enabled` 在 4.6+ 上已废弃。

### 6. 适配器在 thinking 缺失时不再丢掉 effort

`adapters/transformation.py` `_translate_thinking_to_openai` 原本在 `"thinking" not in request` 时直接 return，`output_config` 一并被丢。抽出 `_forward_bedrock_effort_config()`，两条路径共用，顺带消掉一处重复。

不修这个，`#79798` 那种「省掉 thinking 但发了 effort」的请求，effort 到不了补丁 5 能看见的地方，表现为档位静默失效。

## 验证结果

```
pytest tests/test_litellm/llms/bedrock/chat/ \
       tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/

544 passed, 0 failed          （官方 523 + 本分支新增 21）
  test_empty_turn_diagnostics.py                 9 passed
  test_inference_profile_adaptive_thinking.py   12 passed
```

三个补丁的测试全部集中在两个新文件里，官方测试文件一行未改 —— 这样 rebase 到官方新版本时冲突面只有四个源文件的小改动。

| 用例 | 覆盖 |
| --- | --- |
| `test_finish_reason_mapping`（5 例参数化） | 四种取值映射 + 未知值兜底 `end_turn` |
| `test_no_visible_output_warns_with_upstream_context` | 只有 thinking 块的流恰好一条 warning，且带上 model 与 output_tokens |
| `test_refusal_stream_warns_and_reports_refusal` | 被拦的流同时产出 `refusal` 和 warning |
| `test_visible_output_does_not_warn` | 正常有文本的流不打 warning |
| `test_warning_fires_once_per_stream` | 连续两次 `message_delta` flush 只出一条 warning（钉住 `warned_no_visible_output` 幂等位）|
| `test_adaptive_thinking_forwarded_verbatim`（3 例） | 裸 ARN / 带 `bedrock/converse/` 前缀的 ARN / 真实模型 id 三者结果一致 |
| `test_every_effort_tier_survives_the_arn`（5 例） | low / medium / high / xhigh / max 全部不被压成固定预算 |
| `test_resolvable_non_adaptive_model_still_downgrades` | Claude 3.7 等可解析的老模型 id 仍照旧降级（豁免只针对判定不了的 ARN） |
| `test_legacy_thinking_through_an_arn_is_untouched` | 门 1 单独看时不动客户端的固定预算 |
| `test_request_body_carries_both_fields` | 端到端：两个字段都进 `additionalModelRequestFields` |
| `test_legacy_budget_is_translated_to_adaptive`（5 例） | 16000/8192→xhigh、4096→high、2048→medium、1024→low，修 violoop 那条 400 |
| `test_caller_effort_wins_over_the_budget_derived_tier` | `setdefault` 语义：显式档位不被预算反推值覆盖 |
| `test_high_effort_with_thinking_off_enables_adaptive`（2 例） | xhigh / max 遇上 `disabled` 时开启 adaptive，修 Claude Code 那条 400 |
| `test_high_effort_without_any_thinking_param_enables_adaptive` | 同上，但客户端整个省掉了 `thinking` |
| `test_disabled_thinking_is_left_alone_below_the_ceiling` | 钉住边界：`high` 及以下不动客户端的 `disabled` |
| `test_reasoning_effort_reaches_bedrock_as_adaptive` | 门 3 那条路也落到同一形状 |
| `test_without_base_model_the_legacy_shape_survives` | 钉住 `model_info.base_model` 这个配置依赖 |
| `test_reasoning_effort_gate_is_capability_blind_on_its_own` | 钉住补丁 4 为什么必须挂在 `get_optional_params`：门 3 单看修不了 |
| `test_effort_survives_when_thinking_is_absent` | 适配器不再把 effort 丢掉 |
| `test_effort_still_rides_along_with_thinking` | 重构只是放宽，没改原路径 |
| `test_structured_output_format_is_not_mistaken_for_an_effort_tier` | 只转发 effort 子集，`format` 不会变成重复 schema |
| `test_non_bedrock_target_never_receives_the_raw_param` | 非 Bedrock 目标拿不到裸 `output_config` |

## 线上排查顺序

出现空白回复时：

1. 网关日志搜 `no client-visible content block`，读其中的 `stop_reason` 与 `output_tokens`
2. `stop_reason=refusal` → 内容被拦，属预期行为，让调用方换措辞，不要重试
3. `stop_reason=max_tokens` 且 `output_tokens` 接近上限 → 思考吃满预算、正文未开始，提高 max_tokens
4. `stop_reason=end_turn` 且 `output_tokens > 0` → 还有未知的内容丢失路径，抓该请求的 Bedrock Converse 原始事件流继续查
5. 完全没有这条 warning 但客户端仍空白 → 问题不在本适配器，查客户端渲染或连接中断

出现 Bedrock 400 时，看发往 Bedrock 的 `additionalModelRequestFields`：

| 报错 | 实际发出的形状 | 说明 |
| --- | --- | --- |
| `"thinking.type.enabled" is not supported` | `thinking:{"type":"enabled",...}` | 补丁 4 没生效。首先查 `model_info.base_model` 还在不在 |
| `output_config.effort '...' is not supported when thinking is disabled` | effort 高于 high 且 thinking 非 adaptive | 补丁 5 没生效 |
| 思考深度变浅、effort 像没生效 | `thinking:{"type":"enabled","budget_tokens":2048}` | 补丁 3 没生效 |

三种都先确认镜像是从 `litellm-Bvio` 打的，不是官方镜像。

## rebase 维护指南

```
litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py  +48/-4
litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py      补丁 1 + 补丁 6
litellm/types/llms/anthropic.py                                                 +3/-1
litellm/llms/bedrock/chat/converse_transformation.py                            补丁 3 + 补丁 5
litellm/utils.py                                                                补丁 4（约 16 行，含注释）
tests/.../adapters/test_empty_turn_diagnostics.py                               新文件
tests/.../adapters/test_bedrock_effort_bridge.py                                新文件
tests/.../bedrock/chat/test_inference_profile_adaptive_thinking.py              新文件
```

跟进上游时：

1. `git fetch upstream main`（或 fetch 目标 tag）
2. `git rebase upstream/main`（或该 tag）
3. `streaming_iterator.py` —— 若官方重构了 `_delta_has_content` 或 `_augment_message_delta_usage`，`_visible_delta_gate` 的四个调用点和 warning 的接入点需要重新对齐。另注意 `_visible_delta_gate` 依赖 `_delta_has_content` 的后置条件（返回 True 即保证 `delta` 是带合法 `type` 的 dict）才敢直接下标取 `delta["type"]`；官方若放宽该后置条件，这里要补回类型守卫
4. `utils.py` —— 补丁 4 挂在 `get_optional_params` 的 bedrock 分支末尾。**若官方把 `_translate_legacy_thinking_for_adaptive_model` 自己接到了 converse 路由上，丢掉本补丁用官方的**（判据：在 `utils.py` 或 `converse_transformation.py` 里 grep 到这个函数名）
5. `converse_transformation.py` —— 补丁 3 是一个 `and not ...` 条件；补丁 5 是一个独立 staticmethod 加一处调用。若官方在门 1 加了 ARN 豁免，丢掉补丁 3
6. 跑 `pytest tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/ tests/test_litellm/llms/bedrock/chat/`

退役判据：`AnthropicFinishReason` 里出现 `refusal`，且 `_translate_legacy_thinking_for_adaptive_model` 被接到 converse 路由 —— 两条都满足时本分支可以退役，直接打官方镜像。

## 当前状态

- `litellm-Bvio`：基线 `upstream/main` + 六个补丁
- `litellm-Bvio-pre-upstream`：重建前的旧分支（1.95.0）。**它带着补丁 4 的原始实现**，是这次能定位问题的关键参照，不要删
- `litellm_sse_keepalive` / `litellm_opus_5_latest_staging`：原自研分支，未动
- `litellm-fork` 工作区里那 3 个文件的未提交改动可以丢弃（实测未修好门 1，锚点在上游已不存在），但它们没有任何 git 备份，确认后再清
- 与本次无关的独立问题：客户端记录里另有 56 次 `Connection closed mid-response` 和 22 次 504，属 CloudFront 到源站那一跳，本次未处理

## 上游值得提的 PR

补丁 4 是 upstream 自己的函数没接全，属明确的 bug，值得回推：`_translate_legacy_thinking_for_adaptive_model` 只挂在 native Anthropic Messages 路由上，Bedrock converse 路由完全没有，导致任何走 converse 的 Claude 4.6+ 部署收到 legacy `thinking.type=enabled` 就 400。补丁 5 同理，属通用的参数调和。补丁 1-2 和 3 也都不含我们特有的东西。
