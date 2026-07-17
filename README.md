# SecretCrawler

> 专业合规网络爬虫 GUI 工具 · v1.0.0 · GPL-3.0

**本项目为 GPL 3.0 协议开源，仅供学习开发使用。任何使用本项目产生的法律责任由使用者自行承担。**

SecretCrawler 是一款基于 PySide6 的桌面网络爬虫应用，集成可视化元素拾取、合规校验、多浏览器支持、脚本扩展与系统托盘等能力，目标是让开发者在不违反《网络安全法》《个人信息保护法》《反不正当竞争法》的前提下高效完成数据采集任务。

---

## ✨ 核心特性

### 🕷️ 爬取引擎
- **多线程调度**：可配置并发数、请求间隔、超时、重试、最大重定向
- **深度/广度控制**：最大深度、最大页面数自由调整（无上限）
- **请求头 & Cookie 管理**：支持自定义 User-Agent、POST 数据、Cookie 键值对
- **代理支持**：HTTP/SOCKS 代理配置
- **文件下载**：扩展名白/黑名单、特定字符串过滤、自动归类存储

### 🎯 可视化元素拾取（EasySpider 风格）
- **Ctrl + 点击拾取**：网页内任意元素自动生成 XPath / CSS 选择器
- **持久标记**：已拾取元素绿色虚线外框，悬停橙色高亮
- **右键上下文菜单**：输入文字 / 点击元素 / 采集数据 / 选中全部同类 / 设为翻页 / 设为循环
- **流程步骤面板**：元素库 + 流程步骤双 Tab，支持上移/下移/删除/清空
- **任务序列化**：`.pickertask` JSON 格式保存/加载
- **任务执行**：按顺序执行动作，循环动作支持 `:nth-child(N)` 子元素遍历
- **元素库 → 流程步骤**：右键元素直接加入流程

### 🌐 多浏览器支持
*该部分功能尚未完善*
- **SecretCrawler 内嵌浏览器**（基于 QtWebEngine，默认）
- **Google Chrome**：通过 `--remote-debugging-port=9222` 启动外部进程
- **Microsoft Edge**：自动探测 Windows 安装路径（注册表 + 默认路径）

### 📜 脚本扩展（.scskill）
*该部分功能尚未完善*
支持三种脚本文件后缀，存放于 `skill/` 目录：

| 后缀 | 类型 | 执行方式 |
|------|------|----------|
| `.scskill` | PickerTask JSON | 由「网页爬寻」生成，直接执行流程步骤 |
| `.py.scskill` | Python 脚本 | 在受限命名空间中 `exec`（提供 `requests` / `bs4` / `crawler` 等） |
| `.js.scskill` | JavaScript 脚本 | 注入到 QWebEngineView 中 `runJavaScript` 执行 |

内置 `.scskill` 编辑器：语法校验、保存、运行一站式。

### ⚖️ 合规与探测
- **Robots 协议**：自动解析 `robots.txt` 并遵守 `Disallow` 规则
- **状态码警告**：429 / 403 / 503 等触发暂停 + 系统托盘通知
- **API 探测**：识别页面是否为 JSON API 响应
- **登录识别**：检测登录表单并提示
- **TOS 警告**：服务条款关键字检测
- **数据分级提示**：禁止爬取个人隐私 / 商业秘密 / 政府涉密数据

### 🖥️ 系统集成
- **启动闪屏**：展示 `app.ico` + 加载进度
- **自动更新**：从 GitHub Releases 拉取新版本并提示
- **系统托盘**：关闭主窗口时隐藏到托盘，任务保持运行
- **用户协议**：首次启动展示（markdown 库缺失时自动降级为 Qt 内置渲染）
- **帮助文档**：内置「如何使用」窗口

### 📊 数据展示
- **结果表格**：支持列搜索、CSV/JSON/Excel/SQLite 导出
- **统计视图**：pyqtgraph 实时绘制抓取趋势
- **日志视图**：集成 Python logging，分级显示
- **Robots 视图**：可视化 robots.txt 解析结果

---

## 🚀 快速开始

### 环境要求
- Python 3.10+
- Windows / macOS / Linux（Windows 下功能最完整）

### 安装依赖

```bash
pip install -r requirements.txt
```

主要依赖：

| 包 | 版本 | 用途 |
|----|------|------|
| PySide6 | ~= 6.5 | GUI 框架 + QtWebEngine |
| requests | ~= 2.31 | HTTP 客户端 |
| beautifulsoup4 | ~= 4.12 | HTML 解析 |
| lxml | ~= 5.0 | XPath 解析后端（需 5.0+ 以兼容 Python 3.13） |
| openpyxl | ~= 3.1 | Excel 导出 |
| PySocks | ~= 1.7 | SOCKS 代理支持 |
| pyqtgraph | ~= 0.13 | 统计图表 |
| Pillow | ~= 10.0 | 闪屏图标处理 |
| markdown | ~= 3.5 | 用户协议渲染（缺失时自动降级） |

### 运行

```bash
python main.py
```

### 加载示例项目

启动后在工具栏点击「加载项目」，选择 `examples/example.crawlproj` 即可载入抓取 [quotes.toscrape.com](http://quotes.toscrape.com/) 的示例配置。

---

## 📁 项目结构

```
internet_bug/
├── main.py                      # 应用入口（依赖检查、托盘、闪屏）
├── app.ico                      # 应用图标
├── requirements.txt             # Python 依赖清单
├── version.txt / last_version.txt
├── contributors.txt
│
├── crawler/                     # 爬虫核心引擎
│   ├── engine.py                #   CrawlEngine 主调度
│   ├── models.py                #   数据类（CrawlConfig / PickerTask / PickerAction 等）
│   ├── scheduler.py             #   多线程任务调度
│   ├── downloader.py            #   HTTP 下载器
│   ├── parser.py                #   HTML 解析 + 字段提取
│   ├── pipeline.py              #   数据处理管道
│   ├── url_manager.py           #   URL 去重与队列
│   ├── robots.py                #   robots.txt 解析
│   ├── compliance.py            #   合规校验
│   ├── status_handler.py        #   状态码告警
│   ├── api_detector.py          #   API 响应识别
│   ├── auth_detector.py         #   登录表单识别
│   ├── browser_launcher.py      #   外部浏览器启动（Chrome / Edge）
│   └── updater.py               #   GitHub 自动更新
│
├── ui/                          # PySide6 界面层
│   ├── main_window.py           #   主窗口（菜单 / 工具栏 / 状态栏 / 选项卡）
│   ├── config_panel.py          #   左侧配置面板（分组表单 + 爬寻步骤）
│   ├── result_table.py          #   结果表格（含搜索）
│   ├── stats_view.py            #   统计视图（pyqtgraph）
│   ├── log_view.py              #   日志视图
│   ├── robots_view.py           #   robots.txt 可视化
│   ├── scskill_editor.py        #   .scskill 脚本编辑器
│   ├── element_picker_window.py #   可视化元素拾取器（内嵌浏览器）
│   ├── agreement_window.py      #   用户协议窗口
│   ├── help_window.py           #   帮助文档窗口
│   ├── about_window.py          #   关于软件窗口
│   ├── splash.py                #   启动闪屏
│   ├── update_prompt_window.py  #   更新提示窗口
│   ├── update_progress_window.py#   更新进度窗口
│   ├── update_log_window.py     #   更新日志窗口
│   └── widgets.py               #   复用控件
│
├── examples/                    # 示例项目
│   └── example.crawlproj
│
├── skill/                       # .scskill 脚本目录（首次启动自动创建）
│
├── tests/                       # 单元测试
│   ├── test_parser.py
│   ├── test_pipeline.py
│   ├── test_robots.py
│   └── test_url_manager.py
│
└── output/                      # 默认输出目录
```

---

## 🧭 使用流程

### 1. 配置爬取任务
1. 启动后同意用户协议
2. 在左侧配置面板填入起始 URL、请求方法、并发数等
3. 配置提取规则（CSS / XPath 选择器 + 字段名）
4. 选择存储格式（CSV / JSON / Excel / SQLite）

### 2. 通过网页爬寻（可视化）
1. 在「爬寻步骤」分区选择浏览器类型
2. 点击「通过网页爬寻」打开内嵌浏览器
3. 输入目标 URL 后启用「拾取模式」
4. **按住 Ctrl 点击网页元素**拾取，元素进入元素库
5. 右键元素可选择「加入流程步骤」
6. 右键网页元素弹出动作菜单（输入文字 / 点击 / 采集 / 翻页 / 循环）
7. 点击「保存任务」导出 `.pickertask`，或「导出为 .scskill」生成脚本
8. 点击「执行任务」按顺序执行流程

### 3. 通过脚本运行
1. 将 `.scskill` / `.py.scskill` / `.js.scskill` 文件放入 `skill/` 目录
2. 在左侧「爬寻步骤」分区的下拉菜单中选择脚本
3. 点击「按 .scskill 运行」
4. 也可在右侧「.scskill 编辑器」标签页中编辑、校验、运行

### 4. 后台运行
- 关闭主窗口时自动隐藏到系统托盘，任务保持运行
- 出现警告状态时任务自动暂停，托盘图标变色并弹出通知

---

## ⚖️ 法律合规指南

### 访问控制三要素

| 要素 | 合规要求 | 违规后果 |
|------|----------|----------|
| **Robots 协议** | 必须解析 `robots.txt` 并遵守 `Disallow` 规则 | 成为侵权诉讼关键证据 |
| **数据分级** | 禁止爬取：个人隐私 / 商业秘密 / 政府涉密数据<br>允许爬取：公开非敏感数据（需控制频率） | 最高 5000 万元罚款或刑事责任 |
| **授权机制** | 商用前需取得网站书面授权<br>优先使用官方 API 接口 | 构成非法获取计算机信息系统数据罪 |

### 技术红线
- ❌ 破解验证码 / 绕过登录认证
- ❌ 伪造 User-Agent 或 IP 欺骗
- ❌ 每秒超过 10 次的高频请求
- ❌ 存储未脱敏的用户隐私数据

本项目内置 robots 协议解析、状态码告警、数据分级提示，**但合规责任最终由使用者承担**。

---

## 🧪 测试

```bash
pytest tests/
```

覆盖 `parser` / `pipeline` / `robots` / `url_manager` 四个核心模块。

---

## 📦 打包

使用 PyInstaller 打包为单文件可执行程序（Windows 下生成 `Crawler.exe`）：

```bash
pyinstaller --noconsole --onefile --icon app.ico main.py
```

打包后会自动内嵌 `examples/` 与 `app.ico`，首次启动时还原到 exe 同级目录。

---

## 📜 许可证

本项目基于 [GPL-3.0](https://www.gnu.org/licenses/gpl-3.0.html) 协议开源。

## 👥 贡献者

- **secret-Alan** 

## 🔗 仓库

- GitHub: https://github.com/secret-Alan/internet_crawler.git

---

## 🙏 免责声明

本工具仅供**学习与合规开发使用**。使用者需自行遵守所在国家/地区的法律法规，因不当使用产生的任何法律责任与本仓库作者无关。
