# astrbot_plugin_file_delivery

把 `file_vault` 里的受管文件直接回传到聊天目标。

适合和下面这些能力配合使用：

- `astrbot_plugin_ebooks`
  - 下载后的电子书先转存到 `file_vault`
- `astrbot_plugin_bookshelf`
  - 需要把书或导入文件继续发给聊天目标
- `aranbox-mcp`
  - 通过工具链继续调用 `send_file_vault_item_to_qq`

## 它是怎么发文件的

插件会按下面的顺序尝试发送路径：

1. `flash_transfer_dir`
   - 先把文件复制到一个共享中转目录，再让 QQ 适配器从这个路径发送
2. `docker cp`
   - 如果 AstrBot 运行在容器里，且没法走共享目录，就尝试把文件 `docker cp` 到 NapCat 容器里的 `/tmp/...`
3. `direct`
   - 直接把原文件路径交给 QQ 适配器

最稳的方案是第一种：`AstrBot` 容器和 `NapCat` 容器挂同一个共享目录。

## 推荐配置

### 方案 A：两个容器共享文件夹

这是最推荐的方案。

假设宿主机准备一个目录：

```bash
mkdir -p /data/astrbot_shared/astrbot_flash
```

然后把它同时挂进两个容器，例如都挂到：

- AstrBot 容器：`/tmp/astrbot_flash`
- NapCat 容器：`/tmp/astrbot_flash`

插件配置建议：

```json
{
  "file_vault_root": "/AstrBot/data/file_vault",
  "flash_transfer_dir": "/tmp/astrbot_flash",
  "napcat_container_name": "napcat",
  "allowed_sender_ids": "123456789",
  "allow_anyone": false,
  "allow_cross_user_delivery": false
}
```

说明：

- `file_vault_root`
  - 指向 AstrBot 容器内的 `file_vault`
- `flash_transfer_dir`
  - 指向 AstrBot 容器内可见、同时 NapCat 也能看到的共享目录
- `napcat_container_name`
  - 这时基本只是兜底，主路径会优先走共享目录

这种方式的好处是：

- 最稳定
- 不依赖 `docker cp`
- AstrBot 和 NapCat 都只处理本地文件路径

### 方案 B：不挂共享目录，走 `docker cp`

如果你暂时没法给两个容器挂同一个共享目录，也可以先用兜底方案。

插件配置：

```json
{
  "file_vault_root": "/AstrBot/data/file_vault",
  "flash_transfer_dir": "",
  "napcat_container_name": "napcat",
  "allowed_sender_ids": "123456789",
  "allow_anyone": false,
  "allow_cross_user_delivery": false
}
```

要求：

- AstrBot 运行环境里要能执行 `docker`
- AstrBot 容器里要能访问 Docker socket，或者宿主机运行 AstrBot
- `napcat_container_name` 必须和真实容器名一致

这个方案能用，但不如共享目录稳。

## 如果 AstrBot 不在 Docker 里

如果 AstrBot 直接跑在宿主机上，而 NapCat 在容器里，也建议优先用共享目录。

例如：

- 宿主机目录：`/data/astrbot_shared/astrbot_flash`
- AstrBot 配置里填：`flash_transfer_dir=/data/astrbot_shared/astrbot_flash`
- NapCat 容器里把这个宿主机目录挂到 `/tmp/astrbot_flash`

这样 AstrBot 写宿主机目录，NapCat 读容器挂载目录，也能工作。

## 权限配置

### 默认行为

- LLM 工具调用时
  - 默认只允许把文件发回“当前和机器人说话的这个用户”
- 手动命令或跨用户转发
  - 默认不允许

### 想只给自己用

最常见配置是：

```json
{
  "allowed_sender_ids": "123456789",
  "allow_anyone": false,
  "allow_cross_user_delivery": false
}
```

这样：

- 你可以手动触发
- 但不能随便跨用户转发

### 想允许跨用户转发

```json
{
  "allowed_sender_ids": "123456789",
  "allow_anyone": false,
  "allow_cross_user_delivery": true
}
```

这种只建议在你明确知道自己在做什么时开启。

## 常见问题

### 为什么需要两个容器共享文件夹？

因为发送在线文件时，QQ 适配器最终需要“自己能看到”的文件路径。

如果文件只存在于 AstrBot 容器的内部路径里，NapCat 看不到，就会发失败。

所以最稳的做法就是：

- AstrBot 先把文件复制到共享目录
- NapCat 再从同一个共享目录读取并发送

### 不共享文件夹能不能发？

能，插件会尝试 `docker cp` 兜底。

但如果：

- AstrBot 容器里没有 `docker`
- 没挂 Docker socket
- 容器名不对

就会失败。

### 怎么确认现在走的是哪条路径？

插件会依次尝试：

- `flash_transfer_dir`
- `docker_cp`
- `direct`

如果前面的路径失败，错误里会把尝试过的模式带出来。

### `file_vault_root` 应该填什么？

优先填 AstrBot 运行时真正能看到的路径。

容器内通常是：

```text
/AstrBot/data/file_vault
```

如果你误填成宿主机路径，插件会尝试按 `ASTRBOT_HOST_DATA_ROOT` 自动映射回容器路径，但公开版仍然建议你直接写容器内路径，最不容易出错。
