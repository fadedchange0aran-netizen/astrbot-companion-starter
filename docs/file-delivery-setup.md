# 文件回传配置

这篇文档专门说明 `astrbot_plugin_file_delivery` 怎么落地，尤其是：

- `file_vault_root` 应该填什么
- `flash_transfer_dir` 为什么最推荐
- AstrBot 和 NapCat 两个容器怎么挂共享目录
- 不挂共享目录时怎么走 `docker cp` 兜底

## 核心结论

最推荐的方案是：

1. AstrBot 把 `file_vault` 放在自己的数据目录里
2. AstrBot 和 NapCat 再额外挂同一个共享目录
3. `flash_transfer_dir` 指向这个共享目录在 AstrBot 容器里的路径

也就是：

- `file_vault_root`
  - 解决“文件原件放在哪”
- `flash_transfer_dir`
  - 解决“发给 QQ 前，NapCat 从哪读到这个文件”

## 发送流程

`astrbot_plugin_file_delivery` 会按下面顺序尝试：

1. `flash_transfer_dir`
2. `docker cp`
3. `direct`

其中第一种最稳。

原因很简单：

- AstrBot 容器里看得到的路径
  - NapCat 不一定看得到
- NapCat 真正发送在线文件时
  - 必须拿到它自己能访问的文件路径

所以如果两个容器共用一个目录，成功率最高。

## 推荐目录规划

假设宿主机准备两个目录：

```bash
mkdir -p /data/astrbot/data
mkdir -p /data/astrbot/shared/astrbot_flash
```

这里建议这样理解：

- `/data/astrbot/data`
  - AstrBot 自己的数据目录
- `/data/astrbot/shared/astrbot_flash`
  - AstrBot 和 NapCat 共同可见的临时发文件目录

## Docker Compose 示例

下面是一个最小思路示例，重点只看挂载关系：

```yaml
services:
  astrbot:
    image: your-astrbot-image
    container_name: astrbot
    volumes:
      - /data/astrbot/data:/AstrBot/data
      - /data/astrbot/shared/astrbot_flash:/tmp/astrbot_flash

  napcat:
    image: your-napcat-image
    container_name: napcat
    volumes:
      - /data/astrbot/shared/astrbot_flash:/tmp/astrbot_flash
```

关键点只有两个：

- AstrBot 挂：
  - `/data/astrbot/data:/AstrBot/data`
  - `/data/astrbot/shared/astrbot_flash:/tmp/astrbot_flash`
- NapCat 挂：
  - `/data/astrbot/shared/astrbot_flash:/tmp/astrbot_flash`

这样：

- AstrBot 能从 `/AstrBot/data/file_vault` 找到受管文件
- AstrBot 能把待发送文件复制到 `/tmp/astrbot_flash`
- NapCat 也能从 `/tmp/astrbot_flash` 读取同一个文件并发送

## 插件配置示例

推荐直接这样配：

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

各字段含义：

- `file_vault_root`
  - 建议直接写 AstrBot 容器内路径 `/AstrBot/data/file_vault`
- `flash_transfer_dir`
  - 建议写两个容器共享的目录，例如 `/tmp/astrbot_flash`
- `napcat_container_name`
  - 主要给 `docker cp` 兜底使用；共享目录方案里不是主路径
- `allowed_sender_ids`
  - 只填你自己的 `sender_id`
- `allow_anyone`
  - 生产环境建议保持 `false`
- `allow_cross_user_delivery`
  - 默认建议保持 `false`

## 如果 AstrBot 直接跑在宿主机

如果 AstrBot 不在 Docker 里，而 NapCat 在容器里，也一样建议共享目录。

例如：

- 宿主机真实目录：
  - `/data/astrbot/shared/astrbot_flash`
- AstrBot 插件配置：
  - `flash_transfer_dir=/data/astrbot/shared/astrbot_flash`
- NapCat 容器挂载：
  - `/data/astrbot/shared/astrbot_flash:/tmp/astrbot_flash`

这种情况下：

- AstrBot 直接把文件复制到宿主机目录
- NapCat 从容器内挂载路径读取

也能正常工作。

## 兜底方案：docker cp

如果你暂时不想挂共享目录，也可以把：

```json
{
  "flash_transfer_dir": ""
}
```

这时插件会尝试：

- 用 `docker cp` 把文件复制进 `napcat_container_name` 对应的容器

前提是：

- AstrBot 运行环境里有 `docker`
- AstrBot 那边有权限访问 Docker
- `napcat_container_name` 填的确实是目标容器名

这个方案能用，但不如共享目录稳，也更容易受容器权限和 Docker socket 影响。

## 最短排障

如果发文件失败，优先按这个顺序查：

1. 确认 `file_vault_root` 真的是 AstrBot 当前运行环境里能看到的路径
2. 确认 `flash_transfer_dir` 在 AstrBot 和 NapCat 两边都存在
3. 确认两个容器里看到的是同一个共享目录
4. 确认 `allowed_sender_ids` 已填你自己的 `sender_id`
5. 如果你走 `docker cp`：
   - 确认 AstrBot 里能执行 `docker`
   - 确认容器名 `napcat_container_name` 正确

## 你最容易记住的一版

如果你懒得记细节，就记这一句：

- `file_vault_root` 写 AstrBot 容器内路径
- `flash_transfer_dir` 写两个容器共享的同一路径
- 优先不要依赖 `docker cp`
