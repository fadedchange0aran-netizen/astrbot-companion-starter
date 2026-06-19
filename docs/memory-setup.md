# 记忆配置

这个 starter 不直接内置 `LivingMemory` 源码，但默认把它当成陪伴体验的基础件。

公开仓库：

- `https://github.com/fadedchange0aran-netizen/astrbot_plugin_livingmemory`

## 最小必填项

`LivingMemory` 至少先配这两个：

- `provider_settings.embedding_provider_id`
- `provider_settings.llm_provider_id`

留空时会走 AstrBot 默认 Provider，但更建议你显式指定，便于后续排障。

## 推荐口径

### 私聊长期陪伴

建议：

- 开启人格隔离
- 开启会话隔离
- `recall_engine.top_k = 5`
- `recall_engine.injection_method = extra_user_content`

### 群聊陪伴

建议：

- 开启 `session_manager.enable_full_group_capture`
- 保持 `filtering_settings.use_persona_filtering = true`
- 根据消息量决定是否保留 `filtering_settings.use_session_filtering = true`

如果群很多、消息量很大，可以先减小 `context_window_size`，避免不必要的总结成本。

## 推荐先开的 Agent 工具

建议先只开：

- `agent_tools.enable_recall_tool = true`

建议暂时先不开：

- `agent_tools.enable_memorize_tool = false`

原因是主动写记忆工具更强，也更容易把低质量内容直接写进长期记忆库。

## 推荐先保持默认的项

如果你是第一次搭，下面这些先别急着魔改：

- `recall_engine.top_k`
- `graph_memory.*`
- `importance_decay.*`
- `forgetting_agent.*`

先跑通，再根据实际体验决定要不要压 token 或提高召回密度。

## 跟这个 starter 的配合关系

### 为什么这里不把记忆仓直接塞进 starter

因为 `LivingMemory` 本身已经是一个独立公开仓，单独维护更清楚：

- 记忆问题去记忆仓排
- 接入问题去 `adapter`
- 链接体验问题去 `link_context`

这样闺蜜后面更新时不会把“记忆插件”和“体验层插件”混成一坨。

### 这套 starter 对记忆的依赖

这个 starter 默认只依赖 `LivingMemory` 的基础能力：

- 长期记忆召回
- 群聊 / 私聊上下文延续
- Agent 主动回忆工具

`link_context` 与 `adapter` 不要求记忆插件做特殊魔改才能工作，但配上记忆后整体体验会明显更好。*** End Patch
