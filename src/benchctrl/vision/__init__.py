"""Bench vision: a camera (and optionally an Axelera Metis NPU) as a device.

The vision device is split in two, and the split is the whole design:

* :py:mod:`benchctrl.vision.service` — the HTTP surface, **stdlib only**. It
  routes requests to two duck-typed collaborators, a *camera* and a *detector*,
  and knows nothing about pylon or the Axelera runtime. The production sidecar
  and the simulator both run this exact module, so the simulator cannot drift
  from the API the driver was written against.
* :py:mod:`benchctrl.vision.camera`, :py:mod:`benchctrl.vision.detector`,
  :py:mod:`benchctrl.vision.server` — the heavy half: pypylon, numpy, OpenCV,
  ``axelera.runtime``. Imported by nothing but the ``benchctrl-vision`` console
  script, and only importable where the ``[vision]`` extra (and the Axelera
  runtime) are installed. They ship with the sidecar container in
  ``deploy/vision/``.

The benchctrl *driver* for all this is :py:mod:`benchctrl.drivers.bench_vision`
— a stdlib HTTP client that talks to the service on loopback. That is what
makes the device portable across benchctrl's local / remote / sim modes: in
local mode the driver talks to a sidecar on the same host; in remote mode the
agent on the bench box opens the same driver against *its* loopback sidecar;
in sim mode :py:mod:`benchctrl.sim.vision` stands up the service with a
synthetic camera. Same driver, same tools, same config in every case.

Nothing in this package is imported by the core, the agent or the MCP server
unless something asks for the vision device.
"""
