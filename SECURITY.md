# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/pravin-python/latkan-desktop/security/advisories/new)
instead. Include the OS, the Latkan version (`latkan --version`) and steps to reproduce.

You can expect an acknowledgement within a few days.

## What Latkan does and doesn't do

- It makes **no network connections**. The only socket is a local one (`QLocalServer`) used to
  keep a single instance running.
- It reads only images you pick yourself and stores copies in its own settings folder.
- "Start at login" writes one per-user entry: a `HKCU\...\Run` value on Windows, a LaunchAgent on
  macOS, or an XDG autostart `.desktop` file on Linux. Turning the option off removes it.
