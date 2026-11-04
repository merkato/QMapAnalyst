import os

from qgis.PyQt.QtCore import QCoreApplication, QLocale, QSettings, Qt, QTranslator
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction


class QMapAnalystPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.translator = None
        self.dockwidget = None
        self.action = None
        self.init_translator()

    def tr(self, message):
        return QCoreApplication.translate("QMapAnalystPlugin", message)

    def init_translator(self):
        """Ładuje plik .qm zgodny z językiem interfejsu QGIS (ustawienie locale/userLocale)."""
        locale = QSettings().value("locale/userLocale", "") or QLocale().name()
        locale = str(locale)[:2]
        qm_path = os.path.join(self.plugin_dir, "i18n", f"QMapAnalyst_{locale}.qm")
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

    def run(self, checked=True):
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
