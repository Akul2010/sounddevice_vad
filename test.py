#!/usr/bin/env python
# -*- coding: utf-8 -*-
import audioop
import collections
import math
import re
import sounddevice as sd
import webrtcvad
import sys


DISPLAYWIDTH = 80
VAD_AGGRESSIVENESS = 3


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


class TestVAD:
    def __init__(self):
        # The WebRTC VAD only accepts 16-bit mono PCM audio, sampled at
        # 8000, 16000, 32000 or 48000 Hz. A frame must be either 10, 20,
        # or 30 ms in duration:
        self.input_samplerate = 16000
        self.input_channels = 1
        self.input_length = 0.03
        self.input_chunksize = int(self.input_samplerate * self.input_channels * self.input_length)
        self.distribution = {}
        self._timeout_frames = 10 # frames before change in state
        self.frames = collections.deque([], 30) # frame buffer
        self.recording_frames = []
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self._minimum_capture_frames = 30 # minimum number of frames to capture
        self._threshold = 30
        self.last_voice_frame = 0
        self.recording = False
        self._maxsnr = None
        self._minsnr = None

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
            while True:
                sd.sleep(30)

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
                println(
                    "Started recording",
                    scroll=True
                )
                self.recording = True
                # Include the previous 10 frames in the recording.
                self.recording_frames = list(self.frames)[-self._timeout_frames:]
                self.last_voice_frame = len(self.recording_frames)
        else:
            # We're recording
            self.recording_frames.append(frame)
            if(voice_detected):
                self.last_voice_frame = len(self.recording_frames)
            if(self.last_voice_frame < len(self.recording_frames) - self._timeout_frames):
                # We have waited past the timeout number of frames
                # so we believe the speaker has finished speaking.
                if(len(self.recording_frames) < self._minimum_capture_frames):
                    println(
                        " ".join([
                            "Recorded {:.2f} seconds, less than threshold",
                            "of {:.2f} seconds. Discarding"
                        ]).format(
                            len(self.recording_frames) * self.input_length,
                            self._minimum_capture_frames * self.input_length
                        ),
                        scroll=True
                    )
                else:
                    println(
                        "Recorded {:.2f} seconds".format(
                            len(self.recording_frames) * self.input_length
                        ),
                        scroll=True
                    )
                self.recording = False
                self.recording_frames = []
                self.last_voice_frame = 0

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
