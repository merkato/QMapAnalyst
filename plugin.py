import os
from qgis.PyQt.QtCore import QTranslator, QCoreApplication, Qt, QLocale
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .dockwidget import QMapAnalystDockWidget

class QMapAnalystPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.translator = None
        self.dockwidget = None
        self.action = None

        self.init_translator()

    def tr(self, message):
        return QCoreApplication.translate('QMapAnalystPlugin', message)

    def init_translator(self):
        """Ładuje plik .qm odpowiedni dla języka interfejsu QGIS."""
        locale = QLocale().name()[:2]
        qm_path = os.path.join(self.plugin_dir, 'i18n', f'QMapAnalyst_{locale}.qm')
        
        if os.path.exists(qm_path):
            self.translator = QTranslator()
            self.translator.load(qm_path)
            QCoreApplication.installTranslator(self.translator)

    def initGui(self):
        """Dodaje przycisk wtyczki do paska narzędzi i menu Raster."""
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.action = QAction(QIcon(icon_path), self.tr("QMapAnalyst"), self.iface.mainWindow())
        
        self.action.triggered.connect(self.run)
        
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.tr("&Raster"), self.action)

    def unload(self):
        """Sprząta po wyłączeniu wtyczki."""
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu(self.tr("&Raster"), self.action)
        
        if self.dockwidget:
            self.iface.removeDockWidget(self.dockwidget)
            self.dockwidget.deleteLater()

    def run(self):
        """Uruchamia główne okno dokowalne wtyczki."""
        if not self.dockwidget:
            self.dockwidget = QMapAnalystDockWidget(self.iface)
            
        # Zmiana z Qt.RightDockWidgetArea na Qt.DockWidgetArea.RightDockWidgetArea (wymóg PyQt6)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dockwidget)
        self.dockwidget.show()