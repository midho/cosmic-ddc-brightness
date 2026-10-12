# Input Remapper and Vaydeer

Use Input Remapper's graphical editor to make the Vaydeer number keys send
F13, F14, and F15. Add the matching commands as COSMIC shortcuts. The default
top row lowers brightness, cycles the selected monitor, and raises brightness.

For example, the top row can be:

| Vaydeer button | Remapped key | COSMIC command |
|---|---|---|
| Top-left | `KEY_F13` | `cosmic-ddc-brightness down` |
| Top-middle | `KEY_F14` | `cosmic-ddc-brightness cycle-monitor` |
| Top-right | `KEY_F15` | `cosmic-ddc-brightness up` |

Optional GeekMagic layout: map the top-middle key to
`cosmic-ddc-brightness toggle-geekmagic`. When GeekMagic is configured, pressing
the two outer keys together cycles through all displays, each configured DDC
monitor, and GeekMagic. The service treats the opposite commands as one chord,
so it does not also change brightness.

For release-aware repeating inside Input Remapper, use these output macros.

Top-left:

```text
key(KEY_F13).if_tap(else=hold(key(KEY_F13).if_tap(timeout=100)),timeout=250)
```

Top-right:

```text
key(KEY_F15).if_tap(else=hold(key(KEY_F15).if_tap(timeout=100)),timeout=250)
```

Holding either outer button repeats its COSMIC shortcut. Solaar-style tools
that report both press and release can use the service's `press-*` and
`release-*` commands instead.

Test the preset, then turn on **Autoload** if you want it applied at login or
reconnect. Some Vaydeer keypads need a separate Linux activation step before
they send key events. That step is outside this project.
