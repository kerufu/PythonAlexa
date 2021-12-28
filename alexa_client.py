import cgi
import datetime
import http
import io
import json
import logging
import logging.config
import os
import pprint
import queue

import threading
import time
import uuid

from requests.exceptions import HTTPError
import hyper
from hyper import HTTP20Connection
from hyper.http20.response import HTTP20Response
import pyaudio

import constants, context, ping
from state import SpeechState, RecognizeEventState

AVS_AUDIO_CHUNK_PREFERENCE = 320
SAMPLE_RATE = 16000

DIR = os.path.dirname(os.path.abspath(__file__))

class HTTP20Downchannel(HTTP20Response):
    def close(self):
        print("Not closing stream {}".format(self._stream))

class AlexaClient:
    _content_cache = {}
    _tokens_filename = DIR + "/tokens.json"
    
    def __init__(self, base_url=constants.BASE_URL_NORTH_AMERICA):
        # self.ping_manager = ping.PingManager(60*4, self.ping)
        self.context_manager = context.ContextManager()
        self.base_url = base_url

        # setup speaker output
        self.output_stream = pyaudio.PyAudio().open(
            input=False,
            output=True,
            start=True,
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
        )

        self._log = self._setup_logging()
        self._eventQueue = queue.Queue()
        self.speech_state = SpeechState.IDLE
        self.recognize_event_state = RecognizeEventState.IDLE
        self._audio_playing_queue = queue.Queue()
        self.output_queue = queue.Queue()
        self.recognize_audio_queue = queue.Queue()
        self.waiting_for_audio = False
        t = threading.Thread(target=self.eventQueueThread, daemon=True)
        t.start()
        audio_thread = threading.Thread(target=self.audio_playing_thread, daemon=True)
        audio_thread.start()

        time.sleep(2) # Let the SynchronizeState finish

    def _setup_logging(self):
        logging.config.dictConfig({
            'version': 1,
            'formatters': {
                'default': {
                    'format': "%(asctime)-15s %(module)-15s %(process)d %(threadName)s %(levelname)-8s %(message)s"
                }
            },
            'handlers': {
                'stdout': {'class': 'logging.StreamHandler',
                        'formatter': 'default',
                        'level': "DEBUG",
                    },
            },
            'loggers': {
                'root': {'handlers': ['stdout'],
                        'level': 'DEBUG'
                    },
            }
        })
        return logging.getLogger("root")

    def audio_playing_thread(self):
        try:
            while True:
                read_audio = self._audio_playing_queue.get(block=True, timeout=None)
                self.speech_state = SpeechState.PLAYING
                try:
                    self.output_stream.write(read_audio)
                except:
                    print("cannot play audio")
                self.speech_state = SpeechState.IDLE
        finally:
            self.output_stream.stop_stream()
            self.output_stream.close()

    def play_audio(self, audio_file):
        self._audio_playing_queue.put(audio_file)

    def eventQueueThread(self):
        conn = hyper.HTTP20Connection('avs-alexa-na.amazon.com:443', force_proto="h2")
        # conn = hyper.HTTP20Connection(host=self.base_url, secure=True, force_proto="h2")
        alexa_tokens = self.get_alexa_tokens()
        def handle_downstream():
            directives_stream_id = conn.request('GET',
                                             '/v20160207/directives',
                                             headers={
                                                 'Authorization': 'Bearer %s' % alexa_tokens['access_token']})
            # self._log.info("Alexa: directives stream is %s", directives_stream_id)
            directives_stream = conn._get_stream(directives_stream_id)
            downchannel = HTTP20Downchannel(directives_stream.getheaders(), directives_stream)
            # self._log.info("Alexa: status=%s headers=%s", downchannel.status, downchannel.headers)
            ctype, pdict = cgi.parse_header(downchannel.headers['content-type'][0].decode('utf-8'))
            boundary = bytes("--{}".format(pdict['boundary']), 'utf-8')
            # self._log.info("Downstream boundary is %s", boundary)
            if downchannel.status != 200:
                self._log.warning(downchannel)
                raise ValueError("/directive requests returned {}".format(downchannel.status))
            return directives_stream, boundary

        directives_stream, downstream_boundary = handle_downstream()
        messageId = uuid.uuid4().hex
        self._send_event(
            {"header": {
                "namespace": "System",
                "name": "SynchronizeState",
                "messageId": messageId
            },
             "payload": {}
         }, expectedStatusCode=204)

        downstream_buffer = io.BytesIO()
        while True:
            #self._log.info("Waiting for event to send to AVS")
            #self._log.info("Connection socket can_read %s", conn._sock.can_read)
            try:
                event, attachment, expectedStatusCode, speakingFinishedEvent = self._eventQueue.get(timeout=0.25)
            except queue.Empty:
                event = None

            # TODO check that connection is still functioning and reestablish if needed

            while directives_stream.data or (conn._sock and conn._sock.can_read):
                # we want to avoid blocking if the data wasn't for stream directives_stream
                if conn._sock and conn._sock.can_read:
                    conn._recv_cb()
                while directives_stream.data:
                    framebytes = directives_stream._read_one_frame()
                    #self._log.info(framebytes.split(downstream_boundary))
                    self._read_response(framebytes, downstream_boundary, downstream_buffer)
                    _ = self.get_output()
                    _ = self.get_output()

            if event is None:
                continue
            metadata = {
                # "context": self._context(),
                "context": self.context_manager.get_context(),
                "event": event
            }
            # self._log.debug("Sending to AVS: \n%s", pprint.pformat(metadata))

            boundary = uuid.uuid4().hex
            json_part = bytes(u'--{}\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{}'.format(boundary, json.dumps(metadata).encode('utf-8')), 'utf-8')
            json_hdr = bytes(u'--{}\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.format(boundary), 'utf-8')
            end_part = bytes("\r\n--{}--".format(boundary), 'utf-8')
            

            headers = {':method': 'POST',
                       ':scheme': 'https',
                       ':path': '/v20160207/events',
                       'Authorization': 'Bearer %s' % self.get_alexa_tokens()['access_token'],
                       'Content-Type': 'multipart/form-data; boundary={}'.format(boundary)}
            with conn._write_lock:
                stream_id = conn.putrequest(headers[':method'], headers[':path'])
                default_headers = (':method', ':scheme', ':authority', ':path')
                for name, value in headers.items():
                    is_default = name in default_headers
                    conn.putheader(name, value, stream_id, replace=is_default)
                conn.endheaders(final=False, stream_id=stream_id)

            # self._log.info("Alexa: Making request using stream %s", stream_id)
            #print(json_part)
            conn.send(json_hdr, final=False, stream_id=stream_id)
            conn.send(json.dumps(metadata).encode('utf-8'), final=False, stream_id=stream_id)            
            
            if attachment:
                hdr = bytes(u'\r\n--{}\r\nContent-Disposition: form-data; name="{}"\r\nContent-Type: application/octet-stream\r\n\r\n{}'.format(boundary, attachment[0], json.dumps(metadata).encode('utf-8')), 'utf-8')
                conn.send(hdr, final=False, stream_id=stream_id)
                
                # send empty chunk to alexa
                empty_chunk = chr(0) * AVS_AUDIO_CHUNK_PREFERENCE
                empty_chunk = empty_chunk.encode('utf-8')
                start = time.time()
                while self.speech_state == SpeechState.PLAYING or self.waiting_for_audio:
                    conn.send(empty_chunk, final=False, stream_id=stream_id)
                    time.sleep(0.1)
                print("empty period:", time.time() - start)
                
                while True:
                    #self._log.info("Getting bytes from queue %s", attachment[1])
                    if isinstance(attachment[1], queue.Queue):
                        try:
                            chunk = attachment[1].get(block=True, timeout=1)
                        except queue.Empty as e:
                            chunk = ''
                    else:
                        # chunk = attachment[1].read(AVS_AUDIO_CHUNK_PREFERENCE)
                        chunk = attachment[1]
                    if speakingFinishedEvent and speakingFinishedEvent.is_set():
                        break
                    if chunk:
                        # self.play_audio(chunk)
                        conn.send(chunk, final=False, stream_id=stream_id)
                        break
                    elif speakingFinishedEvent is None:
                        break                     
            conn.send(end_part, final=True, stream_id=stream_id)
            self.recognize_event_state = RecognizeEventState.IDLE
            # self._log.info("Alexa: Made request using stream %s", stream_id)
            resp = conn.get_response(stream_id)
            # self._log.info("Alexa HTTP status code: %s", resp.status)
            # self._log.debug(resp.headers)
            # if expectedStatusCode and resp.status != expectedStatusCode:
            #     self._log.warning("AVS status code unexpected: %s", resp.status)
            #     self._log.warning(resp.headers)
            #     self._log.warning(resp.read())
            if resp.status == 200:
                self._read_response(resp)
            
    def _send_event(self, event, attachment=None, expectedStatusCode=None, speakingFinishedEvent=None):
        self._eventQueue.put((event, attachment, expectedStatusCode, speakingFinishedEvent))

    def get_output(self):
        output = self.output_queue.get()
        # print("output:", output)
        return output

    def Recognize(self, fhandle, dialogid=None, speaking_finished_event=None):
        messageId = self.generate_unique_id()
        dialogRequestId = dialogid or self.generate_unique_id()
        response = self._send_event(
            {"header": {
                "namespace": "SpeechRecognizer",
                "name": "Recognize",
                "messageId": messageId,
                "dialogRequestId": dialogRequestId
            },
             "payload": {
                 "profile": "CLOSE_TALK",
                 "format": "AUDIO_L16_RATE_16000_CHANNELS_1"}
         }, ('audio', fhandle), speakingFinishedEvent=speaking_finished_event)

    def start_stream(self, dialog_request_id):
        self.recognize_event_state = RecognizeEventState.PROCESSING
        self.waiting_for_audio = True
        self.Recognize(self.recognize_audio_queue, dialog_request_id)

    def send_audio_file(
        self, audio_file, dialog_request_id=None, 
        distance_profile=constants.CLOSE_TALK, audio_format=constants.PCM
    ):
        if self.recognize_event_state == RecognizeEventState.PROCESSING:
            self.recognize_audio_queue.put(audio_file)
            self.waiting_for_audio = False
        else:
            self.recognize_event_state = RecognizeEventState.PROCESSING
            dialog_request_id = dialog_request_id or self.generate_unique_id()
            self.Recognize(audio_file, dialog_request_id)

    def _read_response(self, response, boundary=None, buffer=None):
        # self._log.debug("_read_response(%s, %s)", response, boundary)
        if boundary:
            endboundary = boundary + b"--"
        else:
            ctype, pdict = cgi.parse_header(response.headers['content-type'][0].decode('utf-8'))
            boundary = bytes("--{}".format(pdict['boundary']), 'utf-8')
            endboundary = bytes("--{}--".format(pdict['boundary']), 'utf-8')    

        on_boundary = False
        in_header = False
        in_payload = False
        first_payload_block = False
        content_type = None
        content_id = None
        
        def iter_lines(response, delimiter=None):
            pending = None
            for chunk in response.read_chunked():
                #self._log.debug("Chunk size is {}".format(len(chunk)))
                if pending is not None:
                    chunk = pending + chunk
                if delimiter:
                    lines = chunk.split(delimiter)
                else:
                    lines = chunk.splitlines()

                if lines and lines[-1] and chunk and lines[-1][-1] == chunk[-1]:
                    pending = lines.pop()
                else:
                    pending = None

                for line in lines:
                    yield line

            if pending is not None:
                yield pending

        # cache them up to execute after we've downloaded any binary attachments
        # so that they have the content available
        directives = []
        if isinstance(response, bytes):
            buffer.seek(0)            
            lines = (buffer.read() + response).split(b"\r\n")
            buffer.flush()
        else:
            lines = iter_lines(response, delimiter=b"\r\n")
        for line in lines:
            # self._log.debug("iter_line is {}...".format(repr(line)[:60]))
            if line == boundary or line == endboundary:
                # self._log.debug("Newly on boundary")
                on_boundary = True
                if in_payload:
                    in_payload = False
                    if content_type == "application/json":
                        # self._log.info("Finished downloading JSON")                        
                        json_payload = json.loads(payload.getvalue().decode('utf-8'))
                        # self._log.debug("json payload: {}".format(json_payload))
                        if 'directive' in json_payload:
                            # print("Try to play audio")
                            # self._handleDirective(json_payload['directive'])
                            directives.append(json_payload['directive'])
                    else:
                        # self._log.info("Finished downloading {} which is {}".format(content_type, content_id))
                        payload.seek(0)
                        # strip < and >
                        self._content_cache[content_id[1:-1]] = payload
                        
                continue
            elif on_boundary:
                # self._log.debug("Now in header")                
                on_boundary = False
                in_header = True
            elif in_header and line == b"":
                # self._log.debug("Found end of header")
                in_header = False
                in_payload = True
                first_payload_block = True
                payload = io.BytesIO()
                continue

            if in_header:
                # self._log.debug("in header: {}".format(repr(line)))
                if len(line) > 1:
                    header, value = line.decode('utf-8').split(":", 1)
                    ctype, pdict = cgi.parse_header(value)
                    if header.lower() == "content-type":
                        content_type = ctype
                    if header.lower() == "content-id":
                        content_id = ctype

            if in_payload:
                # add back the bytes that our iter_lines consumed
                # self._log.info("Found %s bytes of %s %s, first_payload_block=%s",
                #                len(line), content_id, content_type, first_payload_block)
                if first_payload_block:
                    first_payload_block = False
                else:
                    payload.write(b"\r\n")
                # TODO write this to a queue.Queue in self._content_cache[content_id]
                # so that other threads can start to play it right away
                payload.write(line)

        if buffer is not None:
            if in_payload:
                # self._log.info("Didn't see an entire directive, buffering to put at top of next frame")
                buffer.write(payload.read())
            else:
                buffer.write(boundary)
                buffer.write(b"\r\n")
        
        self.output_queue.put(("Start", None))
        for directive in directives:
            # TODO do this when we get to the end of the JSON block
            # rather than wait for the entire HTTP payload, so we can
            # start acting on it right away - will require potential
            # waiting on audio data
            self._handleDirective(directive)
        self.output_queue.put(("End", None))

    def _handleDirective(self, directive):
        # self._log.info("Handling {}".format(pprint.pformat(directive)))
        namespace, name = directive['header']['namespace'], directive['header']['name']
        if (namespace, name) == ("SpeechSynthesizer", "Speak"):
            self._play_speech(directive['payload'])
        elif (namespace, name) == ("SpeechRecognizer", "ExpectSpeech"):
            self._expect_speech(directive['payload']['timeoutInMilliseconds'],
                               directive['header']['dialogRequestId'])
        else:
            self._log.info("Cannot handle the type of directive {} {}".format(namespace, name))

    def _play_speech(self, detail):
        # self._log.info("Play speech {}".format(detail))
        token = detail['token']
        if detail['url'].startswith("cid:"):
            contentfp = self._content_cache[detail['url'][4:]]
            read_result = contentfp.read()
            del self._content_cache[detail['url'][4:]]
            self.output_queue.put(("Speech", read_result))

    def _expect_speech(self, detail, dialogRequestId):
        # self._log.debug("Receive expected speech {}".format(detail))
        self.output_queue.put(("ExpectSpeech", dialogRequestId))

    def get_alexa_tokens(self):
        date_format = "%a %b %d %H:%M:%S %Y"

        alexa_tokens = json.loads(open(self._tokens_filename,'r').read())

        if 'access_token' in alexa_tokens:
            if 'expiry' in alexa_tokens:

                expiry = datetime.datetime.strptime(alexa_tokens['expiry'], date_format)
                # refresh 60 seconds early to avoid chance of using expired access_token
                if (datetime.datetime.utcnow() - expiry) > datetime.timedelta(seconds=60):
                    self._log.info("Refreshing access_token")
                else:
                    self._log.info("access_token should be OK, expires %s", expiry)                
                    return alexa_tokens

        payload = {'client_id': alexa_tokens['client_id'],
                   'client_secret': alexa_tokens['client_secret'],
                   'grant_type': 'refresh_token',
                   'refresh_token': alexa_tokens['refresh_token']}

        conn = hyper.HTTPConnection('api.amazon.com:443', secure=True, force_proto="h2")
        # conn = hyper.HTTPConnection('alexa.na.gateway.devices.a2z.com', secure=True, force_proto="h2")
        conn.request("POST", "/auth/o2/token",
                     headers={'Content-Type': "application/json"},
                     body=json.dumps(payload).encode('utf-8'))
        r = conn.get_response()        
        self._log.info(r.status)
        tokens = json.loads(r.read().decode('utf-8'))
        self._log.info(tokens)
        expiry_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=tokens['expires_in'])
        tokens['expiry'] = expiry_time.strftime(date_format)
        payload.update(tokens)
        open(self._tokens_filename,'w').write(json.dumps(payload))
        return payload

    def generate_unique_id(self):
        return str(uuid.uuid4())

    # def ping(self):
    #     print("ping ping ping ping ping")
    #     authentication_headers = self.authentication_manager.get_headers()
    #     stream_id = self.connection.request(
    #         'GET',
    #         '/ping',
    #         headers=authentication_headers,
    #     )
    #     return self.connection.get_response(stream_id)
