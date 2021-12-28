import io
import os
import time

from pydub import AudioSegment

from alexa_client import AlexaClient

client = AlexaClient(
    client_id='amzn1.application-oa2-client.bedc7e8a1d4f402e9247b981373f89ff',
    secret='ec49c9aaab98c009e6b7cc164ea89d7a66b09274816da83a3f12154bec1e6441',
    refresh_token='Atzr|IwEBIDkbdOzMypD5flLtYRoUzByDAAh1QJoXJRTcM6f9GSKAgFhQtlCn8fTv10AZQPkC7e_xIumFx21zSLCYYqM18fctAkrNfum7xEo-kVlqjm5mm8YBRkiYInFz-zPmyi9KxSG6kN3Bei0YXj2y02jV7qUfOo-1pOxNHHcJHsWuWKo-Jg4hdbLuNcHxGFrO-Jz5cOtAb2kvF1UG-13naYv86O8OT5Rq4i9x6QAPDTsa0M2Abk80u7VHOGpTm9LflZaR17-mB-erEsmrrmw5GbuEMVnOwwSI3dOyY6ZVwiTQ4XNhCqgi3WnMbCzfWOldAaipJtTRty0dgg4Ho98-XgsmE1r_ZB95qz5UPPJBPITjGv22vHM1TrPH4xd58H-htS0XEdIYuJjEkDgK5JdfXGXAzu2Fo4yngK3jcz42p9Fr0n7xLYKOGKyePgYQmhDdPoP-ncw',
)
print("start alexa")
time.sleep(2)

with open('./resources/what_is_the_weather.wav', 'rb') as f:
    print("Successfully open file")
    client.send_audio_file(f)
    time.sleep(2)
    
    # This is the result from SynchronizeState, ignore
    # output, data = client.get_output()
    # print("output from first queue:", output)
    duration = 0
    while True:
        output, data = client.get_output()
        if output == "Start":
            # output, data = client.get_output()
            continue
        elif output == "Speech":
            # print("speech data length:", len(data))
            try:
                reply = AudioSegment.from_mp3(io.BytesIO(data))
                try:
                    duration = reply.duration_seconds
                    print("Audio duration:", duration)
                except:
                    print("cannot get duration")
                reply = reply.set_frame_rate(16000)
                reply = reply.set_channels(1)
                client.play_audio(reply.raw_data)
                # time.sleep(5)
            except:
                print("cannot play")
            # output, data = client.get_output()
        elif output == "ExpectSpeech":
            client.start_stream(data)
            f_expect_speech = open("./resources/palo_alto.wav", "rb")
            client.send_audio_file(f_expect_speech.read(), data)
            f_expect_speech.close()
            # output, data = client.get_output()
        elif output == "End":
            print("ready to end")
            time.sleep(duration)
            break

    duration = 0
    while True:
        output, data = client.get_output()
        if output == "Start":
            continue
        elif output == "Speech":
            print("speech data length:", len(data))
            try:
                reply = AudioSegment.from_mp3(io.BytesIO(data))
                reply = reply.set_frame_rate(16000)
                reply = reply.set_channels(1)
                duration = reply.duration_seconds
                client.play_audio(reply.raw_data)
            except:
                print("cannot play")
        elif output == "End":
            time.sleep(duration)
            break

    # real result
    # output, data = client.get_output() 
    # print("output from queue:", output)
    # while output != "End":
    #     if output == "Start":
    #         output, data = client.get_output()
    #         continue
    #     elif output == "Speech":
    #         try:
    #             reply = AudioSegment.from_mp3(io.BytesIO(data))
    #             reply = reply.set_frame_rate(44100)
    #             reply = reply.set_channels(1)
    #             client.play_audio(reply.raw_data)
    #         except:
    #             print("cannot play")
    #         output, data = client.get_output()

    # directives = client.get_output()
    # print("directives:", directives)
    # if directives:
    #     for i, directive in enumerate(directives):
    #         print(i, directive)
    #         if directive.name in ['Speak', 'Play']:
    #             # f_output = open(f'./output_{i}.mp3', 'wb')
    #             # f_output.write(directive.audio_attachment)
    #             # f_output.close()
    #             try:
    #                 reply = AudioSegment.from_mp3(io.BytesIO(directive.audio_attachment))
    #                 reply = reply.set_frame_rate(44100)
    #                 reply = reply.set_channels(1)
    #                 client.play_audio(reply.raw_data)
    #             except:
    #                 print("cannot play")
  
os._exit(1) 

