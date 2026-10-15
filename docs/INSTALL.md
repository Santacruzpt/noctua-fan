# Installation guide

This guide covers three parts. Each one builds on the one before it:

1. **[The fan controller on the Raspberry Pi](#part-1--fan-controller-on-the-raspberry-pi):**
   wiring, OS preparation, installation and checks.
2. **[The HTTP API on the Pi](#part-2--http-api-on-the-raspberry-pi):**
   needed only if you want to use Open WebUI or other remote clients.
3. **[The Open WebUI tool](#part-3--open-webui-tool):** on the machine that
   runs Open WebUI.

If you have several Pis, repeat parts 1 and 2 on each one. Part 3 is done once.

At the end you'll find sections on [upgrading](#upgrading),
[uninstalling](#uninstalling) and [troubleshooting](#troubleshooting).

---

## Part 1 — Fan controller on the Raspberry Pi

### 1.1 What you need

- A **Raspberry Pi 4** running **Raspberry Pi OS Bookworm or later** (Lite or
  Desktop, 32- or 64-bit).
- A **Noctua NF-A4x10 5V PWM** fan.
- **4 male-to-female jumper wires.** The male ends go into the fan's 4-pin
  connector and the female ends onto the Pi's header pins.
- Network access to the Pi (SSH or a keyboard and screen).

> ⚠️ Use only the **5V** version of the fan. A 12 V Noctua fan won't run
> properly from the Pi's 5 V pin.

### 1.2 Wire the fan

**Power the Pi off and unplug it first.**

| Fan wire | Function | Pi header pin |
|---|---|---|
| Black  | Ground       | **Pin 6** (GND) |
| Yellow | +5 V power   | **Pin 4** (5 V) |
| Blue   | PWM control  | **Pin 12** (GPIO18) |
| Green  | Tachometer   | **Pin 18** (GPIO24) |

Hold the Pi with the USB ports facing down. Pin 1 is then the top-left pin,
nearest the SD-card end; odd numbers run down the left column and even
numbers down the right.

```
        left  right
 3V3    (1)   (2)   5V
        (3)   (4)   5V      <- yellow
        (5)   (6)   GND     <- black
        (7)   (8)
        (9)   (10)
        (11)  (12)  GPIO18  <- blue
        (13)  (14)
        (15)  (16)
        (17)  (18)  GPIO24  <- green
```

Some notes on the wiring:
- No resistors or other parts are needed. The fan accepts the Pi's 3.3 V PWM
  signal, and the app enables the Pi's internal pull-up on the tach line.
- **Never connect the green wire to 5 V.** The Pi's GPIO pins are 3.3 V only.
- The green wire is optional. Without it everything works except RPM readings
  (see `tach_gpio` in [Configuration](../README.md#configuration)).
- Until the software is running, the fan spins at **full speed**. That's
  normal and is the fan's fail-safe.

### 1.3 Prepare the operating system

If you are setting up a new SD card, use
[Raspberry Pi Imager](https://www.raspberrypi.com/software/). Under
**Edit settings**:

- Set a **hostname** that says where the Pi is, such as `office-pi` or
  `garage-pi`. This makes several Pis easy to tell apart later.
- Create a user. The user Imager creates is automatically in the `gpio`
  group, which lets it run `noctua-fan` without `sudo`.
- Under **Services**, enable **SSH**.

Boot the Pi, log in over SSH (`ssh <user>@<hostname>.local`), then update the
system and install git:

```sh
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git
```

**Recommended if you'll use the Open WebUI tool:** give the Pi a fixed IP
address. The simplest way is a DHCP reservation in your router. The tool
connects by IP, so the address mustn't change. To see the Pi's current
address, run:

```sh
hostname -I
```

### 1.4 Install

```sh
cd ~
git clone https://github.com/Santacruzpt/noctua-fan.git
cd noctua-fan
sudo ./install.sh          # or: sudo ./install.sh --api   (see Part 2)
```

The installer does the following:

| Step | Details |
|---|---|
| Installs `python3-lgpio` | needed for RPM readout |
| Copies the program | `/usr/local/bin/noctua-fan` |
| Creates the config | `/etc/noctua-fan.toml`; an existing config is never overwritten |
| Enables hardware PWM | adds `dtoverlay=pwm,pin=18,func=2` to `/boot/firmware/config.txt` |
| Installs the service | `noctua-fan.service`, enabled at boot |

If the installer added the PWM overlay (it does this on the first install), it
tells you to reboot:

```sh
sudo reboot
```

### 1.5 Check that it works

Log in again after the reboot and work through these checks.

**1. Is hardware PWM available?**

```sh
ls /sys/class/pwm/
```

The output should include `pwmchip0`. If it doesn't, see
[Troubleshooting](#pwmchip0-does-not-exist).

**2. Is the service running?**

```sh
systemctl status noctua-fan --no-pager
```

It should show `active (running)`. The log lines look like
`started: 25000 Hz on pwmchip0/pwm0, tach GPIO24, ...` and
`temp 48.2°C -> duty 26%`.

**3. Current status:**

```sh
noctua-fan status
```

```
mode : auto
temp : 48.2 °C
duty : 26 %
rpm  : 1530 rpm (31 % of 5000 rpm max)
```

**4. Is the fan really under control?** Run it at full speed, check the
reading, then set it low and check again:

```sh
noctua-fan set 100; sleep 6; noctua-fan speed    # expect roughly 4500-5500 rpm, and audible
noctua-fan set 20;  sleep 6; noctua-fan speed    # expect roughly 1100-1400 rpm, near silent
noctua-fan auto                                  # back to temperature control
```

If the speed doesn't change, see
[Troubleshooting](#the-fan-always-runs-at-full-speed). If `rpm` shows `n/a`
or 0, see [Troubleshooting](#rpm-shows-na-or-0).

**5. Speed history.** The first sample is stored after a minute:

```sh
noctua-fan stats
```

On a fresh install this says that the history covers less than the requested
period. That's expected; it fills up over the next 24 hours.

### 1.6 Adjust the settings (optional)

All settings are in `/etc/noctua-fan.toml`, and each one is commented. After
editing the file, restart the service:

```sh
sudo nano /etc/noctua-fan.toml
sudo systemctl restart noctua-fan
```

The setting you're most likely to change is `curve`, the temperature → fan
speed table. For a quieter Pi that runs a little warmer, try:

```toml
curve = [[50, 0], [55, 20], [65, 50], [75, 100]]
```

See the [README](../README.md#configuration) for every setting.

---

## Part 2 — HTTP API on the Raspberry Pi

The API lets other machines read and control the fan over the network, and
the Open WebUI tool needs it. **Do this on every Pi you want to reach from
Open WebUI.**

### 2.1 Install

From the `noctua-fan` folder, run the installer again with `--api`. It's safe
to run it again at any time.

```sh
cd ~/noctua-fan
sudo ./install.sh --api
```

This adds the following:
- **`noctua-fan-api.service`**, which runs `noctua-fan serve` on **port 9102**
  as an unprivileged user.
- **`/etc/noctua-fan/api-token`**, a random secret that is required for
  *changing* the fan speed. Reading needs no token.

### 2.2 Check it on the Pi

```sh
systemctl status noctua-fan-api --no-pager      # active (running)
curl -s http://127.0.0.1:9102/status
```

The second command should print JSON like this:

```json
{"pid": 612, "time": 1792050000.0, "temp": 48.2, "duty": 26.0, "rpm": 1530.0, "mode": "auto", "rpm_pct": 30.6, "max_rpm": 5000}
```

### 2.3 Check it from the Open WebUI machine

Run this on the computer that runs Open WebUI, using the Pi's IP address:

```sh
curl -s http://<pi-ip>:9102/status
```

If this fails, the tool won't work either; see
[Troubleshooting](#the-api-cant-be-reached-from-another-machine).

### 2.4 Get the token

The Open WebUI tool needs the token to change fan speeds:

```sh
sudo cat /etc/noctua-fan/api-token
```

Keep it private: anyone who has it can change the fan speed. To create a new
token, run
`sudo rm /etc/noctua-fan/api-token && sudo ./install.sh --api`, then update
the tool's settings.

**Read-only option.** If you don't want the fan controlled remotely, delete
the token file and restart the API. Reading still works.

```sh
sudo rm /etc/noctua-fan/api-token
sudo systemctl restart noctua-fan-api
```

**Security note.** The API has no encryption and allows reads without a
token, so keep it on your home network. Don't forward port 9102 on your
router.

---

## Part 3 — Open WebUI tool

This is done once, in Open WebUI, no matter how many Pis you have.
It was tested with Open WebUI 0.12.0.

### 3.1 Before you start

- Part 2 is done on every Pi, and step 2.3 works from the Open WebUI machine.
- You have each Pi's **IP address** (`hostname -I` on the Pi) and **token**
  (step 2.4).
- You are an Open WebUI **admin**, or have permission to create tools.

> **Use IP addresses, not `.local` names.** The Open WebUI Docker image can't
> resolve mDNS names such as `office-pi.local`, even when the host can.
> Hostnames that your router's DNS serves (for example `office-pi.lan`) work.

### 3.2 Add the tool

1. Open [`openwebui/noctua_fan_tool.py`](../openwebui/noctua_fan_tool.py) and
   copy its entire contents. On GitHub, use the **Raw** view, or the "copy
   raw file" button.
2. In Open WebUI, go to **Workspace → Tools** and click **+** (create a new
   tool).
3. Replace all the example code in the editor with the copied code.
4. Check the name and ID fields. They default to **Noctua Fan** and
   **noctua_fan** from the file header; keep the ID `noctua_fan`.
5. Click **Save**.

### 3.3 Configure the Pis (Valves)

In **Workspace → Tools**, click the **gear icon (Valves)** next to
*Noctua Fan* and fill in these settings:

| Valve | What to enter | Example |
|---|---|---|
| `pis` | one `name=IP` entry per Pi, separated by commas. Add `:port` only if you changed it from 9102. | `office=192.168.1.20, garage=192.168.1.31` |
| `api_tokens` | the token from step 2.4. Use **one token** if all Pis share it, or **`name=token`** entries per Pi. Leave it empty for read-only. | `office=Xy3…, garage=Qp9…` |
| `timeout` | seconds to wait for a Pi | `10` (the default) |

How to choose names:
- Pick names you'll actually use in chat, like `office`, `garage` or `attic`.
  The model matches them loosely, so "the garage pi" finds `garage`.
- A bare token in `api_tokens` (without `name=`) applies to every Pi that
  doesn't have its own entry. For example, `SHARED, garage=OTHER` means
  `garage` uses `OTHER` and every other Pi uses `SHARED`.

Click **Save**.

### 3.4 Enable the tool for a model

Choose one of these:

- **Always on for one model:** go to **Workspace → Models**, edit the model
  you chat with, tick **Noctua Fan** under **Tools**, and
  save.
- **Per chat:** in the chat input, open the **+ / tools** menu and switch on
  **Noctua Fan**.

### 3.5 Test it

Ask these in a new chat:

| Ask | Expected result |
|---|---|
| *How fast are my Raspberry Pi fans spinning?* | every Pi, each speed in rpm and as % of max |
| *What was the max and average fan speed on the office pi over the last 24 hours?* | that Pi only |
| *Set the garage pi fan to 70 percent.* | confirms that the speed was set |
| *Put the garage fan back on automatic.* | confirms the switch back |

Then check on the Pi that the speed change really happened:

```sh
noctua-fan status     # mode: manual after the "70 percent" request, auto afterwards
```

### 3.6 Using the Open WebUI API (optional)

The browser runs tools automatically. When you call Open WebUI's API from a
script, pass `tool_ids` and the **legacy** function-calling mode. Otherwise
the API returns the tool call without running it.

```sh
curl -s http://localhost:8080/api/chat/completions \
  -H "Authorization: Bearer $OPENWEBUI_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model": "<your-model-id>",
       "messages": [{"role": "user", "content": "How fast are my Pi fans spinning?"}],
       "tool_ids": ["noctua_fan"],
       "params": {"function_calling": "legacy"},
       "stream": false}'
```

### 3.7 Installing or updating the tool from the command line (optional)

The steps in 3.2 can also be scripted against the Open WebUI API with an
admin API key. Run this from the `noctua-fan` folder:

```sh
OWUI=http://localhost:8080
KEY=...your Open WebUI API key...

python3 -c 'import json; print(json.dumps({"id": "noctua_fan", "name": "Noctua Fan",
  "content": open("openwebui/noctua_fan_tool.py").read(),
  "meta": {"description": "Raspberry Pi Noctua fan speed and control", "manifest": {}}}))' > /tmp/tool.json

# first install:
curl -s $OWUI/api/v1/tools/create -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' --data-binary @/tmp/tool.json
# later updates (valves are kept):
curl -s $OWUI/api/v1/tools/id/noctua_fan/update -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' --data-binary @/tmp/tool.json

# valves:
curl -s $OWUI/api/v1/tools/id/noctua_fan/valves/update -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' \
     -d '{"pis": "office=192.168.1.20, garage=192.168.1.31", "api_tokens": "TOKEN", "timeout": 10}'
```

---

## Upgrading

**On each Pi:**

```sh
cd ~/noctua-fan
git pull
sudo ./install.sh          # add --api if you use the API
```

The installer replaces the program and the service files, but **keeps your
`/etc/noctua-fan.toml` and your API token**. New settings take their default
values until you add them to your config. If a release notes a new setting
you want, copy it from `noctua-fan.toml` in the repo.

**In Open WebUI:** open the tool in **Workspace → Tools**, replace the code
with the new `openwebui/noctua_fan_tool.py`, and save. Your valve settings are
kept.

---

## Uninstalling

**On the Pi:**

```sh
sudo systemctl disable --now noctua-fan
sudo systemctl disable --now noctua-fan-api 2>/dev/null || true   # only if you installed the API
sudo rm -f /etc/systemd/system/noctua-fan.service /etc/systemd/system/noctua-fan-api.service
sudo systemctl daemon-reload
sudo rm -f /usr/local/bin/noctua-fan /etc/noctua-fan.toml
sudo rm -rf /etc/noctua-fan /var/lib/noctua-fan
```

Then remove the `dtoverlay=pwm,pin=18,func=2` line (and the comment above it)
from `/boot/firmware/config.txt`, and reboot. With no software controlling
it, the fan will run at full speed until you unplug it.

**In Open WebUI:** go to **Workspace → Tools**, open the *Noctua Fan* menu,
and click **Delete**.

---

## Troubleshooting

### `pwmchip0` does not exist

The PWM overlay isn't loaded. Check the config file:

```sh
grep pwm /boot/firmware/config.txt      # should show dtoverlay=pwm,pin=18,func=2
```

If the line is missing, run `sudo ./install.sh` again, then reboot. If it's
present, make sure you rebooted after installing.

### The fan always runs at full speed

- **The service isn't running.** Check with
  `systemctl status noctua-fan` and `journalctl -u noctua-fan -n 50`.
- **The blue wire isn't on pin 12.** If the PWM line floats, the fan runs at
  full speed.
- **The temperature really is high.** `noctua-fan status` shows the
  temperature; at 75 °C or above the default curve runs the fan at 100 %.

### RPM shows `n/a` or 0

- `n/a` with a log line `python3-lgpio not installed`: run
  `sudo apt install python3-lgpio`, then `sudo systemctl restart noctua-fan`.
- `0` while the fan is clearly spinning: check that the green wire is on
  **pin 18** (GPIO24).
- `0` at 0 % duty: the fan may really have stopped, which many current
  Noctua PWM fans do at 0 %. To keep it spinning at the minimum speed
  instead, set `allow_stop = false`.
- A log line `fan stall?` means the fan has duty > 0 but reports 0 rpm. Check
  the fan and its wiring.

### `permission denied` when running `noctua-fan`

Your user isn't in the `gpio` group. Add it, then log out and back in:

```sh
sudo usermod -aG gpio $USER
```

You can also run the command with `sudo`.

### `noctua-fan stats` says "no speed history yet"

The service stores its first sample a minute after it starts. Check that the
service is running (`systemctl status noctua-fan`).

### The API can't be reached from another machine

1. On the Pi, `curl http://127.0.0.1:9102/status` must work. If it doesn't,
   check `journalctl -u noctua-fan-api -n 50`.
2. Check that `/etc/noctua-fan.toml` doesn't set `api_bind = "127.0.0.1"`.
   That setting only accepts connections from the Pi itself.
3. If you enabled a firewall (Raspberry Pi OS has none by default), allow the
   port: `sudo ufw allow 9102/tcp`.
4. Use the IP address from `hostname -I`, and make sure both machines are on
   the same network.

### The Open WebUI tool says "cannot reach the fan API"

- Run step 2.3 from the Open WebUI machine.
- **"Name or service not known" with a `.local` address:** the container
  can't resolve mDNS names. Use the Pi's IP address in the `pis` valve.
- If Open WebUI runs in Docker with a bridge network (not `--network host`),
  LAN IP addresses still work. `localhost` and `127.0.0.1` mean the container
  itself, not the host.

### The tool says changing the speed "is disabled"

No token is configured for that Pi in the `api_tokens` valve. Add it as
described in step 3.3.

### The tool reports `fan API error 401`

The token in `api_tokens` doesn't match the Pi's
`/etc/noctua-fan/api-token`. Copy it again; watch for missing characters or
extra spaces.

### The tool reports `fan API error 403: fan control over the API is disabled`

The Pi has no token file, so its API is read-only. Run
`sudo ./install.sh --api` on that Pi to create one.

### The model doesn't use the tool, or answers without numbers

- Check that the tool is enabled for that model or chat (step 3.4).
- Ask more directly, and mention the Pi or the fan, for example
  *"Using the fan tool, how fast is the office Pi fan?"*
- When using the API, pass `"params": {"function_calling": "legacy"}`
  (step 3.6).
