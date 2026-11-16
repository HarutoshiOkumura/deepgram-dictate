# deepgram-dictate

Talk instead of type, anywhere on your Linux desktop. Wisprflow but for Linux. 

Tap a key, say what you want to write, tap it again, and the text appears in whatever window you are using: a browser, a chat app, an editor, a terminal.
It works like Wispr Flow, but for Hyprland on Linux, and uses [Deepgram](https://deepgram.com) to turn speech into text.

It understands English by default, and Cantonese, Mandarin and Japanese with their own key combinations.

## How to use it

| Do this | What happens |
|---|---|
| Tap **Right Alt** | It starts listening (in English). Talk for as long as you like. |
| Tap **Right Alt** again | It stops and types what you said. |
| Hold **Right Alt** while you talk, then let go | Same thing, for quick phrases: it types when you let go. |
| Hold **Right Alt** and press **C** | Listen in Cantonese. Tap Right Alt to finish. |
| Hold **Right Alt** and press **M** | Listen in Mandarin. Tap Right Alt to finish. |
| Hold **Right Alt** and press **J** | Listen in Japanese. Tap Right Alt to finish. |
| Press **Esc** while it is listening | Cancel. Nothing gets typed. |

You will hear a short rising chime when it starts listening and a falling chime when it stops.
If you set up the optional status bar icon, a red microphone with the language (EN, 粵, 普, 日) shows while it is listening.

## What you need

- Linux with the **Hyprland** desktop (Wayland) and **PipeWire** for sound. Most current distributions use PipeWire.
- A **Deepgram account**. You can sign up at [console.deepgram.com](https://console.deepgram.com).
- **Python 3.13 or newer** and **[uv](https://docs.astral.sh/uv/)**, the Python package manager.

## Setup

The steps below take about ten minutes.
Commands are written for Fedora; on other distributions, install the packages that provide the same commands.

### Step 1: Install the helper programs

```sh
sudo dnf install wtype wl-clipboard pipewire-utils pulseaudio-utils libnotify libsecret
```

These provide the commands the app relies on:

| Command | Used for |
|---|---|
| `wtype` | typing the text into the active window |
| `pw-record`, `pw-play` | recording the microphone and playing the chimes |
| `pactl` | checking whether the microphone is muted |
| `notify-send` | showing an error message if something goes wrong |
| `secret-tool` | storing your API key safely in the system keyring |
| `wl-copy`, `wl-paste` | the optional paste mode |

You also need a keyring service running (GNOME Keyring or KWallet); most desktops start one automatically.

### Step 2: Download and install the app

```sh
git clone https://github.com/HarutoshiOkumura/deepgram-dictate.git ~/Projects/deepgram-dictate
cd ~/Projects/deepgram-dictate
uv tool install --editable .
```

This installs two commands into `~/.local/bin`:
`dictate`, which you use to set things up and check on the app, and `dictate-key`, which the keyboard shortcuts call.

### Step 3: Add your Deepgram API key

1. Open [console.deepgram.com](https://console.deepgram.com) and choose your project.
2. Go to **API Keys** and click **Create a New API Key**.
3. Give it a name (for example `linux dictation`), choose the **Member** role, and set it to **never expire**.
4. Copy the key, then run:

```sh
dictate set-key
```

Paste the key when asked (it stays hidden while you paste).
The app checks the key with Deepgram first and only saves it if it works.
It is saved in your system keyring, never in a plain file.

### Step 4: Start the background service

```sh
systemctl --user link "$PWD/systemd/deepgram-dictate.socket" "$PWD/systemd/deepgram-dictate.service"
systemctl --user enable --now deepgram-dictate.socket deepgram-dictate.service
```

The service now starts by itself every time you log in.

### Step 5: Add the extra languages

```sh
mkdir -p ~/.config/deepgram-dictate
cp examples/config.toml ~/.config/deepgram-dictate/config.toml
systemctl --user restart deepgram-dictate
```

Skip this step if you only want English.
Mandarin is written in Simplified characters by default; to get Traditional, change `zh-CN` to `zh-TW` in that file and restart the service again.

### Step 6: Add the keyboard shortcuts to Hyprland

```sh
cp hypr/dictation.conf ~/.config/hypr/dictation.conf
echo 'source = ~/.config/hypr/dictation.conf' >> ~/.config/hypr/hyprland.conf
hyprctl reload
```

If you use the JaKooLit Hyprland dotfiles, copy the file to `~/.config/hypr/UserConfigs/Dictation.conf` instead, and add `source = $UserConfigs/Dictation.conf` to the end of `~/.config/hypr/UserConfigs/UserKeybinds.conf`.

### Step 7 (optional): Show a microphone icon in Waybar

This shows a red microphone and the language while the app is listening, and hides itself the rest of the time.
Clicking it finishes the dictation; right-clicking cancels it.

Add this module to your Waybar config:

```jsonc
"custom/dictate": {
    "exec": "$HOME/.local/bin/dictate status --follow --waybar",
    "return-type": "json",
    "format": "{}",
    "tooltip": true,
    "on-click": "$HOME/.local/bin/dictate-key toggle",
    "on-click-right": "$HOME/.local/bin/dictate-key cancel"
}
```

Then add `"custom/dictate"` to one of the `modules-left`, `modules-center` or `modules-right` lists, and reload Waybar with `pkill -SIGUSR2 waybar`.

### Step 8: Check that everything works

```sh
dictate doctor
```

Every line should have a green check mark, ending with `All good.`
If something is marked with a red cross, the line tells you what to fix.

Now open any text box, tap Right Alt, say something, and tap Right Alt again.

## Changing settings

Settings live in `~/.config/deepgram-dictate/config.toml`.
Everything is optional; leave out anything you do not want to change.
After editing, run `systemctl --user restart deepgram-dictate`.

```toml
hold_threshold_ms = 400   # how long you must hold Right Alt for push-to-talk (shorter = a tap)
tail_ms = 150             # keep recording this long after you stop, so the last word is not cut off
max_session_s = 900       # stop automatically after this many seconds, in case you forget
inject = "type"           # "type" (default) or "paste" (see below)
sounds = true             # the start and stop chimes
sound_volume = 0.4        # chime volume, from 0 to 1
mip_opt_out = false       # opt out of Deepgram's Model Improvement Program
audio_source = ""         # which microphone to use; empty means the system default
api_base = "https://api.deepgram.com"   # for example https://api.eu.deepgram.com to keep audio in the EU

[profiles.yue]            # one block per language
label = "粵"              # what the status bar shows
language = "zh-HK"        # Deepgram language code
keyterms = ["Deepgram"]   # names or jargon you want recognised correctly
```

To find the name of a microphone for `audio_source`, run `pactl list short sources`.

**Typing versus pasting.**
By default the app types the text through a virtual keyboard, which works with every language and leaves your clipboard alone.
`paste` puts the text on the clipboard and presses Ctrl+V (Ctrl+Shift+V in terminals), which is faster for long text.
Paste mode recognises terminals by their window name, so a terminal it does not know would receive a stray Ctrl+V.

## Troubleshooting

Start with `dictate doctor`; it checks every part of the setup.
To watch what the app is doing while you try it, run:

```sh
journalctl --user -u deepgram-dictate -f
```

The log never contains what you said.

| Problem | What to do |
|---|---|
| Nothing happens when I press Right Alt | Run `dictate doctor`. If it says no hotkeys are loaded, check Step 6 and run `hyprctl reload`. |
| "Your microphone is muted" | Unmute it, for example with the mic-mute key on your keyboard. |
| "The microphone recorded pure silence" | The microphone may be muted in hardware, unplugged, or the wrong one; check `audio_source`. |
| "Deepgram rejected the API key" | Create a new key (Step 3) and run `dictate set-key` again. |
| Text arrives in the wrong language | Use Right Alt + C, M or J to pick the language before you start talking. |
| Language keys (Right Alt + C, M, J) do nothing | Make sure Step 5 was done; the keys need those language profiles. |
| A dictation failed because of a network problem | The recording is kept in `~/.cache/deepgram-dictate/failed/`, so you can transcribe it later. |

## How it works

- A small background service waits for commands from the keyboard shortcuts.
- When you start, it records your microphone and streams the sound to Deepgram while you talk, so the text is ready about a third of a second after you stop.
- The recording is also kept in memory until the end: if the connection drops, the whole recording is sent again in one piece, and if that fails too it is saved to disk.
- Picking a language a few seconds into a dictation works, because the audio so far is re-sent in the new language, but the text then takes that many extra seconds to arrive.
- If you hold Right Alt and press some other key (for example Right Alt + Tab), the dictation is cancelled instead of left running.
  To notice this, the app checks whether Right Alt is still held down; it never reads what you type.

### Why these choices

The settings were picked by testing them on recorded speech (Google's FLEURS dataset):

- **One language at a time.** Deepgram's automatic multi-language mode made 54% more mistakes in English and confused Mandarin with Cantonese in about a quarter of the clips, so each language has its own key instead.
- **No mid-sentence splitting.** By default Deepgram cuts speech into pieces at every tiny pause, which loses context; turning that off cut mistakes by 8 to 27% in all four languages.

## For developers

```sh
uv sync                                          # install development tools
uv run pytest                                    # run the tests (they use a local fake Deepgram, no key needed)
uv run ruff check . && uv run ruff format --check .   # lint and formatting
```

The code is in `src/deepgram_dictate/`:

| File | What it does |
|---|---|
| `hotkey.py` | the tiny client the keyboard shortcuts run (`dictate-key`) |
| `cli.py` | the `dictate` command |
| `daemon.py` | the background service |
| `controller.py` | decides what each key press means (tap, hold, language, cancel) |
| `session.py` | one dictation: record, stream, fall back, return the text |
| `deepgram.py` | talking to Deepgram |
| `audio.py` | recording the microphone |
| `text.py` | cleaning up the transcript (spacing and punctuation for Chinese and Japanese) |
| `inject.py` | typing or pasting the text |
| `keystate.py` | checking whether Right Alt is still held down |
| `feedback.py` | chimes and notifications |
| `config.py` | reading `config.toml` |
| `doctor.py` | the `dictate doctor` check |
