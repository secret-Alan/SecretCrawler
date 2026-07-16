# 示例项目

本目录存放可直接加载到爬虫配置面板的示例 `.crawlproj` 项目文件。

## example.crawlproj

抓取 [quotes.toscrape.com](http://quotes.toscrape.com/) 名言站点的示例配置。该站点是一个公开的、专门用于爬虫练习的沙盒环境，可合法抓取。

### 加载方式

启动程序：

```
python main.py
```

在工具栏点击「加载项目」，选择 `examples/example.crawlproj` 即可载入全部配置。

### 提取字段

该示例配置了 4 条 CSS 提取规则：

| 字段名   | 选择器              | 说明           |
|----------|---------------------|----------------|
| text     | `span.text`         | 名言正文       |
| author   | `small.author`      | 作者           |
| tags     | `div.tags a.tag`    | 标签列表       |
| url      | `span a[href]`      | 详情链接 (href)|

### 其他示例

可参考此文件结构，将 `start_urls` 改为 `http://books.toscrape.com/` 并调整 `extraction_rules`，即可抓取 books 沙盒站点（书名、价格、库存等字段）。
