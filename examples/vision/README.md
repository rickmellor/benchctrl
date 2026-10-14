# Label-loop specs

Ready-to-run `benchctrl.vision.labelloop` specs for the bench as wired on
2026-09-12 (camera on the SDG1032X's front panel, 40 ms exposure). The crop is
the sensor region around each channel's backlit Output key; re-measure it from
`GET /frame.jpg` if the camera moves. Set the channel to a harmless waveform
first (`sdg1032x_set_basic_wave(ch, wave_type="SINE", frequency_hz=1000,
amplitude_vpp=1.0)`, load HiZ, nothing on the BNC), then:

    python -m benchctrl.vision.labelloop examples/vision/sdg-out1.json ~/datasets/benchpi-sdg-out1 --sanity

or `vision_label_capture(spec, out_dir, sanity=True)` over MCP. Train and
deploy per `docs/vision.md` § Classifiers (`--out sdg-out1`).
