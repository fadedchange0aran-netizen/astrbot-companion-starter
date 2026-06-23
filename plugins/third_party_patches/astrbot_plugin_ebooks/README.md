# astrbot_plugin_ebooks

一个多源电子书搜索与下载插件，当前公开版本作为 patch 版收录在 `third_party_patches/`。

## 当前版本特点

- 支持多来源搜索：Calibre-Web、Liber3、Archive.org、Z-Library、Anna's Archive
- 支持下载后自动转存到 `file_vault`，方便后续继续发送、归档或导入共读
- 保留 AstrBot 命令与函数工具调用方式

## 公开版说明

- 这是基于上游插件整理的公开 patch 版
- 默认不携带任何私有账号、书源凭据或服务器地址
- 某些来源需要你自行准备账号、网络条件或自建服务
- 不同来源的可用性和合规边界请你自行评估

## 常用配置

- `enable_calibre`
  - 启用 Calibre-Web 搜索；需要你自己的 Calibre-Web 服务
- `enable_liber3`
  - 启用 Liber3 搜索
- `enable_archive`
  - 启用 Archive.org 搜索
- `enable_zlib`
  - 启用 Z-Library 搜索；需要你自己的登录账户
- `enable_annas`
  - 启用 Anna's Archive 搜索
- `enable_file_vault_copy`
  - 下载后自动转存到 `file_vault`
- `file_vault_root`
  - 受管文件库根目录；留空时默认使用 AstrBot 工作目录下的 `data/file_vault`

## 常用命令

```text
/ebooks help
/ebooks search <关键词> [数量]
/ebooks download <link or ID,Hash>
/calibre search <关键词>
/liber3 search <关键词>
/zlib search <关键词> [数量]
/archive search <关键词> [数量]
/annas search <关键词> [数量]
```

## 说明

- 推荐把它和 `file_delivery`、`bookshelf` 搭配使用
- 下载后的受管副本可继续交给 `send_file_vault_item_to_qq` 或 `import_file_vault_item_to_bookshelf`
- 如果你只想保留低风险能力，可以只开启公开来源或自建来源
