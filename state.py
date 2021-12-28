import enum

class SpeechState(enum.IntEnum):
    IDLE = 0
    PLAYING = 1

class RecognizeEventState(enum.IntEnum):
    IDLE = 0
    PROCESSING = 1