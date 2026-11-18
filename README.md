# VorteX AI

A local-first AI workspace: chat and code modes, live project preview, model
usage budgets shared with the VorteX AI website, optional VPS access over SSH,
and a desktop build for Windows, macOS and Linux with an in-app updater.

The whole workspace runs on your machine — the FastAPI server, the SQLite
database, your chats and your uploaded files never leave it. Only prompts and
attachments you submit are sent to the AI provider configured in the admin
dashboard.

## Requirements

- Python 3.12 or newer (3.13 works)
- Windows 10/11, macOS 11+ or a modern Linux desktop
- An AI provider endpoint and API key, added in the admin dashboard

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

`app.py` starts the local server on `http://127.0.0.1:8000` and opens the
workspace in a native window (Edge WebView2 on Windows, WebKit elsewhere).
Useful flags:

| Flag | What it does |
| --- | --- |
| `--browser` | open in your default browser instead of the app window |
| `--server-only` | run the server without a window |
| `--port 8123` | prefer a specific port (falls back to the next free one) |

Then create the first admin account:

```bash
# macOS/Linux
VORTEX_ADMIN_EMAIL=you@example.com VORTEX_ADMIN_PASSWORD='a-long-password' python app.py
```

```powershell
# Windows PowerShell
$env:VORTEX_ADMIN_EMAIL="you@example.com"; $env:VORTEX_ADMIN_PASSWORD="a-long-password"; python app.py
```

The admin dashboard lives at `/admin`; it is where you add AI models (name,
base URL, API key), plans and site settings.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `VORTEX_SECRET_KEY` | random key in the data folder | Session signing key. Set it to keep sessions across reinstalls. |
| `VORTEX_ADMIN_EMAIL` | *unset* | Bootstraps the first admin account together with the password. |
| `VORTEX_ADMIN_PASSWORD` | *unset* | Password for that first admin account. |
| `VORTEX_DATA_DIR` | `./data` (packaged app: user data folder) | Where the database, uploads and downloaded updates are stored. |
| `VORTEX_PORT` | `8000` | Preferred local port. |
| `VORTEX_UPDATE_URL` | `updates/latest.json` on GitHub | Release manifest the desktop app checks for updates. |
| `VORTEX_WEB_ORIGIN` | `http://127.0.0.1:8100` | Origin of the companion website for single sign-on. |
| `VORTEX_SHARED_DB` | `<data>/shared_usage.db` | Ledger shared with the website so tokens run out on both surfaces. |

No credentials are committed to this repository: the admin account only exists
when you set the two variables above, and the session key is generated on first
run.

## Desktop app and installers

`app.py` already runs as a desktop app. To produce distributable installers:

```bash
# Windows: app folder + NSIS setup wizard -> installers/
bash installer/windows/build.sh

# Linux: portable tarball with an interactive install.sh -> installers/
bash installer/linux/build_linux.sh

# macOS: .app + .dmg (must run on macOS) -> installers/
bash installer/macos/build_macos.sh
```

The installer sources live in `installer/`: `windows/vortex.nsi` (Terms of
Service and Privacy Policy pages, install location, desktop/Start-menu
shortcuts), `macos/` (dmg builder and the `Install VorteX AI.command`
installer), and `unix/install.sh` (shared macOS/Linux install flow).

## Updates

`updates/latest.json` is the release manifest. Point the app at your own copy:

```bash
VORTEX_UPDATE_URL=https://example.com/latest.json python app.py
```

The manifest lists the newest version, release notes and one download per
platform. When a newer version is published the workspace shows a popup with
the notes and an animated in-app download, plus a manual download link.

## Project layout

```
app/                 FastAPI app: routers, database, AI client, VPS, updates
app/templates/       Jinja templates (workspace, login, admin, pricing)
static/              CSS, JS and images served by the app
installer/           Packaging and installer sources for the three platforms
updates/latest.json  Update channel manifest
app.py               Desktop launcher (server + native window)
```

Runtime data (`data/`, `static/uploads/`) and build output (`build/`, `dist/`,
`installers/`) are generated and ignored by git.

## Security notes

- Chats, projects, settings and saved SSH credentials stay on this device; SSH
  passwords and private keys are never sent to the AI model.
- Tokens and credits are shared with the website through `shared_usage.db`.
- The optional SSH feature can run commands on a server you connect. Review
  what you ask the assistant to do, and give it a server you control.
- Run the app behind `127.0.0.1` as intended; do not expose the local server to
  the internet without adding authentication and TLS.

## License

No license file is included yet. Add one (for example MIT or Apache-2.0) before
publishing if you intend others to reuse this code.
