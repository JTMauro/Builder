// Visuvius desktop shell (Windows). Applies a system-wide correction using Windows' own features:
//   - Text size  (Settings > Accessibility > Text size)
//   - Magnifier  (full-screen overlay, follows mouse, focus and text cursor)
// Everything it changes is saved first and restored on the Revert button, Esc, closing the window,
// quitting, signals, uncaught errors, and on the next launch if the app was killed before reverting.
const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn, execFileSync } = require('child_process');

const isWin = process.platform === 'win32';
const TEXT = { key: 'HKCU\\Software\\Microsoft\\Accessibility', names: ['TextScaleFactor'] };
const MAG = { key: 'HKCU\\Software\\Microsoft\\ScreenMagnifier', names: ['Magnification', 'MagnificationMode', 'FollowMouse', 'FollowFocus', 'FollowCaret'] };
const stateFile = () => path.join(app.getPath('userData'), 'original-state.json');

const run = (cmd, args, opts = {}) => execFileSync(cmd, args, { encoding: 'utf8', windowsHide: true, timeout: 8000, ...opts });
function regGet(key, name) {
  try {
    const m = run('reg', ['query', key, '/v', name]).match(/REG_DWORD\s+0x([0-9a-f]+)/i);
    return m ? { had: true, value: parseInt(m[1], 16) } : { had: false };
  } catch { return { had: false }; }
}
const regSet = (key, name, v) => run('reg', ['add', key, '/v', name, '/t', 'REG_DWORD', '/d', String(v), '/f']);
const regDel = (key, name) => { try { run('reg', ['delete', key, '/v', name, '/f']); } catch {} };
const regRestore = (key, name, o) => (o && o.had) ? regSet(key, name, o.value) : regDel(key, name);

const BROADCAST = [
  "Add-Type -Namespace W -Name U -MemberDefinition '[DllImport(\"user32.dll\",SetLastError=true,CharSet=CharSet.Auto)] public static extern IntPtr SendMessageTimeout(IntPtr h,uint m,UIntPtr w,string l,uint f,uint t,out UIntPtr r);';",
  "$r=[UIntPtr]::Zero; [W.U]::SendMessageTimeout([IntPtr]0xffff,0x1A,[UIntPtr]::Zero,'WindowsThemeElement',2,5000,[ref]$r) | Out-Null",
].join(' ');
const broadcast = () => { try { run('powershell', ['-NoProfile', '-NonInteractive', '-Command', BROADCAST]); } catch {} };

const magRunning = () => { try { return /magnify\.exe/i.test(run('tasklist', ['/FI', 'IMAGENAME eq magnify.exe', '/NH'])); } catch { return false; } };
const magStop = () => { try { run('taskkill', ['/F', '/IM', 'magnify.exe']); } catch {} };
const magStart = () => { try { spawn('magnify.exe', [], { detached: true, stdio: 'ignore', windowsHide: true }).unref(); } catch {} };

function loadState() { try { return JSON.parse(fs.readFileSync(stateFile(), 'utf8')); } catch { return null; } }
function saveState(st) { fs.mkdirSync(path.dirname(stateFile()), { recursive: true }); fs.writeFileSync(stateFile(), JSON.stringify(st)); }

// Remember the originals once, before the first change.
function ensureState() {
  let st = loadState();
  if (st) return st;
  st = { text: {}, mag: {}, magWasRunning: magRunning(), magStarted: false };
  for (const n of TEXT.names) st.text[n] = regGet(TEXT.key, n);
  for (const n of MAG.names) st.mag[n] = regGet(MAG.key, n);
  saveState(st);
  return st;
}

function apply({ text = 100, zoom = 100 } = {}) {
  if (!isWin) return { ok: false, reason: 'Windows only' };
  text = Math.max(100, Math.min(225, Math.round(Number(text) || 100)));
  zoom = Math.max(100, Math.min(400, Math.round(Number(zoom) || 100)));
  if (text === 100 && zoom === 100 && !loadState()) return { ok: true };
  const st = ensureState();

  regSet(TEXT.key, 'TextScaleFactor', text);
  broadcast();

  if (zoom > 100) {
    regSet(MAG.key, 'Magnification', zoom);
    regSet(MAG.key, 'MagnificationMode', 2);     // 2 = full screen
    for (const n of ['FollowMouse', 'FollowFocus', 'FollowCaret']) regSet(MAG.key, n, 1);
    if (magRunning()) magStop();                 // restart so the new zoom level is read
    magStart();
    st.magStarted = true; saveState(st);
  } else if (st.magStarted) {
    magStop(); st.magStarted = false; saveState(st);
  }
  return { ok: true, text, zoom };
}

function revertSync() {
  if (!isWin) return false;
  const st = loadState();
  if (!st) return false;
  try {
    if (st.magStarted && !st.magWasRunning) magStop();
    for (const n of MAG.names) regRestore(MAG.key, n, st.mag[n]);
    for (const n of TEXT.names) regRestore(TEXT.key, n, st.text[n]);
    broadcast();
    fs.unlinkSync(stateFile());
    return true;
  } catch { return false; }   // state file is kept, so the next launch tries again
}

ipcMain.handle('apply', (_e, opts) => apply(opts));
ipcMain.handle('revert', () => ({ ok: revertSync() }));

app.whenReady().then(() => {
  revertSync();   // crash recovery: undo a change left by a killed session
  const win = new BrowserWindow({
    width: 980, height: 820, title: 'Visuvius',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.setMenuBarVisibility(false);
  win.loadFile(path.join(__dirname, '..', 'index.html'));
});

app.on('window-all-closed', () => { revertSync(); app.quit(); });
app.on('before-quit', revertSync);
app.on('will-quit', revertSync);
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(sig, () => { revertSync(); process.exit(0); });
process.on('uncaughtException', err => { revertSync(); console.error(err); process.exit(1); });
