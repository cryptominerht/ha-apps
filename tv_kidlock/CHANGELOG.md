## 1.1.4
- Parent page: "Test: show quiz page on TV" button for troubleshooting.

## 1.1.3
- TVs that don't provide an app list (timeout warning) are now checked app by app; the log shows which streaming apps will be closed while locked.

## 1.1.2
- Fix: if the TV browser was already open (e.g. video playing in it), the quiz never loaded. The browser is now closed first and reopened on the quiz page.

## 1.1.1
- Logs when the quiz page is loaded and by which device; explains why after 3 failed tries.
- Backs off to one browser reopen every 2 minutes instead of every ~20 s.
- Parent page shows the quiz address and the last check-in from the TV.

## 1.1.0
- Main settings are now on the app's Configuration tab.
- Quiz page address is detected automatically.

## 1.0.1
- Fix: web UI link format, which stopped Home Assistant from listing the app.

## 1.0.0
- First release: watch-time lock, UniFi internet block, TV-browser quiz with question bank, parent page.
