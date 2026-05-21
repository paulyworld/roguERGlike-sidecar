"""BLE layer.

Each supported device type is a *profile* (FTMS bike trainer, HRS heart-rate
sensor, ...) that knows its BLE service UUID, the characteristic to subscribe
to, and how to decode notification payloads into our event schema. A
``BleSource`` wraps one profile + one connection and feeds the shared
``EventBus``; multiple sources can run concurrently (bike + chest strap).
"""
