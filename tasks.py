"""
Długie obliczenia (przetwarzanie rastrów GDAL) w tle – QgsTask, żeby QGIS nie przestawał odpowiadać.

fn(progress, canceled) wykonuje się w wątku roboczym i NIE może dotykać interfejsu ani warstw projektu:
progress(ułamek 0…1, opis) raportuje postęp, canceled() mówi, czy użytkownik przerwał. Wynik (albo wyjątek)
trafia do on_done(result, error) w wątku głównym – tam dodaje się warstwy i zapisuje do biblioteki.
"""

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsTask


class _Relay(QObject):
    """Sygnał postępu z opisem (QgsTask.progressChanged nie niesie tekstu)."""
    message = pyqtSignal(float, str)


class FunctionTask(QgsTask):
    def __init__(self, description, fn, on_done):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.fn = fn
        self.on_done = on_done
        self.result = None
        self.error = None
        self.relay = _Relay()

    def _progress(self, frac, text=""):
        frac = min(max(float(frac), 0.0), 1.0)
        self.setProgress(100.0 * frac)
        self.relay.message.emit(frac, text)
        return not self.isCanceled()

    def run(self):
        try:
            self.result = self.fn(self._progress, self.isCanceled)
        except Exception as e:  # noqa: BLE001 – błąd przekazywany do wątku głównego
            self.error = e
            return False
        return not self.isCanceled()

    def finished(self, ok):
        err = self.error
        if self.isCanceled() or (err is None and not ok):
            err = InterruptedError("canceled")
        self.on_done(self.result, err)


_RUNNING = set()                                     # referencje – zadania nie mogą zostać zebrane przez GC


def run_task(description, fn, on_done, background=True, on_progress=None):
    """Uruchamia fn w tle (background=True) albo od razu (testy, tryb wsadowy). Zwraca zadanie albo None."""
    if not background:
        try:
            result = fn(lambda f, t="": (on_progress(f, t) if on_progress else None) is not False, lambda: False)
        except Exception as e:  # noqa: BLE001
            on_done(None, e)
            return None
        on_done(result, None)
        return None
    task = FunctionTask(description, fn, None)

    def done(result, error):
        _RUNNING.discard(task)
        on_done(result, error)
    task.on_done = done
    if on_progress is not None:
        task.relay.message.connect(on_progress)
    _RUNNING.add(task)
    QgsApplication.taskManager().addTask(task)
    return task
