"""Validated controller/touch state at the native engine boundary."""
import math
import threading


class Input:
    def __init__(self):
        self.lock = threading.Lock()
        self.tilt = 0.0
        self.touches = []
        self.actions = []

    @staticmethod
    def number(value, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError('Invalid game input number')
        return float(value)

    def apply(self, message):
        if not isinstance(message, dict):
            raise ValueError('Expected an input object')
        tilt, action = None, None
        touches = []
        if 'tilt' in message:
            tilt = self.number(message['tilt'], -1, 1)
        events = message.get('touches', [])
        if not isinstance(events, list) or len(events) > 16:
            raise ValueError('Invalid touch events')
        if 'touch' in message:
            events = events + [message['touch']]
        for event in events:
            if not isinstance(event, list) or len(event) != 4:
                raise ValueError('Invalid touch event')
            pointer, phase, x, y = event
            if type(pointer) is not int or type(phase) is not int or pointer not in range(16) or phase not in (0, 1, 2):
                raise ValueError('Invalid touch pointer or phase')
            touches.append((pointer, phase, self.number(x, 0, 544), self.number(y, 0, 960)))
        if 'action' in message:
            action = message['action']
            if action not in ('back', 'pause', 'resume'):
                raise ValueError('Invalid game action')
        with self.lock:
            if len(self.touches) + len(touches) > 16:
                raise ValueError('Touch queue is full')
            if action and len(self.actions) >= 16:
                raise ValueError('Action queue is full')
            if tilt is not None:
                self.tilt = tilt
            for touch in touches:
                if touch[1] == 1 and self.touches and self.touches[-1][:2] == touch[:2]:
                    self.touches[-1] = touch
                else:
                    self.touches.append(touch)
            if action:
                self.actions.append(action)

    def take(self):
        with self.lock:
            touches, actions = self.touches, self.actions
            self.touches, self.actions = [], []
            return self.tilt, touches, actions
