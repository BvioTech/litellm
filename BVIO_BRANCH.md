# litellm-Bvio 分支维护说明

## 这个分支是什么

`litellm-Bvio` = **官方 `upstream/main` + 三个上游没有的补丁**。后续构建镜像用这个分支。

当前基线 `0e9cd9893e`（`upstream/main`，2026-08-11，version 1.98.0）。

设计原则：凡是官方已经修的，一律用官方版本，本分支不留自研实现。只保留官方确实没有、而我们排障必需的东西。目前只有三项，见下文。

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
| `cd932ae2a8` support Opus 5 application profiles | `0f41365c34` | 官方方案更简单：ARN 本来就藏了底层模型，本地判定不了，于是原样转发 `output_config` 交给 Bedrock 校验。我们那套 `_output_config_model` / `capability_model` 传递管道不再需要。注意官方只补到 `_transform_request_helper` 这一层，`map_openai_params` 里的降级门仍是空的，由补丁 3 补上 |
| `2614be0301` emit SSE keepalives | `363d56f917` | 官方支持 per-deployment 配置，粒度更细。注意配置项变了，见下节 |
| `litellm-fork` 工作区里未提交的 adaptive 门控（3 文件 62 行） | 无（由补丁 3 重写） | **原判断有误，已纠正。**它改的是 invoke 路由的 `output_config` strip 门和一个已被上游重写的 converse 分支，从未触及 `map_openai_params` 里的 adaptive 降级门。实测：在其所在的 1.93.0 树上带着这 62 行跑 ARN，`thinking:{adaptive}` 仍被降级为 `budget_tokens:2048`，且 `output_config` 整个丢失（比 1.98.0 更差）。锚点变量 `output_config_model` 在 `upstream/main` 已不存在，无法 apply。**可以丢弃** |
| 本次曾自研的 `_CombinedChunkSplitter` reasoning/content 拆分 | `1018d18e6b` | 官方实现更彻底：按三类 payload 拆（reasoning / text / tool_calls），从头重建 `Delta` 而非 deepcopy 清字段，并额外处理了四个边缘情况 —— tool_calls 被拆成两个同 id 的块、multi-choice 丢次要 choices、tool 参数续传被拆断、无 signature 的 thinking 被收集两次 |

官方从未采用我们那套机制：在 `upstream/main` 里 grep `_output_config_model`、`capability_model`、`_translate_legacy_thinking_for_adaptive_model` 全部为 0 处。

## 部署时必须改的配置

keepalive 的配置位置从我们的全局开关换成了官方的按 deployment 配置。**不改配置的话 keepalive 会静默失效。**

```
旧（bvio 2614be0301）：litellm.sse_keepalive_interval_seconds   全局
新（官方 363d56f917）：litellm_params.keepalive_seconds         每个 deployment，或请求体
```

官方实现在 `keepalive_seconds > 0` 时才启用，每 N 秒发 `: ping\n\n` SSE 注释帧。

## 本分支保留的三个补丁

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

### 3. inference profile ARN 下 adaptive thinking 不被降级

`litellm/llms/bedrock/chat/converse_transformation.py` `map_openai_params`，`param == "thinking"` 分支的降级条件追加一项：

```python
and not is_bedrock_application_inference_profile_arn(model)
```

Bedrock 这条链上有两道会被 ARN 误判的门，位置相隔一千行，官方只补了后者：

| | 位置 | 误判后果 | 状态 |
| --- | --- | --- | --- |
| 门 1 | `map_openai_params`（`param == "thinking"`） | `thinking:{adaptive}` 被**降级**成 `{enabled, budget_tokens:2048}` | 本补丁 |
| 门 2 | `_transform_request_helper` | `output_config` 被**丢弃** | 官方 `0f41365c34` |

根因是能力判定依赖模型价格表，而 application inference profile ARN 里没有任何模型信息：

```
_is_adaptive_thinking_model(ARN)                           -> False
_is_adaptive_thinking_model('us.anthropic.claude-opus-5…') -> True
BedrockModelInfo.get_base_model(ARN)                       -> 'ymo9b3n37s88'
```

配置层救不了：`model_info.base_model` 只喂给 `get_provider_chat_config` 和 `get_supported_openai_params`（`utils.py` 里显式 `passed_params.pop("base_model")`），`map_openai_params` 拿到的永远是 `litellm_params.model` 原串。

修前修后（`model` = 线上那个 ARN，输入为 Claude Code 实际发的 `thinking:{adaptive}` + `output_config:{effort:high}`）：

```
修前  thinking:{"type":"enabled","budget_tokens":2048}  output_config:{"effort":"high"}   ← 自相矛盾
修后  thinking:{"type":"adaptive"}                      output_config:{"effort":"high"}   ← 与真实模型 id 完全一致
```

`budget_tokens: 2048` 是降级分支里写死 `reasoning_effort="medium"` 的产物，客户端设的 high / xhigh / max 全部作废。

处理原则跟随官方 `0f41365c34`：ARN 藏了底层模型，本地判定不了，就原样转发交给 Bedrock 校验。若该 profile 背后是 4.6 之前的模型，Bedrock 会拒 —— 那是调用方发了 adaptive 的责任，不该由 litellm 猜。

**这个 bug 不是 1.98.0 引入的。** `litellm-Bvio-pre-upstream`（1.95.0，先前线上版本）同一处逐字相同，1.93.0 亦然，从来没人修过。

#### 刻意没改的地方

`_handle_reasoning_effort_parameter`（同文件）里的 `_is_adaptive_thinking_model` 判定保持原样，ARN 走 `reasoning_effort` 时仍拿到 legacy `budget_tokens`、不产出 `output_config`。理由：这条路上调用方发的是 OpenAI 风格的 `reasoning_effort`，并未要求 adaptive，表示形式由 litellm 选；固定预算被所有 Claude 世代接受，而从一个不透明 ARN 推断 adaptive 会打坏背后是老模型的 profile。我们的流量走 `/v1/messages` + `thinking:{adaptive}`，只经过门 1，碰不到这里。测试里有一条用例把这个决定钉住，避免以后被误当成漏改。

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
| `test_warning_fires_once_per_stream` | 多条 reasoning chunk 不会让 warning 重复 |
| `test_adaptive_thinking_forwarded_verbatim`（3 例） | 裸 ARN / 带 `bedrock/converse/` 前缀的 ARN / 真实模型 id 三者结果一致 |
| `test_every_effort_tier_survives_the_arn`（5 例） | low / medium / high / xhigh / max 全部不被压成固定预算 |
| `test_resolvable_non_adaptive_model_still_downgrades` | Claude 3.7 等可解析的老模型 id 仍照旧降级（豁免只针对判定不了的 ARN） |
| `test_legacy_thinking_through_an_arn_is_untouched` | 主动要固定预算的调用方拿到原值 |
| `test_reasoning_effort_through_an_arn_keeps_the_legacy_budget` | 钉住上文「刻意没改的地方」，防止以后被误当漏改 |
| `test_request_body_carries_both_fields` | 端到端：两个字段都进 `additionalModelRequestFields` |

## 线上排查顺序

出现空白回复时：

1. 网关日志搜 `no client-visible content block`，读其中的 `stop_reason` 与 `output_tokens`
2. `stop_reason=refusal` → 内容被拦，属预期行为，让调用方换措辞，不要重试
3. `stop_reason=max_tokens` 且 `output_tokens` 接近上限 → 思考吃满预算、正文未开始，提高 max_tokens
4. `stop_reason=end_turn` 且 `output_tokens > 0` → 还有未知的内容丢失路径，抓该请求的 Bedrock Converse 原始事件流继续查
5. 完全没有这条 warning 但客户端仍空白 → 问题不在本适配器，查客户端渲染或连接中断

思考深度明显变浅、或客户端设的 effort 像是没生效时：确认发往 Bedrock 的 `additionalModelRequestFields.thinking` 是不是 `{"type":"adaptive"}`。若变成了 `{"type":"enabled","budget_tokens":2048}`，说明补丁 3 没生效（构建时用了官方镜像，或 rebase 时丢了这个条件）。

## rebase 维护指南

补丁面很小，跟随上游的成本已经压到最低：

```
litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py       +8
litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py  +60/-5
litellm/types/llms/anthropic.py                                                  +4/-2
litellm/llms/bedrock/chat/converse_transformation.py                             +11
tests/.../adapters/test_empty_turn_diagnostics.py                                新文件
tests/.../bedrock/chat/test_inference_profile_adaptive_thinking.py               新文件
```

跟进上游时：

1. `git fetch upstream main`（或 fetch 目标 tag）
2. `git rebase upstream/main`（或该 tag）
3. 只需关注 `streaming_iterator.py` —— 若官方重构了 `_delta_has_content` 或 `_augment_message_delta_usage`，`_visible_delta_gate` 的四个调用点和 warning 的接入点需要重新对齐
4. `converse_transformation.py` 那 11 行只是一个 `and not ...` 条件加注释，冲突面极小；但**若官方自己在门 1 加了 ARN 豁免，直接丢掉本补丁用官方的**（判据：`map_openai_params` 的 `param == "thinking"` 分支里出现 `is_bedrock_application_inference_profile_arn`）
5. 跑 `pytest tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/ tests/test_litellm/llms/bedrock/chat/`

如果哪天官方自己实现了这三项（可关注 `AnthropicFinishReason` 是否加入 `refusal`、门 1 是否加了 ARN 豁免），本分支即可退役，直接打官方镜像。

## 当前状态

- `litellm-Bvio`：基线 `upstream/main` + 三个补丁
- `litellm-Bvio-pre-upstream`：重建前的旧分支，保留自研 Opus 5 / keepalive 补丁以备查
- `litellm_sse_keepalive` / `litellm_opus_5_latest_staging`：原自研分支，未动
- `litellm-fork` 工作区里那 3 个文件的未提交改动可以丢弃（实测未修好门 1，锚点在上游已不存在），但它们没有任何 git 备份，确认后再清
- 与本次无关的独立问题：客户端记录里另有 56 次 `Connection closed mid-response` 和 22 次 504，属 CloudFront 到源站那一跳，本次未处理
