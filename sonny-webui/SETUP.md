# Sonny Homelab Setup Guide (AI PC + Gaming PC)

All steps use single-line Command Prompt (`cmd.exe`) commands — no PowerShell policy changes, no unsigned `.ps1` scripts, no model downloads, and zero public internet exposure.

## Prerequisites

| Requirement | Check | Notes |
| --- | --- | --- |
| Windows 11 Pro | `winver` | Both PCs. `RemoteSigned` execution policy is fine — the kit ships no `.ps1` files. |
| Python **3.11+** | `python --version` | Needed on **both** PCs. 3.12, 3.13, and 3.14 all work. |
| Docker Desktop | `docker version` | AI PC only. |
| Ollama + your models | `ollama list` | AI PC only. Must show `sonny7-uncensored-8b:latest` and `sonny7-vision-4b-64k:latest`. |
| Tailscale (connected) | `tailscale ip -4` | Both PCs. |

**Python command name:** every command below says `python`. On Windows that is the python.org install, which is what the kit expects. Do **not** type `python3` — that name belongs to Linux/macOS, and on a stock Windows PC it triggers the Microsoft Store stub:

```
Python was not found; run without arguments to install from the Microsoft Store...
```

If `python --version` fails but `py -3 --version` works, substitute `py -3` for `python` in every command below. If neither is found:

```cmd
winget install --id Python.Python.3.12 -e
```

## Machine Roles & Ports

| Machine | Role | Services & Bindings |
| --- | --- | --- |
| **AI PC** (7800X3D · 64 GB · RTX 5090 32 GB) | Main AI (Ollama) + Open WebUI hub + AI-PC control | Open WebUI `127.0.0.1:3000` (Tailscale HTTPS `:443`), existing Ollama `127.0.0.1:11434`, Open Terminal `127.0.0.1:8001` |
| **Gaming PC** (7800X3D · 64 GB · RTX 5090 32 GB) | **Coding** (`C:\SonnyWork\code`) & **Media Generation** (`C:\SonnyWork\media`) ONLY — never runs Open WebUI or main AI | Open Terminal `<gaming-tailscale-ip>:8002` (closed & unloaded whenever Gaming Mode is ON) |

---

## Step 0 — Verify the Kit Before Touching Your PCs (~30 sec, optional)

On **either** PC, from inside the copied `sonny-webui` folder, confirm every file is intact and no secrets leaked into the tree:

```cmd
python scripts\validate_kit.py
```

Expected: `[PASS] Sonny kit validated cleanly (31 files checked, 0 issues).`

The kit's own test suite is also runnable and needs no Docker, Ollama, or network (all servers are mocked in-process):

```cmd
cd /d C:\SonnyWebUI
```

```cmd
python -m unittest discover -s tests
```

Expected: `Ran 25 tests` then `OK`.

---

## Step 1 — Generate Private Local Keys (AI PC, ~1 min)

1. Copy the `sonny-webui` folder to `C:\SonnyWebUI` on the **AI PC**.
2. On the **Gaming PC**, open `cmd.exe` and get its `100.x.y.z` Tailscale IP:
   ```cmd
   tailscale ip -4
   ```
3. On the **AI PC**, open `cmd.exe` and generate your private `.env` and TOML keys (replace `<GAMING_TS_IP>` with the IP from step 2):
   ```cmd
   cd /d C:\SonnyWebUI
   ```
   ```cmd
   python scripts\init_config.py --gaming-tailscale-ip <GAMING_TS_IP>
   ```
   *(If `tailscale` isn't on PATH, add `--ai-tailscale-ip <AI_TS_IP>` as well.)*

---

## Step 2 — Install & Auto-Configure Sonny on the AI PC (~3 min)

1. Start AI-PC Open Terminal (`127.0.0.1:8001`) and Open WebUI (`127.0.0.1:3000`):
   ```cmd
   C:\SonnyWebUI\ai-pc\INSTALL_AI_PC.cmd
   ```
2. Open `http://127.0.0.1:3000` in your browser and create your single owner/admin account. *(New signups stay automatically locked for everyone else after your account is created.)*
3. Auto-register the **Sonny** model preset, automatic Ollama router, guarded dual-PC tool, and both PC terminal connections (prompts for your Open WebUI email/password locally; never prints keys):
   ```cmd
   python C:\SonnyWebUI\scripts\configure_webui.py
   ```
   *(If you run a media container like ComfyUI on the Gaming PC that you want Sonny able to stop automatically, add `--media-containers comfyui` — you can re-run this command anytime.)*

---

## Step 3 — Install Coding/Media Worker & Kill-Switch on the Gaming PC (~3 min)

1. Copy **only** `C:\SonnyWebUI\gaming-pc` (including the generated `gaming-pc.toml` and `firewall_rule.txt`) to the **Gaming PC** (for example `C:\SonnySetup\gaming-pc`).
2. On the **Gaming PC**, open **Command Prompt as Administrator** (so it also applies the Windows Firewall rule locking TCP `8002` to the AI PC's Tailscale IP only) and run:
   ```cmd
   C:\SonnySetup\gaming-pc\INSTALL_GAMING_PC.cmd
   ```
3. After it finishes, delete the temporary `C:\SonnySetup` folder if you like — everything active is installed cleanly in `C:\SonnyAgent` (with `C:\SonnyAgent\gaming-pc.toml` locked to your Windows user) and workspaces at `C:\SonnyWork\code` and `C:\SonnyWork\media`.

---

## Step 4 — How to Stop / Free the Gaming PC Anytime You Want to Game (<1 sec)

You have four ways to stop Sonny on the Gaming PC at any second:

1. **Hotkey on Gaming PC:** Press **`Ctrl + Alt + G`** (triggers `C:\SonnyAgent\GAME_MODE_ON.cmd`).
2. **Desktop / Xeneon Edge 14.5" Touchscreen:** Tap **`STOP SONNY - GAME MODE`** on the Gaming PC desktop (drag **`STOP SONNY - GAME MODE`**, **`ALLOW SONNY`**, and **`SONNY GAMING STATUS`** onto your Corsair Xeneon Edge screen for one-tap control).
3. **Ask Sonny in chat (AI PC or iPhone):**
   - *"Stop Gaming PC jobs"* → kills all running Sonny coding/media jobs immediately (`jobs_only`), keeps Gaming PC connected.
   - *"Lock Gaming Mode"* → kills all jobs, runs `GAME_MODE_ON.cmd`, closes port `8002`, and writes `C:\SonnyAgent\GAMING_MODE.lock`.
4. **Optional standalone media renderers:** Edit `C:\SonnyAgent\media_stop_list.txt` on the Gaming PC and uncomment any standalone containers/tools you run outside Sonny (e.g. `container:comfyui`, `process:ffmpeg.exe`, `process:blender.exe`) so `GAME_MODE_ON.cmd` stops those too. *(Windows, Steam, games, anti-cheat, Discord, OBS, Corsair iCUE, Tailscale, and broad `python.exe` are permanently blocked from being killed.)*

**When you finish gaming:** Tap **`ALLOW SONNY`** (`C:\SonnyAgent\GAME_MODE_OFF.cmd`) on the Gaming PC. Sonny can never turn Gaming Mode OFF remotely, and rebooting while locked stays locked.

---

## Step 5 — Enable Private iPhone & Touchscreen Access (~2 min)

1. On the **AI PC**, publish Open WebUI strictly inside your Tailscale tailnet (`tailscale serve` only — never public):
   ```cmd
   C:\SonnyWebUI\ai-pc\ENABLE_TAILSCALE_HTTPS.cmd
   ```
2. Copy the printed `https://<ai-pc-name>.<your-tailnet>.ts.net` link.
3. On your **iPhone** (with Tailscale connected), open that URL in Safari → sign in once (session lasts 4 weeks) → tap **Share → Add to Home Screen** → **Add**.
4. *(Optional)* Open `http://127.0.0.1:3000` on your AI PC's **Corsair Xeneon Edge 14.5" touchscreen** for an always-visible Sonny chat/control panel.

---

## Step 6 — Verify Everything End-to-End (~2 min)

1. On the **AI PC**, run the read-only live smoke check:
   ```cmd
   python C:\SonnyWebUI\scripts\smoke_test_live.py --require-gaming-online
   ```
2. In Sonny (`http://127.0.0.1:3000`), send:
   - `"Check homelab status"` → verifies read-only CPU/RAM/RTX 5090 VRAM on both PCs with no confirmation prompt.
   - Attach any image and ask `"What is in this image?"` → verifies automatic switch to your AI PC vision model without choosing a model.
   - `"Start a coding job on the Gaming PC running: echo hello from gaming pc"` → verifies the Confirm/Deny prompt and `C:\SonnyWork\code` tracking.
3. Tap **`STOP SONNY - GAME MODE`** on the Gaming PC, then run:
   ```cmd
   python C:\SonnyWebUI\scripts\smoke_test_live.py
   ```
   Confirm Gaming PC reports port `8002` closed (`GAMING MODE ON`) while AI PC chat and Ollama keep working normally. Tap **`ALLOW SONNY`** when ready to resume coding/media work.

---

## Day-to-Day Operations & Troubleshooting

- **Revoke all browser/iPhone sessions instantly:** Run `python C:\SonnyWebUI\scripts\init_config.py --gaming-tailscale-ip <GAMING_TS_IP> --rotate`, then `docker compose -f C:\SonnyWebUI\ai-pc\compose.yaml up -d` and `python C:\SonnyWebUI\scripts\configure_webui.py`.
- **Full interactive file browser & live terminal tabs:** Click the **Terminal** icon in Open WebUI's message bar and select **`AI-PC`** or **`Gaming-PC (Code & Media)`**; files, terminals, notes, and chats open as tabs side by side.
- **If `INSTALL_AI_PC.cmd` warns that container couldn't reach `host.docker.internal:11434`:** Confirm Docker Desktop is updated and Ollama is running (`http://127.0.0.1:11434/api/version`), and allow Docker Desktop's local bridge in Windows Firewall if blocked.
- **If Windows Open Terminal mentions `VCRUNTIME140.dll`:** Install Microsoft's official x64 Visual C++ Redistributable and rerun the start shortcut.
