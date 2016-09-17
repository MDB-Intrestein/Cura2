# Copyright (c) 2015 Jaime van Kessel
# The ImperialPlugin is released under the terms of the AGPLv3 or higher.

from . import ImperialPlugin
from UM.i18n import i18nCatalog
i18n_catalog = i18nCatalog("ImperialPlugin")


def getMetaData():
    return {
        "type": "extension",
        "plugin":
        {
            "name": "Imperial plugin",
            "author": "Jaime van Kessel",
            "version": "2.2",
            "api": 3,
            "description": i18n_catalog.i18nc("Description of plugin", "Extension that allows to quickly convert models in metric and imperial units")
        }
    }


def register(app):
    return {"extension": ImperialPlugin.ImperialPlugin()}
