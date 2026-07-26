# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Mumble-based voice radio system for the Airwaysn online flight-sim network: pilots' sim COM1 frequency drives which Mumble channel they sit in, controllers run a stack of frequencies they monitor and transmit on, and server-side ATIS bots broadcast synthesized speech onto their own frequencies. Code comments and all user-facing UI strings are Chinese — keep new strings Chinese to match.

There is no dependency manifest, test suite, or build script in the repo. Everything is run directly with `python` or packaged with PyInstaller.

## Components

| Directory | Entry point | Role |
|---|---|---|
| `client/` | `gui.py` | Pilot client for MSFS — reads COM1 via **SimConnect** (`AircraftRequests.get("COM_ACTIVE_FREQUENCY:1")`) |
| `xplane_client/` | `gui.py` | Pilot client for X-Plane — reads COM1 via **X-Plane UDP** (`XPlaneRadio` in `radio.py`) |
| `controller/` | `gui.py` | ATC client — a **stack of frequencies** modelled on [TrackAudio](https://github.com/pierr3/TrackAudio), each with RX/TX/XC |
| `server/` | `login.py`, `ATIS/mumble.py` | Runs on the Mumble/Murmur host: Ice authenticator + server-side ATIS bot fleet |

`client/` and `xplane_client/` are **near-duplicate forks** of each other — same `radio.py`/`settings.py`/`gui.py` layout, identical apart from where COM1 comes from, so a fix to audio or PTT logic usually has to be applied to both. `controller/` no longer shares that shape: it was restructured around the radio stack (`radiostack.py` + `voice.py`) and has no `radio.py`.

## Commands

Run each app **from inside its own directory** — icon paths (`.\favicon.ico`) and config files are resolved relative to the CWD:

```powershell
cd client;        python gui.py     # MSFS pilot client (needs MSFS running)
cd xplane_client; python gui.py     # X-Plane pilot client (needs X-Plane in a flight)
cd controller;    python gui.py     # ATC client
cd server\ATIS;   python mumble.py  # server-side ATIS manager (needs ffmpeg on PATH)
python server\login.py              # Murmur Ice authenticator (run on the Mumble host)
```

Build a Windows bundle (from the component directory, output lands in `dist/`):

```powershell
pyinstaller gui.spec
```

Tests — only the controller has any; both run from `controller/` and need no server, no audio device and no network:

```powershell
python -m unittest test_radiostack -v    # radio stack model + RX/TX/XC coupling rules
python -m unittest test_radiostack.CouplingRulesTest.test_tx_forces_rx_on   # one test
python smoke_gui.py                      # builds every window/dialog offscreen
```

`smoke_gui.py` replaces `QMessageBox.warning`/`critical` with a recorder before touching the GUI — a modal dialog blocks forever even under the offscreen platform, so any new code path that can pop one needs that stub to stay in place.

Three environment facts that will cost an afternoon otherwise:

- **Use Python 3.12.** PyAudio has no wheel for 3.13+, and building it from source needs PortAudio headers plus MSVC.
- **pymumble needs the native `opus` library** (`opuslib` loads it through `ctypes` at runtime). Without it every import of `radio.py`/`voice.py` dies with `Could not find Opus library`. `controller/gui.spec` now locates `opus.dll` at build time and bundles it — via `find_library`, then `pyogg`'s copy, then `controller/opus.dll` — so a packaged build works on a machine that has never seen Opus. The other three specs do not do this yet.
- **`favicon.ico` is actually a PNG** with the wrong extension. Qt sniffs the content so the window icon is fine, but PyInstaller 6 refuses it as an exe icon unless `pillow` is installed to convert it.

X-Plane dataref diagnostics — useful when frequency reads come back wrong:

```powershell
cd xplane_client
python xplane.py            # RREF, 0.01 MHz precision
python xplane.py --833      # com1_frequency_hz_833 dataref, 0.001 MHz precision
python xplane.py --probe    # try every candidate dataref and print raw + MHz
python xplane.py --data     # DATA output row 3 (must be enabled in X-Plane settings)
```

Third-party packages used (install with pip — note the PyPI name for pymumble_py3 is **`pymumble`**): `PyQt6`, `pymumble`, `pyaudio`, `numpy`, `pygame`, `keyboard` (pilot clients), `pynput` (controller), `SimConnect` (client only), `edge-tts` + `requests` + `pyttsx3` + `scipy` (server ATIS), `zeroc-ice` + generated `MumbleServer` slice modules (server login — gitignored, must be generated from the host's `MumbleServer.ice`).

## Cross-cutting conventions

**Frequency → channel name.** The one contract every component shares:

```python
freq_value = int(round(float(frequency_mhz) * 1000))     # 125.400 -> 125400
channel_name = f"FREQ_{str(freq_value).zfill(6)}"        # -> "FREQ_125400"
```

Channels are created at the Mumble root as `temporary=True` if missing, then `move_in`. Changing this formula breaks pilot/controller/ATIS interop simultaneously.

**ATIS username encoding.** ATIS bots log in as `{cid}_atis{freq6}` (e.g. `1005_atis118000`). `server/login.py` matches `^.*_atis\d{6}`, authenticates the `cid` part against `https://airwaysn.org/api/v1/public/auth`, and returns the 6-digit frequency as the Murmur user id so multiple ATIS sessions per user don't collide. `server/ATIS/mumble.py` uses the reserved account `900` for its bots. Frequency `199.998` is deliberately skipped (placeholder for "no frequency").

**Authentication.** There are no local accounts — the Mumble server delegates to the Airwaysn web API via the Ice authenticator in `server/login.py`, which also kicks a prior session when the same name reconnects.

That kick needs **two** Ice objects registered, which is easy to get wrong: `userConnected`/`userDisconnected` belong to `ServerCallback`, not `ServerAuthenticator`, so `setAuthenticator()` alone leaves `online_users` permanently empty and nothing is ever kicked. `main()` registers a `ServerCallbackI` via `addCallback()` as well, and seeds `online_users` from `getUsers()` for anyone already connected. All Ice calls carry the `{"secret": …}` context — `kickUser` without it raises `InvalidSecretException`.

**Mumble 1.5 renamed the Ice slice module** from `Murmur` to `MumbleServer`; the interfaces themselves are unchanged (signatures checked against `v1.5.735/src/murmur/MumbleServer.ice`). `login.py` imports `MumbleServer` and falls back to `Murmur`, so it runs on either. Regenerate the bindings on the host with `slice2py /usr/share/mumble-server/MumbleServer.ice` — the generated package is gitignored. On Debian 13 the config lives at `/etc/mumble/mumble-server.ini`. The Mumble host is hardcoded as `hjdczy.top` in `client/radio.py`, `xplane_client/radio.py`, `controller/gui.py`, and `server/ATIS/mumble.py`.

**Audio path.** Mono `paInt16`, 20 ms frames (`CHUNK = int(RATE * 0.02)`). Each client runs `_find_best_sample_rate()`, probing `[48000, 44100, 32000, 24000, 16000]` against the selected devices at startup and again on every device change. Note that pymumble's `sound_output.add_sound()` expects 48 kHz PCM and no resampling is done — a fallback rate produces pitch-shifted audio, so 48 kHz is the intended path and lower rates are a last resort.

**PTT and indicators.** Pilot clients poll `keyboard.is_pressed()` plus a pygame joystick button in a background thread; the controller uses a `pynput` listener plus an on-screen button. RX indicators are lit from the `PYMUMBLE_CLBK_SOUNDRECEIVED` callback and cleared by a 0.5 s timeout loop.

**pygame on Windows.** `os.environ['SDL_VIDEODRIVER'] = 'dummy'` and `SDL_AUDIODRIVER = 'dummy'` must be set *before* `import pygame` — that is why these lines sit above the imports in `gui.py`, `radio.py`, and `client/settings.py`. pygame is only used for joystick input; SDL video/audio must stay disabled so it does not fight PyQt6 and PyAudio.

**Qt threading.** pymumble callbacks fire on the library thread, so every GUI marshals them into the Qt thread through `pyqtSignal` wrappers: `ErrorSignal`/`ConnectSignal` in `client/gui.py` and `xplane_client/gui.py`, `VoiceSignals` in `controller/gui.py`. Follow that pattern for anything that touches widgets from a Mumble or audio callback.

**Settings files.** Written to the CWD as JSON: `radio_settings.json` for `client/` and `controller/` (gitignored), `xplane_radio_settings.json` for `xplane_client/`. The pilot clients persist the Mumble username and password in plaintext; the controller persists `last_username` plus the whole radio stack under `radios`, so a session comes back with the same frequencies and their RX/TX/XC state.

**ATIS text processing.** `server/ATIS/process.py` expands digits into radio readback (Chinese `幺两拐洞`, English `niner`/`zero`) and turns a lone uppercase letter into its NATO word. Server ATIS splits the upstream `text_atis` on `|` into English|Chinese. Broadcasters check the channel for other speakers and pause rather than talk over traffic. ATIS lives **only** on the server side now — the controller client's ATIS was removed.

## The controller's radio stack

`controller/` is modelled on TrackAudio (`C:\Docs\Dev\TrackAudio`): a controller adds several frequencies and each is a "radio" with three switches. The **coupling rules are copied from TrackAudio's `radio.tsx`** and are pinned by `test_radiostack.py`:

- turning **RX off** also clears TX and XC — not receiving makes transmitting and coupling meaningless
- turning **TX on** forces RX on — there is no transmit-only radio
- turning **XC on** forces both RX and TX on

`radiostack.py` holds that model and nothing else — no I/O, no Qt, no Mumble — which is why it is the one part with real unit tests. `voice.py` owns the Mumble side, `gui.py` draws one `RadioRow` per radio.

**How one Mumble connection covers several frequencies.** A Mumble user can only *be* in one channel, so `voice.py` uses two mechanisms and both need raw protobuf because pymumble does not wrap them:

- **RX** → `UserState.listening_channel_add/remove` (Mumble 1.4 channel listeners), sent via `send_message(PYMUMBLE_MSG_TYPES_USERSTATE, …)`. pymumble's `ModUserState` only forwards a fixed set of fields and drops these.
- **TX** → a `VoiceTarget` carrying **all** TX channels at once. `sound_output.set_whisper()` cannot do this: for `id == 1` pymumble copies only `targets[0]`, so multi-channel transmit has to build the message directly.

The client still **joins** the selected radio's channel (the one marked `▸`) rather than sitting in root. That is deliberate: if the server is Mumble 1.3, the listener message is silently ignored and the controller would otherwise hear nothing at all — this way the primary frequency always works and `listeners_working` drives a warning in the status bar. Incoming audio is routed to a radio by `user["channel_id"]`, which is what makes per-frequency RX indication and per-frequency volume possible.

**XC (cross-couple)** is implemented client-side in `_forward_cross_couple`: audio received on one XC frequency is re-sent to the other XC frequencies by temporarily swapping the voice target. It restores the normal TX target afterwards — if you add an early return in there, make sure the target still gets restored or the controller's next PTT goes to the wrong frequencies.

## Related repositories

This repo is the **voice layer** of a three-part network. The other two live alongside it and own the contracts this one consumes:

| Path | Repo | Role |
|---|---|---|
| `C:\Docs\Dev\can-fsd` | can-fsd | Go FSD daemon — pilot/controller connections, flight plans, and the live datafeed. Docs: `Readme.md` |
| `C:\Docs\Dev\can-web` | can-web | Astro + Vue website — accounts, roster, radar, docs. Owns the MySQL schema (Prisma). Docs: `CLAUDE.md` |

Two integration points, both hardcoded here:

**Authentication → can-web.** `server/login.py` POSTs `{cid, password}` to `https://airwaysn.org/api/v1/public/auth`, implemented at `can-web/src/pages/api/v1/public/auth.ts`. That route compares against the cleartext `user.password` column (the FSD network password, which equals the member's website password) and additionally **rejects `user.rating < 1`** — an unrated member cannot use voice even with correct credentials. Failures are rate-limited per ASN ID, so a client stuck in a reconnect loop with a bad password will lock that account out of voice for the window. Because the Murmur user id is `int(name)`, the Mumble username must be the numeric ASN ID. The `900` / `p@ssw0rd` ATIS account in `login.py` bypasses the API entirely and is local to this repo.

**ATIS datafeed → can-fsd.** `server/ATIS/request.py` polls `https://data.airwaysn.org/v1/data.json`, can-fsd's datafeed (`internal/api`, HTTP port 20350), and `server/ATIS/mumble.py` speaks every `atis[]` entry it finds. It consumes `atis[].callsign`, `.frequency` and `.text_atis` — can-fsd guarantees `text_atis` is a JSON array, never null, and has golden-file tests pinning that document. Two details owned by can-fsd: a station lands in `atis[]` only if its callsign ends in `_ATIS`, and `frequency` is a full MHz string (`"128.500"`) where **`199.998` means "no frequency set"** — never build a `FREQ_*` channel from it. Splitting `text_atis` on `|` into English|Chinese is a convention of *this* repo, not of the datafeed.

**No FSD connection from this repo's clients.** The voice clients only speak Mumble; nothing here logs in to can-fsd's FSD port. A controller's presence on the network comes from whatever ATC client they run (EuroScope and friends), exactly as TrackAudio does it. A previous iteration had `controller/fsdclient.py` log the client-side ATIS in as an `_ATIS` station over the FSD protocol; it was removed with the ATIS feature. If that is ever wanted again, the packet layouts are in can-fsd's `internal/fsd/conn.go`, `handler.go` and `docs/protocol.md` — and note the ninth `$ID` field (challenge) must be omitted so the server never starts a VATSIM `$ZC` challenge that only official clients hold keys for.

No authorisation is shared: can-fsd checks the `division` roster before a controller may staff a position, but the voice server has no equivalent — any account that authenticates can join any `FREQ_*` channel.

Naming has drifted across the three: can-web now calls the network **Cerulean Aviation Network (formerly AirwaySN)** and can-fsd takes its network name from `config.json`'s `version`, while this repo still hardcodes `airwaysn.org`, `data.airwaysn.org` and the Mumble host `hjdczy.top`.

## Reference docs

- `xplane_client/API.md` — X-Plane UDP protocol notes (BECN discovery on `239.255.1.1:49707`, RREF requests, dataref precision) and a SimConnect-vs-X-Plane comparison table.
- `client/API.md` — vendored upstream pymumble API reference, not project documentation.
