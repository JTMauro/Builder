# Visuvius desktop (Windows)

Runs `../index.html` in an Electron window. When you apply a correction it can also change the whole
computer, using Windows' own features (both can be switched off with the checkboxes in tab 1):

- **Text size** (Settings > Accessibility > Text size): 100% to 140%.
- **Magnifier overlay** (full-screen Windows Magnifier): 100% to 175%, follows mouse, focus and text cursor.

    cd vision_tool/desktop
    npm install
    npm start

Revert: the "Revert screen to normal" button, Esc, closing the window, or quitting restores the original
settings and closes the magnifier. If the app is killed, the next launch restores them. Originals are saved in
the app's user data folder first.

Untested on Windows. Some apps pick up the new text size only when reopened. If anything is stuck: close
Magnifier (Win + Esc) and set Settings > Accessibility > Text size back to 100%.
