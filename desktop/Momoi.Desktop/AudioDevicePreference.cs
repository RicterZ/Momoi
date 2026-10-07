using System;
using System.Collections.Generic;
using System.Linq;
using Microsoft.Win32;

namespace Momoi.Desktop;

internal static class AudioDevicePreference
{
    internal static int Rank(string name, bool input)
    {
        string cable = input ? "CABLE Output" : "CABLE Input";
        if (name.Contains(cable, StringComparison.OrdinalIgnoreCase)) return 0;
        string steam = input ? "Steam Streaming Microphone" : "Steam Streaming Speakers";
        return name.Contains(steam, StringComparison.OrdinalIgnoreCase) ? 1 : 2;
    }
    internal static IEnumerable<string> Installed()
    {
        foreach (string flow in new[] { "Capture", "Render" })
        {
            using var root = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\" + flow);
            if (root is null) continue;
            foreach (string key in root.GetSubKeyNames())
            {
                using var device = root.OpenSubKey(key);
                using var properties = root.OpenSubKey(key + @"\Properties");
                if (device?.GetValue("DeviceState") is not int state) continue;
                string name = properties?.GetValue("{a45c254e-df1c-4efd-8020-67d146a850e0},14") as string ?? key;
                yield return (flow == "Capture" ? "输入：" : "输出：") + name + ((state & 15) == 1 ? "" : "（不可用）");
            }
        }
    }
}
