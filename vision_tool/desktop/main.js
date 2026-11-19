// Visuvius desktop shell (Windows). Applies a system-wide correction using Windows' own settings:
//   - Display scaling (same as Settings > System > Display > Scale): everything on screen gets bigger, no overlay
//   - Text size (Settings > Accessibility > Text size)
// Everything it changes is saved first and restored on the Revert button, Esc, closing the window,
// quitting, signals, uncaught errors, and on the next launch if the app was killed before reverting.
const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');

const isWin = process.platform === 'win32';
const TEXT = { key: 'HKCU\\Software\\Microsoft\\Accessibility', name: 'TextScaleFactor' };
const stateFile = () => path.join(app.getPath('userData'), 'original-state.json');
const dpiScript = path.join(__dirname, 'dpi.ps1');

const run = (cmd, args) => execFileSync(cmd, args, { encoding: 'utf8', windowsHide: true, timeout: 15000 });
function regGet(key, name) {
  try {
    const m = run('reg', ['query', key, '/v', name]).match(/REG_DWORD\s+0x([0-9a-f]+)/i);
    return m ? { had: true, value: parseInt(m[1], 16) } : { had: false };
  } catch { return { had: false }; }
}
const regSet = (key, name, v) => run('reg', ['add', key, '/v', name, '/t', 'REG_DWORD', '/d', String(v), '/f']);
const regDel = (key, name) => { try { run('reg', ['delete', key, '/v', name, '/f']); } catch {} };

const BROADCAST = [
  "Add-Type -Namespace W -Name U -MemberDefinition '[DllImport(\"user32.dll\",SetLastError=true,CharSet=CharSet.Auto)] public static extern IntPtr SendMessageTimeout(IntPtr h,uint m,UIntPtr w,string l,uint f,uint t,out UIntPtr r);';",
  "$r=[UIntPtr]::Zero; [W.U]::SendMessageTimeout([IntPtr]0xffff,0x1A,[UIntPtr]::Zero,'WindowsThemeElement',2,5000,[ref]$r) | Out-Null",
].join(' ');
const broadcast = () => { try { run('powershell', ['-NoProfile', '-NonInteractive', '-Command', BROADCAST]); } catch {} };

const dpi = (...args) => run('powershell', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', dpiScript, ...args]);

function loadState() { try { return JSON.parse(fs.readFileSync(stateFile(), 'utf8')); } catch { return null; } }
function saveState(st) { fs.mkdirSync(path.dirname(stateFile()), { recursive: true }); fs.writeFileSync(stateFile(), JSON.stringify(st)); }

// Remember the originals once, before the first change.
function ensureState() {
  let st = loadState();
  if (st) return st;
  st = { text: regGet(TEXT.key, TEXT.name), dpi: null };
  try { st.dpi = JSON.parse(dpi('-Mode', 'get')); } catch { st.dpi = null; }
  saveState(st);
  return st;
}

// text: percent (100-225). steps: display-scale steps above the recommended scale (0 = unchanged; 1 = +25%).
function apply({ text = 100, steps = 0 } = {}) {
  if (!isWin) return { ok: false, reason: 'Windows only' };
  text = Math.max(100, Math.min(225, Math.round(Number(text) || 100)));
  steps = Math.max(0, Math.min(6, Math.round(Number(steps) || 0)));
  if (text === 100 && steps === 0 && !loadState()) return { ok: true };
  const st = ensureState();
  const result = { ok: true, text, steps };
  regSet(TEXT.key, TEXT.name, text);
  broadcast();
  try {
    if (steps > 0 || st.dpi) {
      if (steps > 0) dpi('-Mode', 'steps', String(steps));
      else if (st.dpi) dpi('-Mode', 'restore', JSON.stringify(st.dpi));
    }
  } catch (e) { result.ok = false; result.reason = 'Display scaling failed: ' + String(e.message).split('\n')[0]; }
  return result;
}

function revertSync() {
  if (!isWin) return false;
  const st = loadState();
  if (!st) return false;
  try {
    if (st.dpi) { try { dpi('-Mode', 'restore', JSON.stringify(st.dpi)); } catch {} }
    if (st.text && st.text.had) regSet(TEXT.key, TEXT.name, st.text.value); else regDel(TEXT.key, TEXT.name);
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
