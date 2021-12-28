import audioop
import collections
import io
import math
import queue
import random
import signal as signal_handler
import threading
import time

import numpy as np
from pydub import AudioSegment

import config
from alexa_client import AlexaClient
from adapter.zmq.zmq import Subscriber
from framework.ipc.msg.led_update import LedUpdate
from framework.startup.node import Node
from framework.startup.task import Thread
from outputs.led.led_color_settings import LedColorSetting
from outputs.led.led_type import LedType


class TimeOutException(Exception):
    pass


class WaitTimeoutError(Exception):
    pass


class AlexaNode(Node):
    def __init__(self):
        super().__init__()
        self.client = AlexaClient()

        self.energy_threshold = 300  # minimum audio energy to consider for recording
        self.dynamic_energy_threshold = True
        self.dynamic_energy_adjustment_damping = 0.15
        self.dynamic_energy_ratio = 1.5
        self.pause_threshold = 0.8  # seconds of non-speaking audio before 
        # a phrase is considered complete
        self.operation_timeout = None  # seconds after an internal operation 
        # (e.g., an API request) starts before it
        # times out, or ``None`` for no timeout
        self.phrase_threshold = 0.3  # minimum seconds of speaking audio before 
        # we consider the speaking audio a phrase -
        # values below this are ignored
        # (for filtering out clicks and pops)
        self.non_speaking_duration = 0.5  # seconds of non-speaking audio to 
        # keep on both sides of the recording

        self.queue = queue.SimpleQueue()
        self.thinking = False

    def _buffer_to_audio(self, raw_buffer):
        return np.frombuffer(raw_buffer, dtype=np.int16)

    def alarm_handler(self, signum, frame):
        print("Cannot get response from Alexa server!")
        raise TimeOutException()

    def pick_one_mic_channel(self, raw_buffer):
        '''
        This function is used to split one channel raw data from
        four channels raw data.
        '''
        audio = self._buffer_to_audio(raw_buffer)
        # There are 4 channels. Here I choose the first mic
        mic0 = audio[0::config.CHANNELS]
        mic0_raw = mic0.tobytes()
        return mic0_raw

    def record_audio_to_alexa(self, timeout=config.WAITING_LENGTH, phrase_time_limit=config.RECOGNITION_LENGTH):
        seconds_per_buffer = config.CHUNK_SIZE / config.SAMPLE_RATE
        # number of buffers of non-speaking audio during a phrase, 
        # before the phrase should be considered complete
        pause_buffer_count = int(math.ceil(self.pause_threshold / seconds_per_buffer))
        # minimum number of buffers of speaking audio 
        # before we consider the speaking audio a phrase
        phrase_buffer_count = int(math.ceil(self.phrase_threshold / seconds_per_buffer))
        # maximum number of buffers of non-speaking audio 
        # to retain before and after a phrase
        non_speaking_buffer_count = int(math.ceil(self.non_speaking_duration / seconds_per_buffer))

        # read audio input for phrases until there is a phrase that is long enough
        elapsed_time = 0  # number of seconds of audio read
        buffer = b""  # an empty buffer means that the stream has ended 
        # and there is no data left to read
        while True:
            frames = collections.deque()

            # store audio input until the phrase starts
            while True:
                # handle waiting too long for phrase by raising an exception
                elapsed_time += seconds_per_buffer
                if timeout and elapsed_time > timeout:
                    raise WaitTimeoutError("listening timed out while waiting for phrase to start")

                buffer = self.queue.get()
                if len(buffer) == 0:
                    break  # reached end of the stream
                frames.append(buffer)
                if len(
                        frames) > non_speaking_buffer_count:  # ensure we only keep the needed amount of non-speaking buffers
                    frames.popleft()

                # detect whether speaking has started on audio input
                energy = audioop.rms(buffer, 2)  # energy of the audio signal
                if energy > self.energy_threshold:
                    break

                # dynamically adjust the energy threshold using asymmetric weighted average
                if self.dynamic_energy_threshold:
                    damping = self.dynamic_energy_adjustment_damping ** seconds_per_buffer  # account for different chunk sizes and rates
                    target_energy = energy * self.dynamic_energy_ratio
                    self.energy_threshold = self.energy_threshold * damping + target_energy * (1 - damping)

            # read audio input until the phrase ends
            pause_count, phrase_count = 0, 0
            phrase_start_time = elapsed_time
            while True:
                # handle phrase being too long by cutting off the audio
                elapsed_time += seconds_per_buffer
                if phrase_time_limit and elapsed_time - phrase_start_time > phrase_time_limit:
                    break

                buffer = self.queue.get()
                if len(buffer) == 0:
                    break  # reached end of the stream
                frames.append(buffer)
                phrase_count += 1

                # check if speaking has stopped for longer than the pause threshold on the audio input
                energy = audioop.rms(buffer, 2)  # unit energy of the audio signal within the buffer
                if energy > self.energy_threshold:
                    pause_count = 0
                else:
                    pause_count += 1
                if pause_count > pause_buffer_count:  # end of the phrase
                    break

            # check how long the detected phrase is, and retry listening if the phrase is too short
            phrase_count -= pause_count  # exclude the buffers for the pause before the phrase
            # phrase is long enough or we've reached the end of the stream, so stop listening
            if phrase_count >= phrase_buffer_count or len(buffer) == 0:
                break

        # obtain frame data
        for i in range(pause_count - non_speaking_buffer_count):
            # remove extra non-speaking frames at the end
            frames.pop()
        frame_data = b"".join(frames)

        return frame_data

    def think_about_it(self):
        self.thinking = True
        # random thinking phrase
        self.base_outputs.play_sound("alexa/" + random.choice(config.THINKING_PHRASES) + ".mp3")

        while self.thinking:
            color_settings = [LedColorSetting(
                led_type=LedType.EARS,
                brightness=100,
                b=random.randint(0, 255),
                g=random.randint(0, 255),
                r=random.randint(0, 255))
            ]
            self.base_outputs.set_leds(LedUpdate(color_settings, duration=0.1, priority=2))
            # sleep to make flash light effect
            time.sleep(0.1)

    def sound_subscriber(self):
        sound_buffer_bus = Subscriber(config.SOUND_BUFFER_BUS)
        for sound in sound_buffer_bus:
            self.queue.put(self.pick_one_mic_channel(sound.buffer))

    def clear_queue(self):
        if self.queue.qsize() > 0:
            self.queue.get_nowait()

    def run(self):
        try:
            timer_for_break = 0
            keep_listening = True
            dialog_request_id = None
            print("Listening...")

            with Thread(self.sound_subscriber):
                # Light ear to blue when getting ready to record
                color_settings = [LedColorSetting(LedType.EARS, 100, 50, 255, 0)]
                self.base_outputs.set_leds(LedUpdate(color_settings, duration=config.RECOGNITION_LENGTH, priority=2))
                wav = self.record_audio_to_alexa()

                while keep_listening:
                    # if timer_for_break == 0:
                    #     think_about_it_thread = threading.Thread(target=self.think_about_it)
                    #     think_about_it_thread.start()

                    # signal_handler.signal(signal_handler.SIGALRM, self.alarm_handler)
                    # signal_handler.alarm(10)

                    # try:
                    #     directives = self.client.send_audio_file(wav, \
                    #                                              dialog_request_id=dialog_request_id, \
                    #                                              distance_profile=constants.FAR_FIELD)
                    # except TimeOutException as es:
                    #     print(es)
                    #     directives = None
                    #     break
                    # finally:
                    #     signal_handler.alarm(0)

                    if directives:
                        timer_for_break = 0
                        dialog_request_id = None
                        keep_listening = False
                        for directive in directives:
                            if directive.name == "Speak" or directive.name == "Play":
                                print("diractive name:", directive.name)
                                reply = AudioSegment.from_mp3(io.BytesIO(directive.audio_attachment))
                                reply = reply.set_frame_rate(int(config.SAMPLE_RATE * config.AUDIO_SPEED_RATE))
                                reply = reply.set_channels(1)
                                reply = reply.raw_data
                                audio_length = len(reply) / (config.SAMPLE_RATE * config.AUDIO_SPEED_RATE)

                                self.thinking = False
                                think_about_it_thread.join()

                                # Light ear to orange when Alexa is responding
                                color_settings = [LedColorSetting(LedType.EARS, 100, 255, 50, 0)]
                                self.base_outputs.set_leds(LedUpdate(color_settings, duration=audio_length, priority=2))

                                self.base_outputs.play_sound(reply, wait=False)
                                print("Finish alexa playing!")
                                # sleep for 1 sec to make output stream finish playing audio
                                # time.sleep(1)

                            elif directive.name == "ExpectSpeech":
                                keep_listening = True
                                dialog_request_id = directive.dialog_request_id
                        self.clear_queue()
                        print("keep listening:", keep_listening)
                        if keep_listening:
                            print("Listening...")
                            # Light ear to blue when hearing "Hey Kiki"
                            color_settings = [LedColorSetting(LedType.EARS, 100, 50, 255, 0)]
                            self.base_outputs.set_leds(
                                LedUpdate(color_settings, duration=config.RECOGNITION_LENGTH, priority=2))
                            wav = self.record_audio_to_alexa()
                            print("Finish recording")
                    else:
                        timer_for_break += 1

                    if timer_for_break > config.TIME_UP_FOR_BREAK:
                        # Turn off ear led
                        color_settings = [LedColorSetting(LedType.EARS, 0, 0, 0, 0)]
                        self.base_outputs.set_leds(LedUpdate(color_settings, priority=2))
                        break

        except Exception as ex:
            template = "An exception of type {0} occurred. Arguments: {1!r}"
            message = template.format(type(ex).__name__, ex.args)
            print(message)
            if type(ex) != WaitTimeoutError:
                try:
                    self.client = self.alexa.reconnect()
                    print("Reconnected")
                except:
                    print("Cannot reconnect")
        finally:
            self.thinking = False
            # try:
            #     think_about_it_thread.join()
            # except:
            #     pass

            # Turn off ear light when finish Alexa
            color_settings = [LedColorSetting(LedType.EARS, 0, 0, 0, 0)]
            self.base_outputs.set_leds(LedUpdate(color_settings, priority=2))
