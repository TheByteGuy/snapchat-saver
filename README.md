# snapchat-saver

A self-hosted container that keeps your Snapchat streaks alive. Once a day it:

1. fetches a random fun fact,
2. renders it onto a colourful gradient image ("Fact of the day"),
3. feeds that image to a real Chromium browser as a fake webcam, and
4. sends it as a **real Snap** through Snapchat Web to the friends you picked,
   so it counts towards your streaks.

It runs as a single Docker container with a password-protected web dashboard
where you log in to Snapchat (through an embedded view of the container's
browser), pick recipients, set the send time, preview the image and see the
history. Failures (and, if you want, successes) are posted to a Discord webhook.

It is built for **HexOS / TrueNAS SCALE** ("Install via YAML" custom app), but
runs on any Docker host.

> [!WARNING]
> **Read this before using it.**
> This project automates Snapchat Web unofficially. Automating your account is
> **against Snapchat's Terms of Service**, and Snapchat may lock, suspend or ban
> the account. Snapchat also changes its website without notice; when it does,
> parts of the automation can break until the selectors are updated (see
> [Troubleshooting](#troubleshooting)). Use it at your own risk, on your own
> account, and do not rely on it as the only thing keeping an important
> streak alive.

> [!CAUTION]
> **Security:** the dashboard gives full control of a logged-in Snapchat
> session. Keep it on your LAN or reach it through a VPN (Tailscale,
> WireGuard, ...). **Do not port-forward it to the internet**, and use a long
> `DASHBOARD_PASSWORD`.

---

## Contents

- [Install on HexOS / TrueNAS SCALE](#install-on-hexos--truenas-scale)
- [First-run walkthrough](#first-run-walkthrough)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Selector overrides](#selector-overrides)
- [Updating](#updating)
- [Local development](#local-development)

---

## Install on HexOS / TrueNAS SCALE

HexOS runs on TrueNAS SCALE; the steps below use the TrueNAS web UI, which
HexOS exposes (in HexOS: open the TrueNAS UI from the HexOS settings, or browse
to `http://<nas-ip>` / the TrueNAS port your install uses).

### 1. Create a dataset for the app's data

1. **Datasets** → select your pool (or its `apps` dataset) → **Add Dataset**.
2. Name it `snapchat-saver`, e.g. giving `/mnt/tank/apps/snapchat-saver`
   (`tank` is your pool name). The "Apps" dataset preset is a good choice.
3. Permissions: the container runs as uid/gid **568** (the TrueNAS `apps`
   user) by default and will `chown` the folder on start. If you prefer
   another owner, set `PUID`/`PGID` in the YAML to that user's uid/gid.

This dataset holds the logged-in browser profile, settings, images and
history. Keep it; deleting it logs you out of Snapchat.

### 2. Make sure the image can be pulled

The image is published to the GitHub Container Registry as
`ghcr.io/thebyteguy/snapchat-saver:latest` by the workflow in
`.github/workflows/docker.yml`.

- **Easiest:** make the package public once. On GitHub open your profile →
  **Packages** → `snapchat-saver` → **Package settings** → *Danger zone* →
  **Change visibility** → *Public*. No credentials are needed afterwards.
- **Keep it private:** create a GitHub *personal access token (classic)*
  with only the `read:packages` scope, then in TrueNAS go to **Apps** →
  **Configuration** (or ⋮ menu) → **Manage Container Images / Docker
  Registries** → **Add**, registry `ghcr.io`, username = your GitHub user,
  password = the token. (The menu name differs slightly between TrueNAS
  releases.)

If you forked the repository, replace `thebyteguy` in the image name with
your GitHub user name (lowercase).

### 3. Install via YAML

1. **Apps** → **Discover Apps** → ⋮ (top right) → **Install via YAML**.
2. Name: `snapchat-saver`.
3. Paste the contents of [`docker-compose.yml`](docker-compose.yml) and edit:
   - `DASHBOARD_PASSWORD` – a long, unique password,
   - `TZ` – your time zone (e.g. `America/New_York`),
   - the volume's left side – the dataset path from step 1, e.g.
     `/mnt/tank/apps/snapchat-saver:/data`,
   - the host port `30880` if something else already uses it,
   - `PUID`/`PGID` if your dataset is not owned by 568:568.
4. **Save**. TrueNAS pulls the image (about 2 GB, it contains a full
   Chromium) and starts it. Logs are under **Apps** → `snapchat-saver` →
   **Logs**.

### 4. Open the dashboard

Browse to `http://<nas-ip>:30880` and log in with `DASHBOARD_PASSWORD`.

---

## First-run walkthrough

1. **Log in to the dashboard** at `http://<nas-ip>:30880`.
2. **Open Snapchat login.** In the *Snapchat* section click the button that
   opens the Snapchat login; the embedded browser view shows the real
   Chromium running inside the container.
3. **Log in** with your Snapchat username and password in that view, and
   approve the login on your phone if Snapchat asks (it usually does for a
   new device). Solve any captcha the same way.
4. Click **Check login**. The status should change to *logged in*.
5. **Load my friends** and tick the recipients that should get the daily Snap
   (or add a name manually exactly as it appears in your chat list). Leave
   friends you do not want spammed unticked.
6. **Schedule:** set the send time and your time zone, and make sure the
   schedule is enabled. Pick a time well before midnight so a failed attempt
   can be retried (the app retries up to three times, 15 minutes apart).
7. **Discord:** paste a Discord webhook URL (Discord channel → *Edit
   Channel* → *Integrations* → *Webhooks* → *New Webhook* → *Copy URL*), choose
   whether successful sends should also be posted, and click **Send test
   alert**.
8. **Generate preview** to see today's image.
9. **Send test snap now** to one friend (or yourself via a second account) and
   check on your phone that it arrived and looks right.

From now on it sends once a day by itself. The dashboard shows the next run
and the history of sends.

---

## Configuration

Most settings (recipients, time, time zone, Discord) are set in the
dashboard and stored in `/data/app.db`. The container itself is configured
with environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `DASHBOARD_PASSWORD` | `changeme` | Password for the web dashboard. **Change it.** |
| `TZ` | `UTC` | Container time zone (IANA name). Also the default for the schedule's time zone. |
| `PUID` | `568` | User id all processes run as; `/data` is chowned to it on start. 568 = TrueNAS `apps`. |
| `PGID` | `568` | Group id all processes run as. |
| `DATA_DIR` | `/data` | Where all state lives. Leave as is in Docker; mount a volume there instead. |
| `SNAP_HEADLESS` | `false` | Run Chromium headless. The embedded login view then shows nothing, so only use it after logging in, or for development. |
| `SNAP_MIRROR_FEED` | `false` | Flip the fake-camera image horizontally before it is fed to Chromium. Turn it on if received Snaps show the text mirrored. |
| `SNAP_CHROMIUM_ARGS` | *(empty)* | Extra Chromium flags, space separated, appended to the launch arguments (e.g. `--disable-gpu`). |
| `SNAP_CHROMIUM_PATH` | *(bundled)* | Path to a different Chromium/Chrome executable instead of the Playwright-bundled one. |
| `SNAP_WEB_URL` | `https://www.snapchat.com/web` | Snapchat Web start URL. Only change it if Snapchat moves the web client. |
| `SNAP_FEED_LAYOUT` | `landscape` | Shape of the fake-camera frame. The default centres the image in a 1920x1080 frame so it survives Snapchat's portrait crop; `portrait` feeds the image as is. |
| `SNAP_FEED_SCALE` | `0.96` | How much of the frame height the image fills in the landscape layout. Lower it if the edges of the image get cropped. |
| `SNAP_STEP_TIMEOUT` | `20` | Seconds to wait for each step of the send flow. |

Ports: the container listens on **8080** (dashboard, including the embedded
browser view over a WebSocket). VNC (5900) is bound to `127.0.0.1` inside the
container and is never exposed.

Files in `/data`:

| Path | Contents |
| --- | --- |
| `chromium-profile/` | The logged-in Chromium profile (cookies, local storage). |
| `app.db` | Settings, recipients, history, used facts. |
| `images/` | Generated images (`preview.png` and one per send). |
| `camera/` | The fake-webcam feed currently given to Chromium. |
| `debug/` | Screenshots + HTML saved whenever a browser step fails. |
| `secret_key` | Key that signs dashboard sessions (generated on first start). |
| `selectors.json` | Optional selector overrides (see below). |

### Users, permissions and the Chromium sandbox

The container starts as root only to create a user for `PUID`/`PGID`, fix the
ownership of `/data` and remove stale lock files; supervisord then starts
Xvfb, the window manager, x11vnc and the dashboard (and therefore Chromium)
as that **non-root** user. Files written to the dataset are owned by
`PUID:PGID`, which is what TrueNAS expects for app datasets.

Chromium is launched by Playwright with Playwright's defaults, which turn off
Chromium's own internal sandbox; the container is the isolation boundary.
This works in TrueNAS SCALE's default container security context – no extra
capabilities, `privileged` or seccomp changes are needed. (Chromium's
namespace sandbox would need `unshare`/`clone` with new user namespaces,
which Docker's default seccomp profile blocks; if you want to enable it
anyway you must run the container with a seccomp profile that allows those
syscalls, e.g. Playwright's documented `seccomp_profile.json`.)

If the dataset is an SMB/NFS share or uses ACLs that root inside the
container cannot `chown`, the log shows a warning; then give uid/gid
`PUID`/`PGID` write access yourself (TrueNAS: dataset → **Edit
Permissions**).

Running the container with `user:` in compose is also supported; then no
`chown` happens and `/data` must already be writable by that user.

---

## How it works

```
┌──────────────────────────── container ────────────────────────────┐
│ tini → supervisord                                                │
│   ├─ Xvfb :99 (virtual 1280x900 screen)                           │
│   ├─ openbox (keeps the browser window maximised)                 │
│   ├─ x11vnc on 127.0.0.1:5900                                     │
│   └─ uvicorn app.main:app :8080                                   │
│        ├─ dashboard + noVNC page, WebSocket bridge → x11vnc       │
│        ├─ scheduler (daily job, retries)                          │
│        └─ Playwright → Chromium (persistent profile in /data)     │
└───────────────────────────────────────────────────────────────────┘
```

- **Facts:** fetched from the free Useless Facts API
  (`uselessfacts.jsph.pl`). Facts already used are skipped. If the API is
  down, a bundled list of 365 facts is used, so a Snap is still sent.
- **Image:** rendered with Pillow as a 1080×1920 PNG with a random
  multi-stop gradient, a "Fact of the day" header with the date, and the
  fact auto-sized to fit.
- **Fake webcam:** Chromium is started with its fake-media-capture flags
  pointing at a fixed feed file in `/data/camera`. Before sending, the
  day's image is written to that file, so Snapchat Web's camera "sees" the
  image.
- **Real Snap:** Playwright drives Snapchat Web like a person would: open the
  camera, take the Snap, choose the recipients, send. Because it is a real
  camera Snap (not a chat image), it counts for streaks.
- **Login session:** the browser profile is persistent, so you log in once.
  If Snapchat logs the session out, the daily run fails and you get a
  Discord alert.
- **Embedded browser view:** the dashboard serves noVNC and bridges its
  WebSocket to x11vnc inside the container, all behind the dashboard login
  on the single port 8080.

---

## Troubleshooting

**Where are the logs?** TrueNAS: **Apps** → `snapchat-saver` → **Logs**.
Docker: `docker logs -f snapchat-saver`. All processes log there.

**"Not logged in" / Discord says the session is logged out.** Open the
dashboard, open the Snapchat login in the embedded view, log in again and
approve on your phone, then click **Check login**. Snapchat occasionally
expires web sessions, especially after password changes or "log out of all
devices".

**A step fails after Snapchat changed their site.** Every failed browser
step saves a screenshot and the page HTML to `/data/debug/` (on the NAS:
`<your dataset>/debug/`). Look at the newest files to see where it got
stuck. Then either update the image (a fix may already be published) or
override the failing selector in `/data/selectors.json` (see
[Selector overrides](#selector-overrides)) and use **Send test snap now** to
check.

**The received Snap is mirrored or cropped.** If the text reads backwards,
set `SNAP_MIRROR_FEED=true` (or back to `false` if it was on) and redeploy.
Snapchat Web crops the camera feed to the phone-like frame; the image is
designed with safe margins, so slight cropping at the edges is expected.

**The embedded browser view is black.** Wait a few seconds after the
container starts, then reload. If `SNAP_HEADLESS=true` is set there is
nothing to show; unset it to log in.

**Container keeps restarting / app is "unhealthy".** Check the logs. A
common cause is `/data` not being writable by `PUID`/`PGID` (the entrypoint
prints a warning). If a supervised program keeps failing to start, the
container deliberately exits so TrueNAS/Docker restarts it cleanly.

**"Profile in use" / browser will not start after a crash.** The entrypoint
removes Chromium's stale `Singleton*` lock files on every start, so a
restart of the app fixes it.

**Streak safety advice.** Treat this as a safety net, not a replacement:
open Snapchat and send a manual Snap now and then, keep Discord alerts on
(at least for failures) and act on them the same day, and check the
dashboard history occasionally. Snapchat can change the site or challenge
the login at any time.

---

## Selector overrides

When Snapchat changes its web app, the selectors the automation uses to find
buttons can stop matching. Instead of waiting for a new image, create
`/data/selectors.json`. It is re-read whenever it changes; no restart needed.

Each key is a step name, each value an ordered list of
[Playwright selectors](https://playwright.dev/python/docs/locators) (CSS,
`role=button[name=/regex/i]`, `text=...`, `xpath=...`). The first one that
matches a visible element wins.

Step names: `logged_out`, `logged_in`, `chat_list_item`, `chat_list_scroll`,
`open_camera`, `camera_video`, `shutter`, `send_to`, `picker_container`,
`picker_search`, `picker_recipient`, `send_button`, `popup_dismiss`.

- A plain list **replaces** the built-in candidates for that step.
- `{"prepend": [...], "append": [...]}` **extends** them instead.
- In `picker_recipient`, `{name}` is replaced with the recipient's display name.
- Unknown keys and invalid JSON are logged and ignored.

```json
{
  "shutter": ["button[aria-label='Take a Snap']"],
  "send_to": {"prepend": ["button:has-text('Send to')"]}
}
```

To find out which step broke, look at the container log (each step logs which
candidate matched, e.g. `step=shutter matched candidate #2`) and at the
screenshot and page HTML saved as `/data/debug/<timestamp>-<step>.png/.html`.

---

## Updating

- **TrueNAS / HexOS:** **Apps** → `snapchat-saver` → **Edit** → **Save**
  (re-pulls `latest`), or use the app's *Update*/*Pull image* action when
  offered. Your data stays in the dataset; you stay logged in.
- **Docker:** `docker compose pull && docker compose up -d`.
- To pin a version, replace `:latest` with a release tag such as `:1.2.0`
  (published for git tags `v1.2.0`) or a commit tag `:sha-abc1234`.

New images are built by GitHub Actions on every push to `main` (tag
`latest`), on version tags (`v*` → semver tags) and on manual runs; pull
requests are built but not pushed.

---

## Local development

Requirements: Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium

# tests (no network or Snapchat needed)
python -m pytest -q

# run the dashboard locally; state goes to ./data
export DATA_DIR=./data DASHBOARD_PASSWORD=dev SNAP_HEADLESS=false
uvicorn app.main:app --reload --port 8080
```

Locally, Chromium opens as a normal window on your desktop. The embedded
browser view needs an X display with x11vnc on `127.0.0.1:5900` and noVNC
files in `NOVNC_DIR` (default `/usr/share/novnc`), which the Docker image
provides; for the full stack, build and run the container:

```bash
docker build -t snapchat-saver .
docker run --rm -p 8080:8080 --shm-size=1g \
  -e DASHBOARD_PASSWORD=dev -e PUID=$(id -u) -e PGID=$(id -g) \
  -v "$PWD/data:/data" snapchat-saver
```

Project layout:

| Path | Purpose |
| --- | --- |
| `app/` | Python package (`app.main:app` is the FastAPI app). |
| `app/snapchat/` | Playwright automation of Snapchat Web. |
| `app/web/` | Dashboard templates, static files, VNC bridge. |
| `tests/` | pytest suite. |
| `Dockerfile`, `docker/` | Image, entrypoint, supervisord and openbox config. |
| `docker-compose.yml` | Compose file for TrueNAS "Install via YAML". |
| `.github/workflows/docker.yml` | Tests, then build and push to GHCR. |
