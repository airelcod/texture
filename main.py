"""
Entry point unificado. Detecta plataforma y arranca el frontal adecuado.
En Android se debe llamar a este archivo como `main.py` en el proyecto
pyside6-android-deploy.
"""
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QPalette

from core import APP_NAME, BG0, BG1, BG2, TEXT, MUTED, ACCENT, IS_ANDROID, build_stylesheet


def apply_dark_palette(app: QApplication):
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG1))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor(BG0))
    pal.setColor(QPalette.AlternateBase, QColor(BG2))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(BG2))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("white"))
    pal.setColor(QPalette.PlaceholderText, QColor(MUTED))
    app.setPalette(pal)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    apply_dark_palette(app)
    app.setStyleSheet(build_stylesheet())

    if IS_ANDROID:
        from mobile_main import MobileWindow
        w = MobileWindow()
        w.showMaximized()
    else:
        from desktop_main import MainWindow
        w = MainWindow()
        w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()