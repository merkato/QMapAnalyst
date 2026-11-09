import os

from qgis.PyQt.QtCore import QCoreApplication, Qt, QTranslator
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import Qgis, QgsMessageLog

from . import deps


class QMapAnalystPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.translator = None
        self.dockwidget = None
        self.action = None
        self._deps_warned = False
        self.init_translator()

    def tr(self, message):
        return QCoreApplication.translate("QMapAnalystPlugin", message)

    def init_translator(self):
        """Ładuje tłumaczenie: polskie dla polskiego interfejsu QGIS, angielskie dla każdego innego."""
        from .lang import ui_language
        qm_path = os.path.join(self.plugin_dir, "i18n", f"QMapAnalyst_{ui_language()}.qm")
        if os.path.exists(qm_path):
            self.translator = QTranslator()
            if self.translator.load(qm_path):
                QCoreApplication.installTranslator(self.translator)
            else:
                self.translator = None

    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, "icon.png"))
        self.action = QAction(icon, self.tr("QMapAnalyst"), self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.tr("&Raster"), self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu(self.tr("&Raster"), self.action)
        if self.dockwidget is not None:
            try:
                self.dockwidget.cleanup()
            finally:
                self.iface.removeDockWidget(self.dockwidget)
                self.dockwidget.deleteLater()
                self.dockwidget = None
        if self.translator is not None:
            QCoreApplication.removeTranslator(self.translator)
            self.translator = None

    def _install_hint(self, packages):
        lines = [f"{label}:\n    {cmd}" for label, cmd in deps.install_commands(packages)]
        return "\n\n".join(lines)

    def check_dependencies(self):
        """False, gdy brakuje numpy (panel nie może działać). Brak scipy – ostrzeżenie, funkcje okrojone."""
        required, optional = deps.check()
        if required:
            text = self.tr("Wtyczka QMapAnalyst wymaga pakietu Python: {pkgs}.").format(pkgs=", ".join(required))
            hint = self._install_hint(required)
            QgsMessageLog.logMessage(f"{text}\n{hint}", "QMapAnalyst", Qgis.MessageLevel.Critical)
            box = QMessageBox(QMessageBox.Icon.Warning, "QMapAnalyst",
                              text + "\n\n" + self.tr("Zainstaluj go i uruchom ponownie QGIS:"),
                              QMessageBox.StandardButton.Ok, self.iface.mainWindow())
            box.setInformativeText(hint)
            box.exec()
            return False
        if optional and not self._deps_warned:
            self._deps_warned = True
            pkgs = deps.packages_of(optional)
            features = "; ".join(self.tr(deps.OPTIONAL[m]) for m in optional)
            text = self.tr("Brak pakietu {pkgs} – niedostępne: {features}.").format(pkgs=", ".join(pkgs), features=features)
            hint = self.tr("Instalacja:") + "\n\n" + self._install_hint(pkgs)
            QgsMessageLog.logMessage(f"{text}\n{hint}", "QMapAnalyst", Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushMessage("QMapAnalyst", text, hint, Qgis.MessageLevel.Warning, 0)
        return True

    def run(self, checked=True):
        if not self.check_dependencies():
            self.action.setChecked(False)
            return
        # Import leniwy – błędy w module panelu nie blokują ładowania wtyczki
        from .dockwidget import QMapAnalystDockWidget

        if self.dockwidget is None:
            self.dockwidget = QMapAnalystDockWidget(self.iface)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dockwidget)
            self.dockwidget.visibilityChanged.connect(self.action.setChecked)
        if self.dockwidget.isVisible() and not checked:
            self.dockwidget.hide()
        else:
            self.dockwidget.show()
            self.dockwidget.raise_()
