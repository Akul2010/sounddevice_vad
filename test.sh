#!/bin/sh

pactl unload-module module-echo-cancel
pactl load-module module-echo-cancel aec_method=webrtc source_name=ec.source sink_name=ec.sink
pacmd set-default-source ec.source
pacmd set-default-sink ec.sink
./test.py
pactl unload-module module-echo-cancel
