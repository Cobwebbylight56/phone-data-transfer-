"""Brand guide page - the same knowledge the rescue plan draws on, browsable."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout, QWidget

from ptransfer.oem import ALL_PROFILES, VendorProfile

SAFETY = {
    "safe": ("#1a7f37", "data-safe"),
    "usually": ("#9a6700", "usually keeps data"),
    "wipes": ("#cf222e", "ERASES DATA"),
}


class GuidePage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Brand guides</h2>"))

        row = QHBoxLayout()
        row.addWidget(QLabel("Brand:"))
        self.combo = QComboBox()
        for p in ALL_PROFILES:
            self.combo.addItem(p.display_name, p)
        self.combo.currentIndexChanged.connect(self._render)
        row.addWidget(self.combo, 1)
        layout.addLayout(row)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        layout.addWidget(self.view, 1)
        self._render()

    def select_brand(self, key: str) -> None:
        for i in range(self.combo.count()):
            profile: VendorProfile = self.combo.itemData(i)
            if profile.key == key:
                self.combo.setCurrentIndex(i)
                return

    def _render(self) -> None:
        profile: VendorProfile = self.combo.currentData()
        if profile is None:
            return
        html = [f"<h2>{profile.display_name}</h2>"]

        html.append("<h3>Getting into each mode</h3><ul>")
        for c in profile.key_combos:
            note = f"<br><i>{c.note}</i>" if c.note else ""
            html.append(f"<li><b>{c.mode}</b><br>{c.steps}{note}</li>")
        html.append("</ul>")

        if profile.tools:
            html.append("<h3>Tools</h3><ul>")
            for t in profile.tools:
                colour, tag = SAFETY[t.data_safety.value]
                url = f"<br><a href='{t.url}'>{t.url}</a>" if t.url else ""
                note = f"<br><i>{t.note}</i>" if t.note else ""
                html.append(
                    f"<li><b>{t.name}</b> <span style='color:{colour}'>[{tag}]</span>"
                    f"<br>{t.purpose}{url}{note}</li>"
                )
            html.append("</ul>")

        if profile.rescue_steps:
            html.append("<h3>If it will not boot</h3><ol>")
            for s in profile.rescue_steps:
                flag = " <span style='color:#cf222e'><b>[ERASES DATA]</b></span>" if s.warns else ""
                html.append(f"<li><b>{s.title}</b>{flag}<br>{s.detail}</li>")
            html.append("</ol>")

        if profile.notes:
            html.append("<h3>Worth knowing</h3><ul>")
            for n in profile.notes:
                html.append(f"<li>{n}</li>")
            html.append("</ul>")

        self.view.setHtml("".join(html))
