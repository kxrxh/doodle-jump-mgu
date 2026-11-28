# Hardware findings and recovery behavior

## Evidence from 28 November 2026

Tests used an iDrive 7 MGU running `P18_23w44.2-1-3`, app 0.14.5,
mgu-render 0.6.26 and GPU worker 2.1.1, followed by a worker 2.1.2 trial.

- The installed app's host SHA-256 matched the published release. Steering and
  shooting worked. The initial CPU fallback ran at about 23 FPS.
- VIDEO recovered to roughly 58–61 renderer FPS and stayed connected for more
  than two minutes in the captured log.
- Native binding failed with `layer_not_ready`. The renderer socket's peer
  credentials reported PID 1, while the compositor surface and the process
  holding the listening socket belonged to the renderer service, PID 12858.
- Resolving the inherited listening socket allowed native binding. The owner
  subsequently observed 56–60 native presented FPS during settled gameplay.
- A process restart also exposed blank Native/0 FPS, a five-second frame timeout,
  CPU fallback and renderer reconnects. The combined fixes in 0.14.6 have not yet
  had a complete cold-start test on the car.
- SDK `INVALID_MODELID` errors and disabled-analytics errors occurred in the same
  log. No causal link to the native frame timeout was established.

## Changes supported by the evidence

The native worker accepts a compositor surface only when its parent layer matches
the app's placement challenge and its process either matches the socket peer or
holds that exact listening socket. The socket-activation regression fixture puts
the socket creator and service in different processes, and checks rejection of a
foreign surface PID. Rectangle, parent-layer and ambiguity checks remain active.

The host switches its drawing mode to Native only after a successful native
presentation acknowledgement. An unpresented acknowledgement retains the last
completed CPU/VIDEO picture instead of removing it from the Store canvas.

A native frame timeout disables further native attempts for the same Store
renderer identity, generation and placement. Worker reconnection can use VIDEO
(or CPU when VIDEO is unavailable). A changed Store renderer can try native
again. This avoids repeatedly challenging the same failed surface after each
worker reconnect.

Native binding has its own bounded ten-second worker stage. The three-second
Store prepare and restore stages remain separate. This accounts for several
750 ms Wayland roundtrips and initial EGL setup, without extending frame timeouts.
Signal shutdown lets the host exit and has a two-second forced-kill fallback.

## Research and limits

Linux documents that `SO_PEERCRED` captures credentials at connection/listen
creation time, explaining PID 1 for a systemd-created listener:
[unix(7)](https://man7.org/linux/man-pages/man7/unix.7.html).

Wayland frame callbacks are redraw scheduling notifications. A compositor may
withhold them for an obscured or off-screen surface:
[wl_surface.frame](https://pq.pages.freedesktop.org/wayland.freedesktop.org/docs/html/apa.html#protocol-spec-wl_surface-request-frame).
This is relevant to the observed timeout, but does not prove its exact cause.
The current data does not justify changing EGL backends, bypassing visibility
checks, or attributing the timeout to missing initial compositor events.

Before marking cold startup fully verified, launch normally after a head-unit
restart, check first presentation and FPS, hide and reopen the app, and repeat
after changing themes. Check that a timed-out native attempt recovers without
repeated flashing, that saves survive, and that no test service override remains.
The temporary on-car worker override used during diagnosis clears on reboot.
Wireless SSH was not established; the Wi-Fi address resolved but TCP probes
timed out. No wireless firewall change was made.
