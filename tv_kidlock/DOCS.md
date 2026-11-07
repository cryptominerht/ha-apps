# TV Kid Lock

After `watch_minutes` of watching, the TV pauses, its internet is blocked through a
UniFi firewall policy, and a quiz opens in the TV's browser. A set number of correct
answers in a row unlocks it.

## First start
1. Click **Start**. The log says it created `config.yaml`, then the app stops.
2. Open the **app_configs** share (Samba share app, or Studio Code Server). Find the folder
   ending in **`_tv_kidlock`** (older HA: `addon_configs`).
3. Edit `config.yaml`:
   - `tv.host`: the TV's IP
   - `quiz_url`: `http://<this HA box's IP>:5000/`
   - `parent_pin`: a PIN for the parent page
   - `home_assistant.internet_block_switch`: the UniFi policy switch, e.g. `switch.block_tv_internet`
   - Leave `home_assistant.url` and `token` empty.
4. Turn on the TV and click **Start**. Press **Allow** when the TV asks to pair with *KidLock*.

## Day to day
- **Parent page:** **Open web UI**, or `http://<HA IP>:5000/parent`. Shows status,
  *Unlock now* / *Quiz now*, results per subject, and recent wrong answers.
- **Questions:** edit the `.yaml` files in `questions/` in the same `_tv_kidlock` folder.
  They're picked up without a restart. See `questions/_FORMAT.yaml`.
- **Movie night:** create a Toggle helper `input_boolean.tv_kid_lock` and turn it off.

Full guide: INSTALL.md in the project.
