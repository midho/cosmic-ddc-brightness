# COSMIC keyboard shortcuts

Add shortcuts in **Settings → Keyboard → Keyboard shortcuts → Custom**.

For a key that sends one event per tap, add one of these commands:

| Action | Command |
|---|---|
| Brightness down | `cosmic-ddc-brightness down` |
| Select brightness target | `cosmic-ddc-brightness cycle-monitor` |
| Brightness up | `cosmic-ddc-brightness up` |
| Show current state | `cosmic-ddc-brightness show` |

Use the full path if COSMIC cannot find the command:

```text
/home/YOUR_NAME/.local/bin/cosmic-ddc-brightness up
```

COSMIC keeps custom shortcuts together. The installer leaves that group alone
to protect shortcuts you already made.
