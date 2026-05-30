"""Per-vendor instrument drivers.

Drivers are imported by full path:

    from benchctrl.drivers.otii_arc import OtiiArc
    from benchctrl.drivers.eastwood_qr10x import QR10x
    from benchctrl.drivers.rigol_dl3031a import RigolDL3031A

The framework intentionally does **not** re-export driver classes at
the top level (``benchctrl.OtiiArc``, ``benchctrl.QR10x``). Driver
identity is part of how you describe what your bench is doing, so
the full path is the canonical way to spell it. The top-level
``benchctrl`` namespace contains only framework primitives
(``Recording``, ``StandardChannel``, ``BenchError`` hierarchy).
"""
