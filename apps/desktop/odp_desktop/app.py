# -*- coding:utf-8 -*-
"""ODPlatform 桌面端主窗口 (PySide6).

布局:
  左栏  数据集选择 / 格式转换 / 质量检查 / 配置生成 / 任务执行
  右栏  日志页(实时) + 结果页(结构化 HTML) + 图表页(matplotlib)

设计纪律 (跟 CLI 层一致):
  - 业务全在 odp_platform 服务层, 这里只做"取参数 → 后台跑 → 展示"
  - 长任务走 QThreadPool, UI 不冻结; 运行时有进度条 + 状态提示
"""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QStackedWidget, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from . import tasks
from .charts import ChartWidget
from .log_bridge import install_log_bridge
from .workers import Worker


class DropZone(QLabel):
    """拖放区: 接受数据集文件夹或 zip, 发出 dropped(本地路径) 信号."""

    dropped = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setText("拖数据集文件夹 / zip 到这里\n自动导入到 data/raw/")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(56)
        self.setStyleSheet(
            "QLabel { border: 2px dashed #999; border-radius: 6px; color: #666; }"
        )

    def dragEnterEvent(self, event) -> None:
        if self._first_valid_path(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        path = self._first_valid_path(event.mimeData())
        if path:
            self.dropped.emit(path)
            event.acceptProposedAction()

    @staticmethod
    def _first_valid_path(mime) -> str | None:
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            p = url.toLocalFile()
            if p and (Path(p).is_dir() or p.lower().endswith(".zip")):
                return p
        return None


class MainWindow(QMainWindow):
    """ODPlatform 桌面端."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ODPlatform — 目标检测开发平台")
        self.resize(1080, 720)

        # 日志桥 (单例) + 线程池
        self._signals = install_log_bridge()
        self._signals.message.connect(self._append_log)
        self._pool = QThreadPool.globalInstance()
        self._busy = False

        self._build_ui()
        self._build_status_bar()
        self._refresh_datasets()

    # ====================================================================
    # UI 构建
    # ====================================================================

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # ---- 左栏: 控制面板 ----
        left = QWidget()
        left.setFixedWidth(340)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)

        left_layout.addWidget(self._build_dataset_group())
        left_layout.addWidget(self._build_transform_group())
        left_layout.addWidget(self._build_validate_group())
        left_layout.addWidget(self._build_genconfig_group())
        left_layout.addWidget(self._build_task_group())
        left_layout.addStretch(1)

        # ---- 右栏: 日志 + 结果 + 图表 ----
        self.tabs = QTabWidget()

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(10000)
        self.log_view.setFont(QFont("Menlo", 11))
        self.tabs.addTab(self.log_view, "日志")

        self.result_view = QTextBrowser()
        self.result_view.setOpenExternalLinks(True)
        self.tabs.addTab(self.result_view, "结果")

        self.chart_view = ChartWidget()
        self.tabs.addTab(self.chart_view, "图表")

        root.addWidget(left)
        root.addWidget(self.tabs, 1)

    def _build_status_bar(self) -> None:
        self._status_label = QLabel("就绪")
        self.statusBar().addWidget(self._status_label, 1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)          # 不确定进度 (滚动条)
        self._progress.setFixedWidth(160)
        self._progress.setVisible(False)
        self.statusBar().addPermanentWidget(self._progress)

    def _build_dataset_group(self) -> QGroupBox:
        group = QGroupBox("数据集")
        layout = QVBoxLayout(group)

        self.drop_zone = DropZone()
        self.drop_zone.dropped.connect(self._on_dataset_dropped)
        layout.addWidget(self.drop_zone)

        row = QHBoxLayout()
        self.dataset_combo = QComboBox()
        row.addWidget(self.dataset_combo, 1)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh_datasets)
        row.addWidget(refresh_btn)
        layout.addLayout(row)

        self.dataset_combo.currentTextChanged.connect(self._on_dataset_changed)
        return group

    def _build_transform_group(self) -> QGroupBox:
        group = QGroupBox("格式转换 (odp-transform)")
        layout = QVBoxLayout(group)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["pascal_voc", "coco", "yolo"])
        layout.addWidget(self._labeled("格式", self.format_combo))

        grid = QGridLayout()
        self.train_rate = QDoubleSpinBox()
        self.train_rate.setRange(0.0, 1.0)
        self.train_rate.setSingleStep(0.05)
        self.train_rate.setValue(0.8)
        self.val_rate = QDoubleSpinBox()
        self.val_rate.setRange(0.0, 1.0)
        self.val_rate.setSingleStep(0.05)
        self.val_rate.setValue(0.1)
        grid.addWidget(QLabel("train"), 0, 0)
        grid.addWidget(self.train_rate, 0, 1)
        grid.addWidget(QLabel("val"), 1, 0)
        grid.addWidget(self.val_rate, 1, 1)
        layout.addLayout(grid)

        self.transform_btn = QPushButton("开始转换")
        self.transform_btn.clicked.connect(self._run_transform)
        layout.addWidget(self.transform_btn)
        return group

    def _build_validate_group(self) -> QGroupBox:
        group = QGroupBox("质量检查 (odp-validate)")
        layout = QVBoxLayout(group)
        self.validate_btn = QPushButton("开始检查")
        self.validate_btn.clicked.connect(self._run_validate)
        layout.addWidget(self.validate_btn)
        return group

    def _build_genconfig_group(self) -> QGroupBox:
        group = QGroupBox("配置生成 (odp-gen-config)")
        layout = QVBoxLayout(group)
        self.genconfig_combo = QComboBox()
        self.genconfig_combo.addItems(["train", "val", "infer"])
        layout.addWidget(self._labeled("配置", self.genconfig_combo))
        self.genconfig_btn = QPushButton("生成配置")
        self.genconfig_btn.clicked.connect(self._run_genconfig)
        layout.addWidget(self.genconfig_btn)
        return group

    def _build_task_group(self) -> QGroupBox:
        group = QGroupBox("任务执行 (train / val / infer)")
        layout = QVBoxLayout(group)

        self.task_combo = QComboBox()
        self.task_combo.addItems(["train", "val", "infer"])
        self.task_combo.currentIndexChanged.connect(self._on_task_changed)
        layout.addWidget(self._labeled("任务", self.task_combo))

        self.model_edit = QLineEdit("yolo11n.pt")
        layout.addWidget(self._labeled("模型", self.model_edit))

        self.data_edit = QLineEdit()
        layout.addWidget(self._labeled("数据(yaml)", self.data_edit))

        # 动态第三字段: train→epochs, val→split, infer→source
        self.task_stack = QStackedWidget()
        self.epochs_spin = QSpinBox()
        self.epochs_spin.setRange(1, 10000)
        self.epochs_spin.setValue(100)
        self.split_combo = QComboBox()
        self.split_combo.addItems(["val", "test", "train"])
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("图片/视频路径 或 摄像头号(0)")
        self.task_stack.addWidget(self.epochs_spin)
        self.task_stack.addWidget(self.split_combo)
        self.task_stack.addWidget(self.source_edit)
        layout.addWidget(self.task_stack)

        self.task_btn = QPushButton("执行任务")
        self.task_btn.clicked.connect(self._run_task)
        layout.addWidget(self.task_btn)
        return group

    @staticmethod
    def _labeled(text: str, widget: QWidget) -> QWidget:
        """一个 label + widget 的竖排小容器."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(text))
        lay.addWidget(widget)
        return box

    # ====================================================================
    # 数据集
    # ====================================================================

    def _refresh_datasets(self) -> None:
        from odp_platform.common.paths import RAW_DATA_DIR
        raw = Path(RAW_DATA_DIR)
        current = self.dataset_combo.currentText()
        self.dataset_combo.clear()
        if raw.is_dir():
            names = sorted(p.name for p in raw.iterdir() if p.is_dir())
            self.dataset_combo.addItems(names)
        if current:
            idx = self.dataset_combo.findText(current)
            if idx >= 0:
                self.dataset_combo.setCurrentIndex(idx)

    def _on_dataset_changed(self, name: str) -> None:
        if name:
            self.data_edit.setText(f"{name}.yaml")

    def _on_dataset_dropped(self, path: str) -> None:
        self._start_task("导入数据集", tasks.import_dataset, path)

    def _on_task_changed(self, index: int) -> None:
        self.task_stack.setCurrentIndex(index)

    # ====================================================================
    # 后台任务调度
    # ====================================================================

    def _start_task(self, task_name: str, fn, *args) -> None:
        if self._busy:
            return
        self._set_busy(True, task_name)
        self.tabs.setCurrentWidget(self.log_view)
        worker = Worker(fn, *args)
        worker.signals.finished.connect(self._on_task_done)
        worker.signals.error.connect(self._on_task_error)
        self._pool.start(worker)

    def _set_busy(self, busy: bool, task_name: str = "") -> None:
        self._busy = busy
        for btn in (
            self.transform_btn, self.validate_btn,
            self.genconfig_btn, self.task_btn,
        ):
            btn.setEnabled(not busy)
        if busy:
            self._status_label.setText(f"运行中: {task_name} ...")
            self._progress.setVisible(True)
        else:
            self._progress.setVisible(False)

    def _on_task_done(self, result) -> None:
        self._set_busy(False)
        self._status_label.setText("完成")
        self._render_result(result)
        self.chart_view.set_result(result)
        if result.get("kind") == "import":
            self._refresh_datasets()
            idx = self.dataset_combo.findText(result.get("name"))
            if idx >= 0:
                self.dataset_combo.setCurrentIndex(idx)
        self.tabs.setCurrentWidget(self.result_view)

    def _on_task_error(self, message: str) -> None:
        self._set_busy(False)
        self._status_label.setText("出错")
        self._render_result({"kind": "error", "message": message})
        self.tabs.setCurrentWidget(self.result_view)

    # ====================================================================
    # 各按钮 → 任务
    # ====================================================================

    def _run_transform(self) -> None:
        name = self.dataset_combo.currentText()
        if not name:
            self._render_result({"kind": "error", "message": "请先选择数据集(或把数据集放进 data/raw/ 后刷新)"})
            return
        self._start_task(
            "格式转换", tasks.transform_dataset, name,
            self.format_combo.currentText(),
            self.train_rate.value(), self.val_rate.value(),
        )

    def _run_validate(self) -> None:
        name = self.dataset_combo.currentText()
        if not name:
            self._render_result({"kind": "error", "message": "请先选择数据集"})
            return
        self._start_task("质量检查", tasks.validate_dataset_checked, name)

    def _run_genconfig(self) -> None:
        self._start_task("配置生成", tasks.generate_config, self.genconfig_combo.currentText())

    def _run_task(self) -> None:
        model = self.model_edit.text().strip()
        data = self.data_edit.text().strip()
        kind = self.task_combo.currentText()
        if kind == "train":
            self._start_task("训练", tasks.run_train, model, data, self.epochs_spin.value())
        elif kind == "val":
            self._start_task("评估", tasks.run_val, model, data, self.split_combo.currentText())
        else:
            source = self.source_edit.text().strip()
            self._start_task("推理", tasks.run_infer, model, source)

    # ====================================================================
    # 日志 / 结果渲染
    # ====================================================================

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _render_result(self, result: dict) -> None:
        self.result_view.setHtml(_result_to_html(result))


# ====================================================================
# 结果 → HTML
# ====================================================================

def _esc(value) -> str:
    return html.escape(str(value))


def _kv(rows) -> str:
    return "".join(
        f"<tr><td style='padding:3px 12px 3px 0;color:#666'>{_esc(k)}</td>"
        f"<td>{_esc(v)}</td></tr>"
        for k, v in rows
    )


def _result_to_html(result: dict) -> str:
    kind = result.get("kind")

    if kind == "error":
        return f"<h3 style='color:#c0392b'>出错</h3><p>{_esc(result.get('message'))}</p>"

    if kind == "import":
        if result.get("already_here"):
            msg = "已在 data/raw/ 下, 无需重复导入"
        else:
            msg = f"图片 {result.get('images')} 张, 标注 {result.get('annotations')} 个"
        return (
            "<h3>数据集导入完成</h3>"
            f"<table>{_kv([('数据集', result.get('name')), ('结果', msg),
                          ('路径', result.get('path'))])}</table>"
            "<p>现在可以在左边选中它, 点「开始转换」。</p>"
        )

    if kind == "transform":
        counts = result.get("counts", {})
        return (
            "<h3>格式转换完成</h3>"
            f"<table>{_kv([('train', counts.get('train')), ('val', counts.get('val')),
                          ('test', counts.get('test')), ('yaml', result.get('yaml'))])}</table>"
        )

    if kind == "validate":
        sev = result.get("overall_severity", "?")
        color = {"PASS": "#27ae60", "INFO": "#27ae60",
                 "WARNING": "#e67e22", "ERROR": "#c0392b"}.get(sev, "#333")
        ds = result.get("dataset_summary", {})
        rows = _kv([
            ("总体", f"<b style='color:{color}'>{_esc(sev)}</b>"),
            ("类别数", ds.get("nc")),
            ("类别", ", ".join(ds.get("classes", []))),
            ("图像总数", ds.get("total_images")),
            ("报告", result.get("report_path") or "(未写盘)"),
        ])
        split_rows = "".join(
            f"<tr><td>{_esc(s)}</td><td>{_esc(st['image_count'])}</td>"
            f"<td>{_esc(st['annotated_count'])}</td><td>{_esc(st['total_instances'])}</td></tr>"
            for s, st in result.get("stats_per_split", {}).items()
        )
        check_rows = "".join(
            f"<tr><td>{_esc(r['severity'])}</td><td>{_esc(r['name'])}</td>"
            f"<td>{_esc(r['summary'])}</td></tr>"
            for r in result.get("results", [])
        )
        return (
            "<h3>质量检查结果</h3>"
            f"<table>{rows}</table>"
            "<h4>各 split 数据量</h4>"
            "<table border='0' cellspacing='0' cellpadding='4'>"
            "<tr><th style='text-align:left'>split</th><th style='text-align:left'>图像</th>"
            "<th style='text-align:left'>有标注</th><th style='text-align:left'>实例</th></tr>"
            f"{split_rows}</table>"
            "<h4>检查项</h4>"
            "<table border='0' cellspacing='0' cellpadding='4'>"
            "<tr><th style='text-align:left'>级别</th><th style='text-align:left'>检查项</th>"
            "<th style='text-align:left'>结论</th></tr>"
            f"{check_rows}</table>"
        )

    if kind == "gen_config":
        ok = "已生成" if result.get("generated") else "已存在(未覆盖)"
        return (
            "<h3>配置生成</h3>"
            f"<table>{_kv([('状态', ok), ('路径', result.get('path'))])}</table>"
            "<p>编辑该 yaml 后即可在任务执行里使用(或走 CLI)。</p>"
        )

    if kind in ("train", "val", "infer"):
        if result.get("success"):
            rows = [("状态", "<b style='color:#27ae60'>成功</b>"),
                    ("输出目录", result.get("output_dir"))]
            metrics = result.get("metrics") or {}
            for k, v in metrics.items():
                rows.append((k, v))
            if kind == "train":
                rows.append(("耗时(秒)", result.get("train_time")))
            elif kind == "val":
                rows.append(("耗时(秒)", result.get("val_time")))
            else:
                rows.append(("耗时(秒)", result.get("infer_time")))
                rows.append(("已保存", result.get("saved")))
            return f"<h3>{kind} 完成</h3><table>{_kv(rows)}</table>"
        else:
            return (
                f"<h3>{kind} 失败</h3>"
                f"<p style='color:#c0392b'>{_esc(result.get('error'))}</p>"
            )

    return f"<pre>{_esc(result)}</pre>"


def main() -> int:
    """桌面端入口."""
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("ODPlatform")
    win = MainWindow()
    win.show()
    return app.exec()
