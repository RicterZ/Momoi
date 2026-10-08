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
    internal sealed record InstalledDevice(string Name, bool Input, bool Available);
    internal static string FriendlyName(object? friendly, object? description, object? interfaceName)
    {
        foreach (var value in new[] { friendly, description, interfaceName })
            if (value is string text && !string.IsNullOrWhiteSpace(text) && !Guid.TryParse(text.Trim('{', '}'), out _)) return text;
        return "未命名音频设备";
    }
    internal static IEnumerable<InstalledDevice> InstalledDevices()
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
                string name = FriendlyName(
                    properties?.GetValue("{a45c254e-df1c-4efd-8020-67d146a850e0},14"),
                    properties?.GetValue("{a45c254e-df1c-4efd-8020-67d146a850e0},2"),
                    properties?.GetValue("{026e516e-b814-414b-83cd-856d6fef4822},2"));
                yield return new(name, flow == "Capture", (state & 15) == 1);
            }
        }
    }
    internal static IEnumerable<string> Installed() => InstalledDevices().Select(device =>
        (device.Input ? "输入：" : "输出：") + device.Name + (device.Available ? "" : "（不可用）"));
}
