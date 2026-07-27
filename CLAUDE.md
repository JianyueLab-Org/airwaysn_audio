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
| `atis/` | `gui.py` | ATIS client modelled on [vATIS](https://github.com/vatis-project/vatis) — stations, presets, templates; broadcasts over Mumble |
| `xpc/` | `gui.py` | **XPC for CAN** — X-Plane pilot client modelled on [xPilot](https://github.com/xpilot-project/xpilot): Mumble voice **and** an FSD connection |
| `msfs/` | `gui.py` | **MSFS for CAN** — the same client for Microsoft Flight Simulator, over SimConnect |
| `server/` | `login.py`, `ATIS/mumble.py` | Runs on the Mumble/Murmur host: Ice authenticator + server-side ATIS bot fleet |

`client/` and `xplane_client/` are **near-duplicate forks** of each other — same `radio.py`/`settings.py`/`gui.py` layout, identical apart from where COM1 comes from, so a fix to audio or PTT logic has to be applied to both, every time. `controller/`, `atis/` and `xpc/` no longer share that shape: they were rebuilt around their own models (`radiostack.py` + `voice.py`, `profile.py` + `broadcast.py`, and `xplane.py` + `fsdpilot.py` + `voice.py`) and have no `radio.py`.

`xpc/` **supersedes `xplane_client/`** in scope: same simulator, but it also logs in to FSD so the aircraft appears on the network, where `xplane_client/` is voice-only. `xplane_client/` is still the smaller, voice-only option and is not deprecated.

There is no shared package — each component is imported flat from its own directory, which is why `mumblecompat.py` and `applog.py` exist as per-component copies rather than one import. Keep that pattern when adding cross-cutting helpers; a shared parent module would need path juggling in every PyInstaller spec.

Two things the pilot clients still lack that the controller and ATIS have: **logging** (they still `print`, which goes nowhere in a `console=False` build) and any tests.

## Commands

Run each app **from inside its own directory** — icon paths (`.\favicon.ico`) and config files are resolved relative to the CWD:

```powershell
cd client;        python gui.py     # MSFS pilot client (needs MSFS running)
cd xplane_client; python gui.py     # X-Plane pilot client (needs X-Plane in a flight)
cd controller;    python gui.py     # ATC client
cd atis;          python gui.py     # ATIS client
cd xpc;           python gui.py     # XPC for CAN — X-Plane pilot client (voice + FSD)
cd msfs;          python gui.py     # MSFS for CAN — same, for Microsoft Flight Simulator
cd server\ATIS;   python mumble.py  # server-side ATIS manager (needs ffmpeg on PATH)
python server\login.py              # Murmur Ice authenticator (run on the Mumble host)
```

Build a Windows bundle (from the component directory):

```powershell
pyinstaller gui.spec
```

**Run the one in `dist/`, never the one in `build/`.** PyInstaller leaves an identically-named exe in its work directory (`build/gui/`, named after the spec file), but that one is only the bootloader plus the archive — `python312.dll`, `opus.dll` and the Qt libraries all live in `dist/<name>/_internal/`. Launching the `build/` copy fails with *"Failed to load Python DLL … LoadLibrary: The specified module could not be found"*, which reads like a broken build but is just the wrong exe. The shippable output is `dist/airwaysn-controller/`, `dist/airwaysn-atis/` and `dist/xpc-for-can/`.

Tests — the controller, the ATIS client and XPC have them; each runs from its own directory and needs no server, no audio device and no network:

```powershell
cd controller
python -m unittest test_radiostack -v    # radio stack model + RX/TX/XC coupling rules
python -m unittest test_radiostack.CouplingRulesTest.test_tx_forces_rx_on   # one test
python -m unittest test_applog           # logging, including uncaught-exception capture
python smoke_gui.py                      # builds every window/dialog offscreen

cd atis
python -m unittest test_atis -v          # METAR, templates, stations, vATIS import, FSD
python -m unittest test_applog
python smoke_gui.py

cd xpc
python -m unittest test_xpc -v           # PBH, position packets, RREF, traffic, model matching
python -m unittest test_xpc.PbhTest      # the one that must match can-fsd exactly
python -m unittest test_xpc.ModelMatchingTest   # CSL fallback chain
python smoke_gui.py

cd msfs
python -m unittest test_msfs -v          # BCD squawk, SimVar units, aircraft.cfg
python -m unittest test_msfs.RealWorldLayoutTest   # the ones a live install found
python smoke_gui.py
```

`test_xpc.py` loads `plugin/PI_XpcTraffic.py` directly (the plugin guards its `import xp` so it imports outside X-Plane) to check the bridge reassembler and the animation-value ordering. The rest of the plugin needs a running simulator and is not covered.

**Logging.** Both clients ship `applog.py` (a per-component copy, matching how the rest of the repo duplicates rather than shares). `applog.setup(debug)` installs a rotating file handler writing `airwaysn-controller.log` / `airwaysn-atis.log` to the CWD, plus a console handler when one exists. This is not optional polish: the packaged builds are `console=False`, so a bare `print` goes nowhere and a user reporting "it won't connect" has nothing to send you. Use `log = logging.getLogger("模块名")` per module rather than `print`.

`setup()` also replaces `sys.excepthook` **and** `threading.excepthook` — an uncaught exception in a Qt slot or a worker thread otherwise vanishes silently, leaving a frozen window and an empty log. `--debug` (or the settings checkbox, persisted as `debug`) drops the level to DEBUG, which is where the protocol traffic lives: every FSD packet in and out (with the `#AA` password redacted), channel-listener changes, voice-target changes, and the radio-stack sync.

`smoke_gui.py` replaces `QMessageBox.warning`/`critical` with a recorder before touching the GUI — a modal dialog blocks forever even under the offscreen platform, so any new code path that can pop one needs that stub to stay in place.

Three environment facts that will cost an afternoon otherwise:

- **Use Python 3.12.** PyAudio has no wheel for 3.13+, and building it from source needs PortAudio headers plus MSVC.
- **pymumble 1.6.1 does not work on Python 3.12 out of the box.** Its `connect()` builds the TLS socket with `ssl.wrap_socket()`, which was **removed in 3.12**, and its `except AttributeError` fallback calls *the same removed function* — so the exception escapes, from inside pymumble's own thread. The caller only sees `is_alive() == False` and naturally reports "server rejected the connection", which sends you hunting for a password problem while the TLS handshake never even started (the Murmur log shows a connection opening and closing milliseconds later). `mumblecompat.install()` reinstates a `wrap_socket` built on `SSLContext`. **All four components carry a copy and call it at import** — without it the voice path is dead in every packaged build, pilot clients included.
- **Every spec must bundle `opus.dll`.** `opuslib` loads it through `ctypes` at runtime, so PyInstaller's static analysis never sees it and a packaged build dies at startup on any machine that does not already have Opus. All four specs now run the same `find_opus()` (`find_library` → the component's own `opus.dll` → `pyogg`'s copy).
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

**Diagnosing a refused connection.** pymumble raises `ConnectionRejectedError(mess.reason)` on a server `Reject` — dropping the `type` field, which is the one that actually says *why*, and which Murmur often fills in while leaving `reason` empty. Both clients therefore connect through a `RejectAwareMumble` subclass that parses the `Reject` message in `dispatch_control_message` before handing it on, keeping `reject_type` for the UI. `REJECT_REASONS` maps those types to Chinese; the distinction that matters most is `WrongUserPW` ("密码错误") versus `AuthenticatorFail` ("服务端认证器故障"), because the second one means `server/login.py` is down and no amount of password fiddling will help.

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

**Two client-visible errors are really the same server ACL gap.** Mumble's default root ACL does not necessarily carry everything this system needs, and in both cases the client's symptom points somewhere unhelpful:

| Symptom | Missing permission | What the user sees |
|---|---|---|
| 管制端 "没有权限（频道监听需要 Listen 权限）" | `Listen` `0x800` | Silence on every frequency except the joined one |
| ATIS "Channel FREQ_127800 does not exists" | `MakeTempChannel` `0x400` | Looks like a missing channel; the server actually refused to create it |

`Listen` is the one that bites hardest: **Mumble 1.4 added it, so a server upgraded from 1.2/1.3 keeps an old root ACL without it**. Frequency channels are temporary children of root and inherit its ACL, so granting once on root covers all of them:

```powershell
python server\fix_acl.py            # 只看，不改
python server\fix_acl.py --apply    # 真的写进去
```

Run it on the Mumble host (Ice only listens on `127.0.0.1`). It prints the root ACL with every permission bit spelled out, reports which of `REQUIRED` are missing and what each one breaks, ORs them into the `all` group entry that applies to subchannels, and reads back to confirm rather than trusting that `setACL` didn't throw. Bit values come from mumble's `src/ACL.h`.

If the permissions are all present and radios are still silent, the cause is the `listenersperuser` / `listenersperchannel` caps in `mumble-server.ini` instead — the `PERMISSIONDENIED` callbacks in `controller/voice.py` and `atis/broadcast.py` distinguish those cases by denial type.

`getACL` returns inherited ACLs too and those are read-only; the script filters them before `setACL`. Root has none, but don't rely on that if you adapt it for another channel.

The client still **joins** the selected radio's channel (the one marked `▸`) rather than sitting in root. That is deliberate: if the server is Mumble 1.3, the listener message is silently ignored and the controller would otherwise hear nothing at all — this way the primary frequency always works and `listeners_working` drives a warning in the status bar. Incoming audio is routed to a radio by `user["channel_id"]`, which is what makes per-frequency RX indication and per-frequency volume possible.

## The ATIS client

`atis/` is modelled on vATIS, so the vocabulary is theirs: a **profile** holds **stations**, a station holds **presets**, and a preset holds a **template**. The template is free text with `[WIND]`-style variables; the variable names and the `:VOX` suffix are copied from vATIS's documented set (`template.py: ALIASES`).

The idea worth preserving is that **every weather element has two forms**: `text` reproduces the METAR group (`09004MPS`) for the written ATIS, `voice` is what gets spoken (`wind zero niner zero at four meters per second`). `metar.py` produces both, and `template.render()` returns both — the same template rendered twice. In the voice pass every variable takes its spoken form automatically; `:VOX` only exists to force the spoken form into the *written* ATIS. Altitudes are spoken as `three thousand`, not digit-by-digit, which is `spell_altitude`'s whole reason to exist.

`vatis_import.py` reads real vATIS profile JSON. Field names are camelCase and enums are strings because vATIS sets `PropertyNamingPolicy = CamelCase` and `UseStringEnumConverter` in its `SourceGenerationContext`; older profiles call the station list `composites` instead of `stations`, and older builds may write `atisType` as a number, so all of those are accepted. **`frequency` is a uint in hertz** (`133800000` = 133.800) — the single most damaging field to misread, so `parse_frequency` also tolerates kHz/MHz and rejects anything outside the VHF band. Anything vATIS supports that this client does not (`atisFormat` fine-grained readback options, IDS endpoint, static definition libraries, recorded voice) is skipped and *reported in the import dialog* rather than silently dropped.

Contractions come across too: vATIS references them as `@NAME` in a template, and each has a text and a voice form, which maps exactly onto the two-pass rendering — `render()` takes the station's contraction table and expands `@NAME` differently in each pass.

Station callsigns follow vATIS: `ZSPD_ATIS`, `ZSPD_D_ATIS`, `ZSPD_A_ATIS` for combined/departure/arrival. Each station has a **code range** limiting which information letters it uses, so a departure and an arrival ATIS at one field can't be confused; the letter advances by one whenever the raw METAR text changes, and wraps within the range.

**Creating the frequency channel is a round trip, not a local operation.** `new_channel()` only sends a message; the channel appears once the server echoes a `ChannelState`. `_join_channel` used to `sleep(0.2)` and then look — long enough on localhost, not on a remote server, and the failure surfaced as `Channel FREQ_127800 does not exists`, which reads like the channel *can't* be created. It now polls until `CHANNEL_TIMEOUT` and bails early if a `PermissionDenied` arrives, so a refused creation is reported as a permission problem and a slow server just waits. Pinned by `JoinChannelTest`.

Audio goes out over Mumble, not AFV: `broadcast.py` opens its own connection per station as `{cid}_atis{freq6}` (the shape `server/login.py` authenticates) and opens **no** local audio device. Transmission is paced against `sound_output.get_buffer_size()` and yields the frequency if anyone else keys up. `update_text()` swaps the script for the *next* cycle rather than cutting off the current one.

**One connection, one output queue — the rule that keeps audio on the right frequency.** `sound_output.target` is a single mutable field shared by PTT and cross-couple, and a `VoiceTarget` id is a server-side registration that gets *overwritten* when reprogrammed. Both of those bit us: reusing one id meant a cross-couple forward silently retargeted the controller's next PTT. The invariants now, pinned by `test_voice.py`:

- PTT owns id `1`; each cross-coupled frequency gets its own id from `XC_TARGET_BASE`, programmed once in `sync()`. Forwarding switches the id, it never reprograms one.
- `_audio_lock` guards every `target =` / `add_sound()` pair.
- Cross-couple **does not forward while transmitting** — two streams into one queue interleave into garbage, and the controller's own voice is what matters.
- `start_transmit()` joins the previous transmit thread first; otherwise the old thread's exit resets `target` to 0 *after* the new one started talking.

**XC (cross-couple)** is implemented client-side in `_forward_cross_couple`: audio received on one XC frequency is re-sent to the other XC frequencies by temporarily swapping the voice target. It restores the normal TX target afterwards — if you add an early return in there, make sure the target still gets restored or the controller's next PTT goes to the wrong frequencies.

## XPC for CAN (`xpc/`)

The X-Plane pilot client, laid out like xPilot: three independent links, none of which can take the others down.

| Module | xPilot equivalent | Role |
|---|---|---|
| `xplane.py` | `src/simulator/` | UDP link to X-Plane — position, attitude, transponder, COM1/2 |
| `fsdpilot.py` | `src/fsd/` | Pilot-side FSD connection: login, position reports, text, flight plans |
| `voice.py` | `src/audio/` + `afv-native/` | Mumble voice; the channel follows COM1 |
| `traffic.py` | `src/aircrafts/` | Other aircraft: sample history, interpolation, model-match state |
| `cslmatch.py` | `src/aircrafts/` (model matching) | CSL package parsing and the type→model fallback chain |
| `bridge.py` | — | UDP transport to the X-Plane plugin |
| `plugin/PI_XpcTraffic.py` | `xpilot` XPL plugin | Runs *inside* X-Plane (XPPython3): draws traffic, feeds TCAS |
| `gui.py` | `Resources/Views/` | Connect bar, messages, nearby-ATC list, radio bar |

**The X-Plane link subscribes, it does not poll.** `xplane_client/radio.py` sends one RREF and waits for the reply every time it wants COM1. That round-trip is fine for a channel switch but not for position reports 5× a second, so `XPlaneLink` sends one RREF per dataref *once* with a rate, then just keeps reading whatever X-Plane pushes and holds the latest value. `snapshot()` returns the whole set already converted to FSD's units (feet, knots, MHz) — X-Plane reports metres and m/s.

**`ConnectionResetError` is normal here, not an error.** On Windows, sending UDP to a port nobody is listening on comes back as an ICMP port-unreachable, which surfaces as `ConnectionResetError` on the *next* `recvfrom`. Before X-Plane is running that is every single read. Treating it as a fatal socket error re-subscribes once a second forever (visible as a wall of "已订阅 14 个 dataref" in the log); it has to be treated like a timeout. `_still_waiting()` owns that decision and is pinned by `WaitingTest`: warn at `STALE_AFTER` (3 s), rediscover only at `REDISCOVER_AFTER` (15 s).

**PBH packing must be the exact inverse of can-fsd's decoder.** Pitch, bank and heading ride in one 32-bit integer at 10 bits each (`360/1024` ≈ 0.35° per step), with bit 1 as the on-ground flag. `pack_pbh()` normalises to 0–360 *before* quantising — a raw negative angle overflows its 10-bit field and puts the aircraft at a nonsense attitude on everyone else's screen. `test_xpc.py` carries a copy of can-fsd's `PitchBankHeading` decode (`internal/fsd/packet.go`) and round-trips against it; if that Go function ever changes, that test is what catches it.

Other things worth knowing:

- **Callsigns are pre-checked client-side** against can-fsd's `IsValidCallsign` (2–10 characters, `A-Z0-9_-`) so the user gets an explanation instead of a login rejection.
- **`$ID`'s ninth field (challenge) is deliberately omitted** so the server never starts a VATSIM `$ZC` challenge that only official clients hold keys for. Same reasoning as the removed `controller/fsdclient.py`.
- **Packets are colon-delimited**, so every free-text field (real name, remarks, route, chat) goes through `sanitize()`. A colon in a remarks field otherwise shifts every following field by one.
- **`#AP` carries the password**, so `_redact()` masks it before anything reaches the log — users paste logs into chat.
- Position reports drop to one every 5 s when parked on the ground, and the transponder mode character (`S`/`N`/`Y`) is driven by X-Plane's `transponder_mode` dataref, with `Y` held for 8 s after IDENT.
- FSD and voice failures are isolated in `gui.py`: `on_fsd_status('error')` clears only `self.fsd`, so losing the network connection does not drop the frequency you are listening to.

### Rendering other aircraft

xPilot draws traffic from a C++ plugin built on XPMP2. This does it in Python instead, split across two processes, because **X-Plane's drawing API is only reachable from a plugin, and the plugin runs inside X-Plane's own Python (XPPython3)** — PyQt6, pymumble and pyaudio can't go there.

    client (xpc/)                          plugin (inside X-Plane)
    FSD → traffic.py → cslmatch.py  ──UDP──→  PI_XpcTraffic.py → draw + TCAS

Everything hard lives client-side where it has unit tests; the plugin is deliberately thin because changing it means restarting the simulator.

**`TrafficTable` is falsy when empty** — it defines `__len__`, so `if not self.traffic` is true for a table with no aircraft in it. `fsdpilot.py` must test `if self.traffic is None`. Getting this wrong silently drops *every* traffic packet, because the table is empty exactly when the first aircraft arrives.

**Aircraft type does not come from the position packet.** `@` carries no model information, so the type is fetched over `#SB`: on first sight the client sends `#SB{me}:{them}:PIR`, and the reply is `PI:GEN:EQUIPMENT=B738:AIRLINE=CCA`. can-fsd relays `#SB` verbatim (`handleSquawkbox`, `internal/fsd/handler.go:515`), so no server change is needed. **We must also answer other clients' `PIR`** or everyone else renders us as a generic model. Until the reply arrives the aircraft is drawn with a fallback model and `model_dirty` tells the renderer to re-match once it does.

**Model matching must always return something.** `ModelSet.match()` degrades type+airline → type → same family → generic-by-category → first model in the package, and returns the reason so a "that aircraft looks wrong" report is diagnosable from the log. An aircraft you cannot see is far more dangerous than one with the wrong livery.

**Two X-Plane facts that shape the plugin**, both confirmed against `Resources/plugins/DataRefs.txt`:

- **`XPLMInstance`-drawn aircraft do not appear on TCAS.** The panel reads `sim/cockpit2/tcas/targets/*` (64 slots, index 0 is the user), which is a separate system that has to be filled by hand. Those datarefs are "writeable only when `override_TCAS` is set", and `override_TCAS` itself is "only writeable by the plugin that has the AI planes acquired" — hence `xp.acquirePlanes()` first. The arrays carry `flight_id` and `icao_type`, so callsign and type show correctly on the ND. Traffic is capped at 63 and sorted by range client-side, because when there are more aircraft than slots the far ones are the right ones to drop.
- **The animation datarefs must be registered before any CSL model loads.** CSL OBJ8 files reference `libxplanemp/controls/*` by name; X-Plane resolves those while parsing the `.obj`, so registering afterwards gives you a model that renders but never moves. `XPluginStart` registers them, and the values themselves travel through `instanceSetPosition`'s `data` list — whose order must match the `createInstance` dataref list exactly.

**X-Plane 11 and 12 are both supported, by probing rather than version checks.** Two things differ, and neither needs a version branch:

- **8.33 kHz COM frequency.** `sim/cockpit2/radios/actuators/com1_frequency_hz_833` (kHz, so `/1000`) only exists from X-Plane 11.30; the older `sim/cockpit/radios/com1_freq_hz` is in units of 10 kHz (`/100`) and can't represent 132.005. `xplane.py` subscribes to **both** and prefers the precise one — a dataref that doesn't exist is simply never pushed, no error.
- **TCAS override needs X-Plane 11.50.** The plugin calls `findDataRef` on `override_TCAS` and the target arrays; if any is missing it sets `tcas_available = False`, still draws traffic, and says so in the log. It also **skips `acquirePlanes()` entirely in that case** — holding the AI planes without being able to use them would block LiveTraffic and friends for nothing.

**XPPython3 version depends on the simulator**: v4.x is built against SDK 420 and is X-Plane 12 only; X-Plane 11.52 needs v3.1.5. The settings tab says so, because installing the wrong one is a silent no-op.

Also worth knowing: `instanceSetPosition` takes `(x, y, z, pitch, heading, roll)` — heading before roll. Writing the TCAS targets makes X-Plane mirror the nearest 19 aircraft back into the legacy `sim/multiplayer/position/plane#_*` datarefs automatically, so plugins still reading those keep working. `xp.worldToLocal()` does the coordinate conversion, so unlike the legacy 19-slot multiplayer datarefs there is no hand-rolled tangent-plane maths. If LiveTraffic or another XPMP2-based plugin is loaded it will have registered the same `libxplanemp` datarefs and acquired the AI planes; the plugin detects this and logs a warning rather than fighting over them.

The bridge is UDP with one JSON object per datagram, fragmented over `seq`/`part`/`total`. UDP rather than TCP because this is a pure position stream — a dropped frame is replaced 200 ms later, whereas a reliable queue would build up latency. The plugin keeps only the newest complete frame; late frames make aircraft jump backwards. `bridge.Reassembler` and the plugin's copy are separate implementations, and `test_xpc.py` loads the plugin file to check they agree.

## MSFS for CAN (`msfs/`)

The same client as `xpc/`, for Microsoft Flight Simulator. `fsdpilot.py`, `voice.py`, `traffic.py`, `applog.py` and `mumblecompat.py` are **byte-identical copies** of the `xpc/` versions — everything that is not the simulator is shared by duplication, matching how the rest of the repo works. Only two modules differ:

| Module | Replaces | Role |
|---|---|---|
| `simlink.py` | `xpc/xplane.py` | SimConnect instead of X-Plane UDP |
| `inject.py` + `aimatch.py` | `xpc/bridge.py` + `plugin/` + `cslmatch.py` | AI aircraft instead of an XPPython3 plugin |

**`snapshot()` must stay field-for-field identical to `xpc/xplane.py`'s**, because `fsdpilot.py` and `voice.py` are shared copies that consume it. `SnapshotTest.test_field_names_match_the_xplane_client` pins that.

**MSFS needs no plugin, and TCAS is free.** SimConnect lets an outside process create AI aircraft (`AICreateNonATCAircraft`), so this is one process rather than XPC's client-plus-plugin split. Because the injected aircraft are real SimObjects, the cockpit's traffic display sees them without the hand-filled TCAS arrays that X-Plane requires.

**The object ID comes back asynchronously and Python-SimConnect loses it.** `AICreateNonATCAircraft` only queues the request; the real object ID arrives in a `SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID` message correlated by `dwRequestID`. The wrapper *does* handle that message — but it stores the result in `os.environ["SIMCONNECT_OBJECT_ID"]` with **no request correlation**, so creating twenty aircraft overwrites one global and you cannot tell which ID belongs to which. `inject.py` therefore wraps `my_dispatch_proc`, records `requestID → objectID` in its own table, and hands the message on.

**Two SimVar traps.** `TRANSPONDER CODE:1` is **BCD** — reading `0x1200` as decimal gives 4608 and the wire squawk is garbage. And `PLANE_PITCH_DEGREES` / `PLANE_BANK_DEGREES` are in **radians despite the name**, with the opposite sign convention to FSD (nose-up is negative in the sim).

**Model matching reads the sim's installed aircraft, not CSL packages.** `aimatch.py` walks each package tree for `aircraft.cfg`, taking `icao_type_designator` from `[GENERAL]` and one `title` + `icao_airline` per `[FLTSIM.n]` section. The title string is what `AICreateNonATCAircraft` takes, so it must be reproduced exactly. The fallback chain is the same as `cslmatch.py`'s and the two `FAMILIES` tables are meant to stay in sync.

Three things only a real install revealed — the synthetic `aircraft.cfg` tests all passed while a live scan found 10 liveries instead of 375:

- **The package directory must come from `UserCfg.opt`.** `InstalledPackagesPath` can point anywhere; on the dev machine it is `D:\MSFS2022` (257 aircraft) while the guessed `%APPDATA%\Microsoft Flight Simulator\Packages` holds none. Guessing paths silently misses the whole install.
- **`attachments/` is not aircraft.** Fenix-style addons put dozens of component configs under it, each with a `[GENERAL]` section and a `title` but no type designator. Treated as aircraft they pollute the table and one of them ends up standing in for everyone.
- **`icao_type_designator` is not clean.** Values like `"A359 ULR"` and `"A-319 CFM SL"` occur; `_clean_icao` takes the first token and requires 2–4 alphanumerics, because a bogus code in the index means real aircraft of that type never match. The ultimate fallback also prefers a model that *has* a type code.

## Related repositories

This repo is the **voice layer** of a three-part network. The other two live alongside it and own the contracts this one consumes:

| Path | Repo | Role |
|---|---|---|
| `C:\Docs\Dev\can-fsd` | can-fsd | Go FSD daemon — pilot/controller connections, flight plans, and the live datafeed. Docs: `Readme.md` |
| `C:\Docs\Dev\can-web` | can-web | Astro + Vue website — accounts, roster, radar, docs. Owns the MySQL schema (Prisma). Docs: `CLAUDE.md` |

Two integration points, both hardcoded here:

**Authentication → can-web.** `server/login.py` POSTs `{cid, password}` to `https://airwaysn.org/api/v1/public/auth`, implemented at `can-web/src/pages/api/v1/public/auth.ts`. That route compares against the cleartext `user.password` column (the FSD network password, which equals the member's website password) and additionally **rejects `user.rating < 1`** — an unrated member cannot use voice even with correct credentials. Failures are rate-limited per ASN ID, so a client stuck in a reconnect loop with a bad password will lock that account out of voice for the window. Because the Murmur user id is `int(name)`, the Mumble username must be the numeric ASN ID. The `900` / `p@ssw0rd` ATIS account in `login.py` bypasses the API entirely and is local to this repo.

**ATIS datafeed → can-fsd.** `server/ATIS/request.py` polls `https://data.airwaysn.org/v1/data.json`, can-fsd's datafeed (`internal/api`, HTTP port 20350), and `server/ATIS/mumble.py` speaks every `atis[]` entry it finds. It consumes `atis[].callsign`, `.frequency` and `.text_atis` — can-fsd guarantees `text_atis` is a JSON array, never null, and has golden-file tests pinning that document. Two details owned by can-fsd: a station lands in `atis[]` only if its callsign ends in `_ATIS`, and `frequency` is a full MHz string (`"128.500"`) where **`199.998` means "no frequency set"** — never build a `FREQ_*` channel from it. Splitting `text_atis` on `|` into English|Chinese is a convention of *this* repo, not of the datafeed.

**Which clients speak FSD.** Two do, and they are the only ones: `atis/fsdclient.py` logs a station in as an `_ATIS` controller (`#AA`), and `xpc/fsdpilot.py` logs an aircraft in as a pilot (`#AP`). Packet layouts for both come from can-fsd's `internal/fsd/conn.go`, `handler.go` and `docs/protocol.md`.

The other three — `client/`, `xplane_client/`, `controller/` — speak **only** Mumble and never touch the FSD port. A controller's presence on the network comes from whatever ATC client they run (EuroScope and friends), exactly as TrackAudio does it; keep it that way, because the voice server has no roster check and an FSD login from `controller/` would imply one.

In both FSD clients the ninth `$ID` field (challenge) is deliberately omitted so the server never starts a VATSIM `$ZC` challenge that only official clients hold keys for.

No authorisation is shared: can-fsd checks the `division` roster before a controller may staff a position, but the voice server has no equivalent — any account that authenticates can join any `FREQ_*` channel.

Naming has drifted across the three: can-web now calls the network **Cerulean Aviation Network (formerly AirwaySN)** and can-fsd takes its network name from `config.json`'s `version`, while this repo still hardcodes `airwaysn.org`, `data.airwaysn.org` and the Mumble host `hjdczy.top`.

## Reference docs

- `xplane_client/API.md` — X-Plane UDP protocol notes (BECN discovery on `239.255.1.1:49707`, RREF requests, dataref precision) and a SimConnect-vs-X-Plane comparison table. Applies to `xpc/xplane.py` too, except that XPC subscribes rather than polls.
- `client/API.md` — vendored upstream pymumble API reference, not project documentation.
