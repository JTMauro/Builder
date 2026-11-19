# Reads or sets Windows display scaling for every active display, live (no sign-out), the same way
# Settings > System > Display > Scale does. Uses the undocumented DisplayConfig DPI calls.
#   -Mode get                      -> JSON list of displays {Low,High,Id,Min,Cur,Max}
#   -Mode steps -Value 2           -> set each display to 2 steps above its recommended scale (100,125,150,175,200,...)
#   -Mode restore -Value '<json>'  -> put each display back to the Cur offset from an earlier `get`
param([string]$Mode, [string]$Value = '')
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Collections.Generic;
public class Src { public int Low; public int High; public uint Id; public int Min; public int Cur; public int Max; }
public static class Dpi {
  [DllImport("user32.dll")] static extern int GetDisplayConfigBufferSizes(uint f, out uint np, out uint nm);
  [DllImport("user32.dll")] static extern int QueryDisplayConfig(uint f, ref uint np, IntPtr paths, ref uint nm, IntPtr modes, IntPtr topo);
  [DllImport("user32.dll")] static extern int DisplayConfigGetDeviceInfo(IntPtr pkt);
  [DllImport("user32.dll")] static extern int DisplayConfigSetDeviceInfo(IntPtr pkt);
  static IntPtr Pkt(int type, int size, int low, int high, uint id) {
    IntPtr p = Marshal.AllocHGlobal(size);
    for (int i = 0; i < size; i += 4) Marshal.WriteInt32(p, i, 0);
    Marshal.WriteInt32(p, 0, type); Marshal.WriteInt32(p, 4, size);
    Marshal.WriteInt32(p, 8, low); Marshal.WriteInt32(p, 12, high); Marshal.WriteInt32(p, 16, (int)id);
    return p;
  }
  public static List<Src> All() {
    uint np, nm;
    if (GetDisplayConfigBufferSizes(2, out np, out nm) != 0) throw new Exception("GetDisplayConfigBufferSizes failed");
    IntPtr paths = Marshal.AllocHGlobal((int)np * 72), modes = Marshal.AllocHGlobal((int)nm * 64);
    try {
      if (QueryDisplayConfig(2, ref np, paths, ref nm, modes, IntPtr.Zero) != 0) throw new Exception("QueryDisplayConfig failed");
      var list = new List<Src>(); var seen = new HashSet<string>();
      for (int i = 0; i < np; i++) {
        int o = i * 72, low = Marshal.ReadInt32(paths, o), high = Marshal.ReadInt32(paths, o + 4);
        uint id = (uint)Marshal.ReadInt32(paths, o + 8);
        if (!seen.Add(low + ":" + high + ":" + id)) continue;
        IntPtr p = Pkt(-3, 32, low, high, id);
        try {
          if (DisplayConfigGetDeviceInfo(p) != 0) continue;
          list.Add(new Src { Low = low, High = high, Id = id, Min = Marshal.ReadInt32(p, 20), Cur = Marshal.ReadInt32(p, 24), Max = Marshal.ReadInt32(p, 28) });
        } finally { Marshal.FreeHGlobal(p); }
      }
      return list;
    } finally { Marshal.FreeHGlobal(paths); Marshal.FreeHGlobal(modes); }
  }
  public static int SetRel(int low, int high, uint id, int rel) {
    IntPtr p = Pkt(-4, 24, low, high, id);
    try { Marshal.WriteInt32(p, 20, rel); return DisplayConfigSetDeviceInfo(p); } finally { Marshal.FreeHGlobal(p); }
  }
}
"@
switch ($Mode) {
  'get' { ConvertTo-Json -InputObject @([Dpi]::All()) -Compress }
  'steps' {
    $k = [int]$Value
    foreach ($s in [Dpi]::All()) {
      $rel = [Math]::Max($s.Min, [Math]::Min($s.Max, $k))
      if ([Dpi]::SetRel($s.Low, $s.High, $s.Id, $rel) -ne 0) { throw "SetRel failed" }
    }
  }
  'restore' {
    foreach ($s in ($Value | ConvertFrom-Json)) { [void][Dpi]::SetRel($s.Low, $s.High, [uint32]$s.Id, [int]$s.Cur) }
  }
}
