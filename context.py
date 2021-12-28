class ContextManager:
    def get_context(self):
        playbackState = {
            "header": {
                "namespace": "AudioPlayer",
                "name": "PlaybackState"
            },
            "payload": self._get_playback_state()
        }
        volumeState = {
            "header": {
                "namespace": "Speaker",
                "name": "VolumeState"
            },
            "payload": self._get_volume_state()
        }
        speechState = {
            "header": {
                "namespace": "SpeechSynthesizer",
                "name": "SpeechState"
            },
            "payload": self._get_speech_state()
        }
        return [volumeState, speechState, playbackState]

    def _get_speech_state(self):
        return {
            "token": "",
            "offsetInMilliseconds": 0,
            "playerActivity": "FINISHED"
        }

    def _get_playback_state(self):
        return {
            "token": '',
            "offsetInMilliseconds": 0,
            "playerActivity": "IDLE"
        }
    
    def _get_volume_state(self):
        return {
            "volume": 100,
            "mute": False,
        }