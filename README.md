<img width="1515" height="865" alt="image" src="https://github.com/user-attachments/assets/46332311-bf20-4ba0-9efa-98c852cff7c0" /># Valdas – live-tuning plugin for One ROM

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

- One ROM with the **RP2350** chip (the plugin is built for RP2350) ( I use RGB version, better to see errors)
- Windows PC with [TunerPro RT](https://www.tunerpro.net)
- Your ECU bin file and a matching XDF definition
- ADX file for tracking, maybe fits mine

## Install

1. Download `valdas-plugin-trace-v*.*.uf2` from the [Releases](../../releases) page.
2. Put One ROM into boot mode: ` short B (Bootsel) and gnd pinds and power up`.
<img width="1515" height="865" alt="image" src="https://github.com/user-attachments/assets/07740871-6066-4f7a-b4f7-eb0eabd92357" />
3. A new drive appears on your PC – this is the One ROM boot folder.
4. remove cable from ` short B (Bootsel) and gnd pinds and power up`.
5. Copy the `.uf2` file into that drive.
6. The drive disappears by itself when flashing is done and One ROM restarts with Valdas.

Optional: check the download is not damaged by comparing its SHA-256 with `SHA256SUMS.txt`.

## Connect to TunerPro RT

1. Plug One ROM into the PC (and into the ECU's ROM socket).
2. In Windows Device Manager, note the new **COM port**.
3. Open TunerPro RT and load your bin and XDF.
4. **Tools → Preferences → 
<img width="1486" height="807" alt="image" src="https://github.com/user-attachments/assets/ecbbd72f-c0ff-46cc-81b9-a83f9c06e4b5" />
5. **Emulation → Upload bin** to send your tune to One ROM.
6. Turn on **Emulate** to edit live.
7. Turn on **Trace** to see which cells the ECU is reading.


## Good to know

- Autosave works for up to **63 changed pages** at a time. If you change more than that in one go, upload the whole bin again.
- If saved edits don't match the current bin, Valdas ignores them and starts from the uploaded bin.
- Only one program can read the One ROM USB log at a time.

## Tested on

| ECU / car | ROM type | Status |
|-----------|----------|--------|
| Motronicm2.8.1 | 27C512 | Working |

Tested it on something else? Please open an [issue](../../issues) and tell us – it helps others.

## Safety

Use at your own risk. A wrong tune can damage an engine. Always check your changes, and don't tune while driving on public roads.

## Credits

- Built for and with [One ROM](https://github.com/piersfinlayson/one-rom) by Piers Finlayson (MIT License). Thank you!
- TunerPro RT by Mark Mansur.
- Valdas speaks a protocol compatible with the Moates Ostrich so TunerPro RT can talk to it. Valdas is not made by, connected to, or supported by Moates.
- Claude ai, most job i did using it, i am not programer, but it helped to make it

## License

Free for personal, non-commercial use. See `LICENSE`.
Parts that come from One ROM stay under their MIT License – see `LICENSE-ONEROM`.
