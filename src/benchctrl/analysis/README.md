# `benchctrl.analysis`

Intentionally empty at v1.0.

This package is reserved for v1.x and later analytics work that spans
drivers and operates on captured data: power profiling, multi-recording
comparison, anomaly detection, custom statistics, etc.

Battery-specific analytics already live in `benchctrl.battery`
(capacity / OCV-ESR interpolation, life estimation) since they were
the first to land. As more cross-driver analysis features appear, this
is where they go.
