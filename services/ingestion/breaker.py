"""Circuit breaker (C21): after `threshold` consecutive failures the dependency is treated as down for `cooldown` seconds."""
import time


class Breaker:
    def __init__(self, name, threshold=5, cooldown=30.0):
        self.name, self.threshold, self.cooldown = name, threshold, cooldown
        self.fails, self.until = 0, 0.0

    @property
    def is_open(self):
        return self.until > time.time()

    def retry_after(self):
        return max(1, int(self.until - time.time()))

    def ok(self):
        self.fails = 0

    def fail(self):
        self.fails += 1
        if self.fails >= self.threshold:
            self.until = time.time() + self.cooldown
            self.fails = self.threshold - 1  # half-open: one more failure re-opens at once
            return True
        return False
