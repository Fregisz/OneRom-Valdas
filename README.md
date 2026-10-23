# Valdas – live-tuning plugin for One ROM

Valdas is a free system plugin for [One ROM](https://onerom.org) that turns it into a live-tuning ROM emulator for ECUs.
It talks to **TunerPro RT** over USB, so you can change your tune while the engine is running and use **Trace** to see which map cells the ECU is reading in real time.

> Valdas is a hobby project. It is shared for free and is **not for sale**.

---

## Features

- **Live tuning** from TunerPro RT – changes go straight into the emulated ROM, no chip burning
- **Trace** – EPROM address trace on request, so TunerPro RT can show which cells the ECU is reading
- **Autosave** – your live edits are saved to One ROM's flash and restored after power-off
- Shows up as a USB serial (COM) port – no special driver for TunerPro
- Install by drag-and-drop into the One ROM boot drive – no programmer needed

## What you need

- One ROM with the **RP2350** chip (the plugin is built for RP2350)
- USB cable
- Windows PC with [TunerPro RT](https://www.tunerpro.net)
- Your ECU bin file and a matching XDF definition

## Install

1. Download `valdas-plugin-trace-v1.0.0.uf2` from the [Releases](../../releases) page.
2. Put One ROM into boot mode: `<!-- fill in exactly how, e.g. hold the BOOT button while plugging in USB -->`.
3. A new drive appears on your PC – this is the One ROM boot folder.
4. Copy the `.uf2` file into that drive.
5. The drive disappears by itself when flashing is done and One ROM restarts with Valdas.

Optional: check the download is not damaged by comparing its SHA-256 with `SHA256SUMS.txt`.

## Connect to TunerPro RT

1. Plug One ROM into the PC (and into the ECU's ROM socket).
2. In Windows Device Manager, note the new **COM port**.
3. Open TunerPro RT and load your bin and XDF.
4. **Tools → Preferences → Emulation**: choose **Ostrich 2.0** as the emulator type and select that COM port.
5. **Emulation → Upload bin** to send your tune to One ROM.
6. Turn on **Emulate** to edit live.
7. Turn on **Trace** to see which cells the ECU is reading.

`<!-- Add screenshots: docs/images/tunerpro-settings.png -->`

## Good to know

- Autosave works for up to **63 changed pages** at a time. If you change more than that in one go, upload the whole bin again.
- If saved edits don't match the current bin, Valdas ignores them and starts from the uploaded bin.
- Only one program can read the One ROM USB log at a time.

## Tested on

| ECU / car | ROM type | Status |
|-----------|----------|--------|
| `<!-- fill in -->` | `<!-- e.g. 27C512 -->` | Working |

Tested it on something else? Please open an [issue](../../issues) and tell us – it helps others.

## Safety

Use at your own risk. A wrong tune can damage an engine. Always check your changes, and don't tune while driving on public roads.

## Credits

- Built for and with [One ROM](https://github.com/piersfinlayson/one-rom) by Piers Finlayson (MIT License). Thank you!
- TunerPro RT by Mark Mansur.
- Valdas speaks a protocol compatible with the Moates Ostrich so TunerPro RT can talk to it. Valdas is not made by, connected to, or supported by Moates.

## License

Free for personal, non-commercial use. See `LICENSE`.
Parts that come from One ROM stay under their MIT License – see `LICENSE-ONEROM`.
