#!/usr/bin/env python
# -*- coding: utf-8 -*-
# The purpose of this program is simply to play a wave file and record
# at the same time.
import numpy as np
import queue
import sounddevice as sd
import soundfile as sf
import threading


play_queue = queue.Queue()
record_queue = queue.Queue()
test_file = "Hello_there.wav"


def callback(indata, outdata, frames, time, status):
    if status:
        print(f"Status: {status}")
    record_queue.put(indata.copy())
    try:
        outdata[:] = play_queue.get_nowait()
    except queue.Empty as e:
        print('Buffer is empty')
        raise sd.CallbackAbort from e
    print(f"Outdata shape is {outdata.shape}")

def main():
    event = threading.Event()
    data, sr = sf.read(test_file, dtype='float32')
    input_bits = 16
    samplerate = sr
    l = data.shape[0]
    print(f"audio data shape: {data.shape}")    
    l = data.shape[0]
    print(f"audio data shape: {data.shape}")    
    length = 0.15
    try:
        channels = data.shape[1]
    except IndexError:
        channels = 1
        data = data.reshape((l, channels))
    print(f"channels: {channels}")
    chunksize = int(samplerate * channels * length)
    print(f"chunksize={chunksize}")

    
    print(f"audio data shape: {data.shape}")
    l = data.shape[0]
    print(f"Adding {chunksize - l%chunksize}")
    data = np.append(data, np.zeros(chunksize * 3 - l%chunksize, dtype='float32').reshape((chunksize *3 - l%chunksize, 1))).reshape((l+chunksize * 3 - l%chunksize, channels))
    # data = np.append(data, np.zeros(chunksize - l%chunksize, dtype='float32').reshape((chunksize - l%chunksize, 1))).reshape((l+chunksize - l%chunksize, channels))
    l = data.shape[0]
    print(f"audio data shape: {data.shape}")
    
    print(f"Splitting into {l/chunksize} chunks")
    splits = np.split(data, l//chunksize)
    for chunk in splits:
        print("adding chunk")
        play_queue.put(chunk)
    print("Opening stream")
    with sd.Stream(
        samplerate=samplerate,
        blocksize=chunksize,
        channels=1,
        callback=callback,
        finished_callback=event.set
    ):
        event.wait()
    # Convert the record_queue to a wav file
    print("Writing echo file")
    data = np.empty((0, 1), dtype='float32')
    while True:
        try:
            data=np.concatenate((data, record_queue.get_nowait()))
        except queue.Empty as e:
            print('Record buffer is empty')
            break
    print(f"data shape is {data.shape}")
    sf.write("Hello_there_echo.wav", data, samplerate)
        
    print("Program complete")

if __name__ == "__main__":
    print("Devices:")
    for device in sd.query_devices():
        try:
            if device['max_input_channels'] > 0:
                sd.check_input_settings(
                    device=device['index'],
                    channels=1,
                    dtype="float32",
                    samplerate=16000
                )
                print(f"\t{device['name']} is OK")
        except sd.PortAudioError as e:
            print(f"\t{device['name']} is not compatible")
    main()
