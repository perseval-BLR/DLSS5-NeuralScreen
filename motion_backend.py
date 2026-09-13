"""Track the backend actually initialized by the current worker."""


def normalize_backend(value):
    return "nvofa" if value == "nvofa" else "cpu"


class MotionBackendStatus:
    def __init__(self):
        self.worker = None
        self.active = False
        self.failed = False

    def update(self, worker, logs):
        if worker is not self.worker:
            self.worker = worker
            self.active = self.failed = False
        # Keep the last verdict when the bounded log buffer rotates.
        for line in reversed(logs):
            if "[nvofa] unavailable:" in line:
                self.active, self.failed = False, True
                break
            if "[nvofa] active:" in line:
                self.active, self.failed = True, False
                break
        return self.active
