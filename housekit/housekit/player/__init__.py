"""HouseKit Player: a standalone cue-list player for the folders HouseKit builds.

decode  ffmpeg -> PCM, plus where a track's sound starts and stops and how it ends
engine  the mixer: voices, fades, sample-accurate scheduled starts, ducking, master, meter
cues    cue lists built from a gig / playlist folder, saved as cues.json next to the files
show    the transport: GO, follow-ons, crossfades, pads
"""
