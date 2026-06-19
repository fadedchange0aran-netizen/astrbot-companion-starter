# 旧记忆导入

这份文档面向两种情况：

- 你以前已经在别的 AstrBot 实例里用过 `LivingMemory`
- 你手上已经有旧的 `livingmemory.db` 或备份目录

## 先说结论

最稳的做法不是“手搓导表”，而是：

1. 先把旧数据库文件备份好
2. 把旧 `livingmemory.db` 放到新实例的 `plugin_data` 目录
3. 重启插件
4. 必要时执行索引重建

## 默认目录

`LivingMemory` 的默认数据目录一般在：

```text
data/plugin_data/astrbot_plugin_livingmemory/
```

常见文件包括：

- `livingmemory.db`
- `faiss_index.bin` 或其他索引文件
- `backups/`

## 从旧实例迁移到新实例

### 方案 A：直接迁移数据库

适合：

- 你主要想保留已有长期记忆
- 新旧实例的 `LivingMemory` 版本差异不算特别大

步骤：

1. 关闭旧实例或确认数据库不再写入
2. 复制旧的 `livingmemory.db`
3. 放到新实例：

```text
data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db
```

4. 启动 AstrBot
5. 执行：

```text
/lmem status
/lmem search 关键词
```

如果能搜到旧内容，说明基础迁移已经成功。

### 方案 B：从备份恢复

如果你手上不是主库，而是备份文件，例如：

```text
data/plugin_data/astrbot_plugin_livingmemory/backups/livingmemory_backup_<timestamp>.db
```

可以：

1. 复制这份备份
2. 重命名为 `livingmemory.db`
3. 放到新的 `plugin_data/astrbot_plugin_livingmemory/`
4. 重启插件

## 什么时候要重建索引

下面这些情况建议重建索引：

- 能看到记忆条数，但搜索结果明显不对
- 更换过 embedding provider
- 旧库迁移后检索异常
- 版本跨度较大

命令：

```text
/lmem rebuild-index
```

如果你还需要给旧记忆补图谱数据，可以再执行：

```text
/lmem rebuild-graph
```

## 验证迁移是否成功

建议至少做这几步：

1. `/lmem status`
2. `/lmem search 你确信旧库里存在的关键词`
3. 让机器人自然聊两轮，观察是否会回忆到旧内容
4. 打开 WebUI 看记忆列表是否存在

## 注意事项

- 迁移前一定先备份原库
- 不建议同时手改数据库和跑中的实例
- 如果更换过 embedding provider，优先考虑重建索引
- 如果只想迁移“有用记忆”，可以先手动整理旧库，再迁到新实例

## 这份文档和官方记忆仓的关系

`LivingMemory` 公开仓已经有安装、配置和版本恢复说明，但这份 starter 文档额外补了“旧实例迁移”的操作顺序，方便第一次搬家的人直接照着做。*** End Patch
