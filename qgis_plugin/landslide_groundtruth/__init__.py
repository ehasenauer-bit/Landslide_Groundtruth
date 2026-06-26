"""QGIS plugin entry point. QGIS calls classFactory(iface) to instantiate."""


def classFactory(iface):
    from .plugin import LandslidePlugin
    return LandslidePlugin(iface)
