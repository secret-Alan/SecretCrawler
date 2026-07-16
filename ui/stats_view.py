"""统计图表面板。

本模块实现 spec 中「统计图表面板」需求，提供专业爬虫项目的实时统计
视图控件 StatsView，包含：
    - 顶部数值卡片（已抓取 / 已发现 / 队列剩余 / 成功 / 失败 / 重试）
    - 状态码分布水平条形图（pyqtgraph 优先；缺失时回退到 QTableWidget）
    - 每秒抓取速率折线图（近 60 秒，X 轴为「秒前」坐标）
    - 已用时间与预计剩余时间（ETA）

控件通过 update_stats 槽接收引擎 stats_updated 信号推送的 CrawlStats 快照；
内部 1 秒 QTimer 周期性刷新速率折线图与 ETA，避免每次信号都重绘。

类定义:
    StatsView - 统计图表面板控件（QWidget）
"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# 尝试导入 pyqtgraph；缺失时启用回退路径（状态码表格 + 速率占位）
try:
    import pyqtgraph as pg

    # 浅色主题：白底黑字
    pg.setConfigOption("background", "w")
    pg.setConfigOption("foreground", "k")
    _HAS_PYQTGRAPH = True
except Exception:  # pragma: no cover - 依赖可选
    pg = None  # type: ignore[assignment]
    _HAS_PYQTGRAPH = False


# 速率折线图保留的历史窗口（秒）
_RATE_WINDOW_SECONDS = 60

# 速率缓冲上限（样本条数），避免长任务无限增长
_RATE_BUFFER_CAP = _RATE_WINDOW_SECONDS * 4

# 卡片样式：浅灰背景 + 圆角边框
_CARD_QSS = """
QWidget#StatCard {
    background: #f7f9fc;
    border: 1px solid #d6dce5;
    border-radius: 6px;
}
"""
# 数值文字样式：大号粗体蓝色
_VALUE_QSS = "font-size: 20pt; font-weight: bold; color: #1a73e8;"
# 标签文字样式：小号灰色
_CAPTION_QSS = "font-size: 9pt; color: #555555;"

# 图表统一配色（条形与折线）
_CHART_BRUSH = "#4a90d9"


def _format_hms(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS 字符串；负值按 0 处理。"""
    total = int(seconds)
    if total < 0:
        total = 0
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class StatsView(QWidget):
    """统计图表面板控件。

    通过 update_stats 接收 CrawlStats 快照更新数值卡片与状态码分布图；
    内部 1 秒 QTimer 周期性刷新每秒速率折线图与已用时间 / ETA。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # 顶部数值卡片：value QLabel 字典，按字段名索引
        self._value_labels: dict[str, QLabel] = {}

        # 速率折线滚动缓冲：(timestamp, pages_crawled)
        self._rate_buffer: list[tuple[float, int]] = []
        # 当前速率（页面/秒），由 _refresh_rate_chart 计算并供 ETA 使用
        self._current_rate: float = 0.0
        # 最新一次 stats 的 started_at 与 queue_size，供 ETA 计算
        self._started_at: float = 0.0
        self._queue_size: int = 0

        # 状态码图表控件（二者之一为 None，取决于 pyqtgraph 是否可用）
        self._status_plot: Any = None  # pyqtgraph PlotWidget
        self._status_table: QTableWidget | None = None  # 回退用表格
        # 速率折线图控件（pyqtgraph 不可用时为占位 QLabel）
        self._rate_plot: Any = None
        self._rate_placeholder: QLabel | None = None

        # ---- 整体布局：卡片行 / 图表区 / 时间标签行 ----
        root = QVBoxLayout(self)

        # ---- 13.1 顶部数值卡片行 ----
        root.addLayout(self._build_cards_row())

        # ---- 13.2 & 13.3 中部图表区：左状态码、右速率 ----
        root.addWidget(self._build_charts_area(), 1)

        # ---- 13.4 底部已用时间与 ETA ----
        root.addLayout(self._build_time_row())

        # ---- 1 秒刷新定时器：负责速率折线图与 ETA 标签 ----
        self._timer = QTimer(self)
        self._timer.setInterval(1000)  # 1000ms
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    # ---------- 公共 API ----------

    @Slot(object)
    def update_stats(self, stats: Any) -> None:
        """接收引擎推送的 CrawlStats 快照并刷新数值与状态码图。

        - 顶部数值卡片立即更新（成本低）。
        - 状态码分布图立即更新（条目数量小）。
        - 速率折线图与 ETA 标签由 1 秒定时器统一刷新，避免过频重绘。
        """
        # 防御性读取：允许 stats 为任意具备这些属性的对象
        pages_crawled = int(getattr(stats, "pages_crawled", 0) or 0)
        urls_discovered = int(getattr(stats, "urls_discovered", 0) or 0)
        queue_size = int(getattr(stats, "queue_size", 0) or 0)
        success = int(getattr(stats, "success", 0) or 0)
        failed = int(getattr(stats, "failed", 0) or 0)
        retries = int(getattr(stats, "retries", 0) or 0)
        status_codes = getattr(stats, "status_codes", None) or {}
        started_at = float(getattr(stats, "started_at", 0.0) or 0.0)

        # 更新数值卡片
        self._set_value("pages_crawled", pages_crawled)
        self._set_value("urls_discovered", urls_discovered)
        self._set_value("queue_size", queue_size)
        self._set_value("success", success)
        self._set_value("failed", failed)
        self._set_value("retries", retries)

        # 记录 ETA 所需字段
        self._started_at = started_at
        self._queue_size = queue_size

        # 追加到速率缓冲
        self._rate_buffer.append((time.time(), pages_crawled))
        # 限制缓冲上限
        if len(self._rate_buffer) > _RATE_BUFFER_CAP:
            del self._rate_buffer[: len(self._rate_buffer) - _RATE_BUFFER_CAP]

        # 立即更新状态码分布图
        self._refresh_status_chart(status_codes)

    def reset(self) -> None:
        """清空所有统计：归零卡片、清空速率缓冲与图表。"""
        # 归零卡片
        for key in self._value_labels:
            self._set_value(key, 0)
        # 清空速率缓冲与当前速率
        self._rate_buffer.clear()
        self._current_rate = 0.0
        self._started_at = 0.0
        self._queue_size = 0
        # 清空状态码图
        self._refresh_status_chart({})
        # 清空速率折线图
        if _HAS_PYQTGRAPH and self._rate_plot is not None:
            self._rate_plot.clear()
        # 立即刷新一次时间标签
        self._refresh_time_labels()

    # ---------- 布局构建 ----------

    def _build_cards_row(self) -> QGridLayout:
        """构建顶部 6 个数值卡片，使用 QGridLayout 排成一行。"""
        # (内部键, 显示标签)
        cards = [
            ("pages_crawled", "已抓取"),
            ("urls_discovered", "已发现"),
            ("queue_size", "队列剩余"),
            ("success", "成功"),
            ("failed", "失败"),
            ("retries", "重试"),
        ]
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        for col, (key, caption) in enumerate(cards):
            value_label = self._make_card(grid, 0, col, caption)
            self._value_labels[key] = value_label
        return grid

    def _make_card(
        self, grid: QGridLayout, row: int, col: int, caption: str
    ) -> QLabel:
        """在 grid 的 (row, col) 处创建一张数值卡片，返回其值 QLabel。

        卡片外观：浅灰背景 + 圆角边框；上方为大号粗体数值，下方为标签。
        """
        card = QWidget()
        card.setObjectName("StatCard")
        card.setStyleSheet(_CARD_QSS)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 8, 8, 8)
        card_layout.setSpacing(2)

        value_label = QLabel("0")
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value_label.setStyleSheet(_VALUE_QSS)

        caption_label = QLabel(caption)
        caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption_label.setStyleSheet(_CAPTION_QSS)

        card_layout.addWidget(value_label)
        card_layout.addWidget(caption_label)

        grid.addWidget(card, row, col)
        return value_label

    def _build_charts_area(self) -> QWidget:
        """构建中部图表区：左侧状态码分布图，右侧速率折线图。"""
        container = QWidget()
        layout = QGridLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        size_policy = QSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        # ---- 状态码分布图（左） ----
        status_container = QWidget()
        status_layout = QVBoxLayout(status_container)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_title = QLabel("状态码分布")
        status_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_title.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(status_title)
        if _HAS_PYQTGRAPH:
            self._status_plot = pg.PlotWidget()
            self._status_plot.setSizePolicy(size_policy)
            self._status_plot.setLabel("left", "状态码")
            self._status_plot.setLabel("bottom", "次数")
            self._status_plot.showGrid(x=True, y=False)
            status_layout.addWidget(self._status_plot, 1)
        else:
            # 回退：QTableWidget，两列「状态码 | 次数」
            self._status_table = QTableWidget(0, 2)
            self._status_table.setHorizontalHeaderLabels(["状态码", "次数"])
            header = self._status_table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self._status_table.verticalHeader().setVisible(False)
            self._status_table.setSizePolicy(size_policy)
            status_layout.addWidget(self._status_table, 1)
        status_container.setSizePolicy(size_policy)
        layout.addWidget(status_container, 0, 0)

        # ---- 速率折线图（右） ----
        rate_container = QWidget()
        rate_layout = QVBoxLayout(rate_container)
        rate_layout.setContentsMargins(0, 0, 0, 0)
        rate_title = QLabel("每秒抓取速率（近 60 秒）")
        rate_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rate_title.setStyleSheet("font-weight: bold;")
        rate_layout.addWidget(rate_title)
        if _HAS_PYQTGRAPH:
            self._rate_plot = pg.PlotWidget()
            self._rate_plot.setSizePolicy(size_policy)
            self._rate_plot.setLabel("bottom", "秒前")
            self._rate_plot.setLabel("left", "页面/秒")
            self._rate_plot.showGrid(x=True, y=True)
            # X 轴范围固定为 [-60, 0]，0 表示当前时刻
            self._rate_plot.setXRange(-_RATE_WINDOW_SECONDS, 0)
            rate_layout.addWidget(self._rate_plot, 1)
        else:
            # 无 pyqtgraph：占位提示
            self._rate_placeholder = QLabel("pyqtgraph 未安装，速率折线图不可用")
            self._rate_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._rate_placeholder.setStyleSheet("color: #999;")
            self._rate_placeholder.setSizePolicy(size_policy)
            rate_layout.addWidget(self._rate_placeholder, 1)
        rate_container.setSizePolicy(size_policy)
        layout.addWidget(rate_container, 0, 1)

        # 左右两列均分
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return container

    def _build_time_row(self) -> QHBoxLayout:
        """构建底部已用时间与 ETA 标签行。"""
        row = QHBoxLayout()
        self._elapsed_label = QLabel("已用时间: 00:00:00")
        self._eta_label = QLabel("预计剩余: 计算中…")
        self._elapsed_label.setStyleSheet("font-weight: bold;")
        self._eta_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self._elapsed_label)
        row.addStretch(1)
        row.addWidget(self._eta_label)
        return row

    # ---------- 内部刷新 ----------

    def _set_value(self, key: str, value: int) -> None:
        """更新指定卡片的数值。"""
        label = self._value_labels.get(key)
        if label is not None:
            label.setText(str(value))

    def _refresh_status_chart(self, status_codes: dict[int, int]) -> None:
        """刷新状态码分布图（pyqtgraph 水平条形图或回退表格）。

        参数 status_codes 为「状态码 -> 次数」映射；非法条目会被跳过。
        """
        # 过滤掉无法转为 int 的条目，并按状态码升序
        items: list[tuple[int, int]] = []
        for code, count in status_codes.items():
            try:
                items.append((int(code), int(count)))
            except (TypeError, ValueError):
                continue
        items.sort(key=lambda x: x[0])

        if _HAS_PYQTGRAPH and self._status_plot is not None:
            self._status_plot.clear()
            if items:
                codes = [c for c, _ in items]
                counts = [n for _, n in items]
                n = len(items)
                # 水平条形图：y 为位置（条形居中于整数），width 为长度（次数），x 起点为 0
                y_positions = [i - 0.3 for i in range(n)]
                bar = pg.BarGraphItem(
                    x=[0] * n,
                    y=y_positions,
                    width=counts,
                    height=0.6,
                    brush=_CHART_BRUSH,
                )
                self._status_plot.addItem(bar)
                # Y 轴显示状态码文本（与条形中心对齐）
                ticks = [[(i, str(codes[i])) for i in range(n)]]
                try:
                    self._status_plot.getAxis("left").setTicks(ticks)
                except Exception:
                    # 某些 pyqtgraph 版本 setTicks 行为略有差异，失败时忽略
                    pass
                # X 轴从 0 起，避免条形被裁切
                max_count = max(counts) if counts else 1
                if max_count < 1:
                    max_count = 1
                self._status_plot.setXRange(0, max_count, padding=0.1)
            else:
                # 无数据：清空 Y 轴刻度
                try:
                    self._status_plot.getAxis("left").setTicks([[]])
                except Exception:
                    pass
        elif self._status_table is not None:
            # 回退表格：两列「状态码 | 次数」
            self._status_table.setRowCount(0)
            for code, count in items:
                r = self._status_table.rowCount()
                self._status_table.insertRow(r)
                self._status_table.setItem(r, 0, QTableWidgetItem(str(code)))
                self._status_table.setItem(r, 1, QTableWidgetItem(str(count)))

    def _on_tick(self) -> None:
        """1 秒定时器槽：刷新速率折线图与时间标签。"""
        self._refresh_rate_chart()
        self._refresh_time_labels()

    def _refresh_rate_chart(self) -> None:
        """根据滚动缓冲计算每秒速率并刷新折线图。

        - 仅保留最近 60 秒的样本。
        - 相邻样本之间计算瞬时速率，绘制为折线。
        - 整体速率（首末样本斜率）用于 ETA 计算。
        """
        now = time.time()
        # 仅保留最近 60 秒内的样本
        cutoff = now - _RATE_WINDOW_SECONDS
        self._rate_buffer = [
            (t, p) for (t, p) in self._rate_buffer if t >= cutoff
        ]
        buf = self._rate_buffer

        # 无 pyqtgraph：仅计算速率供 ETA 使用，不绘图
        if not (_HAS_PYQTGRAPH and self._rate_plot is not None):
            self._current_rate = self._compute_overall_rate(buf)
            return

        self._rate_plot.clear()
        if len(buf) < 2:
            # 样本不足：无法计算速率
            self._current_rate = 0.0
            return

        xs: list[float] = []
        ys: list[float] = []
        for i in range(1, len(buf)):
            t1, p1 = buf[i - 1]
            t2, p2 = buf[i]
            dt = t2 - t1
            if dt <= 0:
                continue
            rate = (p2 - p1) / dt
            if rate < 0:
                rate = 0.0
            mid_t = (t1 + t2) / 2
            # 转换为「秒前」坐标：当前时刻为 0，过去为负
            xs.append(mid_t - now)
            ys.append(rate)

        if xs:
            pen = pg.mkPen(color=_CHART_BRUSH, width=2)
            self._rate_plot.plot(xs, ys, pen=pen, clear=False)

        # 整体速率（用于 ETA）：首末样本斜率
        self._current_rate = self._compute_overall_rate(buf)

    @staticmethod
    def _compute_overall_rate(buf: list[tuple[float, int]]) -> float:
        """根据缓冲首末样本计算整体速率（页面/秒）。"""
        if len(buf) < 2:
            return 0.0
        t_first, p_first = buf[0]
        t_last, p_last = buf[-1]
        total_dt = t_last - t_first
        if total_dt <= 0:
            return 0.0
        rate = (p_last - p_first) / total_dt
        return rate if rate > 0 else 0.0

    def _refresh_time_labels(self) -> None:
        """刷新已用时间与预计剩余时间标签。"""
        # 已用时间：从 started_at 起算
        if self._started_at > 0:
            elapsed = time.time() - self._started_at
            self._elapsed_label.setText(f"已用时间: {_format_hms(elapsed)}")
        else:
            self._elapsed_label.setText("已用时间: 00:00:00")

        # 预计剩余：根据当前速率与队列剩余估算
        if self._current_rate > 0 and self._queue_size > 0:
            eta_seconds = self._queue_size / self._current_rate
            self._eta_label.setText(f"预计剩余: ~{_format_hms(eta_seconds)}")
        else:
            self._eta_label.setText("预计剩余: 计算中…")
