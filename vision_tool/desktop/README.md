# Visuvius desktop (Windows)

Runs `../index.html` in an Electron window. When you apply a correction it changes the whole computer
using Windows' own settings (each can be switched off in tab 1):

- **Display scaling** (Settings > System > Display > Scale): everything on every screen gets bigger, with no overlay
  or panning. Steps up from your recommended scale: 100 > 125 > 150 > 175 > 200%.
- **Text size** (Settings > Accessibility > Text size): optional extra, 100% to 140%.

    cd vision_tool/desktop
    npm install
    npm start

Revert: the "Revert screen to normal" button, Esc, closing the window, or quitting restores the original
scale and text size. If the app is killed, the next launch restores them. Originals are saved in the app's
user data folder before anything changes.

Untested on Windows. Display scaling uses undocumented Windows calls (`dpi.ps1`). Some apps need to be
reopened to redraw at the new size. If anything is stuck, set Settings > System > Display > Scale and
Settings > Accessibility > Text size back to your usual values.
