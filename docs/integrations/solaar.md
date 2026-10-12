# Solaar press and release rules

In Solaar's rule editor, connect the press and release events to these
commands:

| Event | Command |
|---|---|
| Brightness-up press | `cosmic-ddc-brightness press-up` |
| Brightness-up release | `cosmic-ddc-brightness release-up` |
| Brightness-down press | `cosmic-ddc-brightness press-down` |
| Brightness-down release | `cosmic-ddc-brightness release-down` |

Keep both release rules. A release stops repeating. The service's hold limit
also stops it if a release event goes missing.

Add these rules to your existing Solaar file. Replacing the whole file could
erase other keyboard or mouse rules.
