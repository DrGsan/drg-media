"""Bound idle queue probes; durable jobs survive missed/coalesced wakeups."""
import threading


class IdlePoller:
    def __init__(self, intervals, event=None):
        self.intervals=tuple(intervals)
        self.event=event if event is not None else threading.Event()
        self.index=0

    def wake(self):
        self.event.set()

    def wait(self, worked):
        if worked:self.index=0
        delay=self.intervals[self.index]
        if not worked:self.index=min(self.index+1,len(self.intervals)-1)
        if self.event.wait(delay):self.index=0
        self.event.clear()
