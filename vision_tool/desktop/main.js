// Visuvius desktop shell (Windows). Changes the system text size (Settings > Accessibility > Text size)
// and always restores the original value: on the Revert button, Esc, closing the window, quitting,
// signals, uncaught errors, and on the next launch if the app was killed before it could revert.
const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { execFile, execFileSync } = require('child_process');

const KEY = 'HKCU\\Software\\Microsoft\\Accessibility';
const VALUE = 'TextScaleFactor';
const isWin = process.platform === 'win32';
const stateFile = () => path.join(app.getPath('userData'), 'original-text-scale.json');

const BROADCAST = [
  "Add-Type -Namespace W -Name U -MemberDefinition '[DllImport(\"user32.dll\",SetLastError=true,CharSet=CharSet.Auto)] public static extern IntPtr SendMessageTimeout(IntPtr h,uint m,UIntPtr w,string l,uint f,uint t,out UIntPtr r);';",
  "$r=[UIntPtr]::Zero; [W.U]::SendMessageTimeout([IntPtr]0xffff,0x1A,[UIntPtr]::Zero,'WindowsThemeElement',2,5000,[ref]$r) | Out-Null",
].join(' ');

function readCurrentSync() {
  try {
    const out = execFileSync('reg', ['query', KEY, '/v', VALUE], { encoding: 'utf8', windowsHide: true });
    const m = out.match(/REG_DWORD\s+0x([0-9a-f]+)/i);
    return m ? { had: true, value: parseInt(m[1], 16) } : { had: false, value: 100 };
  } catch { return { had: false, value: 100 }; }   // value absent means 100%
}
function writeSync(pct) {
  execFileSync('reg', ['add', KEY, '/v', VALUE, '/t', 'REG_DWORD', '/d', String(pct), '/f'], { windowsHide: true });
}
function deleteSync() {
  try { execFileSync('reg', ['delete', KEY, '/v', VALUE, '/f'], { windowsHide: true }); } catch {}
}
function broadcast(sync) {
  const args = ['-NoProfile', '-NonInteractive', '-Command', BROADCAST];
  if (sync) { try { execFileSync('powershell', args, { windowsHide: true, timeout: 8000 }); } catch {} }
  else execFile('powershell', args, { windowsHide: true }, () => {});
}

function setTextScale(pct) {
  if (!isWin) return { ok: false, reason: 'Windows only' };
  pct = Math.max(100, Math.min(225, Math.round(Number(pct) || 100)));
  if (pct === 100 && !fs.existsSync(stateFile())) return { ok: true, percent: 100 };
  if (!fs.existsSync(stateFile())) {                 // remember the original once, before the first change
    fs.mkdirSync(path.dirname(stateFile()), { recursive: true });
    fs.writeFileSync(stateFile(), JSON.stringify(readCurrentSync()));
  }
  writeSync(pct);
  broadcast(false);
  return { ok: true, percent: pct };
}

function revertSync() {
  if (!isWin || !fs.existsSync(stateFile())) return false;
  try {
    const orig = JSON.parse(fs.readFileSync(stateFile(), 'utf8'));
    if (orig.had) writeSync(orig.value); else deleteSync();
    broadcast(true);
    fs.unlinkSync(stateFile());
    return true;
  } catch { return false; }   // state file is kept, so the next launch tries again
}

ipcMain.handle('text-scale', (_e, pct) => setTextScale(pct));
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
