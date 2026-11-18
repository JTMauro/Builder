# Visuvius desktop (Windows)

Runs `../index.html` in an Electron window and, when you apply a correction, also sets the Windows
text size (Settings > Accessibility > Text size, 100% to 140% from the correction level).

    cd vision_tool/desktop
    npm install
    npm start

Revert: the "Revert screen to normal" button, Esc, closing the window, or quitting all restore the original
text size. If the app is killed, the next launch restores it. Original value is saved in the app's user data folder.

Untested on Windows. Windows applies the new text size to some apps live and to others only when they
are reopened. If anything is stuck, set Settings > Accessibility > Text size back to 100%.
