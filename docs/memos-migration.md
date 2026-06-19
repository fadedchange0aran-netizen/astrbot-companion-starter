# MemOS / Memos 迁移

如果她之前的 AI 记忆主要在 MemOS / Memos 体系里，可以按下面两条思路迁移。

## 先区分两种情况

### 情况 A：她还在继续用 MemOS MCP

这时最简单，不需要先“导出成文件再导入”。

直接继续配置 MemOS MCP，让新机器人继续读写原来的记忆空间即可。

公开文档里给了 MCP 配置方式，核心环境变量是：

- `MEMOS_API_KEY`
- `MEMOS_USER_ID`
- `MEMOS_CHANNEL`

其中最重要的是：

- `MEMOS_USER_ID` 必须保持稳定
- 不要换成随机值、设备 ID 或会话 ID

只要继续沿用原来的 `MEMOS_USER_ID`，新的客户端 / 机器人就还是在读同一个人的记忆。

## 情况 B：她想从 MemOS 迁到本地记忆体系

这种情况下不要纠结“直接一键导库”。

更稳的做法是分两步：

1. 先从 MemOS 导出原始记忆或对话
2. 再把它整理后导入新的记忆系统

## MemOS 官方口径里能做什么

### Cloud / API 侧

MemOS Cloud 文档给了原始消息写入与检索接口，例如：

- `add_message`
- `search_memory`

也就是说，如果她手里还有原始聊天记录，可以直接按消息批次重新写入新的 MemOS 空间，或者整理后再转存到别的记忆系统。

### 开源 MOS 侧

开源 MOS API 文档里有 `Dumping Memories` 概念，可以把 memory cube 导到目录里做持久化备份，然后再通过 `register_mem_cube(...)` 在新环境注册。

这条路线更适合：

- 她自己部署过开源 MemOS / MOS
- 她手里能接触到底层 memory cube

## 我建议她怎么迁

### 方案 1：继续用 MemOS，当成旧记忆源

适合：

- 她原来就是用 MemOS Cloud / MCP
- 她暂时不想把旧记忆迁到 `LivingMemory`

做法：

1. 在新客户端继续配置同一个 `MEMOS_API_KEY`
2. 保持原来的 `MEMOS_USER_ID`
3. 验证 `search_memory` 和 `add_message` 正常

这样旧记忆不用搬家，机器人也能继续接着用。

### 方案 2：把 MemOS 里的关键记忆筛出来，人工迁入 LivingMemory

适合：

- 她想把陪伴机器人逐步切到 AstrBot + LivingMemory
- 她不想把 MemOS 整套链路永久保留

做法：

1. 先在 MemOS 里检索关键记忆
2. 把高价值事实、人物关系、偏好、纪念日、长期项目整理出来
3. 作为结构化文本或人工整理条目导入新的 `LivingMemory`

这条路线最慢，但最干净，也最适合“陪伴型”长期记忆。

### 方案 3：如果她部署的是开源 MOS，就先 dump 再注册

适合：

- 她用的是开源 MemOS / MOS
- 她对服务端有控制权

可以参考官方的 `Dumping Memories` / `register_mem_cube(...)` 口径，先做本地导出，再在新环境恢复。

## 实操建议

如果她之前说的“记忆在 memos 上”更接近 MCP / Cloud 用法，我建议她先这样走：

1. 先继续保留 MemOS MCP
2. 先把 AstrBot 这套最小体验跑通
3. 只把最重要的人设关系、偏好和关键历史手动迁到 `LivingMemory`
4. 后面再决定是否彻底把 MemOS 记忆迁完

这样不会卡在“一次性大迁移”。

## 外部参考

- [MemOS MCP Usage Guide](https://memos-docs.openmem.net/cn/mcp_agent/mcp/guide/)
- [MemOS Cloud Quick Start](https://memos-docs.openmem.net/dashboard/quick_start/)
- [MOS API Overview](https://memos-docs.openmem.net/open_source/modules/mos/overview/)
