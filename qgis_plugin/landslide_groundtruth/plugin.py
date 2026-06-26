"""Main plugin class: adds a toolbar button/menu entry that toggles the dock."""
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .dock import LandslideDock

MENU = "Landslide Ground-Truthing"
ICON = os.path.join(os.path.dirname(__file__), "icon.png")


class LandslidePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None

    def initGui(self):
        self.action = QAction(QIcon(ICON), MENU, self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(MENU, self.action)

    def toggle(self, checked):
        if self.dock is None:
            self.dock = LandslideDock(self.iface)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self.action.setChecked)
        self.dock.setVisible(checked)

    def unload(self):
        if self.dock is not None:
            self.dock.teardown()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu(MENU, self.action)
            self.action = None
