"""The developer panel: what the last few requests actually cost, stage by stage.

It reads the ring buffer in core/diag.py and nothing else - no timers, no polling,
no background work. It refreshes when it is opened, when *Refresh* is pressed, and
when the session opens it from "Retro, diagnostics"; a panel that redraws itself
would be one more thing running on the modest machine this app is built for.

The numbers are the real ones the pipeline wrote: a missing stage is a dash, never
a zero, and the "not measured" note at the bottom says why something is absent
rather than letting an empty cell imply it was instant.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from core import diag

from . import theme

COLUMNS = ("TIME", "ORIGIN", "TOTAL ms", "FIRST TEXT", "FIRST AUDIO", "SLOWEST", "MODEL",
           "STREAM", "RESULT")


def _number(value):
    return "\u2014" if value is None else f"{value:.0f}"


class DiagnosticsPanel(QDialog):
    """A read-only view of the recorded timings, plus JSON and CSV export."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Retro - latency")
        self.setModal(False)
        self.setStyleSheet(theme.STYLESHEET + f"""
            QDialog {{ background: {theme.BG.name()}; }}
            QLabel#title {{ color: {theme.TEXT.name()}; font-size: 13px; font-weight: 600;
                           letter-spacing: 1.6px; }}
            QLabel#summary {{ color: {theme.TEXT.name()}; font-size: 12px; }}
            QLabel#detail {{ color: {theme.TEXT_DIM.name()}; font-size: 11px; }}
            QTableWidget {{ background: transparent; border: 1px solid {theme.BORDER.name()};
                           color: {theme.TEXT_DIM.name()}; gridline-color: #18202B;
                           font-size: 11px; selection-background-color: #182636; }}
            QHeaderView::section {{ background: transparent; color: {theme.TEXT_DIMMER.name()};
                                   border: none; padding: 5px 6px; font-size: 9px;
                                   letter-spacing: 1px; }}
            QPushButton#tool {{ border: 1px solid {theme.BORDER.name()}; border-radius: 9px;
                               color: {theme.TEXT_DIM.name()}; padding: 4px 12px;
                               letter-spacing: 1px; font-size: 10px; }}
            QPushButton#tool:hover {{ color: {theme.TEXT.name()}; border-color: #33404F; }}
        """)
        self.resize(620, 540)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel("LATENCY")
        title.setObjectName("title")
        root.addWidget(title)

        self.summary = QLabel("")
        self.summary.setObjectName("summary")
        root.addWidget(self.summary)

        self.latest = QLabel("")
        self.latest.setObjectName("detail")
        self.latest.setWordWrap(True)
        self.latest.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.latest)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.table.setFont(mono)
        root.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setObjectName("detail")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.refresh_btn = self._tool("REFRESH", self.refresh)
        self.json_btn = self._tool("EXPORT JSON", self.export_json)
        self.csv_btn = self._tool("EXPORT CSV", self.export_csv)
        clear_btn = self._tool("CLEAR", self.clear)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.json_btn)
        row.addWidget(self.csv_btn)
        row.addStretch(1)
        row.addWidget(clear_btn)
        root.addLayout(row)
        self.refresh()

    def _tool(self, text, slot):
        button = QPushButton(text)
        button.setObjectName("tool")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    # ---------- reading -----------------------------------------------------

    def refresh(self):
        """Rebuilds from the recorder. Cheap: at most forty rows."""
        stats = diag.summary()
        if not stats["count"]:
            self.summary.setText("Nothing measured yet - say something or type a command.")
        else:
            parts = [f"last {stats['count']} requests",
                     f"average {stats['average_ms']:.0f} ms",
                     f"median {stats['median_ms']:.0f} ms",
                     f"p95 {stats['p95_ms']:.0f} ms",
                     f"{stats['failures']} failed",
                     f"{stats['retries']} retried"]
            if stats["first_audio_ms"] is not None:
                parts.append(f"first audio {stats['first_audio_ms']:.0f} ms")
            self.summary.setText("   \u00b7   ".join(parts))
        if stats.get("slowest_stage"):
            label = diag.STAGE_LABELS.get(stats["slowest_stage"], stats["slowest_stage"])
            self.summary.setText(self.summary.text()
                                 + f"\nslowest stage on average: {label} "
                                   f"({stats['slowest_stage_ms']:.0f} ms)")

        items = [trace.data() for trace in diag.recent()]
        latest_item = stats.get("latest")
        if latest_item:
            lines = [f"latest {latest_item['id']}  \u00b7  {latest_item['origin']}  \u00b7  "
                     f"total {latest_item['total_ms']:.0f} ms  \u00b7  "
                     + ("streamed" if latest_item["streaming"] else
                        "not streamed" if latest_item["streaming"] is False else "streaming unknown")]
            stages = "   ".join(f"{name} {stage['ms']:.0f}"
                                for name in diag.STAGE_ORDER
                                for stage in [latest_item["stages"].get(name)] if stage)
            if stages:
                lines.append(stages + "  (ms)")
            for key, label in (("time_to_first_text_ms", "first text"),
                               ("time_to_first_audio_ms", "first audio"),
                               ("playback_ms", "playback")):
                if latest_item[key] is not None:
                    lines.append(f"{label} {latest_item[key]:.0f} ms")
            if latest_item["errors"]:
                lines.append("errors: " + ", ".join(
                    f"{item['stage']} {item['type'] or 'failure'}"
                    + (f" HTTP {item['status']}" if item["status"] else "")
                    for item in latest_item["errors"]))
            if latest_item["unavailable"]:
                lines.append("not measured: " + "; ".join(
                    f"{name} ({reason})" for name, reason in
                    sorted(latest_item["unavailable"].items())))
            self.latest.setText("\n".join(lines))
        else:
            self.latest.setText("")

        self.table.setRowCount(len(items))
        for row, item in enumerate(reversed(items)):
            slowest = item["slowest"]
            stage = item["stages"].get(slowest) if slowest else None
            cells = (
                item["at"],
                item["origin"],
                _number(item["total_ms"]),
                _number(item["time_to_first_text_ms"]),
                _number(item["time_to_first_audio_ms"]),
                (f"{slowest} {stage['ms']:.0f}" if stage else "\u2014"),
                item["model"] or "\u2014",
                ("\u2014" if item["streaming"] is None
                 else "yes" if item["streaming"] else "no"),
                ("ok" if item["ok"] else ("stopped" if item["cancelled"] else "failed")),
            )
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(str(text))
                if column in (2, 3, 4):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, cell)
        expected = diag.STAGE_ORDER
        self.status.setText(
            "stages: " + ", ".join(expected)
            + ". A dash is a stage that did not run; absent metrics are listed above, "
              "never reported as zero.")

    # ---------- actions -----------------------------------------------------

    def export_json(self):
        path = diag.export_json()
        self.status.setText(f"wrote {path}" if path else "could not write the JSON file")

    def export_csv(self):
        path = diag.export_csv()
        self.status.setText(f"wrote {path}" if path else "could not write the CSV file")

    def clear(self):
        diag.clear_history()
        self.refresh()
        self.status.setText("history cleared")

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)
