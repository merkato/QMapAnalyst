def classFactory(iface):
    from .plugin import QMapAnalystPlugin
    return QMapAnalystPlugin(iface)