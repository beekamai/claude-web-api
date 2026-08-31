# OpenClaude integration patch

This package applies the OpenClaude changes required by `claude-web-api`:

- forwards the OpenClaude session ID and working directory to the API;
- publishes partial streaming text in fullscreen mode, including on Windows;
- coalesces repaint work while keeping the activity spinner visible;
- shows live activity and an explicitly estimated token count;
- enables OpenClaude's dedicated PowerShell tool on Windows so PowerShell
  variables are not accidentally expanded by an outer Bash process;
- keeps the query alive for 60 seconds after a shell timeout so the bounded
  persisted result can still reach the model;
- wakes Bash and PowerShell immediately after timeout backgrounding and
  releases the execution lease before result mapping and hooks.

It is pinned to upstream commit
`a3dc345f12d41b171cdb5cb74c1304b6cca483d8` from
`https://github.com/Gitlawb/openclaude.git`. The integration build is labeled
`0.25.0-main.a3dc345-claudeweb.3`. This is **not** an official OpenClaude
`v0.25.0` release.

## Install

Requirements: Git, Node.js 22+, npm, Bun/Bunx, and a checkout at the exact
commit above. On Windows, use PowerShell 7; elevation may be required when the
global npm root is under `Program Files`.

```powershell
git clone https://github.com/Gitlawb/openclaude.git
git -C .\openclaude checkout a3dc345f12d41b171cdb5cb74c1304b6cca483d8

pwsh -File .\Install-OpenClaudePatch.ps1 `
  -Mode Check `
  -SourcePath .\openclaude

pwsh -File .\Install-OpenClaudePatch.ps1 `
  -Mode Install `
  -SourcePath .\openclaude
```

`Check` is read-only. `Install` verifies the commit and every target file,
applies the patch (or recognizes that it is already applied), installs locked
dependencies, runs targeted tests, type-checks, and builds. It then creates a
custom-version tarball with `npm pack` and installs that tarball with
`npm install -g`.

The settings overlay enables fullscreen streaming and disables the updater,
title probe, and non-streaming fallback. The complete pre-install
`~/.openclaude/settings.json` is saved byte-for-byte before it is changed.

Backups are stored under:

```text
~/.openclaude/patch-backups/<timestamp>-0.25.0-main.a3dc345-claudeweb.3/
```

Each backup contains a manifest, the previous global npm package as a tarball,
an explicit copy of its `dist/cli.mjs`, the previous settings file when one
existed, and the newly installed tarball.

## Roll back

Restore the newest backup:

```powershell
pwsh -File .\Install-OpenClaudePatch.ps1 -Mode Rollback
```

Restore a specific backup:

```powershell
pwsh -File .\Install-OpenClaudePatch.ps1 `
  -Mode Rollback `
  -BackupId 20260726T180000Z-0.25.0-main.a3dc345-claudeweb.3
```

Rollback reinstalls the saved npm tarball and restores the exact settings
preimage. If the installer originally applied the source patch, it also
reverses that patch when the source still matches exactly. Pass
`-KeepSourcePatch` to leave the checkout patched.

No account profiles, API configuration, browser data, logs, or live runtime
state are included in this package.
