# -*- coding:utf-8 -*-
"""ODPlatform 桌面端 — Dataset Converter & Validator (macOS 原生风格).

结构:
  标题栏(红黄绿灯 + 名称 + 停止按钮)
  分段导航(数据集 / 质量检查 / 格式转换 / 训练实验)
  左栏(280px 工具操作区) + 右栏(预览/结果区), 可拖拽
  底部实时日志
"""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtGui import QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from . import tasks
from .charts import ChartWidget, training_curve_figure
from .log_bridge import install_log_bridge
from .workers import SubprocessWorker, Worker

# ---- 颜色 ----
BLUE = "#007AFF"
GREEN = "#34C759"
ORANGE = "#FF9500"
RED = "#FF3B30"
GRAY = "#8E8E93"
BG = "#F5F5F7"

# ---- 五个工具 ----
TOOLS = ["数据集", "质量检查", "格式转换", "训练", "检测"]


# ====================================================================
# 样式
# ====================================================================
APP_STYLE = f"""
QMainWindow, QWidget {{ background: {BG}; color: #1D1D1F; font-family: -apple-system; font-size: 13px; }}
QFrame#header {{ background: #ffffff; border-bottom: 1px solid #E5E5EA; }}
QFrame#nav {{ background: {BG}; }}
QPushButton {{ background: transparent; border: none; border-radius: 8px; padding: 7px 14px; }}
QPushButton:hover {{ background: rgba(0,0,0,0.05); }}
QPushButton:disabled {{ color: #C7C7CC; }}
QPushButton[role="primary"] {{ background: {BLUE}; color: white; font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: #0069D9; }}
QPushButton[role="primary"]:disabled {{ background: #B3D7FF; }}
QPushButton[role="secondary"] {{ background: #ffffff; color: {BLUE}; border: 1px solid {BLUE}; }}
QPushButton[role="danger"] {{ color: {RED}; }}
QPushButton[role="nav"] {{ border-radius: 8px; padding: 5px 16px; color: #1D1D1F; background: transparent; }}
QPushButton[role="nav"]:checked {{ background: #ffffff; font-weight: 600; }}
QComboBox, QLineEdit, QSpinBox {{ background: #ffffff; border: 1px solid #D1D1D6; border-radius: 6px; padding: 5px 8px; }}
QGroupBox {{ background: #ffffff; border: 1px solid #E5E5EA; border-radius: 10px; margin-top: 8px; padding: 8px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: #6E6E73; }}
QScrollArea {{ border: none; background: transparent; }}
QTreeWidget, QListWidget, QTableWidget, QPlainTextEdit, QTextBrowser {{ background: #ffffff; border: 1px solid #E5E5EA; border-radius: 8px; }}
QListWidget::item {{ padding: 8px; border-radius: 8px; }}
QListWidget::item:selected {{ background: {BLUE}; color: white; }}
QFrame#card {{ background: #ffffff; border: 1px solid #E5E5EA; border-radius: 10px; }}
QFrame#term {{ background: #1E1E1E; border-radius: 8px; }}
QFrame#term QLabel {{ background: transparent; color: #4AF626; font-family: Menlo; font-size: 12px; }}
QStatusBar {{ background: #ffffff; border-top: 1px solid #E5E5EA; }}
QProgressBar {{ border: none; background: #E5E5EA; border-radius: 3px; height: 6px; }}
QProgressBar::chunk {{ background: {BLUE}; border-radius: 3px; }}
"""


def _pil_to_pixmap(img, max_w: int = 240, max_h: int = 170) -> QPixmap:
    img = img.copy()
    img.thumbnail((max_w, max_h))
    data = img.convert("RGB").tobytes("raw", "RGB")
    qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg)


def _card(title: str, value: str, sub: str = "", value_color: str = "#1D1D1F") -> QFrame:
    """统计卡片."""
    card = QFrame()
    card.setObjectName("card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 10, 14, 10)
    v = QLabel(value)
    v.setStyleSheet(f"font-size: 26px; font-weight: 700; color: {value_color};")
    t = QLabel(title)
    t.setStyleSheet("color: #8E8E93; font-size: 12px;")
    lay.addWidget(v)
    lay.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setStyleSheet("color: #8E8E93; font-size: 11px;")
        lay.addWidget(s)
    return card


def _row(label: str, value: QWidget) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    l = QLabel(label)
    l.setStyleSheet("color: #6E6E73; font-size: 12px;")
    lay.addWidget(l)
    lay.addWidget(value)
    return box


def _warmup_heavy_imports() -> None:
    """主线程预 import 重依赖.

    torch 首次 import 不能在 Qt 线程池的 worker 线程里做(会触发
    'can't register atexit after shutdown'), 所以启动时在主线程先 import 一次,
    之后 worker 里的 `import torch` 就只是命中缓存, 不再走初始化.
    """
    for mod in ("torch", "ultralytics"):
        try:
            __import__(mod)
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dataset Converter & Validator")
        self.resize(1180, 800)
        self.setMinimumSize(1000, 660)
        _warmup_heavy_imports()

        self._signals = install_log_bridge()
        self._pool = QThreadPool.globalInstance()
        self._busy = False
        self._cancel_fn = None
        self._current_dataset = ""
        self._detect_result = None

        self._build_ui()
        self._refresh_datasets()
        self._refresh_experiments()

    # ==================================================================
    # 整体骨架
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._build_nav())

        # 主区: 左(操作) + 右(内容)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.left_stack = QStackedWidget()
        self.right_stack = QStackedWidget()
        for i in range(len(TOOLS)):
            left, right = self._build_page(i)
            self.left_stack.addWidget(left)
            self.right_stack.addWidget(right)

        splitter.addWidget(self.left_stack)
        splitter.addWidget(self.right_stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 880])
        root.addWidget(splitter, 1)

        # 底部实时日志
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(8000)
        self.log_view.setFont(QFont("Menlo", 11))
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background:#1E1E1E; color:#4AF626; border:none; border-radius:0; }")
        self.log_view.setFixedHeight(150)
        root.addWidget(self.log_view)

        self._signals.message.connect(self.log_view.appendPlainText)

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(46)
        lay = QHBoxLayout(header)
        lay.setContentsMargins(16, 0, 14, 0)

        title = QLabel("Dataset Converter & Validator")
        title.setStyleSheet("font-weight: 600; font-size: 13px;")
        lay.addWidget(title)

        lay.addStretch(1)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setProperty("role", "danger")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self._cancel)
        lay.addWidget(self.stop_btn)

        return header

    def _build_nav(self) -> QFrame:
        nav = QFrame()
        nav.setObjectName("nav")
        lay = QHBoxLayout(nav)
        lay.setContentsMargins(12, 8, 12, 8)
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        for i, name in enumerate(TOOLS):
            btn = QPushButton(name)
            btn.setProperty("role", "nav")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, idx=i: self._switch_page(idx))
            self._nav_group.addButton(btn, i)
            lay.addWidget(btn)
        self._nav_group.button(0).setChecked(True)
        lay.addStretch(1)
        return nav

    def _switch_page(self, idx: int) -> None:
        self.left_stack.setCurrentIndex(idx)
        self.right_stack.setCurrentIndex(idx)
        # 切页时同步上下文
        name = self._current_dataset or self.dataset_combo.currentText()
        if idx == 1:
            self.val_dataset_label.setText(name or "(未选择)")
        elif idx == 3:
            self._refresh_experiments()

    # ==================================================================
    # 四个页面
    # ==================================================================
    def _build_page(self, idx: int):
        builders = [
            (self._build_dataset_left, self._build_dataset_right),
            (self._build_validate_left, self._build_validate_right),
            (self._build_convert_left, self._build_convert_right),
            (self._build_train_left, self._build_train_right),
            (self._build_detect_left, self._build_detect_right),
        ]
        return builders[idx][0](), builders[idx][1]()

    # ---------------- 数据集 ----------------
    def _build_dataset_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QLabel("数据集")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        lay.addWidget(title)

        pick_btn = QPushButton("选择数据集文件夹")
        pick_btn.setProperty("role", "primary")
        pick_btn.clicked.connect(self._pick_folder)
        lay.addWidget(pick_btn)

        self.dataset_combo = QComboBox()
        self.dataset_combo.currentTextChanged.connect(self._on_dataset_selected)
        lay.addWidget(_row("或从已导入的里选", self.dataset_combo))

        refresh = QPushButton("刷新列表")
        refresh.setProperty("role", "secondary")
        refresh.clicked.connect(self._refresh_datasets)
        lay.addWidget(refresh)

        # 数据集信息
        info = QGroupBox("当前数据集")
        info_lay = QVBoxLayout(info)
        self.info_name = QLabel("—")
        self.info_format = QLabel("—")
        self.info_counts = QLabel("—")
        self.info_classes = QLabel("—")
        for l in (self.info_name, self.info_format, self.info_counts, self.info_classes):
            l.setWordWrap(True)
            info_lay.addWidget(l)
        lay.addWidget(info)

        lay.addStretch(1)

        self.ds_check_btn = QPushButton("开始检查")
        self.ds_check_btn.setProperty("role", "primary")
        self.ds_check_btn.clicked.connect(lambda: self._goto_page(1))
        lay.addWidget(self.ds_check_btn)

        self.ds_convert_btn = QPushButton("转换格式")
        self.ds_convert_btn.setProperty("role", "secondary")
        self.ds_convert_btn.clicked.connect(lambda: self._goto_page(2))
        lay.addWidget(self.ds_convert_btn)
        return w

    def _build_dataset_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        self.ds_title = QLabel("未选择数据集")
        self.ds_title.setStyleSheet("font-size: 15px; font-weight: 600;")
        lay.addWidget(self.ds_title)

        cards = QHBoxLayout()
        self.ds_card_images = _card("图片数", "—")
        self.ds_card_ann = _card("标注数", "—")
        self.ds_card_cls = _card("类别数", "—")
        cards.addWidget(self.ds_card_images)
        cards.addWidget(self.ds_card_ann)
        cards.addWidget(self.ds_card_cls)
        lay.addLayout(cards)

        # 样本预览(带框)
        prev_label = QLabel("样本预览")
        prev_label.setStyleSheet("font-weight: 600;")
        lay.addWidget(prev_label)
        self.preview_grid = QGridLayout()
        self.preview_grid.setSpacing(6)
        lay.addLayout(self.preview_grid)

        # 目录结构
        tree_label = QLabel("目录结构")
        tree_label.setStyleSheet("font-weight: 600;")
        lay.addWidget(tree_label)
        self.ds_tree = QTreeWidget()
        self.ds_tree.setHeaderLabel("结构")
        lay.addWidget(self.ds_tree, 1)
        return w

    def _on_dataset_selected(self, name: str) -> None:
        if name:
            self._current_dataset = name
            self._refresh_dataset_info(name)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择数据集文件夹")
        if not folder:
            return
        self._run_inprocess("导入数据集", tasks.import_dataset, folder,
                            on_done=self._on_import_done)

    def _on_import_done(self, result) -> None:
        self._refresh_datasets()
        name = result.get("name")
        if name:
            idx = self.dataset_combo.findText(name)
            if idx >= 0:
                self.dataset_combo.setCurrentIndex(idx)

    def _refresh_datasets(self) -> None:
        from odp_platform.common.paths import RAW_DATA_DIR
        raw = Path(RAW_DATA_DIR)
        current = self.dataset_combo.currentText()
        self.dataset_combo.blockSignals(True)
        self.dataset_combo.clear()
        if raw.is_dir():
            self.dataset_combo.addItems(sorted(p.name for p in raw.iterdir() if p.is_dir()))
        if current and self.dataset_combo.findText(current) >= 0:
            self.dataset_combo.setCurrentText(current)
        self.dataset_combo.blockSignals(False)
        if self.dataset_combo.currentText():
            self._refresh_dataset_info(self.dataset_combo.currentText())

    def _refresh_dataset_info(self, name: str) -> None:
        info = tasks.detect_dataset_info(name)
        if not info.get("exists"):
            self.info_name.setText(name)
            self.info_format.setText("(目录不存在)")
            return
        fmt_disp = {"pascal_voc": "VOC", "coco": "COCO", "yolo": "YOLO"}.get(info["format"], "未知")
        self.info_name.setText(f"名称：{info['name']}")
        self.info_format.setText(f"格式：{fmt_disp}")
        self.info_counts.setText(f"图片 {info['images']} 张 / 标注 {info['annotations']} 个")
        self.info_classes.setText("类别：" + (", ".join(info["classes"]) if info["classes"] else "(YOLO 需手动填类别)"))
        self.ds_title.setText(f"数据集：{name}（{fmt_disp}）")
        self.ds_card_images.findChild(QLabel).setText(str(info["images"]))
        self.ds_card_ann.findChild(QLabel).setText(str(info["annotations"]))
        self.ds_card_cls.findChild(QLabel).setText(str(len(info["classes"])) if info["classes"] else "?")
        self._refresh_preview(name)
        self._refresh_tree(name)
        # 训练/评估页的数据字段跟着当前数据集走
        if hasattr(self, "train_data_edit"):
            self.train_data_edit.setText(f"{name}.yaml")

    def _refresh_preview(self, name: str) -> None:
        # 清空旧预览
        while self.preview_grid.count():
            item = self.preview_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        from odp_platform.common.paths import RAW_DATA_DIR
        ann_dir = Path(RAW_DATA_DIR) / name / "annotations"
        for i, img_path in enumerate(tasks.sample_image_paths(name, limit=4)):
            try:
                label_path = ann_dir / (Path(img_path).stem + ".txt")
                img = tasks.draw_boxes_pil(img_path, str(label_path))
                pm = _pil_to_pixmap(img)
            except Exception:
                continue
            lb = QLabel()
            lb.setPixmap(pm)
            lb.setToolTip(Path(img_path).name)
            lb.setStyleSheet("border:1px solid #E5E5EA; border-radius:6px;")
            self.preview_grid.addWidget(lb, 0, i)

    def _refresh_tree(self, name: str) -> None:
        from odp_platform.common.paths import RAW_DATA_DIR
        self.ds_tree.clear()
        root = Path(RAW_DATA_DIR) / name
        top = QTreeWidgetItem([name])
        self.ds_tree.addTopLevelItem(top)
        for sub in ("images", "annotations"):
            d = root / sub
            if not d.is_dir():
                continue
            files = sorted(p.name for p in d.iterdir() if p.is_file())
            sub_item = QTreeWidgetItem([f"{sub}（{len(files)}）"])
            top.addChild(sub_item)
            for f in files[:8]:
                sub_item.addChild(QTreeWidgetItem([f]))
        top.setExpanded(True)

    # ---------------- 质量检查 ----------------
    def _build_validate_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QLabel("质量检查")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        lay.addWidget(title)

        lay.addWidget(QLabel("检查数据集："))
        self.val_dataset_label = QLabel("(未选择)")
        self.val_dataset_label.setStyleSheet("font-weight: 600;")
        lay.addWidget(self.val_dataset_label)

        check_group = QGroupBox("检查项")
        cg = QVBoxLayout(check_group)
        self.check_labels = {}
        for key, disp in [
            ("pair_existence", "图片-标注完整性检查"),
            ("label_format", "标注格式 / 非法类别检查"),
            ("split_uniqueness", "数据泄露检查"),
            ("yaml_schema", "yaml 字段一致性检查"),
        ]:
            cb = QCheckBox(disp)
            cb.setChecked(True)
            cb.setEnabled(False)
            cg.addWidget(cb)
            self.check_labels[key] = cb
        lay.addWidget(check_group)

        lay.addStretch(1)

        self.val_run_btn = QPushButton("执行检查")
        self.val_run_btn.setProperty("role", "primary")
        self.val_run_btn.clicked.connect(self._run_validate)
        lay.addWidget(self.val_run_btn)
        return w

    def _build_validate_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        self.val_progress = QProgressBar()
        self.val_progress.setRange(0, 0)
        self.val_progress.setVisible(False)
        self.val_progress_label = QLabel("")
        self.val_progress_label.setStyleSheet("color:#8E8E93;")
        lay.addWidget(self.val_progress_label)
        lay.addWidget(self.val_progress)

        cards = QHBoxLayout()
        self.val_card_pass = _card("通过", "—", value_color=GREEN)
        self.val_card_warn = _card("警告", "—", value_color=ORANGE)
        self.val_card_err = _card("错误", "—", value_color=RED)
        cards.addWidget(self.val_card_pass)
        cards.addWidget(self.val_card_warn)
        cards.addWidget(self.val_card_err)
        lay.addLayout(cards)

        lay.addWidget(QLabel("问题详情"))
        self.issue_table = QTableWidget(0, 5)
        self.issue_table.setHorizontalHeaderLabels(["序号", "文件", "问题类型", "描述", "严重程度"])
        self.issue_table.horizontalHeader().setStretchLastSection(True)
        self.issue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self.issue_table, 1)

        self.export_btn = QPushButton("导出报告")
        self.export_btn.setProperty("role", "secondary")
        self.export_btn.clicked.connect(self._export_report)
        self.export_btn.setEnabled(False)
        lay.addWidget(self.export_btn)
        return w

    def _run_validate(self) -> None:
        name = self._current_dataset or self.dataset_combo.currentText()
        if not name:
            self._log_plain("请先选择数据集")
            return
        self.val_dataset_label.setText(name)
        self.val_progress.setVisible(True)
        self.val_progress_label.setText("正在检查...")
        self._run_inprocess("质量检查", tasks.validate_dataset_checked, name,
                            on_done=self._on_validate_done)

    def _on_validate_done(self, result) -> None:
        self.val_progress.setVisible(False)
        counts = {"PASS": 0, "INFO": 0, "WARNING": 0, "ERROR": 0}
        for r in result.get("results", []):
            counts[r["severity"]] = counts.get(r["severity"], 0) + 1
        passed = counts.get("PASS", 0) + counts.get("INFO", 0)
        self.val_card_pass.findChild(QLabel).setText(str(passed))
        self.val_card_warn.findChild(QLabel).setText(str(counts.get("WARNING", 0)))
        self.val_card_err.findChild(QLabel).setText(str(counts.get("ERROR", 0)))
        # 明确的总体结论
        if counts.get("ERROR", 0):
            self.val_progress_label.setText(f"✗ 检查完成：{counts['ERROR']} 个错误")
            self.val_progress_label.setStyleSheet("color:#FF3B30;")
        elif counts.get("WARNING", 0):
            self.val_progress_label.setText(f"⚠ 检查完成：{counts['WARNING']} 个警告")
            self.val_progress_label.setStyleSheet("color:#FF9500;")
        else:
            self.val_progress_label.setText(f"✓ 检查通过（{passed} 项）")
            self.val_progress_label.setStyleSheet("color:#34C759;")
        # 更新左侧检查项状态色
        for r in result.get("results", []):
            cb = self.check_labels.get(r["name"])
            if cb:
                color = {"PASS": GREEN, "INFO": GREEN, "WARNING": ORANGE, "ERROR": RED}.get(r["severity"], GRAY)
                cb.setStyleSheet(f"color: {color};")
        self._fill_issues(result)
        self.export_btn.setEnabled(True)
        self._last_report_path = result.get("report_path")

    def _fill_issues(self, result) -> None:
        self.issue_table.setRowCount(0)
        idx = 0
        for r in result.get("results", []):
            if r["severity"] in ("PASS", "INFO"):
                continue
            det = r.get("details", {}) if isinstance(r, dict) else {}
            fname = _first_file(det)
            self.issue_table.insertRow(idx)
            self.issue_table.setItem(idx, 0, QTableWidgetItem(str(idx + 1)))
            self.issue_table.setItem(idx, 1, QTableWidgetItem(fname))
            self.issue_table.setItem(idx, 2, QTableWidgetItem(r["name"]))
            self.issue_table.setItem(idx, 3, QTableWidgetItem(r["summary"]))
            sev = QTableWidgetItem(r["severity"])
            sev.setForeground(Qt.GlobalColor.red if r["severity"] == "ERROR" else Qt.GlobalColor.darkYellow)
            self.issue_table.setItem(idx, 4, sev)
            idx += 1

    def _export_report(self) -> None:
        p = getattr(self, "_last_report_path", None)
        if not p:
            return
        self._open_path(Path(p).parent)

    # ---------------- 格式转换 ----------------
    def _build_convert_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QLabel("格式转换")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        lay.addWidget(title)

        self.conv_src = QComboBox()
        self.conv_src.addItems(["pascal_voc", "coco", "yolo"])
        lay.addWidget(_row("源格式", self.conv_src))

        self.conv_dst = QComboBox()
        self.conv_dst.addItems(["YOLO"])
        self.conv_dst.setEnabled(False)
        lay.addWidget(_row("目标格式", self.conv_dst))

        self.conv_classes = QLineEdit()
        self.conv_classes.setPlaceholderText("YOLO 格式需填类别, 逗号分隔")
        lay.addWidget(_row("类别(仅 YOLO)", self.conv_classes))

        opts = QGroupBox("转换选项")
        og = QVBoxLayout(opts)
        self.opt_split = QCheckBox("生成训练/验证/测试划分（80/10/10）")
        self.opt_split.setChecked(True)
        og.addWidget(self.opt_split)
        lay.addWidget(opts)

        lay.addStretch(1)
        self.conv_run_btn = QPushButton("开始转换")
        self.conv_run_btn.setProperty("role", "primary")
        self.conv_run_btn.clicked.connect(self._run_convert)
        lay.addWidget(self.conv_run_btn)
        return w

    def _build_convert_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        self.conv_summary = QLabel("转换配置摘要\n—")
        self.conv_summary.setStyleSheet("background:#fff; border:1px solid #E5E5EA; border-radius:8px; padding:12px;")
        lay.addWidget(self.conv_summary)

        self.conv_result = QLabel("")
        self.conv_result.setWordWrap(True)
        lay.addWidget(self.conv_result)
        return w

    def _run_convert(self) -> None:
        name = self._current_dataset or self.dataset_combo.currentText()
        if not name:
            self._log_plain("请先选择数据集")
            return
        fmt = self.conv_src.currentText()
        info = tasks.detect_dataset_info(name)
        classes = None
        if fmt == "yolo":
            raw = self.conv_classes.text().strip()
            if raw:
                classes = [c.strip() for c in raw.split(",") if c.strip()]
            if not classes:
                self._log_plain("YOLO 格式需要填类别名")
                return
        self.conv_summary.setText(
            f"源格式 → 目标格式：{fmt} → YOLO\n"
            f"图片：{info.get('images', '?')} 张 / 标注：{info.get('annotations', '?')} 个\n"
            f"数据集：{name}"
        )
        self.conv_result.setText("转换中...")
        train_rate = 0.8 if self.opt_split.isChecked() else 1.0
        val_rate = 0.1 if self.opt_split.isChecked() else 0.0
        self._run_inprocess("格式转换", tasks.transform_dataset, name, fmt, train_rate, val_rate,
                            classes, on_done=self._on_convert_done)

    def _on_convert_done(self, result) -> None:
        if result.get("kind") == "transform":
            counts = result.get("counts", {})
            self.conv_result.setText(
                f"<span style='color:#34C759'>✓ 转换成功</span><br>"
                f"train {counts.get('train')} / val {counts.get('val')} / test {counts.get('test')}<br>"
                f"输出：{html.escape(str(result.get('yaml')))}"
            )

    # ---------------- 训练 / 评估 / 检测 执行 ----------------

    def _run_train(self) -> None:
        model = self.train_model_edit.text().strip() or "yolo11n.pt"
        data = self.train_data_edit.text().strip() or "det_demo.yaml"
        tasks.ensure_config("train")
        if not self._transform_now(data):
            return
        self._run_subprocess("训练", tasks.train_args(
            model, data, self.train_epochs.value(),
            self.train_imgsz.value(), self.train_device.currentText(),
        ))

    def _run_val(self) -> None:
        model = self.train_model_edit.text().strip() or "yolo11n.pt"
        data = self.train_data_edit.text().strip() or "det_demo.yaml"
        tasks.ensure_config("val")
        if not self._transform_now(data):
            return
        self._run_subprocess("评估", tasks.val_args(
            model, data, "val", self.train_imgsz.value(),
            self.train_device.currentText(),
        ))

    def _transform_now(self, data_field: str) -> bool:
        """训练/评估前同步重新转换目标数据集, 保证 data/ 里是它的划分.

        因为 data/train-val-test 是所有数据集共享的, 若之前转换过别的数据集,
        这里的数据就不匹配当前要训练的数据集了. 训练前重转一遍最保险.
        """
        from pathlib import Path

        import yaml as _yaml

        from odp_platform.common.paths import dataset_yaml_path

        name = Path(data_field).stem
        info = tasks.detect_dataset_info(name)
        if not info.get("exists"):
            self._log_plain(f"数据集 {name} 不存在")
            return False
        fmt = info.get("format")
        if fmt == "unknown":
            self._log_plain(f"数据集 {name} 格式未知, 请先在转换页转换")
            return False
        classes = None
        if fmt == "yolo":
            yp = dataset_yaml_path(name)
            if yp.exists():
                try:
                    doc = _yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
                    names = doc.get("names") or {}
                    classes = list(names.values()) if isinstance(names, dict) else (
                        names if isinstance(names, list) else None)
                except Exception:
                    classes = None
            if not classes:
                self._log_plain(f"YOLO 数据集 {name} 缺少类别信息, 请先在转换页填写类别转换一次")
                return False
        self._log_plain(f"训练前自动转换数据集 {name} ...")
        try:
            tasks.transform_dataset(name, fmt, 0.8, 0.1, classes=classes)
            return True
        except Exception as e:
            self._log_plain(f"训练前转换失败: {e}")
            return False

    def _gen_config(self) -> None:
        for kind in ("train", "val", "infer"):
            tasks.ensure_config(kind)
        self._log_plain("配置文件已就绪: configs/runtime/{train,val,infer}.yaml")

    def _run_detect(self) -> None:
        model = self.detect_model_edit.text().strip() or tasks.latest_checkpoint()
        source = self.detect_source_edit.text().strip()
        if not source:
            self._log_plain("请先选择检测源(图片/文件夹)")
            return
        self.detect_result_label.setText("检测中...")
        self._detect_result = None
        self._run_subprocess("检测", tasks.detect_args(model, source, self.detect_conf.value()),
                             on_line=self._on_detect_line)

    def _on_detect_line(self, line: str) -> None:
        if line.startswith("ODP_RESULT "):
            try:
                import json
                self._detect_result = json.loads(line[len("ODP_RESULT "):])
            except Exception:
                pass

    def _pick_detect_image(self) -> None:
        f, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.jpg *.jpeg *.png *.bmp)")
        if f:
            self.detect_source_edit.setText(f)

    def _pick_detect_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self.detect_source_edit.setText(d)

    # ---------------- 训练实验 ----------------
    def _build_train_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QLabel("训练")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        lay.addWidget(title)

        run_group = QGroupBox("启动训练 / 评估")
        rg = QVBoxLayout(run_group)
        self.train_model_edit = QLineEdit("yolo11n.pt")
        self.train_data_edit = QLineEdit("det_demo.yaml")
        rg.addWidget(_row("模型", self.train_model_edit))
        rg.addWidget(_row("数据(yaml)", self.train_data_edit))
        grid = QGridLayout()
        self.train_epochs = QSpinBox()
        self.train_epochs.setRange(1, 10000)
        self.train_epochs.setValue(50)
        self.train_imgsz = QSpinBox()
        self.train_imgsz.setRange(32, 2000)
        self.train_imgsz.setValue(320)
        self.train_device = QComboBox()
        self.train_device.addItems(["mps", "cpu"])
        grid.addWidget(QLabel("epochs"), 0, 0)
        grid.addWidget(self.train_epochs, 0, 1)
        grid.addWidget(QLabel("imgsz"), 0, 2)
        grid.addWidget(self.train_imgsz, 0, 3)
        rg.addLayout(grid)
        rg.addWidget(_row("设备", self.train_device))
        btns = QHBoxLayout()
        self.train_btn = QPushButton("开始训练")
        self.train_btn.setProperty("role", "primary")
        self.train_btn.clicked.connect(self._run_train)
        self.val_btn = QPushButton("开始评估")
        self.val_btn.setProperty("role", "secondary")
        self.val_btn.clicked.connect(self._run_val)
        btns.addWidget(self.train_btn)
        btns.addWidget(self.val_btn)
        rg.addLayout(btns)
        self.gencfg_btn = QPushButton("生成配置文件")
        self.gencfg_btn.setProperty("role", "secondary")
        self.gencfg_btn.clicked.connect(self._gen_config)
        rg.addWidget(self.gencfg_btn)
        lay.addWidget(run_group)

        lab = QLabel("实验列表")
        lab.setStyleSheet("font-weight: 600;")
        lay.addWidget(lab)
        self.exp_list = QListWidget()
        self.exp_list.currentRowChanged.connect(self._on_exp_selected)
        lay.addWidget(self.exp_list, 1)

        refresh = QPushButton("刷新实验列表")
        refresh.setProperty("role", "secondary")
        refresh.clicked.connect(self._refresh_experiments)
        lay.addWidget(refresh)

        self.del_exp_btn = QPushButton("删除实验")
        self.del_exp_btn.setProperty("role", "danger")
        self.del_exp_btn.clicked.connect(self._delete_experiment)
        lay.addWidget(self.del_exp_btn)

        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return scroll

    def _build_detect_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QLabel("检测")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        lay.addWidget(title)

        self.detect_model_edit = QLineEdit(tasks.latest_checkpoint())
        lay.addWidget(_row("模型", self.detect_model_edit))

        self.detect_source_edit = QLineEdit()
        self.detect_source_edit.setPlaceholderText("图片/视频/文件夹路径")
        lay.addWidget(_row("检测源", self.detect_source_edit))

        src_btns = QHBoxLayout()
        pick_img = QPushButton("选图片")
        pick_img.setProperty("role", "secondary")
        pick_img.clicked.connect(self._pick_detect_image)
        pick_dir = QPushButton("选文件夹")
        pick_dir.setProperty("role", "secondary")
        pick_dir.clicked.connect(self._pick_detect_dir)
        src_btns.addWidget(pick_img)
        src_btns.addWidget(pick_dir)
        lay.addLayout(src_btns)

        self.detect_conf = QDoubleSpinBox()
        self.detect_conf.setRange(0.0, 1.0)
        self.detect_conf.setSingleStep(0.05)
        self.detect_conf.setValue(0.25)
        lay.addWidget(_row("置信度阈值", self.detect_conf))

        lay.addStretch(1)
        self.detect_btn = QPushButton("开始检测")
        self.detect_btn.setProperty("role", "primary")
        self.detect_btn.clicked.connect(self._run_detect)
        lay.addWidget(self.detect_btn)
        return w

    def _build_detect_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        self.detect_result_label = QLabel("检测结果将显示在这里")
        self.detect_result_label.setWordWrap(True)
        self.detect_result_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.detect_result_label.setStyleSheet("font-size: 14px;")
        lay.addWidget(self.detect_result_label)
        lay.addStretch(1)
        return w

    def _build_train_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        self.exp_title = QLabel("选择左侧实验查看详情")
        self.exp_title.setStyleSheet("font-size: 15px; font-weight: 600;")
        lay.addWidget(self.exp_title)

        cards = QHBoxLayout()
        self.exp_card_map = _card("mAP50-95", "—", value_color=BLUE)
        self.exp_card_map50 = _card("mAP50", "—", value_color=BLUE)
        self.exp_card_prec = _card("Precision", "—")
        self.exp_card_recall = _card("Recall", "—")
        for c in (self.exp_card_map, self.exp_card_map50, self.exp_card_prec, self.exp_card_recall):
            cards.addWidget(c)
        lay.addLayout(cards)

        self.exp_curve = ChartWidget()
        lay.addWidget(self.exp_curve, 1)

        self.exp_confusion = QLabel("")
        self.exp_confusion.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.exp_confusion)

        btns = QHBoxLayout()
        self.export_model_btn = QPushButton("导出模型")
        self.export_model_btn.setProperty("role", "secondary")
        self.export_model_btn.clicked.connect(self._export_model)
        btns.addWidget(self.export_model_btn)
        self.view_log_btn = QPushButton("查看完整日志")
        self.view_log_btn.setProperty("role", "secondary")
        self.view_log_btn.clicked.connect(self._view_log)
        btns.addWidget(self.view_log_btn)
        btns.addStretch(1)
        lay.addLayout(btns)
        return w

    def _refresh_experiments(self) -> None:
        self.exp_list.clear()
        self._experiments = tasks.list_experiments()
        for e in self._experiments:
            m = e.get("mAP50")
            m_str = f"{m:.3f}" if m is not None else "—"
            item = QListWidgetItem(f"{e['name']}  ·  {e['model']}  ·  mAP50 {m_str}")
            item.setToolTip(f"{e['group']} · {e['time']}")
            self.exp_list.addItem(item)

    def _on_exp_selected(self, row: int) -> None:
        if row < 0 or not getattr(self, "_experiments", None):
            return
        e = self._experiments[row]
        self.exp_title.setText(f"{e['name']}（{e['group']} · {e['model']}）")
        for card, key in ((self.exp_card_map, "mAP50_95"), (self.exp_card_map50, "mAP50"),
                          (self.exp_card_prec, "precision"), (self.exp_card_recall, "recall")):
            v = e.get(key)
            card.findChild(QLabel).setText(f"{v:.3f}" if v is not None else "—")
        # 曲线
        self.exp_curve.set_figure(training_curve_figure(e.get("results_csv", "")))
        # 混淆矩阵
        cm = Path(e.get("confusion_png", ""))
        if cm.exists():
            self.exp_confusion.setPixmap(QPixmap(str(cm)).scaled(480, 360, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.exp_confusion.setText("(无混淆矩阵)")
        self._current_exp = e

    def _export_model(self) -> None:
        e = getattr(self, "_current_exp", None)
        if e:
            self._open_path(Path(e["output_dir"]))

    def _view_log(self) -> None:
        e = getattr(self, "_current_exp", None)
        if e and e.get("log_path"):
            self._open_path(Path(e["log_path"]))

    def _delete_experiment(self) -> None:
        row = self.exp_list.currentRow()
        if row < 0 or not getattr(self, "_experiments", None):
            return
        from PySide6.QtWidgets import QMessageBox
        e = self._experiments[row]
        ret = QMessageBox.question(
            self, "删除实验",
            f"确定删除实验「{e['name']}」的输出目录吗？\n{e['output_dir']}",
        )
        if ret == QMessageBox.StandardButton.Yes:
            import shutil
            shutil.rmtree(e["output_dir"], ignore_errors=True)
            self._refresh_experiments()

    # ==================================================================
    # 任务执行 / 取消
    # ==================================================================
    def _log_plain(self, msg: str) -> None:
        self._signals.message.emit(msg)

    def _set_busy(self, busy: bool, task_name: str = "", cancelable: bool = False) -> None:
        self._busy = busy
        self.stop_btn.setVisible(busy and cancelable)   # 只有可取消的任务才显示停止
        self.stop_btn.setText(f"停止 · {task_name}" if (busy and cancelable) else "停止")

    def _run_inprocess(self, task_name: str, fn, *args, on_done=None) -> None:
        if self._busy:
            return
        self._set_busy(True, task_name, cancelable=False)
        self._worker = Worker(fn, *args, log_signals=self._signals)  # 持引用防 GC
        self._cancel_fn = None  # 快任务不支持取消
        self._worker.signals.finished.connect(lambda r: self._on_inprocess_done(r, on_done))
        self._worker.signals.error.connect(lambda e: self._on_inprocess_error(e))
        self._pool.start(self._worker)

    def _on_inprocess_done(self, result, on_done) -> None:
        self._set_busy(False)
        if on_done:
            on_done(result)

    def _on_inprocess_error(self, msg: str) -> None:
        self._set_busy(False)
        self.val_progress.setVisible(False)
        self.val_progress_label.setText("检查出错")
        self.val_progress_label.setStyleSheet("color:#FF3B30;")
        self._log_plain(f"[错误] {msg}")

    def _run_subprocess(self, task_name: str, args: list[str], on_line=None) -> None:
        if self._busy:
            return
        self._set_busy(True, task_name, cancelable=True)
        self._worker = SubprocessWorker(args, log_signals=self._signals)
        self._cancel_fn = self._worker.cancel
        self._worker.signals.line.connect(lambda l: self._on_subprocess_line(l, on_line))
        self._worker.signals.finished.connect(self._on_subprocess_done)
        self._worker.signals.error.connect(lambda e: self._log_plain(f"[错误] {e}"))
        self._pool.start(self._worker)

    def _on_subprocess_line(self, line: str, on_line) -> None:
        if on_line:
            on_line(line)

    def _on_subprocess_done(self, code: int) -> None:
        self._set_busy(False)
        self._cancel_fn = None
        if code == 0:
            self._log_plain("✓ 完成")
        else:
            self._log_plain(f"✗ 进程退出码 {code}")
        self._refresh_experiments()
        if self._detect_result is not None:
            r = self._detect_result
            self.detect_result_label.setText(
                f"✓ 检测完成：{r['images']} 张图，共 {r['detections']} 个目标\n"
                f"结果保存在：{r['save_dir']}")
            self._detect_result = None

    def _cancel(self) -> None:
        if self._cancel_fn:
            self._log_plain("正在停止...")
            self._cancel_fn()
            self._cancel_fn = None

    # ==================================================================
    # 工具方法
    # ==================================================================
    def _goto_page(self, idx: int) -> None:
        self._nav_group.button(idx).setChecked(True)
        self._switch_page(idx)

    def _open_path(self, path: Path) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def _first_file(details: dict) -> str:
    """从 check details 里扒第一个文件名."""
    if not details:
        return "-"
    for key in ("missing_examples", "errors_preview"):
        v = details.get(key)
        if isinstance(v, dict) and v:
            for lst in v.values():
                if isinstance(lst, list) and lst:
                    first = lst[0]
                    if isinstance(first, dict):
                        return str(first.get("label") or first.get("file") or "-")
                    return str(first)
        elif isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, dict):
                return str(first.get("label") or first.get("file") or "-")
            return str(first)
    return "-"


def main() -> int:
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("Dataset Converter & Validator")
    app.setStyleSheet(APP_STYLE)
    win = MainWindow()
    win.show()
    return app.exec()
