#!/usr/bin/env python
# -*- coding: utf-8 -*-
import audioop
import collections
import contextlib
import functools
import json
import math
import os
import pathlib
import requests
import shutil
import sounddevice as sd
import soundfile as sf
import subprocess
import sys
import tempfile
import threading
import wave
import webrtcvad
import zipfile
from datetime import datetime
from tqdm.auto import tqdm
from vosk import Model, KaldiRecognizer
from pathlib import Path


DISPLAYWIDTH = 80
VAD_AGGRESSIVENESS = 3
VOSK_MODEL = os.path.expanduser("~/VOSK/vosk-model-en-us-0.22-lgraph")


def println(string, scroll=False):
    """
    Print to the screen, overwriting 
    """
    # check and see if string ends with a line feed
    # clear the current line
    columns = DISPLAYWIDTH - 1
    sys.stdout.write("{}{}\r".format(
        string, " " * (columns - len(string)))
    )
    if (scroll):
        sys.stdout.write("\n")
    sys.stdout.flush()


def mic_volume(*args, **kwargs):
    try:
        recording = kwargs['recording']
        snr = kwargs['snr']
        minsnr = kwargs['minsnr']
        maxsnr = kwargs['maxsnr']
        mean = kwargs['mean']
        threshold = kwargs['threshold']
    except KeyError:
        return
    displaywidth = DISPLAYWIDTH-5
    snrrange = maxsnr - minsnr
    if snrrange == 0:
        snrrange = 1  # to avoid divide by zero below

    feedback = ["+"] if recording else ["-"]
    feedback.extend(
        list("".join([
            "||",
            ("=" * int(displaywidth * ((snr - minsnr) / snrrange))),
            ("-" * int(displaywidth * ((maxsnr - snr) / snrrange))),
            "||"
        ]))
    )
    # insert markers for mean and threshold
    if (minsnr < mean < maxsnr):
        feedback[int(displaywidth * ((mean - minsnr) / snrrange))] = 'm'
    if (minsnr < threshold < maxsnr):
        feedback[int(displaywidth * ((threshold - minsnr) / snrrange))] = 't'
    println("".join(feedback))

def download(url, dest):
    
    path = pathlib.Path(dest).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    
    if os.path.isfile(dest):
        # check if the size is correct
        local_file_size = os.path.getsize(dest)
        r = requests.get(url, stream=True, allow_redirects=True)
        remote_file_size = int(r.headers.get('Content-Length', 0))
        r.close()
        if local_file_size < remote_file_size:
            # try to resume download
            resume_header = {'Range': f'bytes={local_file_size}-{remote_file_size}'}
            r = requests.get(
                url,
                headers=resume_header,
                stream=True,
                allow_redirects=True
            )
            desc = "(Unknown total file size)" if remote_file_size == 0 else ""
            if r.status_code != 206: # Partial Content
                r.raise_for_status()
                raise RuntimeError(f"Request to {url} returned status code {r.status_code}")
            r.raw.read = functools.partial(r.raw.read, decode_content=True)
            with tqdm.wrapattr(
                r.raw,
                "read",
                total=remote_file_size,
                initial=local_file_size,
                desc=desc
            ) as r_raw:
                with path.open("ab") as f:
                    shutil.copyfileobj(r_raw, f)
    else:
        r = requests.get(
            url,
            stream=True,
            allow_redirects=True
        )
        desc = "(Unknown total file size)" if remote_file_size == 0 else ""
        if r.status_code != 200: # Okay
            r.raise_for_status()
            raise RuntimeError(f"Request to {url} returned status code {r.status_code}")
        r.raw.read = functools.partial(r.raw.read, decode_content=True)
        with tqdm.wrapattr(
            r.raw,
            "read",
            total=remote_file_size,
            desc=desc
        ) as r_raw:
            with path.open("wb") as f:
                shutil.copyfileobj(r_raw, f)

# Context enabled open so we don't forget to close the file handle
# when hiding system stderr messages.
class hide_stderr:
    def __enter__(self):
        self.fd = os.open('/dev/null', os.O_WRONLY)
        self.std_err = os.dup(2)
        os.dup2(self.fd, 2)
        return self.fd

    def __exit__(self, *args, **kwargs):
        os.dup2(self.std_err, 2)
        os.close(self.fd)
        return True


class TestVAD:
    def __init__(self):
        self.Continue = True
        # sounddevice input device
        # The WebRTC VAD only accepts 16-bit mono PCM audio, sampled at
        # 8000, 16000, 32000 or 48000 Hz. A frame must be either 10, 20,
        # or 30 ms in duration:
        self.input_bits = 16
        self.input_samplerate = 16000
        self.input_channels = 1
        self.input_length = 0.03
        self.input_chunksize = int(self.input_samplerate * self.input_channels * self.input_length)
        # VAD
        self.distribution = {}
        self._timeout_frames = 10 # frames before change in state
        self.frames = collections.deque([], maxlen=30) # frame buffer
        self.recording_frames = []
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self._minimum_capture_frames = 30 # minimum number of frames to capture
        self._threshold = 30
        self.last_voice_frame = 0
        self.recording = False
        self._maxsnr = None
        self._minsnr = None
        # STT
        self.recordings_queue = collections.deque([], maxlen=10)
        self.stt_thread = None
        # TTS
        self.say_queue = collections.deque([], maxlen=10)
        self.tts_thread = None
        # Make sure that the vosk model exists and download it if not
        if not Path(VOSK_MODEL).is_dir():
            zip_file = f"{VOSK_MODEL}.zip"
            print("Downloading vosk model")
            download('https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip', zip_file)
            print("Unzipping model file")
            vosk_working_folder = pathlib.Path(VOSK_MODEL).parent
            with zipfile.ZipFile(zip_file, 'r') as z:
                z.extractall(vosk_working_folder)
        self.model = Model(VOSK_MODEL, 16000)
        self.rec = KaldiRecognizer(self.model, 16000)

    def listen(self):
        print(f"chunksize = {self.input_chunksize}")
        # Main loop is just recording chunks, running voice_detected, then adding to a buffer
        with sd.RawInputStream(
            blocksize=self.input_chunksize,
            dtype="int16",
            channels=self.input_channels,
            samplerate=self.input_samplerate,
            callback=self.audio_callback
        ):
            while self.Continue:
                sd.sleep(30)
        if (hasattr(self, "stt_thread") and hasattr(self.stt_thread, "is_alive") and self.stt_thread.is_alive()):
            self.stt_thread.join()
        if (hasattr(self, "tts_thread") and hasattr(self.tts_thread, "is_alive") and self.tts_thread.is_alive()):
            self.tts_thread.join()
        print()

    def audio_callback(self, indata, frames, time, status):
        """This is called (from a separate thread) for each audio block."""
        if status:
            print(status, file=sys.stderr)
        frame = bytes(indata)
        self.frames.append(frame)
        voice_detected = self.voice_detected(frame)
        if not self.recording:
            if(voice_detected):
                # Voice activity detected, start recording and use
                # the last 10 frames to start
                # println(
                #     "Started recording",
                #     scroll=True
                # )
                self.recording = True
                # Include the previous 10 frames in the recording.
                *frames, = self.frames
                self.recording_frames = frames[-self._timeout_frames:]
                self.last_voice_frame = len(self.recording_frames)
        else:
            # We're recording
            self.recording_frames.append(frame)
            if(voice_detected):
                self.last_voice_frame = len(self.recording_frames)
            if(self.last_voice_frame < (len(self.recording_frames) - self._timeout_frames*2)):
                # We have waited past the timeout number of frames
                # so we believe the speaker has finished speaking.
                if(len(self.recording_frames) > self._minimum_capture_frames):
                    # println(
                    #     "Recorded {:.2f} seconds".format(
                    #         len(self.recording_frames) * self.input_length
                    #     ),
                    #     scroll=True
                    # )
                    # put the audio in a queue and call the stt engine
                    self.recordings_queue.appendleft(self.recording_frames)
                    # println(f"Adding {len(self.recording_frames)} frames to queue", scroll=True)
                    if not (hasattr(self, "stt_thread") and hasattr(self.stt_thread, "is_alive") and self.stt_thread.is_alive()):
                        # start the thread
                        self.stt_thread = threading.Thread(
                            target=self.stt
                        )
                        self.stt_thread.start()
                self.frames.clear()
                self.recording = False
                self.recording_frames = []
                self.last_voice_frame = 0

    @contextlib.contextmanager
    def _write_frames_to_file(self, frames):
        with tempfile.NamedTemporaryFile(
            mode='w+b',
            suffix=".wav",
            prefix=datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ) as f:
            wav_fp = wave.open(f, 'wb')
            wav_fp.setnchannels(self.input_channels)
            wav_fp.setsampwidth(int(self.input_bits // 8))
            wav_fp.setframerate(self.input_samplerate)
            fragment = b''.join(frames)
            wav_fp.writeframes(fragment)
            wav_fp.close()
            f.seek(0)
            yield f

    def stt(self):
        while True:
            try:
                audio = self.recordings_queue.pop()
                # println(f"Popped {len(audio)} frames from queue", scroll=True)
                # println(f"type: {type(audio)}", scroll=True)
                with self._write_frames_to_file(audio) as f:
                    f.seek(44)
                    data = f.read()
                self.rec.AcceptWaveform(data)
                res = json.loads(self.rec.FinalResult())
                transcription = res['text']
                println(f"<< {transcription}", scroll=True)
                if "shut down" in transcription:
                    self.Continue = False
                if transcription.startswith("say "):
                    # start a speak thread
                    self.say_queue.appendleft("here is what you said to say")
                    self.say_queue.appendleft(transcription[4:])
                    if not (hasattr(self, "tts_thread") and hasattr(self.tts_thread, "is_alive") and self.tts_thread.is_alive()):
                        self.tts_thread = threading.Thread(
                            target=self.say,
                            args=('slt',)
                        )
                        self.tts_thread.start()
            except IndexError:
                break

    def say(self, voice="slt"):
        while True:
            try:
                phrase = self.say_queue.pop()
                println(f">> {phrase}", scroll=True)
                cmd = ['flite']
                cmd.extend(['-voice', voice])
                cmd.extend(['-t', phrase])
                with tempfile.NamedTemporaryFile(mode="w+b", suffix='.wav') as f:
                    cmd.append(f.name)
                    subprocess.call(cmd)
                    data, sr = sf.read(f)
                with hide_stderr():
                    sd.play(data, samplerate=sr)
                    sd.wait()
            except IndexError:
                break

    def voice_detected(self, indata):
        rms = audioop.rms(indata, 2)
        if rms > 0 and self._threshold > 0:
            snr = round(20.0 * math.log(rms / self._threshold, 10))
        else:
            snr = 0
        if snr in self.distribution:
            self.distribution[snr] += 1
        else:
            self.distribution[snr] = 1
        # calculate the mean and standard deviation
        sum1 = sum([
            value * (key ** 2) for key, value in self.distribution.items()
        ])
        items = sum([value for value in self.distribution.values()])
        if items > 1:
            # mean = sum( value * freq )/items
            mean = sum(
                [key * value for key, value in self.distribution.items()]
            ) / items
            stddev = math.sqrt((sum1 - (items * (mean ** 2))) / (items - 1))
            if stddev < 1:
                stddev = 1
            self._threshold = mean + (
                stddev
            )
            # Get a range for our display
            # We'll say that the range is mean+/-3*stddev
            if self._minsnr is None:
                self._minsnr = snr
            if self._maxsnr is None:
                self._maxsnr = snr
            self._maxsnr = max(
                snr,
                mean + 3 * stddev,
                self._maxsnr
            )
            self._minsnr = min(
                snr,
                mean - 3 * stddev,
                self._minsnr
            )
            # Loop through visualization plugins
            mic_volume(
                recording=self.recording,
                snr=snr,
                minsnr=self._minsnr,
                maxsnr=self._maxsnr,
                mean=mean,
                threshold=self._threshold
            )
        if(items > 100):
            # Every 50 samples (about 1-3 seconds), rescale,
            # allowing changes in the environment to be
            # recognized more quickly.
            self.distribution = {
                key: (
                    (value + 1) / 2
                ) for key, value in self.distribution.items() if value > 1
            }
        threshold = self._threshold
        # If we are already recording, reduce the threshold so as
        # the user's voice trails off, we continue to record.
        # Here I am setting it to the halfway point between threshold
        # and mean.
        if(self.recording):
            threshold = (mean + threshold) / 2
        if(snr < threshold):
            response = False
        else:
            if(self._vad.is_speech(indata, self.input_samplerate)):
                response = True
            else:
                response = False
        return response


def main():
    testvad=TestVAD()
    testvad.listen()

    
if __name__ == "__main__":
    print("Devices:")
    for device in sd.query_devices():
        try:
            if device['max_input_channels'] > 0:
                sd.check_input_settings(
                    device=device['index'],
                    channels=1,
                    dtype="int16",
                    samplerate=16000
                )
                print(f"\t{device['name']} is OK")
        except sd.PortAudioError as e:
            print(f"\t{device['name']} is not compatible")
            pass
    main()
