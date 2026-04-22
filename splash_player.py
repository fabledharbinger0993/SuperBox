"""
Splash player for RekitBox
Plays a short MP4 animation before launching the main app window.
"""
import sys
import os
from pathlib import Path

try:
    from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout
    from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
    from PyQt5.QtMultimediaWidgets import QVideoWidget
    from PyQt5.QtCore import QUrl, QTimer
except ImportError:
    print("PyQt5 is required for splash animation. Please install with 'pip install PyQt5'.")
    sys.exit(1)

class SplashWindow(QWidget):
    def __init__(self, video_path, on_finish):
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
        self.player.setMedia(QMediaContent(QUrl.fromLocalFile(str(video_path))))
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.stateChanged.connect(self._on_state)
        self._on_finish = on_finish
        self.player.play()

    def _on_status(self, status):
        if status == QMediaPlayer.EndOfMedia:
            QTimer.singleShot(300, self._on_finish)

    def _on_state(self, state):
        if state == QMediaPlayer.StoppedState:
            QTimer.singleShot(300, self._on_finish)

def play_splash_and_continue(video_path, main_entry):
    app = QApplication(sys.argv)
    def finish():
        win.close()
        app.quit()
        os.execv(sys.executable, [sys.executable, main_entry] + sys.argv[1:])
    win = SplashWindow(video_path, finish)
    win.show()
    app.exec_()

if __name__ == "__main__":
    splash = Path(__file__).parent / "static/rekki-entrance.mp4"
    main_py = str(Path(__file__).parent / "main.py")
    play_splash_and_continue(str(splash), main_py)
