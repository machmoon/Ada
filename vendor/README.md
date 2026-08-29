# Vendored third-party code

Code in this directory was **not written for Silkscreen**. It is included as a
working reference for the guided-cursor overlay described in `DEVPOST.md`,
which is not built yet. Nothing in `engine/`, `service/`, or `scripts/`
imports from here, and nothing here runs in CI.

## mudriknow/

| | |
|---|---|
| Upstream | https://github.com/abdallahmagdy15/mudriknow |
| Revision | `ad58192` |
| License | MIT — see `mudriknow/LICENSE`, retained unmodified |
| Stack | Electron, TypeScript, React, robotjs, Windows UI Automation |
| Platform | Windows only (macOS/Linux on the upstream roadmap) |

Why it is here: its auto-guide mode is the feature Silkscreen wants — an on-screen
pointer that lands on each target with a speech bubble and walks a user through a
multi-step task. The technique is to read the active window's accessibility tree
via Windows UI Automation, pair it with a screenshot carrying a coordinate grid,
and have the model emit guide actions that position the overlay. Applying that to
KiCad is how "add a net class" becomes something you watch once and can then do
yourself.

The MIT licence permits use, modification and redistribution provided the
copyright notice is kept. It has been kept. Upstream copyright remains with the
MudrikNow contributors.
