"""数据管道与存储模块。

实现 spec 中「数据管道与存储」需求，提供多种持久化后端：
CSV、JSON、Excel(.xlsx)、SQLite，以及资源文件下载管道。
另提供 MultiPipeline 扇出 与 PipelineFactory 工厂。

本模块为 UI 无关（不依赖 PySide6）。requests / openpyxl 均采用延迟导入，
保证模块在缺少这些可选依赖时仍可被导入（仅在使用对应管道时才报错）。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import urlparse

from crawler.models import CrawlConfig, ResultItem


def _fmt_log(level_name: str, module: str, source_url: str, current_url: str, depth: int, msg: str) -> str:
    """统一日志格式：[时间戳] [级别] [模块] (来源URL→当前URL, 深度N) 消息。"""
    from datetime import datetime, timezone, timedelta
    ts = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    src = source_url or "-"
    return f"[{ts}] [{level_name}] [{module}] ({src}→{current_url}, 深度{depth}) {msg}"


__all__ = [
    "Pipeline",
    "CSVPipeline",
    "JSONPipeline",
    "ExcelPipeline",
    "SQLitePipeline",
    "FileDownloadPipeline",
    "MultiPipeline",
    "PipelineFactory",
]


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------
class Pipeline(ABC):
    """所有数据管道的抽象基类。"""

    @abstractmethod
    def process(self, item: ResultItem) -> None:
        """处理一条抓取结果。"""

    @abstractmethod
    def close(self) -> None:
        """释放资源、刷新缓冲。"""


class _NoopPipeline(Pipeline):
    """空操作管道：仅记录日志，用于 storage_format == "NONE" 场景。"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("crawler.pipeline")

    def process(self, item: ResultItem) -> None:
        fields = ", ".join(item.fields.keys()) or "(无字段)"
        self.logger.debug(
            "%s",
            _fmt_log("DEBUG", "pipeline", "-", item.url, 0, f"忽略一项，字段: {fields}"),
        )

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 路径解析辅助
# ---------------------------------------------------------------------------
def _resolve_output_path(config: CrawlConfig, default_name: str, ext: str) -> str:
    """根据 config.output_path 解析实际输出文件路径。

    规则：
      - 若 output_path 为已存在的目录，则在该目录下使用 default_name；
      - 若 output_path 已以指定扩展名结尾，直接使用；
      - 否则视为文件名并补上扩展名。
    """
    path = config.output_path or default_name
    if os.path.isdir(path):
        path = os.path.join(path, default_name)
    elif path.lower().endswith(ext.lower()):
        pass
    else:
        path = path + ext
    return path


def _ensure_parent_dir(path: str) -> None:
    """确保目标文件所在目录存在。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


# ---------------------------------------------------------------------------
# CSVPipeline (6.1)
# ---------------------------------------------------------------------------
class CSVPipeline(Pipeline):
    """CSV 存储管道。UTF-8 with BOM，Excel 友好；表头由首条结果的字段顺序决定。"""

    def __init__(self, config: CrawlConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger("crawler.pipeline")
        self.path = _resolve_output_path(config, "output.csv", ".csv")
        self._lock = threading.Lock()
        self._header: Optional[list[str]] = None
        self._fp = None
        self._writer = None

    def _ensure_open(self) -> None:
        """延迟打开文件，确保目录存在；避免对已有文件重复写入 BOM。"""
        if self._fp is None:
            _ensure_parent_dir(self.path)
            # 若文件已非空，则用普通 utf-8 追加以避免重复 BOM；否则用 utf-8-sig 写入 BOM。
            need_bom = not (os.path.exists(self.path) and os.path.getsize(self.path) > 0)
            encoding = "utf-8-sig" if need_bom else "utf-8"
            self._fp = open(self.path, "a", encoding=encoding, newline="")
            self._writer = csv.writer(self._fp)

    def process(self, item: ResultItem) -> None:
        with self._lock:
            self._ensure_open()
            # 首条结果决定表头顺序
            if self._header is None:
                self._header = list(item.fields.keys())
                self._writer.writerow(self._header)
            # 按表头顺序写值；缺失键写空串；多余键忽略
            row = [item.fields.get(k, "") for k in self._header]
            self._writer.writerow(row)
            self._fp.flush()

    def close(self) -> None:
        with self._lock:
            if self._fp is not None:
                self._fp.close()
                self._fp = None
                self._writer = None


# ---------------------------------------------------------------------------
# JSONPipeline (6.2)
# ---------------------------------------------------------------------------
class JSONPipeline(Pipeline):
    """JSON 存储管道。内存缓冲，每 100 条刷新一次到磁盘（整文件重写、pretty-print）。"""

    _FLUSH_THRESHOLD = 100

    def __init__(self, config: CrawlConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger("crawler.pipeline")
        self.path = _resolve_output_path(config, "output.json", ".json")
        self._lock = threading.Lock()
        self._items: list[dict] = []

    def _flush(self) -> None:
        _ensure_parent_dir(self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._items, f, indent=2, ensure_ascii=False)

    def process(self, item: ResultItem) -> None:
        with self._lock:
            self._items.append({"url": item.url, "fields": dict(item.fields)})
            if len(self._items) >= self._FLUSH_THRESHOLD:
                self._flush()

    def close(self) -> None:
        with self._lock:
            self._flush()
            self._items = []


# ---------------------------------------------------------------------------
# ExcelPipeline (6.3)
# ---------------------------------------------------------------------------
class ExcelPipeline(Pipeline):
    """Excel(.xlsx) 存储管道，基于 openpyxl。openpyxl 非线程安全，故通过锁串行写入。"""

    def __init__(self, config: CrawlConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger("crawler.pipeline")
        self.path = _resolve_output_path(config, "output.xlsx", ".xlsx")
        self._lock = threading.Lock()
        self._wb = None
        self._ws = None
        self._header: Optional[list[str]] = None

    def process(self, item: ResultItem) -> None:
        # 延迟导入，避免模块级强依赖 openpyxl
        from openpyxl import Workbook

        with self._lock:
            if self._wb is None:
                _ensure_parent_dir(self.path)
                self._wb = Workbook()
                self._ws = self._wb.active
                self._ws.title = "Results"
                self._header = list(item.fields.keys())
                self._ws.append(self._header)
            row = [item.fields.get(k, "") for k in self._header]
            self._ws.append(row)

    def close(self) -> None:
        with self._lock:
            if self._wb is not None:
                _ensure_parent_dir(self.path)
                self._wb.save(self.path)
                self._wb = None
                self._ws = None


# ---------------------------------------------------------------------------
# SQLitePipeline (6.4)
# ---------------------------------------------------------------------------
class SQLitePipeline(Pipeline):
    """SQLite 存储管道。表 crawl_results，列随字段动态扩展（ALTER TABLE）。"""

    TABLE_NAME = "crawl_results"

    def __init__(self, config: CrawlConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger("crawler.pipeline")
        # 解析 db 路径
        path = config.output_path or "output.db"
        if os.path.isdir(path):
            path = os.path.join(path, "output.db")
        elif path.lower().endswith((".db", ".sqlite", ".sqlite3")):
            pass
        else:
            path = path + ".db"
        self.path = path
        self._lock = threading.Lock()
        _ensure_parent_dir(self.path)
        # 连接在 __init__ 中创建
        self._conn = sqlite3.connect(self.path)
        # WAL 模式提升并发写入性能
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._columns: list[str] = []  # 已创建的字段列（不含 id 与 url）
        self._table_created = False

    @staticmethod
    def _quote(name: str) -> str:
        """用双引号包裹列名，内部双引号转义为两个。"""
        return '"' + name.replace('"', '""') + '"'

    def _ensure_table(self, field_keys: list[str]) -> None:
        cols = ["id INTEGER PRIMARY KEY AUTOINCREMENT", "url TEXT"]
        for k in field_keys:
            cols.append(f"{self._quote(k)} TEXT")
        sql = f"CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} ({', '.join(cols)})"
        self._conn.execute(sql)
        self._conn.commit()
        self._columns = list(field_keys)
        self._table_created = True

    def _add_missing_columns(self, field_keys: list[str]) -> None:
        for k in field_keys:
            if k not in self._columns:
                self._conn.execute(
                    f"ALTER TABLE {self.TABLE_NAME} ADD COLUMN {self._quote(k)} TEXT"
                )
                self._columns.append(k)
        self._conn.commit()

    def process(self, item: ResultItem) -> None:
        with self._lock:
            keys = list(item.fields.keys())
            if not self._table_created:
                self._ensure_table(keys)
            else:
                self._add_missing_columns(keys)
            # 以已知所有列为准写入；缺失字段写空串
            cols = ["url"] + [self._quote(k) for k in self._columns]
            placeholders = ", ".join("?" for _ in cols)
            values = [item.url] + [item.fields.get(k, "") for k in self._columns]
            sql = (
                f"INSERT INTO {self.TABLE_NAME} ({', '.join(cols)}) "
                f"VALUES ({placeholders})"
            )
            self._conn.execute(sql, values)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# ---------------------------------------------------------------------------
# FileDownloadPipeline (6.5)
# ---------------------------------------------------------------------------
class FileDownloadPipeline(Pipeline):
    """资源文件下载管道。扫描结果中的 URL，匹配正则后下载到指定目录。"""

    _DEFAULT_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

    def __init__(self, config: CrawlConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger("crawler.pipeline")
        self._lock = threading.Lock()
        self._downloaded_paths: set[str] = set()
        # Task 15.3：合规评估器（由引擎注入）；非 None 时对下载的文本内容做脱敏
        self.compliance_assessor = None
        # 编译下载正则
        if config.download_url_regex:
            try:
                self._regex = re.compile(config.download_url_regex)
            except re.error as e:
                self.logger.error("下载正则编译失败 %r，回退为默认 http(s) 匹配: %s",
                                  config.download_url_regex, e)
                self._regex = self._DEFAULT_URL_RE
        else:
            self._regex = self._DEFAULT_URL_RE
        # 后缀黑白名单归一化（去除前导点、转小写），存为 set 便于查找
        self._ext_whitelist: set[str] = {
            self._normalize_ext(e) for e in config.download_ext_whitelist if e
        }
        self._ext_blacklist: set[str] = {
            self._normalize_ext(e) for e in config.download_ext_blacklist if e
        }
        # 创建下载目录
        if self.config.download_dir:
            os.makedirs(self.config.download_dir, exist_ok=True)

    @staticmethod
    def _normalize_ext(ext: str) -> str:
        """后缀归一化：去除前导点、转小写。"""
        return ext.lstrip(".").lower()

    def _extract_urls(self, item: ResultItem) -> list[str]:
        """从 item.url 与 item.fields 的值中提取匹配的 URL。"""
        candidates: list[str] = []
        if item.url:
            candidates.append(item.url)
        for v in item.fields.values():
            if isinstance(v, str) and v:
                candidates.append(v)
        urls: list[str] = []
        seen: set[str] = set()
        for text in candidates:
            for m in self._regex.finditer(text):
                u = m.group(0)
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
        return urls

    def _should_download(self, url: str) -> bool:
        """根据后缀/字符串黑白名单判定 URL 是否应被下载。

        兼容旧逻辑：
          - 四组列表均为空且 download_url_regex 非空：仍由 self._regex
            在 _extract_urls 阶段负责过滤，此处直接返回 True。
          - 四组列表均为空且 download_url_regex 也为空：默认下载所有
            _extract_urls 提取到的 URL，返回 True。
        """
        cfg = self.config
        lists_empty = (
            not self._ext_whitelist
            and not self._ext_blacklist
            and not cfg.download_str_whitelist
            and not cfg.download_str_blacklist
        )
        if lists_empty:
            # 旧行为：完全交给 self._regex 提取阶段负责
            return True

        # 1. 后缀过滤（白名单与黑名单为相互独立维度）
        path = urlparse(url).path
        basename = os.path.basename(path)
        _, ext = os.path.splitext(basename)
        ext = self._normalize_ext(ext)
        if self._ext_whitelist and ext not in self._ext_whitelist:
            return False
        if ext in self._ext_blacklist:
            return False

        # 2. 字符串过滤（URL 全文）
        if cfg.download_str_whitelist:
            if not any(s and s in url for s in cfg.download_str_whitelist):
                return False
        if cfg.download_str_blacklist:
            if any(s and s in url for s in cfg.download_str_blacklist):
                return False

        return True

    def _skip_reason(self, url: str) -> str:
        """返回 URL 被过滤的具体原因（仅当 _should_download 返回 False 时调用）。"""
        cfg = self.config
        path = urlparse(url).path
        basename = os.path.basename(path)
        _, ext = os.path.splitext(basename)
        ext = self._normalize_ext(ext)
        if self._ext_whitelist and ext not in self._ext_whitelist:
            return f"未命中后缀白名单 {ext or '(无后缀)'}"
        if ext in self._ext_blacklist:
            return f"命中后缀黑名单 {ext}"
        if cfg.download_str_whitelist:
            if not any(s and s in url for s in cfg.download_str_whitelist):
                return "未命中字符串白名单"
        if cfg.download_str_blacklist:
            if any(s and s in url for s in cfg.download_str_blacklist):
                return "命中字符串黑名单"
        return "未知原因"

    def _unique_target(self, url: str) -> str:
        """根据 URL 推导下载文件名，避免覆盖已存在文件。"""
        parsed = urlparse(url)
        basename = os.path.basename(parsed.path) if parsed.path else ""
        if not basename:
            # 路径为空则使用 URL 的 md5 作为文件名
            basename = hashlib.md5(url.encode("utf-8")).hexdigest()
        # 防御性：再次取 basename，避免目录穿越
        basename = os.path.basename(basename) or hashlib.md5(url.encode("utf-8")).hexdigest()
        target = os.path.join(self.config.download_dir, basename)
        if target not in self._downloaded_paths and not os.path.exists(target):
            self._downloaded_paths.add(target)
            return target
        root, ext = os.path.splitext(basename)
        i = 1
        while True:
            new_name = f"{root}_{i}{ext}"
            new_target = os.path.join(self.config.download_dir, new_name)
            if new_target not in self._downloaded_paths and not os.path.exists(new_target):
                self._downloaded_paths.add(new_target)
                return new_target
            i += 1

    def process(self, item: ResultItem) -> None:
        urls = self._extract_urls(item)
        if not urls:
            return
        # 延迟导入 requests，避免模块级强依赖
        import requests

        timeout = self.config.timeout or 30.0
        for url in urls:
            if not self._should_download(url):
                self.logger.debug(
                    "%s",
                    _fmt_log(
                        "DEBUG",
                        "pipeline",
                        "-",
                        url,
                        0,
                        f"跳过下载(被过滤): {self._skip_reason(url)}",
                    ),
                )
                continue
            try:
                # 推导目标路径（加锁，避免并发重名）
                with self._lock:
                    target = self._unique_target(url)
                resp = requests.get(url, stream=True, timeout=timeout)
                resp.raise_for_status()
                # 读取完整内容（若启用合规脱敏，需在写盘前对文本内容做脱敏）
                content = b"".join(
                    chunk for chunk in resp.iter_content(chunk_size=8192) if chunk
                )
                # Task 15.3：合规脱敏——仅对可 UTF-8 解码的文本内容生效，
                #            二进制内容（UnicodeDecodeError）跳过脱敏。
                if self.compliance_assessor is not None:
                    try:
                        text = content.decode("utf-8", errors="strict")
                        redacted = self.compliance_assessor.redact_content(text)
                        content = redacted.encode("utf-8")
                    except UnicodeDecodeError:
                        pass
                # 写盘（加锁，串行化磁盘写）
                with self._lock:
                    with open(target, "wb") as f:
                        f.write(content)
                self.logger.info(
                    "%s",
                    _fmt_log("INFO", "pipeline", "-", url, 0, f"下载文件成功 -> {target}"),
                )
            except Exception as e:
                self.logger.error(
                    "%s",
                    _fmt_log("ERROR", "pipeline", "-", url, 0, f"下载文件失败: {e}"),
                )

    def close(self) -> None:
        # 无持久资源需释放
        pass


# ---------------------------------------------------------------------------
# MultiPipeline (6.6)
# ---------------------------------------------------------------------------
class MultiPipeline(Pipeline):
    """扇出管道：将同一条结果分发给多个子管道。单个子管道异常不影响其他。"""

    def __init__(self, pipelines: list[Pipeline], logger: Optional[logging.Logger] = None):
        self.pipelines = list(pipelines)
        self.logger = logger or logging.getLogger("crawler.pipeline")

    def process(self, item: ResultItem) -> None:
        for p in self.pipelines:
            try:
                p.process(item)
            except Exception as e:
                self.logger.error("管道 %s.process 异常: %s",
                                  type(p).__name__, e, exc_info=True)

    def close(self) -> None:
        for p in self.pipelines:
            try:
                p.close()
            except Exception as e:
                self.logger.error("管道 %s.close 异常: %s",
                                  type(p).__name__, e, exc_info=True)


# ---------------------------------------------------------------------------
# PipelineFactory (6.6)
# ---------------------------------------------------------------------------
class PipelineFactory:
    """根据 CrawlConfig 构建合适的管道实例。"""

    @staticmethod
    def build(config: CrawlConfig, logger: Optional[logging.Logger] = None) -> Pipeline:
        log = logger or logging.getLogger("crawler.pipeline")
        fmt = (config.storage_format or "NONE").upper()

        storage: Optional[Pipeline]
        if fmt == "CSV":
            storage = CSVPipeline(config, logger=log)
        elif fmt == "JSON":
            storage = JSONPipeline(config, logger=log)
        elif fmt == "EXCEL":
            storage = ExcelPipeline(config, logger=log)
        elif fmt == "SQLITE":
            storage = SQLitePipeline(config, logger=log)
        elif fmt == "NONE":
            storage = None
        else:
            log.warning("未知 storage_format %r，回退为 NONE", config.storage_format)
            storage = None

        # 若启用文件下载，则组合 FileDownloadPipeline
        if config.download_files:
            dl = FileDownloadPipeline(config, logger=log)
            if storage is None:
                return dl
            return MultiPipeline([storage, dl], logger=log)

        if storage is None:
            return _NoopPipeline(logger=log)
        return storage
