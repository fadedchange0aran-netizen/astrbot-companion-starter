# astrbot_plugin_llmperception

AstrBot 环境感知注入插件。在每次 LLM 请求前，把当前时间、星期、时段、平台等信息注入到用户消息开头。

当前版本额外支持一层轻量“日期语境提示”：

- 常见节假日 / 工作日 / 休息日判断
- 自定义纪念日、生日等固定日期事件
- 自定义循环里程碑规则，例如每 50 天、每周年
- 可选的生理期手填提醒，根据前端填写的上次开始日与周期天数，在超期时提醒机器人更轻一点地关心
- StackChan 轻量模式配置，可单独覆盖 provider / model，并按需关闭工具

这层能力的定位不是长期记忆，而是“系统先算好，再让机器人自然接住”。

## 关键配置

- `enable_prompt_injection`
  - 总开关
- `enable_holiday_perception`
  - 是否注入今天是工作日、休息日还是法定节假日
- `enable_anniversary_perception`
  - 是否注入纪念日 / 生日 / 里程碑的日期语境
- `trusted_sender_ids`
  - 只有命中这些 sender_id 时，才会注入私密纪念日
- `anniversary_events`
  - 固定日期事件，支持 `YYYY-MM-DD` 或 `MM-DD`
- `milestone_rules`
  - 里程碑规则，支持固定天数、每 `N` 天、每 `N` 周年；`anchor_date` 必须是完整的 `YYYY-MM-DD`
- `last_period_start_date`
  - 上次生理期开始日；建议在 AstrBot 插件面板手动维护
- `cycle_length_days`
  - 询问阈值天数，默认 `35`
- `cycle_checkin_cooldown_days`
  - 超期后重复轻问的最小间隔，默认 `3`
- `enable_stackchan_light_mode`
  - 为 StackChan 启用轻量会话策略

## 当前口径

- 节假日属于公共语境
- 纪念日 / 里程碑 / 生理期属于结构化私密语境
- 生理期先走“前端手填 + 机器人轻问 + 你手动更新开始日”的最小闭环，不做复杂预测
- 这层提示只用于帮助模型“自然想起”，不要让模型复述成系统通知

> 公开版本已移除个人默认值，仅保留通用插件代码与配置模板。
