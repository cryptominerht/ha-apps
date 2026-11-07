# TV Kid Lock

After `watch_minutes` of watching, the TV pauses, its internet is blocked through a
UniFi firewall policy, and a quiz opens in the TV's browser. A set number of correct
answers in a row unlocks it.

## Setup
1. Go to the **Configuration** tab and fill in:
   - **TV IP address**: the TV's fixed IP
   - **Parent PIN**: opens the parent page
   - **UniFi block switch**: e.g. `switch.block_tv_internet`
   - **Minutes of TV before a quiz**, **Correct answers in a row to unlock**, and **Subjects**
     (optional)
2. Click **Save**.
3. Turn on the TV, then click **Start** on the **Info** tab. Press **Allow** when the TV asks
   to pair with *KidLock*. The **Log** tab then shows `Paired with TV`.

The quiz page address is worked out automatically from this HA box's IP.

### Advanced settings and questions (files)
In the **app_configs** share (older HA: `addon_configs`), open the folder ending in
**`_tv_kidlock`**:
- `questions/`: your question files. They're picked up without a restart.
- `config.yaml`: advanced settings such as `block_apps` (app IDs), cooldowns, and idle reset.
  Restart the app after editing.

To get there:
- **Samba share** app: `\\homeassistant.local\app_configs`, or `smb://homeassistant.local` on a Mac
- **Studio Code Server** app: open the `/addon_configs` folder

## Day to day
- **Parent page:** **Open web UI**, or `http://<HA IP>:5000/parent`. Shows status,
  *Unlock now* / *Quiz now*, results per subject, and recent wrong answers.
- **Questions:** edit the `.yaml` files in `questions/` in the same `_tv_kidlock` folder.
  They're picked up without a restart. See `questions/_FORMAT.yaml`.
- **Movie night:** create a Toggle helper `input_boolean.tv_kid_lock` and turn it off.

Full guide: INSTALL.md in the project.
