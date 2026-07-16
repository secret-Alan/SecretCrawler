"""帮助窗口：「如何使用」说明。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)


_HELP_HTML = """
<h2>专业网络爬虫 — 使用说明</h2>

<p>本窗口介绍应用主要功能与配置项的含义。各章节按左侧配置面板的顺序排列。</p>

<h3>1. 起始配置</h3>
<ul>
  <li><b>起始 URL</b>：每行一个，作为爬虫的入口地址；多个 URL 会同时入队。</li>
  <li><b>请求方法</b>：GET / POST 二选一。选择 POST 时需配合「POST 数据」。</li>
  <li><b>POST 数据</b>：键值对表格，每行一组键 / 值，提交时以表单形式发送。</li>
</ul>

<h3>2. 抓取控制</h3>
<ul>
  <li><b>最大深度</b>：从起始 URL 起算的链接跳转层数，0 表示仅抓取起始 URL 本身。</li>
  <li><b>最大页面数</b>：本次任务最多抓取的页面总数，达到后停止入队。</li>
  <li><b>并发线程数</b>：同时进行的 HTTP 请求数量，过大可能触发目标限流。</li>
  <li><b>请求间隔</b>：每个线程两次请求之间的等待秒数，用于限速。</li>
  <li><b>请求超时</b>：单次请求的超时秒数，超时后计入失败重试。</li>
  <li><b>最大重试</b>：单个 URL 失败后的重试次数。</li>
  <li><b>最大重定向</b>：单次请求允许跟随的重定向次数上限。</li>
  <li><i>说明</i>：以上数值字段<b>已无上限限制</b>，可按需填写任意正整数。</li>
</ul>

<h3>3. 请求头与 Cookie</h3>
<ul>
  <li>键值对表格，用于附加自定义 HTTP 头（如 <code>User-Agent</code>、<code>Referer</code> 等）。</li>
  <li>支持「添加 / 删除 / 清空」操作，每行一个键值对。</li>
  <li>Cookie 可通过添加 <code>Cookie</code> 头传入。</li>
</ul>

<h3>4. 代理与 UA</h3>
<ul>
  <li><b>代理列表</b>：每行一个，支持 <code>http://</code>、<code>https://</code>、<code>socks5://</code> 等协议；启用后会轮换使用。</li>
  <li><b>User-Agent 列表</b>：每行一个 UA 字符串，启用后会按策略选用。</li>
  <li><b>UA 轮换开关</b>：开启后每次请求从列表中随机选择一个 UA。</li>
</ul>

<h3>5. 规则与过滤</h3>
<ul>
  <li><b>遵守 robots.txt</b>：开启后会先请求目标站点的 robots.txt，被禁止的 URL 不会抓取。</li>
  <li><b>仅起始域名</b>：仅允许抓取起始 URL 所在的主域。</li>
  <li><b>允许域名</b>：每行一个域名，作为抓取白名单；为空表示不额外限制。</li>
  <li><b>URL 白名单正则</b>：匹配的 URL 才会被抓取（可空）。</li>
  <li><b>URL 黑名单正则</b>：匹配的 URL 会被跳过（可空）。</li>
  <li><b>自动跟随同域链接</b>：开启后自动从已抓页面中提取同域链接入队。</li>
  <li><b>自动发现分页</b>：开启后尝试识别并跟随分页链接（如 <code>?page=2</code>）。</li>
</ul>

<h3>6. 提取规则</h3>
<ul>
  <li><b>字段名</b>：结果列名，将作为输出表的列头。</li>
  <li><b>类型</b>：CSS / XPath / Regex / JSON 四种选择器之一。</li>
  <li><b>表达式</b>：对应类型的查询表达式（如 CSS 选择器、XPath 路径、正则或 JSONPath）。</li>
  <li><b>属性</b>：取元素的属性值（如 <code>href</code>、<code>src</code>），仅在 CSS / XPath 类型下生效。</li>
  <li>多条规则会同时应用，每个页面产出一条记录。</li>
</ul>

<h3>7. 存储与输出</h3>
<ul>
  <li><b>存储格式</b>：CSV / JSON / Excel / SQLite / None（不持久化，仅存内存）。</li>
  <li><b>输出路径</b>：结果文件或数据库的保存位置。</li>
  <li><b>持久化 URL 去重</b>：开启后会将已抓 URL 写入本地去重库，重启后仍可避免重复抓取。</li>
</ul>

<h3>8. 文件下载</h3>
<ul>
  <li><b>启用开关</b>：总开关，关闭后不下载任何文件。</li>
  <li><b>后缀名白名单 / 黑名单</b>：每行一个，<b>无需带点</b>（如 <code>jpg</code>、<code>png</code>、<code>pdf</code>）。</li>
  <li><b>字符串白名单 / 黑名单</b>：每行一个，对完整下载 URL 做子串匹配。</li>
  <li><b>下载 URL 正则</b>：可选，进一步约束可下载的 URL（兼容可选，留空表示不启用正则过滤）。</li>
  <li><b>下载目录</b>：文件保存路径。</li>
  <li><i>过滤规则说明</i>：<b>后缀过滤</b>与<b>字符串过滤</b>是两个相互独立的维度，URL 必须<b>同时</b>通过两个维度才会被下载。</li>
</ul>

<h3>9. 运行控制</h3>
<ul>
  <li><b>开始</b>：读取配置并启动爬虫。</li>
  <li><b>暂停 / 继续</b>：在运行态可暂停，暂停态可继续。</li>
  <li><b>停止</b>：请求引擎优雅停止，等待当前任务结束。</li>
  <li><b>保存项目</b>：将当前配置保存为 <code>*.crawlproj</code> 文件。</li>
  <li><b>加载项目</b>：从 <code>*.crawlproj</code> 或 JSON 文件恢复配置。</li>
  <li><b>导出结果</b>：将结果表格导出为 CSV。</li>
</ul>

<h3>10. 结果查看与导出</h3>
<ul>
  <li><b>结果表格</b>：以表格形式展示每页提取的字段。</li>
  <li><b>双击复制单元格</b>：双击单元格可将其内容复制到剪贴板。</li>
  <li><b>右键菜单</b>：
    <ul>
      <li>复制行：将整行内容复制为制表符分隔文本。</li>
      <li>查看原始响应：打开原始响应查看对话框。</li>
      <li>在浏览器打开：用系统默认浏览器打开该行对应的 URL。</li>
      <li>删除行：从结果表中移除该行。</li>
    </ul>
  </li>
  <li><b>导出 CSV</b>：将当前结果导出为 CSV 文件。</li>
</ul>

<h3>11. 原始响应搜索</h3>
<ul>
  <li>在「查看原始响应」对话框<b>顶部</b>有搜索条。</li>
  <li>输入关键字后点击「上一个 / 下一个」逐个跳转命中位置。</li>
  <li><b>区分大小写</b>：可通过复选框切换是否区分大小写。</li>
  <li><b>命中高亮</b>：所有命中处会高亮显示，并显示命中总数。</li>
</ul>

<hr>
<p><i>提示：配置修改后需重新「开始」才会生效；运行中修改配置不会立即作用于当前任务。</i></p>
"""


class HelpWindow(QDialog):
    """「如何使用」说明窗口（非模态）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("如何使用 — 专业网络爬虫")
        self.resize(820, 640)

        self._browser = QTextBrowser(self)
        self._browser.setReadOnly(True)
        self._browser.setOpenExternalLinks(True)
        self._browser.setHtml(_HELP_HTML)

        self._close_btn = QPushButton("关闭", self)
        self._close_btn.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self._browser)
        layout.addWidget(self._close_btn)
