import sys
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication


def main():
    # HiDPI — must be configured before QApplication is constructed
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)

    # PyQt6 >= 6.11 requires this set before QApplication when QtWebEngineWidgets
    # is imported at module level (which orbit_embed does).
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    _icon_path = Path(__file__).parent / "lingua.ico"
    _app_icon  = QIcon(str(_icon_path)) if _icon_path.exists() else None
    if _app_icon:
        app.setWindowIcon(_app_icon)

    # Point wn at the project-local data folder.
    # Must happen before bridge.py imports wn so the db path is set before
    # wn opens its SQLite connection.
    _wn_default = Path.home() / ".wn_data"
    _wn_local   = Path(__file__).parent / "data" / "wn_data"
    if _wn_default.exists() and not _wn_local.exists():
        import shutil
        print("[lingua] migrating wn data to project folder…")
        shutil.copytree(str(_wn_default), str(_wn_local))
    try:
        import wn as _wn_cfg
        _wn_cfg.config.data_directory = str(_wn_local)
        _wn_cfg.config.allow_multithreading = True
    except ImportError:
        pass

    from orbit_embed import OrbitEmbed
    from bridge import LinguaBridge

    root       = Path(__file__).parent / "app"
    data_root  = Path(__file__).parent / "data"
    model_path = Path(__file__).parent / "Qwen3.5-9B-Q4_K_M.gguf"

    bridge = LinguaBridge(data_root=data_root, model_path=model_path)
    app.aboutToQuit.connect(bridge._stop_server)

    win = OrbitEmbed(
        root=root,
        entry="index.html",
        bridge=bridge,
        title="Lingua",
        show_tabs=False,
        show_nav=False,
        address_bar_mode="hidden",
        theme={"bg": "#0c0c11", "accent": "#c4a35a"},
        dev_mode=False,
    )
    if _app_icon:
        win.setWindowIcon(_app_icon)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
