# calibre-web-douban-api

为 Calibre-Web 提供豆瓣图书搜索和元数据抓取能力的 Metadata Provider 插件。

插件通过豆瓣网页搜索图书，读取图书详情页并返回标题、作者、出版社、出版日期、简介、评分、丛书、标签、ISBN、豆瓣书号和封面等信息。

## 功能

- 保持独立的 `NewDouban` Provider，不会覆盖 Calibre-Web 内置的 `douban` Provider。
- 优先使用豆瓣 HTML 搜索，无结果或请求失败时回退到 JSON 搜索。
- 并发读取最多 5 条图书详情；单本解析失败不会影响其他结果。
- 为请求设置连接/读取超时，并对临时性 HTTP 5xx 错误进行有限重试。
- 将豆瓣 `/subject/<id>/` 中的 ID 保存为 `douban` 书号，同时保留 ISBN。
- 通过 Calibre-Web 本地接口流式代理豆瓣封面，解决图片防盗链问题。
- 封面代理仅允许访问 HTTPS 的 `doubanio.com` 及其子域。

返回的主要标识符示例：

```python
book.identifiers = {
    "douban": "1234567",
    "isbn": "9781234567890",
}
```

## 适用版本

本项目用于 Calibre-Web 0.6.17 及以上版本。更早的 Calibre-Web 版本请使用 [0.6.16 兼容版本](https://github.com/fugary/calibre-web-douban-api/releases/tag/0.6.16)。

Calibre-Web 新版本已经重新提供内置豆瓣插件。如果内置插件能够满足需求，可以不安装本插件；本插件使用独立的 `new_douban` Provider ID，可与内置 Provider 区分。

## 安装

1. 将 [`src/NewDouban.py`](src/NewDouban.py) 复制到 Calibre-Web 的 `cps/metadata_provider/` 目录：

   ```text
   calibre-web/
   └── cps/
       └── metadata_provider/
           └── NewDouban.py
   ```

2. 重启 Calibre-Web。

3. 在 Calibre-Web 的图书元数据搜索中选择 `NewDouban` Provider。

升级时重新复制最新的 `NewDouban.py` 并重启 Calibre-Web 即可。

也可以从 [最新 Release](https://github.com/fugary/calibre-web-douban-api/releases/latest/download/NewDouban.py) 直接下载插件文件。

## 封面代理配置

默认情况下，插件根据当前请求的 `host_url` 自动生成本地封面代理地址。

如果 Calibre-Web 运行在 Docker、NAS 或反向代理后面，自动生成的地址无法从浏览器访问，可编辑 `NewDouban.py` 中的配置：

```python
DOUBAN_PROXY_COVER_HOST_URL = "http://192.168.1.100:8083/"
```

该地址应填写用户浏览器能够访问的 Calibre-Web 根地址，并建议保留结尾 `/`。如果不需要封面代理，可设置：

```python
DOUBAN_PROXY_COVER = False
```

## 本地测试

测试不会访问豆瓣，所有网络请求均使用 mock：

```bash
PYTHONPATH=tests:src python3 -m unittest discover -s tests -p '*Test.py' -v
```

安装运行依赖：

```bash
python3 -m pip install -r requirements.txt
```

## 使用注意

- 本插件通过抓取豆瓣网页获得数据，豆瓣页面结构变化可能影响搜索或字段解析。
- 频繁搜索可能触发豆瓣的访问限制，请控制请求频率。
- 插件不会关闭 SSL 证书验证。
- 封面代理拒绝非豆瓣图片域名，不能作为通用 URL 代理使用。

## 更新记录

### 2026-07-10

- 增加 HTML 搜索失败后的 JSON 回退。
- 增加请求超时、重试和单本失败隔离。
- 增加 `douban` 书号标识符。
- 改进封面请求头和流式代理，并限制允许访问的图片域名。
- 修复标签回退解析、日期格式、ISBN 标识符和重复封面代理问题。
- 增加离线回归测试。

### 2023-07-15

- 针对豆瓣禁止直接访问封面图片的问题，使用本地代理展示封面，并在保存时通过 Requests 下载图片。

### 2022-10-08

- 豆瓣旧版列表 URL 无法继续访问，改为新的搜索方式。

### 2022-08-10

- 适配 Calibre-Web 0.6.17 及以上版本的服务端 Metadata Provider 机制。

## 参考资料

- [Calibre-Web 豆瓣接口更新说明](https://fugary.com/?p=238)
- [Calibre-Web 新版豆瓣插件说明](https://fugary.com/?p=532)
