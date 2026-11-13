"""
Analiza mapy w bibliotece punktów: zapis i odczyt punktów GCP (z statusem, uwagami i punktami
„brak na mapie”), opis mapy, wykrywanie punktów poprawionych w bibliotece po powiązaniu.

Domieszka (mixin) panelu QMapAnalystDockWidget – korzysta z jego pól (gcps, absent, ref_crs,
library_panel, old_raster_layer…) i metod (add_gcp, gcps_changed, set_status…).
"""

import os

from qgis.PyQt.QtWidgets import QApplication, QInputDialog, QMessageBox
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsCoordinateTransform, QgsCsException, QgsPointXY, QgsRasterLayer

import numpy as np

from . import library as lb
from . import math_engine as me
from .library_panel import AnalysisDialog, analysis_years


class LibraryAnalysisMixin:
    """Zapis/odczyt analizy w bibliotece. Wymaga: self.library_analysis (lb.Analysis lub None)."""

    # --- pomocnicze ----------------------------------------------------------
    def _scan_path(self):
        return self.old_raster_layer.source() if self.old_raster_layer is not None else ""

    def library_links(self):
        links = [lb.Link(g.old_x, g.old_y, g.ref_x, g.ref_y, uid=g.uid or None, name=g.name, enabled=g.enabled,
                         status=g.status, comment=g.comment) for g in self.gcps]
        links += [lb.Link(uid=uid, name=name, status="absent", comment=comment)
                  for uid, (name, comment) in self.absent.items()]
        return links

    def _new_library_analysis(self):
        path = self._scan_path()
        a = lb.Analysis(title=self.old_raster_layer.name() if self.old_raster_layer else "", scan_path=path,
                        ref_crs=self._crs_definition(self._ensure_ref_crs()))
        if path and os.path.isfile(path):
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                a.scan_sha256 = lb.file_sha256(path)
            finally:
                QApplication.restoreOverrideCursor()
        return a

    def update_analysis_label(self):
        self.library_panel.set_analysis_label(self.library_analysis)

    def on_scan_changed(self):
        """Nowy skan: analiza innego skanu przestaje być bieżąca; podpowiedź o analizach tego skanu w bibliotece."""
        a = self.library_analysis
        path = self._scan_path()
        if a is not None and a.scan_path and os.path.normcase(os.path.abspath(a.scan_path)) != os.path.normcase(os.path.abspath(path)):
            self.library_analysis = None
            self.update_analysis_label()
        if self.library_analysis is not None or self.library_panel.path is None or not path:
            return
        try:
            with self.library_panel.library() as lib:
                same = lib.find_analyses(scan_path=path)
        except lb.LibraryError:
            return
        if same:
            self.set_status(self.tr("W bibliotece jest analiza tego skanu: „{title}”. Wczytasz ją w zakładce Biblioteka.")
                            .format(title=same[-1].title or "?"), "ok")

    # --- zapis -------------------------------------------------------------------
    def _choose_existing(self, a):
        """Gdy biblioteka ma już analizę tego skanu – zastąpić ją czy zapisać nową? None – anulowano."""
        with self.library_panel.library() as lib:
            same = lib.find_analyses(scan_sha256=a.scan_sha256) if a.scan_sha256 else lib.find_analyses(scan_path=a.scan_path)
        if not same:
            return a
        old = same[-1]
        ans = QMessageBox.question(
            self, "QMapAnalyst",
            self.tr("W bibliotece jest już analiza tego skanu: „{title}”. Zastąpić ją?\n\nNie – zapisz jako nową analizę.")
            .format(title=old.title or "?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
        if ans == QMessageBox.StandardButton.Cancel:
            return None
        return old if ans == QMessageBox.StandardButton.Yes else a

    def save_library_analysis(self):
        if self.library_panel.path is None:
            self.set_status(self.tr("Najpierw otwórz lub załóż bibliotekę punktów (zakładka Biblioteka)."), "warn")
            return False
        if not self.gcps and not self.absent:
            self.set_status(self.tr("Brak punktów do zapisania."), "warn")
            return False
        if self.library_panel.layer is not None and not self.library_panel._save_pending_edits():
            return False
        a = self.library_analysis
        if a is None or a.analysis_id is None:
            a = self._choose_existing(self._new_library_analysis())
            if a is None:
                return False
            if a.analysis_id is None and AnalysisDialog(a, self).exec() != AnalysisDialog.DialogCode.Accepted:
                return False
        a.ref_crs = self._crs_definition(self._ensure_ref_crs())
        a.scan_path = self._scan_path() or a.scan_path
        links = self.library_links()
        try:
            with self.library_panel.library() as lib:
                lib.save_analysis(a)
                lib.save_links(a.analysis_id, links)
        except lb.LibraryError as e:
            self.set_status(self.library_panel._library_error(e), "error")
            return False
        self.library_analysis = a
        self.update_analysis_label()
        self.set_status(self.tr("Zapisano analizę „{title}” w bibliotece (punkty: {n}, brak na mapie: {m}).")
                        .format(title=a.title or "?", n=len(self.gcps), m=len(self.absent)), "ok")
        return True

    def edit_library_analysis(self):
        if self.library_panel.path is None:
            return
        a = self.library_analysis or self._new_library_analysis()
        if AnalysisDialog(a, self).exec() != AnalysisDialog.DialogCode.Accepted:
            return
        self.library_analysis = a
        if a.analysis_id is not None:
            try:
                with self.library_panel.library() as lib:
                    lib.save_analysis(a)
            except lb.LibraryError as e:
                self.set_status(self.library_panel._library_error(e), "error")
                return
        self.update_analysis_label()

    # --- odczyt --------------------------------------------------------------------
    def _pick_analysis(self, analyses):
        scan = os.path.normcase(os.path.abspath(self._scan_path())) if self._scan_path() else None

        def same_scan(a):
            return scan is not None and a.scan_path and os.path.normcase(os.path.abspath(a.scan_path)) == scan

        analyses = sorted(analyses, key=lambda a: (not same_scan(a), a.title.lower()))
        labels = []
        for a in analyses:
            parts = [a.title or "?"]
            if analysis_years(a):
                parts.append(analysis_years(a))
            if a.sheet:
                parts.append(self.tr("ark. {s}").format(s=a.sheet))
            labels.append(("★ " if same_scan(a) else "") + " · ".join(parts) + f"  [#{a.analysis_id}]")
        label, ok = QInputDialog.getItem(self, self.tr("Wczytaj analizę"), self.tr("Analiza mapy:"), labels, 0, False)
        return analyses[labels.index(label)] if ok and label in labels else None

    def open_library_analysis(self, analysis_id=None):
        if self.library_panel.path is None:
            return False
        try:
            with self.library_panel.library() as lib:
                if analysis_id is None:
                    analyses = lib.analyses()
                    if not analyses:
                        self.set_status(self.tr("Biblioteka nie zawiera zapisanych analiz."), "warn")
                        return False
                    a = self._pick_analysis(analyses)
                else:
                    a = lib.get_analysis(analysis_id)
                if a is None:
                    return False
                links = lib.links(a.analysis_id)
        except lb.LibraryError as e:
            self.set_status(self.library_panel._library_error(e), "error")
            return False
        if self.gcps and QMessageBox.question(
                self, "QMapAnalyst", self.tr("Zastąpić bieżące punkty ({n}) punktami analizy „{title}”?")
                .format(n=len(self.gcps), title=a.title or "?")) != QMessageBox.StandardButton.Yes:
            return False
        self._ensure_scan(a)
        self._apply_library_analysis(a, links)
        self.library_panel.invalidate_cache()
        self.check_changed_refs()
        return True

    def _ensure_scan(self, a):
        """Wczytuje skan analizy, gdy żaden nie jest wczytany (lub – za zgodą – gdy wczytany jest inny)."""
        if not a.scan_path or not os.path.isfile(a.scan_path):
            return
        current = self._scan_path()
        if current and os.path.normcase(os.path.abspath(current)) == os.path.normcase(os.path.abspath(a.scan_path)):
            return
        if current and QMessageBox.question(
                self, "QMapAnalyst", self.tr("Analiza dotyczy innego skanu ({f}). Wczytać go?")
                .format(f=os.path.basename(a.scan_path))) != QMessageBox.StandardButton.Yes:
            return
        layer = QgsRasterLayer(a.scan_path, os.path.splitext(os.path.basename(a.scan_path))[0])
        if layer.isValid():
            self.set_old_raster(layer)

    def _apply_library_analysis(self, a, links):
        self.clear_all_gcp()
        self.ref_crs = None
        crs = self._parse_crs(a.ref_crs) if a.ref_crs else None
        if crs is not None:
            self.ref_crs = crs
        self._ensure_ref_crs()
        for ln in links:
            if ln.status == "absent":
                self.absent[ln.uid] = (ln.name, ln.comment)
            else:
                self.add_gcp(ln.x_old, ln.y_old, ln.ref_x, ln.ref_y, enabled=ln.enabled, name=ln.name,
                             uid=ln.uid, status=ln.status, comment=ln.comment)
        self.library_analysis = a
        self.update_absent_label()
        if any(g.name for g in self.gcps) and not self.chk_names.isChecked():
            self.chk_names.setChecked(True)
        self.gcps_changed()
        self.update_ref_markers()
        self.update_analysis_label()
        self.set_status(self.tr("Wczytano analizę „{title}” (punkty: {n}, brak na mapie: {m}).")
                        .format(title=a.title or "?", n=len(self.gcps), m=len(self.absent)), "ok")

    # --- wagi a priori ---------------------------------------------------------------------
    def apriori_point_weights(self, idx, model, old, ref, m_per_unit):
        """Wagi a priori aktywnych punktów (None – wszystkie równe 1).

        Czynnik statusu (library.STATUS_WEIGHTS) oraz niepewność współrzędnych odniesienia z biblioteki
        (sigma_m) względem typowego błędu mapy: σ_m = σ₀ wyrównania z samymi czynnikami statusu."""
        factors = np.array([lb.STATUS_WEIGHTS.get(self.gcps[i].status, 1.0) for i in idx])
        sigmas = {p.uid: p.sigma_m for p in self.library_panel._cached()[0]} if self.library_panel.path else {}
        sigma_ref = [sigmas.get(self.gcps[i].uid) for i in idx]
        sigma_ref = [s / m_per_unit if s and m_per_unit > 0 else None for s in sigma_ref]
        if np.allclose(factors, 1.0) and not any(sigma_ref):
            return None
        sigma_map = None
        if any(sigma_ref):
            sigma_map = me.fit_transform(model, old, ref, factors).sigma0
        return me.apriori_weights(factors, sigma_ref, sigma_map)

    # --- zmiany w bibliotece -------------------------------------------------------------
    def changed_library_points(self, tol=1e-3):
        """GCP, których punkt biblioteki zmienił położenie lub zniknął: lista (indeks, RefPoint lub None, (x, y), d)."""
        crs = self._ensure_ref_crs()
        lib_crs = self.library_panel.library_crs()
        ct = QgsCoordinateTransform(lib_crs, crs, self.project) if lib_crs.isValid() and crs != lib_crs else None
        by_uid = {p.uid: p for p in self.library_panel._cached()[0]}
        out = []
        for i, g in enumerate(self.gcps):
            if not g.uid:
                continue
            p = by_uid.get(g.uid)
            if p is None:
                out.append((i, None, None, None))
                continue
            try:
                q = ct.transform(QgsPointXY(p.x, p.y)) if ct is not None else QgsPointXY(p.x, p.y)
            except QgsCsException:
                continue
            d = ((q.x() - g.ref_x) ** 2 + (q.y() - g.ref_y) ** 2) ** 0.5
            if d > tol:
                out.append((i, p, (q.x(), q.y()), d))
        return out

    def check_changed_refs(self):
        """Ostrzeżenie o punktach poprawionych (lub usuniętych) w bibliotece; aktualizacja za zgodą."""
        if self.library_panel.path is None:
            return 0
        changed = self.changed_library_points()
        moved = [c for c in changed if c[1] is not None]
        gone = [c for c in changed if c[1] is None]
        if gone:
            for i, *_rest in gone:
                self.gcps[i].uid = None
            self.set_status(self.tr("Punkty usunięte z biblioteki (liczba: {n}) – zostają jako punkty spoza biblioteki.")
                            .format(n=len(gone)), "warn")
        if not moved:
            return len(gone)
        _, unit = self.ref_units()
        names = ", ".join(self.point_label(i) for i, *_r in moved[:8]) + ("…" if len(moved) > 8 else "")
        dmax = max(d for *_r, d in moved)
        if QMessageBox.question(
                self, "QMapAnalyst",
                self.tr("Punkty zmieniły położenie w bibliotece (liczba: {n}, największa zmiana {d} {unit}): {names}.\n\n"
                        "Zaktualizować współrzędne odniesienia tych punktów?")
                .format(n=len(moved), d=f"{dmax:.2f}", unit=unit, names=names)) == QMessageBox.StandardButton.Yes:
            for i, _p, (x, y), _d in moved:
                g = self.gcps[i]
                g.ref_x, g.ref_y = x, y
                if g.ref_marker is not None:
                    g.ref_marker.setCenter(self._ref_to_canvas(x, y))
            self.gcps_changed()
            self.set_status(self.tr("Zaktualizowano współrzędne punktów z biblioteki (liczba: {n}).").format(n=len(moved)), "ok")
        return len(changed)

    # --- brak na mapie --------------------------------------------------------------------
    def mark_target_absent(self):
        """Bieżący punkt z biblioteki (wybrany wyszukiwarką lub „Następny z zestawu”) – brak na mapie."""
        p = self._lib_target
        if p is None:
            self.set_status(self.tr("Najpierw wybierz punkt z biblioteki (wyszukiwarka lub „Następny z zestawu”)."), "warn")
            return
        comment, ok = QInputDialog.getText(self, self.tr("Brak na mapie"),
                                           self.tr("Punkt „{name}” – uwagi (np. pominięty przez kartografa):").format(name=p.name))
        if not ok:
            return
        self.absent[p.uid] = (p.name, comment.strip())
        self._clear_lib_target()
        self.update_absent_label()
        self.set_status(self.tr("„{name}” – brak na mapie.").format(name=p.name), "ok")
        if self.library_panel.set_points() is not None:
            self.next_library_point()

    def update_absent_label(self):
        n = len(self.absent)
        self.btn_lib_absent.setToolTip(
            self.tr("Oznacz bieżący punkt z biblioteki jako nieobecny na tej mapie (informacja, nie błąd).")
            + ("\n\n" + self.tr("Brak na mapie ({n}): {names}").format(
                n=n, names=", ".join(name for name, _c in self.absent.values())) if n else ""))
        self.btn_lib_absent.setText(self.tr("Brak na mapie") + (f" ({n})" if n else ""))
