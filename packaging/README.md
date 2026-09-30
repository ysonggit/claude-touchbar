# Root-level setup (Touch Bar hardware)

Nothing here is done by `ctb install`; it all changes kernel drivers, so you run it yourself.

1. **Stock driver prerequisite.** `apple-ib-tb` only activates when `apple-ibridge-hid` owns *both*
   iBridge HID interfaces, and `hid-sensor-hub` (loaded from the initramfs) takes the second one first.
   Blacklist it and rebuild the initramfs, or the stock bar is dead after every boot:
   `echo "blacklist hid_sensor_hub" | sudo tee /etc/modprobe.d/touchbar.conf && sudo update-initramfs -u`.
   `packaging/ctb-display status` shows who owns each interface.
2. **barkeep kernel modules** from https://github.com/mgd34msu/barkeep. Either install them (DKMS) or
   just build them (`bash ~/barkeep/scripts/build.sh`); `ctb-display` falls back to `insmod` from
   `~/barkeep`. Don't enable barkeep's own units; `ctb-bar.service` replaces them. barkeep is
   confirmed only on the 13" MacBookPro13,2; this Mac is a **MacBookPro13,3 (15")**, so treat it as
   an experiment. Note the known **suspend hang**; `ctb-sleep` works around it by handing the bar back
   to the stock drivers before sleeping.
3. `sudo packaging/install-root.sh`: installs `ctb-bar.service`, `/usr/local/lib/ctb/` (`ctb-display`, `ctb-launch`),
   the sleep hook, `/etc/ctb/bar.toml`, and the "Claude Touch Bar" app (desktop entry, icon, polkit policy).
   It does not enable or start anything.
4. Do the **M0 checklist** in the top-level README *before* enabling the service. Switch the display by
   hand with `sudo packaging/ctb-display up` and back with `sudo packaging/ctb-display down`.
5. **Known limitation:** after display mode, the stock row's *icons* only come back on reboot (the keys, touch and
   brightness work again straight away, but the row is black). Nothing reachable from Linux restarts the T1's renderer.
6. Start it from the **Claude Touch Bar** app (installed by step 3; it runs `systemctl start ctb-bar` through
   pkexec, so it asks for an admin password). To start it at every boot instead: `sudo systemctl enable ctb-bar`. If anything goes wrong: `sudo systemctl stop ctb-bar` (its
   `ExecStopPost` runs `ctb-display down`), `sudo packaging/ctb-display down` by hand, or reboot:
   nothing is blacklisted except `hid_sensor_hub`.
7. Remove everything: `sudo packaging/uninstall-root.sh` (then reboot to get the stock icons back).

## What `ctb-display` handles (all seen on this 13,3)
- `up`: stops `iio-sensor-proxy` and disables the ALS IIO buffer/trigger it left on (they hold `apple_ib_als`, which silently blocked unloading
  `apple_ibridge`), refuses to continue if `apple_ibridge` is still loaded (its probe forces USB config 1
  and kills the display session within ~1 s), loads barkeep, re-enumerates into config 2 and waits
  for `/dev/dfr0`. `ctb bar` switches the panel on after its first frame.
- `down`: unloads barkeep, re-enumerates into config 1 (runtime PM off, so it can't fail with -22),
  reloads the stock modules (`modprobe -a`: plain `modprobe a b c` treats `b c` as parameters),
  rebinds both HID interfaces to `apple-ibridge-hid`, retries once if the bar didn't activate
  (`ib: failed to open hid: -5`), and restarts `iio-sensor-proxy`.
