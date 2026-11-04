"""QGIS plugin entry point. QGIS calls classFactory(iface) to instantiate.

Landslide Ground-Truthing — Copyright (C) 2026 Ethan Hasenauer.

This program is free software; you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation; either version 2 of the License, or (at your option) any later
version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file.
"""


def classFactory(iface):
    from .plugin import LandslidePlugin
    return LandslidePlugin(iface)
