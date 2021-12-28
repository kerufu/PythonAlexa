import audioop
import collections
import io
import math
import os
import queue
import random
import threading
import time

import numpy as np
import pyaudio
import tensorflow as tf
# Speech to text
# import speech_recognition as sr
from pydub import AudioSegment
from scipy import signal

import config
from alexa_client import AlexaClient
from framework.startup.node import Node
from framework.ipc.msg.led_update import LedUpdate
from outputs.base_outputs import BaseOutputs
from outputs.led.led_color_settings import LedColorSetting
from outputs.led.led_type import LedType

NUM_OF_KEYWORD = 2

DETECTION_LENGTH = 2  # detecting "hey kiki"
RECOGNITION_LENGTH = 10  # recognizing words
WAITING_LENGTH = 4  # waiting time when recognizing

STD_LENGTH_FOR_MODEL = config.SAMPLE_RATE * DETECTION_LENGTH

CHANNELS = 1
NUM_OF_DETECTION_IN_ONE_SECONDS = 4

CHUNK_SIZE = int(STD_LENGTH_FOR_MODEL / (NUM_OF_DETECTION_IN_ONE_SECONDS * DETECTION_LENGTH))

TIME_UP_FOR_BREAK = 3

DEVICE_ID = 0

SAMPLE_WIDTH = pyaudio.paInt16

KEYWORD_TABLE = [
    "Unknow",  # 0
    "Hey_Kiki",  # 1
]

THINKING_PHRASES = [
    "let_me_think_about_it",
    "i_will_consider_this",
    "let_me_see",
    "let_me_figure_out",
    "let_me_gather_my_thoughts"
]


def mymodel():
    model = tf.keras.models.Sequential([
        tf.keras.layers.Reshape((142, 129, 1)),
        tf.keras.layers.Conv2D(16, 2, activation='relu'),
        tf.keras.layers.MaxPool2D(),
        tf.keras.layers.Conv2D(8, 2, activation='relu'),
        tf.keras.layers.MaxPool2D(),
        tf.keras.layers.Flatten(),
        tf.keras.layers.Dense(64, activation='relu'),
        tf.keras.layers.Dropout(0.05),
        tf.keras.layers.Dense(NUM_OF_KEYWORD, activation='linear')
    ])
    model.compile(loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                  optimizer=tf.keras.optimizers.Adam(),
                  metrics=['accuracy'])
    return model


class WaitTimeoutError(Exception):
    pass


class AlexaTestNode(Node):
    def __init__(self):
        super().__init__()
        self.queue = queue.SimpleQueue()

        self.thinking = False

        self.base_outputs = BaseOutputs()

        self.energy_threshold = 300  # minimum audio energy to consider for recording
        self.dynamic_energy_threshold = True
        self.dynamic_energy_adjustment_damping = 0.15
        self.dynamic_energy_ratio = 1.5
        self.pause_threshold = 0.8  # seconds of non-speaking audio before a phrase is considered complete
        self.operation_timeout = None  # seconds after an internal operation (e.g., an API request) starts before it times out, or ``None`` for no timeout
        self.phrase_threshold = 0.3  # minimum seconds of speaking audio before we consider the speaking audio a phrase - values below this are ignored (for filtering out clicks and pops)
        self.non_speaking_duration = 0.5  # seconds of non-speaking audio to keep on both sides of the recording

    def callback(self, in_data, frame_count, time_info, status):
        self.queue.put(in_data)
        return None, pyaudio.paContinue

    def switch_to_output(self):
        self.stream.stop_stream()
        self.stream.close()
        self.stream = self.pyaudio_instance.open(
            input=False,
            output=True,
            start=True,
            format=SAMPLE_WIDTH,
            channels=CHANNELS,
            rate=config.SAMPLE_RATE,
            frames_per_buffer=CHUNK_SIZE,
            input_device_index=DEVICE_ID
        )

    def switch_to_input(self):
        self.stream.stop_stream()
        self.stream.close()
        self.queue = queue.SimpleQueue()
        self.stream = self.pyaudio_instance.open(
            input=True,
            output=False,
            start=True,
            format=SAMPLE_WIDTH,
            channels=CHANNELS,
            rate=config.SAMPLE_RATE,
            frames_per_buffer=CHUNK_SIZE,
            input_device_index=DEVICE_ID,
            stream_callback=self.callback
        )

    def record_audio_to_alexa(self, timeout=WAITING_LENGTH, phrase_time_limit=RECOGNITION_LENGTH):

        seconds_per_buffer = CHUNK_SIZE / config.SAMPLE_RATE
        # number of buffers of non-speaking audio during a phrase, before the phrase should be considered complete
        pause_buffer_count = int(math.ceil(self.pause_threshold / seconds_per_buffer))
        # minimum number of buffers of speaking audio before we consider the speaking audio a phrase
        phrase_buffer_count = int(math.ceil(self.phrase_threshold / seconds_per_buffer))
        # maximum number of buffers of non-speaking audio to retain before and after a phrase
        non_speaking_buffer_count = int(math.ceil(self.non_speaking_duration / seconds_per_buffer))

        # read audio input for phrases until there is a phrase that is long enough
        elapsed_time = 0  # number of seconds of audio read
        buffer = b""  # an empty buffer means that the stream has ended and there is no data left to read
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
        self.switch_to_output()
        self.thinking = True
        # random thinking phrase
        with open(config.ASSETS_DIR + '/sounds/alexa/'
                  + random.choice(THINKING_PHRASES) + ".wav",
                  'rb') as read_file:
            audio = read_file.read()
        self.stream.write(audio)

        while self.thinking:
            color_settings = [LedColorSetting(
                led_type=LedType.EARS,
                brightness=100,
                b=random.randint(0, 255),
                g=random.randint(0, 255),
                r=random.randint(0, 255))
            ]
            self.base_outputs.set_leds(LedUpdate(color_settings))
            time.sleep(0.1)
        self.switch_to_input()

    def test_wav(self, wav):
        print("Enter test wav")
        self.switch_to_output()
        print("before write")
        self.stream.write(wav)
        print("after write")
        self.switch_to_input()

    def run(self):
        model = mymodel()
        model.load_weights(config.KEYWORD_DETECTION_MODEL_PATH)

        client = AlexaClient()
        # time.sleep(5)

        self.pyaudio_instance = pyaudio.PyAudio()
        self.stream = self.pyaudio_instance.open(
            input=True,
            output=False,
            start=True,
            format=SAMPLE_WIDTH,
            channels=CHANNELS,
            rate=config.SAMPLE_RATE,
            frames_per_buffer=CHUNK_SIZE,
            input_device_index=DEVICE_ID,
            stream_callback=self.callback
        )

        wav = self.queue.get()
        wav = np.frombuffer(wav, dtype=np.int16)
        wav = np.repeat(wav, NUM_OF_DETECTION_IN_ONE_SECONDS * DETECTION_LENGTH + 1)[:STD_LENGTH_FOR_MODEL]

        while True:
            if self.queue.qsize() < NUM_OF_DETECTION_IN_ONE_SECONDS * DETECTION_LENGTH:
                new_sample = self.queue.get()
                new_sample = np.frombuffer(new_sample, dtype=np.int16)
                wav = np.concatenate((wav[CHUNK_SIZE:], new_sample), axis=0)
            elif self.queue.qsize() > 2 * NUM_OF_DETECTION_IN_ONE_SECONDS * DETECTION_LENGTH:
                self.queue = queue.SimpleQueue()
            else:
                wav = b''
                for _ in range(NUM_OF_DETECTION_IN_ONE_SECONDS * DETECTION_LENGTH):
                    wav += self.queue.get()
                wav = np.frombuffer(wav, dtype=np.int16)

            frequencies, times, spectrogram = signal.spectrogram(wav, config.SAMPLE_RATE)

            if np.all(spectrogram):
                spectrogram = np.log(spectrogram)
            else:  # avoid dividing by 0
                spectrogram = np.log(spectrogram + 1.4012985e-45)

            keyword_type = np.argmax(model.predict_on_batch(np.asarray([spectrogram.T], dtype=np.float32)))
            if keyword_type == 1:

                timer_for_break = 0
                keep_listening = True
                dialog_request_id = None

                color_settings = [LedColorSetting(LedType.EARS, 100, 255, 50, 0)]
                self.base_outputs.set_leds(LedUpdate(color_settings))
                try:
                    os.system("echo 255 | tee /sys/devices/platform/pwm-fan/hwmon/hwmon0/pwm1 > /dev/null")
                    wav = self.record_audio_to_alexa()

                    while keep_listening:
                        # if timer_for_break == 0:
                        #     think_about_it_thread = threading.Thread(target=self.think_about_it)
                        #     think_about_it_thread.start()
                        client.send_audio_file(wav, dialog_request_id=dialog_request_id)
                        time.sleep(2)

                        duration = 0
                        dialog_request_id = None
                        keep_listening = False
                        while True:
                            output, data = client.get_output()
                            if output == "Start":
                                continue
                            elif output == "Speech":
                                color_settings = [LedColorSetting(LedType.EARS, 100, 0, 50, 255)]
                                self.base_outputs.set_leds(LedUpdate(color_settings))
                                try:
                                    reply = AudioSegment.from_mp3(io.BytesIO(data))
                                    reply = reply.set_frame_rate(16000)
                                    reply = reply.set_channels(1)
                                    duration = reply.duration_seconds
                                    client.play_audio(reply.raw_data)
                                except:
                                    print("cannot play")
                            elif output == "ExpectSpeech":
                                client.start_stream(data)
                                keep_listening = True
                                dialog_request_id = data
                            elif output == "End":
                                print("ready to end")
                                time.sleep(duration + 1)
                                break

                        if keep_listening:
                            color_settings = [LedColorSetting(LedType.EARS, 0, 0, 0, 0)]
                            self.base_outputs.set_leds(LedUpdate(color_settings))
                            color_settings = [LedColorSetting(LedType.EARS, 100, 255, 50, 0)]
                            self.base_outputs.set_leds(LedUpdate(color_settings))
                            self.queue = queue.SimpleQueue()
                            wav = self.record_audio_to_alexa()

                except Exception as ex:
                    template = "An exception of type {0} occurred. Arguments: {1!r}"
                    message = template.format(type(ex).__name__, ex.args)
                    print(message)
                    if type(ex) != WaitTimeoutError:
                        try:
                            client = AlexaClient(
                                client_id=config.CLIENT_ID,
                                secret=config.SECRET,
                                refresh_token=config.REFRESH_TOKEN
                            )
                            client.connect()
                            print("Reconnected")
                        except:
                            print("Cannot reconnect")
                finally:
                    # self.thinking = False
                    # try:
                    #     think_about_it_thread.join()
                    # except:
                    #     pass

                    os.system("echo 190 | tee /sys/devices/platform/pwm-fan/hwmon/hwmon0/pwm1 > /dev/null")

                    color_settings = [LedColorSetting(LedType.EARS, 0, 0, 0, 0)]
                    self.base_outputs.set_leds(LedUpdate(color_settings))

                    wav = self.queue.get()
                    wav = np.frombuffer(wav, dtype=np.int16)
                    wav = np.repeat(wav, NUM_OF_DETECTION_IN_ONE_SECONDS * DETECTION_LENGTH + 1)[:STD_LENGTH_FOR_MODEL]

        self.stream.stop_stream()
        self.stream.close()
        self.pyaudio_instance.terminate()