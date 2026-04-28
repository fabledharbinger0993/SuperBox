"""
Splash player for RekitBox
Plays a short MP4 animation before launching the main app window.
Falls back directly to main.py if the splash stack is unavailable.
"""
import importlib
import os
import sys
import threading
from pathlib import Path


def _handoff_to_main(main_entry):
    os.execv(sys.executable, [sys.executable, main_entry] + sys.argv[1:])


def play_splash_and_continue(video_path, main_entry):
    if not Path(video_path).exists():
        _handoff_to_main(main_entry)

    try:
        qt_core = importlib.import_module("PyQt5.QtCore")
        qt_multimedia = importlib.import_module("PyQt5.QtMultimedia")
        qt_multimedia_widgets = importlib.import_module("PyQt5.QtMultimediaWidgets")
        qt_widgets = importlib.import_module("PyQt5.QtWidgets")
    except ImportError:
        _handoff_to_main(main_entry)

    QTimer = qt_core.QTimer
    QUrl = qt_core.QUrl
    QMediaContent = qt_multimedia.QMediaContent
    QMediaPlayer = qt_multimedia.QMediaPlayer
    QVideoWidget = qt_multimedia_widgets.QVideoWidget
    QApplication = qt_widgets.QApplication
    QVBoxLayout = qt_widgets.QVBoxLayout
    QWidget = qt_widgets.QWidget

    class SplashWindow(QWidget):
        def __init__(self, splash_path, on_finish):
            super().__init__()
            self.setWindowTitle("RekitBox")
            self.setWindowFlags(self.windowFlags() | 0x00080000)  # Qt.Tool
            self.setFixedSize(480, 270)
            layout = QVBoxLayout()
            self.setLayout(layout)
            self.video_widget = QVideoWidget()
            layout.addWidget(self.video_widget)
            self.player = QMediaPlayer(None, QMediaPlayer.VideoSurface)
            self.player.setVideoOutput(self.video_widget)
            self.player.setMedia(QMediaContent(QUrl.fromLocalFile(str(splash_path))))
            self.player.mediaStatusChanged.connect(self._on_status)
            self.player.stateChanged.connect(self._on_state)
            self._on_finish = on_finish
            self._finished = False
            # Safety timeout via OS-level thread — fires even in background processes
            # (QTimer is not reliable when the process is nohup'd / backgrounded on macOS)
            self._watchdog = threading.Timer(30.0, self._timeout)
            self._watchdog.daemon = True
            self._watchdog.start()
            self.player.play()

        def _timeout(self):
            """Force finish if video hangs — fires via OS thread, works when backgrounded"""
            if not self._finished:
                self._finished = True
                self._on_finish()

        def _on_status(self, status):
            if status == QMediaPlayer.EndOfMedia and not self._finished:
                self._finished = True
                self._watchdog.cancel()
                QTimer.singleShot(300, self._on_finish)

        def _on_state(self, state):
            if state == QMediaPlayer.StoppedState and not self._finished:
                self._finished = True
                self._watchdog.cancel()
                QTimer.singleShot(300, self._on_finish)

    app = QApplication(sys.argv)
    win = None

    def finish():
        if win is not None:
            win.close()
        app.quit()
        _handoff_to_main(main_entry)

    try:
        win = SplashWindow(video_path, finish)
        win.show()
        app.exec_()
    except Exception:
        finish()

if __name__ == "__main__":
    splash = Path(__file__).parent / "static/rekitbox-splash.mp4"
    main_py = str(Path(__file__).parent / "main.py")
    play_splash_and_continue(str(splash), main_py)
