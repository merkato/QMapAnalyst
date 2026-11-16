"""
Okno „Rektyfikacja”: raster mapy dawnej dopasowany do mapy współczesnej metodą, której nie ma
Georeferencer QGIS – model globalny z analizy + pole korekcji lokalnej (rectify.py), przycięcie do ramki,
COG z kanałem alfa, raport i warstwa wektorów korekcji.

Działa na bieżącym stanie panelu: wczytany skan, aktywne GCP, obliczona transformacja, zasięg mapy.
"""

import math
import os
from datetime import datetime

import numpy as np
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressDialog, QPushButton, QSlider, QVBoxLayout,
)
from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer

from . import math_engine as me
from . import rectify as rc

SETTINGS_KEY = "QMapAnalyst/rectify_dir"


def tr(text):
    return QCoreApplication.translate("QMapAnalystRectify", text)


def method_labels():
    return {
        "tps": tr("Wygładzony TPS (zalecana)"),
        "tps_exact": tr("TPS interpolujący (jak Georeferencer)"),
        "triangles": tr("Trójkąty (afiniczna w trójkątach)"),
        "mls": tr("MLS – lokalnie sztywna"),
        "none": tr("Tylko model globalny"),
    }


def format_labels():
    return {"cog_webp": tr("COG, WEBP (mały, z kanałem alfa)"), "cog_jpeg": tr("COG, JPEG"),
            "gtiff_lzw": tr("GeoTIFF, LZW (bezstratny, duży)")}


def correction_inputs(fit, threshold=me.OUTLIER_THRESHOLD):
    """P – położenia przewidziane modelem globalnym, E = P − odniesienie, w – wagi (odstające 0,1)."""
    P = fit.dst - fit.residuals
    E = -fit.residuals
    w = np.ones(len(P)) if fit.weights is None else np.asarray(fit.weights, float).copy()
    if fit.std_residuals is not None:
        w = np.where(np.nan_to_num(fit.std_residuals) > threshold, w * 0.1, w)
    return P, E, w


def nice_resolution(v):
    if not v or not math.isfinite(v) or v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    return min((m * e for m in (1, 2, 2.5, 5, 10)), key=lambda c: abs(c - v))


class RectifyDialog(QDialog):
    def __init__(self, dock, parent=None):
        super().__init__(parent or dock)
        self.dock = dock
        self.st = dock.analysis
        self.correction = None
        self.output = None
        self.folds = 0.0
        self.P, self.E, self.W = correction_inputs(self.st.fit)
        self.setWindowTitle(tr("Rektyfikacja mapy dawnej"))
        self.resize(560, 520)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.recompute)
        self._build()
        self.recompute()

    # --- interfejs -----------------------------------------------------------------
    def _build(self):
        lay = QVBoxLayout(self)
        info = QLabel(tr("Analiza ocenia mapę modelem globalnym; rektyfikacja dodatkowo usuwa lokalne zniekształcenia "
                         "(pole korekcji), żeby raster pokrył się z mapą współczesną. O dokładności mapy mówi analiza, "
                         "nie raster."))
        info.setWordWrap(True)
        info.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(info)
        form = QFormLayout()
        self.cmb_method = QComboBox()
        for key, label in method_labels().items():
            self.cmb_method.addItem(label, key)
        self.cmb_method.currentIndexChanged.connect(self.on_method_changed)
        form.addRow(tr("Metoda"), self.cmb_method)
        row = QHBoxLayout()
        self.chk_auto = QCheckBox(tr("auto"))
        self.chk_auto.setChecked(True)
        self.chk_auto.setToolTip(tr("Wygładzanie z walidacji krzyżowej (najmniejszy błąd dla pominiętego punktu)"))
        self.chk_auto.toggled.connect(self.on_auto_toggled)
        self.sld_smooth = QSlider(Qt.Orientation.Horizontal)
        self.sld_smooth.setRange(0, 100)
        self.sld_smooth.setValue(50)
        self.sld_smooth.setEnabled(False)
        self.sld_smooth.setToolTip(tr("W lewo – luźniej (bliżej TPS interpolującego), w prawo – sztywniej (bliżej modelu globalnego)"))
        self.sld_smooth.valueChanged.connect(lambda _v: self._timer.start())
        row.addWidget(QLabel(tr("luźno")))
        row.addWidget(self.sld_smooth, 1)
        row.addWidget(QLabel(tr("sztywno")))
        row.addWidget(self.chk_auto)
        form.addRow(tr("Wygładzanie"), row)
        self.lbl_stats = QLabel()
        self.lbl_stats.setWordWrap(True)
        form.addRow(tr("Wynik"), self.lbl_stats)
        self.chk_frame = QCheckBox(tr("Przytnij do ramki mapy (zasięg z panelu GCP)"))
        self.chk_frame.setChecked(self.dock.map_frame is not None)
        form.addRow("", self.chk_frame)
        self.spin_res = QDoubleSpinBox()
        self.spin_res.setDecimals(3)
        self.spin_res.setRange(0.001, 1e6)
        self.spin_res.setValue(nice_resolution(self.st.fit.derived.get("scale", 1.0)))
        self.spin_res.setSuffix(f" {self.st.unit}/px")
        form.addRow(tr("Rozdzielczość"), self.spin_res)
        self.cmb_format = QComboBox()
        for key, label in format_labels().items():
            self.cmb_format.addItem(label, key)
        form.addRow(tr("Format"), self.cmb_format)
        row = QHBoxLayout()
        self.ed_out = QLineEdit(self._default_output())
        btn = QPushButton("…")
        btn.clicked.connect(self.browse)
        row.addWidget(self.ed_out, 1)
        row.addWidget(btn)
        form.addRow(tr("Plik wynikowy"), row)
        self.chk_add = QCheckBox(tr("Dodaj raster do projektu"))
        self.chk_add.setChecked(True)
        self.chk_vectors = QCheckBox(tr("Dodaj warstwę wektorów korekcji"))
        self.chk_vectors.setChecked(True)
        form.addRow("", self.chk_add)
        form.addRow("", self.chk_vectors)
        lay.addLayout(form)
        lay.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.btn_run = bb.addButton(tr("Rektyfikuj"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_run.clicked.connect(self.run)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _default_output(self):
        src = self.dock.old_raster_layer.source() if self.dock.old_raster_layer else ""
        base = os.path.splitext(src)[0] if src else os.path.join(os.path.expanduser("~"), "mapa")
        return base + "_rektyfikacja.tif"

    def browse(self):
        path, _f = QFileDialog.getSaveFileName(self, tr("Plik wynikowy"), self.ed_out.text(), "GeoTIFF (*.tif)")
        if path:
            self.ed_out.setText(path if path.lower().endswith(".tif") else path + ".tif")

    def method(self):
        return self.cmb_method.currentData()

    def on_method_changed(self, _i=None):
        tps = self.method() == "tps"
        self.chk_auto.setEnabled(tps)
        self.sld_smooth.setEnabled(tps and not self.chk_auto.isChecked())
        self.recompute()

    def on_auto_toggled(self, on):
        self.sld_smooth.setEnabled(not on and self.method() == "tps")
        self.recompute()

    # --- obliczenia ------------------------------------------------------------------
    def recompute(self):
        m = self.method()
        lam = None
        if m == "tps" and not self.chk_auto.isChecked():
            lam = rc.slider_to_lambda(self.sld_smooth.value())
        try:
            self.correction = rc.build_correction(self.P, self.E, m, lam=lam, w=self.W)
        except rc.RectifyError as e:
            self.correction = None
            self.lbl_stats.setText(tr("Błąd: {e}").format(e=e))
            return
        c = self.correction
        if m == "tps" and c.auto:
            self.sld_smooth.blockSignals(True)
            self.sld_smooth.setValue(int(round(rc.lambda_to_slider(c.lam))))
            self.sld_smooth.blockSignals(False)
        folds, _mask = rc.fold_check(self.rectifier(), self.frame(), n=40)
        u = self.st.unit
        f = self.st.m_per_unit
        parts = [tr("RMSE modelu globalnego {a} m → po korekcji {b} m").format(a=_m(c.rmse_before * f), b=_m(c.rmse_after * f))]
        if math.isfinite(c.loo_rmse):
            parts.append(tr("walidacja krzyżowa {v} m (błąd w miejscu pominiętego punktu)").format(v=_m(c.loo_rmse * f)))
        if folds > 0:
            parts.append("<b>" + tr("Fałdy: {p}% ramki – odwzorowanie zmienia orientację; zwiększ wygładzanie albo sprawdź punkty.")
                         .format(p=f"{100 * folds:.1f}") + "</b>")
        self.folds = folds
        self.lbl_stats.setText("<br>".join(parts) + ("" if u == "m" else f" ({u})"))

    def rectifier(self):
        return rc.Rectifier(self.st.fit.predict, self.correction)

    def frame(self):
        return self.dock.effective_frame()

    # --- przetworzenie -----------------------------------------------------------------
    def run(self):
        if self.correction is None or self.dock.old_raster_layer is None:
            return None
        out = self.ed_out.text().strip()
        if not out:
            return None
        if self.folds > 0.01 and QMessageBox.question(
                self, tr("Rektyfikacja"), tr("Odwzorowanie ma fałdy ({p}% ramki). Kontynuować?").format(
                    p=f"{100 * self.folds:.1f}")) != QMessageBox.StandardButton.Yes:
            return None
        from . import rectify_raster as rr
        func = self.rectifier()
        frame = self.frame()
        gcps = [(px, -ln, X, Y) for px, ln, X, Y in rc.gcp_grid(func, frame, k=24)]
        ring = rc.cutline(func, frame).tolist() if self.chk_frame.isChecked() else None
        crs = self.st.ref_crs.authid() or self.st.ref_crs.toWkt()
        prog = QProgressDialog(tr("Przetwarzanie rastra…"), tr("Przerwij"), 0, 100, self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(300)

        def progress(pct):
            prog.setValue(int(pct * 100))
            QCoreApplication.processEvents()
            return not prog.wasCanceled()
        try:
            rr.warp_scan(self.dock.old_raster_layer.source(), gcps, ring, out, crs, self.spin_res.value(),
                         self.cmb_format.currentData(), progress)
        except RuntimeError as e:
            prog.close()
            QMessageBox.warning(self, tr("Rektyfikacja"), tr("Nie udało się przetworzyć rastra: {e}").format(e=e))
            return None
        prog.close()
        rr.write_settings(out, self.settings())
        self.output = out
        self._add_layers(out, func)
        self.dock.set_status(tr("Zapisano raster po rektyfikacji: {p}").format(p=out), "ok")
        return out

    def settings(self):
        c = self.correction
        fit = self.st.fit
        return {
            "created": datetime.now().isoformat(timespec="seconds"),
            "scan": self.dock.old_raster_layer.source(), "crs": self.st.ref_crs.authid() or self.st.ref_crs.toWkt(),
            "model": fit.model, "method": c.method, "lambda": None if not math.isfinite(c.lam) else c.lam,
            "auto": c.auto, "rmse_global": c.rmse_before, "rmse_corrected": c.rmse_after,
            "loo_rmse": None if not math.isfinite(c.loo_rmse) else c.loo_rmse, "folds": self.folds,
            "frame": [list(map(float, p)) for p in self.frame()], "cut_to_frame": self.chk_frame.isChecked(),
            "resolution": self.spin_res.value(), "format": self.cmb_format.currentData(),
            "points": [{"label": lab, "src": list(map(float, s)), "ref": list(map(float, d)), "weight": float(w)}
                       for lab, s, d, w in zip(self.st.labels, fit.src, fit.dst, self.W)],
        }

    def _add_layers(self, out, func):
        project = QgsProject.instance()
        name = os.path.splitext(os.path.basename(out))[0]
        if self.chk_add.isChecked():
            lyr = QgsRasterLayer(out, name)
            if lyr.isValid():
                project.addMapLayer(lyr)
        if self.chk_vectors.isChecked() and self.correction.method != "none":
            project.addMapLayer(self.vector_layer(func, name))

    def vector_layer(self, func, name):
        """Wektory korekcji na siatce w ramce: od położenia wg modelu globalnego do położenia po korekcji."""
        from .layers import style_arrows
        lyr = QgsVectorLayer(f"LineString?crs={self.st.ref_crs.authid()}&field=korekta_m:double", tr("{n} – korekcja").format(n=name), "memory")
        if not self.st.ref_crs.authid():
            lyr.setCrs(self.st.ref_crs)
        feats = []
        frame = rc.order_corners(self.frame())
        t = np.linspace(0, 1, 13)
        U, V = np.meshgrid(t, t)
        src = rc.bilinear(frame, U, V).reshape(-1, 2)
        a = self.st.fit.predict(src)
        b = func(src)
        for (x0, y0), (x1, y1) in zip(a, b):
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x0, y0), QgsPointXY(x1, y1)]))
            f.setAttributes([float(math.hypot(x1 - x0, y1 - y0) * self.st.m_per_unit)])
            feats.append(f)
        lyr.dataProvider().addFeatures(feats)
        lyr.updateExtents()
        style_arrows(lyr, QColor(150, 40, 170), 0.4)
        return lyr


def _m(v):
    return f"{v:,.0f}".replace(",", " ") if math.isfinite(v) else "–"
