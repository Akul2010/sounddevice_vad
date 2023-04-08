The purpose of this project is to come up with a more streamlined audio system
for a voice assistant. The goal is to demonstrate the system listening,
thinking and talking all at the same time through the use of multiple threads.

I wanted to try using the [sounddevice library](http://python-sounddevice.readthedocs.io/) rather than objects derived from
the audioengine classes in Naomi. The ALSA class doesn't work for mixing
streams, and the pyAudio class has trouble identifying devices. They are both
somewhat problematic and prone to stuttering. Sounddevice uses pyAudio and is
actually pretty similar to the AudioEngine objects Naomi has been using.

I'm still evaluating whether I can add sounddevice as a new option and
continue using the pyAudio and Alsa AudioEngine objects, or if this rewrite
requires retiring those options. If anyone has a preference, please let me
know.
